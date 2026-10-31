"""Immutable landing store for bounded Tushare research-data responses.

The artifacts here preserve provider rows and request scope. They are an
auditable source layer, not a factor or canonical market-data API: downstream
publishers must normalize units and point-in-time availability explicitly.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import math
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..errors import ContractError
from .canonical_v2 import fsync_dir, fsync_tree
from .gate_contracts import canonical_json, sha256_json, validate_contract
from .paths import reject_symlinks_within


TUSHARE_DEPENDENCY_ENDPOINTS = frozenset({
    "daily_basic",
    "index_classify",
    "index_member_all",
    "income",
    "balancesheet",
    "cashflow",
    "fina_indicator",
    "forecast",
    "express",
    "report_rc",
    "moneyflow",
    "hk_hold",
    "top_list",
})

_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "daily_basic": frozenset({"ts_code", "trade_date"}),
    "index_classify": frozenset({"index_code", "industry_name", "level"}),
    "index_member_all": frozenset({"ts_code", "l3_code", "in_date", "out_date"}),
    "income": frozenset({"ts_code", "ann_date", "end_date"}),
    "balancesheet": frozenset({"ts_code", "ann_date", "end_date"}),
    "cashflow": frozenset({"ts_code", "ann_date", "end_date"}),
    "fina_indicator": frozenset({"ts_code", "ann_date", "end_date"}),
    "forecast": frozenset({"ts_code", "ann_date", "end_date"}),
    "express": frozenset({"ts_code", "ann_date", "end_date"}),
    "report_rc": frozenset({"ts_code", "report_date", "quarter"}),
    "moneyflow": frozenset({"ts_code", "trade_date"}),
    "hk_hold": frozenset({"ts_code", "trade_date"}),
    "top_list": frozenset({"ts_code", "trade_date", "reason"}),
}

# Tushare's documented per-request row limits. A response at the cap might be
# truncated, so it cannot be published as complete; callers must narrow it.
_RESPONSE_LIMITS = {
    "daily_basic": 6000,
    "index_member_all": 2000,
    "report_rc": 3000,
    "moneyflow": 6000,
    "top_list": 10000,
}


def _validated_query(query: dict[str, Any]) -> dict[str, str | int | float | bool]:
    if not isinstance(query, dict) or not query:
        raise ContractError("Tushare dependency query must be a non-empty object")
    normalized: dict[str, str | int | float | bool] = {}
    for key, value in query.items():
        if not isinstance(key, str) or not key.strip():
            raise ContractError("Tushare dependency query keys must be non-empty strings")
        if any(secret in key.lower() for secret in ("token", "password", "secret", "credential")):
            raise ContractError("credentials are forbidden in Tushare dependency query metadata")
        if isinstance(value, bool):
            normalized[key] = value
        elif isinstance(value, (str, int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise ContractError(f"Tushare dependency query value {key} must be finite")
            normalized[key] = value
        else:
            raise ContractError(f"Tushare dependency query value {key} must be a JSON scalar")
    return dict(sorted(normalized.items()))


def _generation_id(dataset: str, query: dict[str, Any], checksum: str) -> str:
    return sha256_json({
        "dataset": dataset,
        "query": query,
        "data_checksum_sha256": checksum,
    })


class TushareDependencyStore:
    """Append-only, checksum-verified storage for raw Tushare dependency panels."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._query_index: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def _family(self, dataset: str) -> Path:
        if dataset not in TUSHARE_DEPENDENCY_ENDPOINTS:
            raise ContractError(f"unsupported Tushare dependency dataset: {dataset}")
        family = self.root / "tushare_dependencies" / dataset
        reject_symlinks_within(self.root, family, label="Tushare dependency store path")
        return family

    def publish(
        self,
        dataset: str,
        *,
        query: dict[str, Any],
        source_fetched_at: datetime,
        frame: pd.DataFrame,
    ) -> dict[str, Any]:
        if dataset not in TUSHARE_DEPENDENCY_ENDPOINTS:
            raise ContractError(f"unsupported Tushare dependency dataset: {dataset}")
        if not isinstance(frame, pd.DataFrame):
            raise ContractError("Tushare dependency response must be a DataFrame")
        if (
            not isinstance(source_fetched_at, datetime)
            or source_fetched_at.tzinfo is None
            or source_fetched_at.utcoffset() is None
        ):
            raise ContractError("Tushare dependency source_fetched_at must be timezone-aware")
        query = _validated_query(query)
        columns = [str(column) for column in frame.columns]
        if not columns or len(columns) != len(set(columns)):
            raise ContractError("Tushare dependency response must have unique named columns")
        missing = _REQUIRED_COLUMNS[dataset] - set(columns)
        if missing:
            raise ContractError(f"{dataset} response is missing columns: {sorted(missing)}")
        row_limit = _RESPONSE_LIMITS.get(dataset)
        if row_limit is not None and len(frame) >= row_limit:
            raise ContractError(
                f"{dataset} response reached its {row_limit}-row limit; narrow the query before publishing"
            )

        sink = io.BytesIO()
        try:
            frame.to_parquet(sink, index=False)
        except (ImportError, TypeError, ValueError) as exc:
            raise ContractError(f"{dataset} response cannot be serialized as Parquet: {exc}") from exc
        artifact = sink.getvalue()
        checksum = hashlib.sha256(artifact).hexdigest()
        generation = _generation_id(dataset, query, checksum)
        manifest: dict[str, Any] = {
            "snapshot_version": 1,
            "dataset": dataset,
            "source": "tushare",
            "query": query,
            "source_fetched_at": source_fetched_at.astimezone(timezone.utc).isoformat(),
            "row_count": int(len(frame)),
            "columns": columns,
            "data_artifact": {
                "path": "data.parquet",
                "checksum_sha256": checksum,
            },
            "generation_id": generation,
        }
        validate_contract("tushare_dependency_snapshot.v1.json", manifest)

        family = self._family(dataset)
        family.mkdir(parents=True, exist_ok=True)
        lock_path = family / "publication.lock"
        directory = family / f"generation={generation}"
        reject_symlinks_within(self.root, lock_path, label="Tushare dependency store path")
        reject_symlinks_within(self.root, directory, label="Tushare dependency store path")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if directory.exists():
                existing = self.read_manifest(generation, dataset=dataset)
                if existing["data_artifact"]["checksum_sha256"] != checksum:
                    raise ContractError("immutable Tushare dependency generation has conflicting content")
                self._index_manifest(existing)
                return existing
            staging = family / f".staging_{uuid.uuid4().hex}"
            staging.mkdir()
            try:
                (staging / "data.parquet").write_bytes(artifact)
                (staging / "manifest.json").write_text(
                    json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                fsync_tree(staging)
                staging.replace(directory)
                fsync_dir(family)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        self._index_manifest(manifest)
        return manifest

    @staticmethod
    def _query_key(query: dict[str, Any]) -> str:
        return canonical_json(_validated_query(query)).decode("utf-8")

    def _index_manifest(self, manifest: dict[str, Any]) -> None:
        dataset = manifest["dataset"]
        by_query = self._query_index.setdefault(dataset, {})
        candidates = by_query.setdefault(self._query_key(manifest["query"]), [])
        if all(item["generation_id"] != manifest["generation_id"] for item in candidates):
            candidates.append(manifest)
            candidates.sort(key=lambda item: item["source_fetched_at"])

    def _build_query_index(self, dataset: str) -> dict[str, list[dict[str, Any]]]:
        by_query: dict[str, list[dict[str, Any]]] = {}
        family = self._family(dataset)
        if family.is_dir():
            for child in family.iterdir():
                if not child.is_dir() or not child.name.startswith("generation="):
                    continue
                generation_id = child.name.removeprefix("generation=")
                manifest = self.read_manifest(generation_id, dataset=dataset)
                by_query.setdefault(self._query_key(manifest["query"]), []).append(manifest)
        for candidates in by_query.values():
            candidates.sort(key=lambda item: item["source_fetched_at"])
        return by_query

    def list_manifests(self, dataset: str) -> list[dict[str, Any]]:
        """Return every checksum-verified snapshot for one supported endpoint.

        A derived PIT panel needs to prove exactly which immutable responses it
        consumed. Keep this enumeration on the owning store instead of having
        builders walk its private directory layout.
        """
        if dataset not in TUSHARE_DEPENDENCY_ENDPOINTS:
            raise ContractError(f"unsupported Tushare dependency dataset: {dataset}")
        index = self._build_query_index(dataset)
        manifests = [item for candidates in index.values() for item in candidates]
        return sorted(
            manifests,
            key=lambda item: (
                str(item["query"].get("trade_date", "")),
                item["source_fetched_at"],
                item["generation_id"],
            ),
        )

    def existing_generation_for_query(
        self, dataset: str, query: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return the newest intact immutable response for an exact query, if any.

        Backfill commands use this to resume without spending another upstream
        call. ``refresh`` remains an explicit caller choice when a provider may
        have revised its historical response.
        """
        query = _validated_query(query)
        if dataset not in self._query_index:
            self._query_index[dataset] = self._build_query_index(dataset)
        by_query = self._query_index[dataset]
        candidates = by_query.get(self._query_key(query), [])
        if not candidates:
            return None
        manifest = max(candidates, key=lambda item: item["source_fetched_at"])
        self.read_frame(manifest["generation_id"], dataset=dataset)
        return manifest

    def _directory(self, dataset: str, generation_id: str) -> Path:
        if len(generation_id) != 64 or any(char not in "0123456789abcdef" for char in generation_id):
            raise ContractError("invalid Tushare dependency generation id")
        directory = self._family(dataset) / f"generation={generation_id}"
        reject_symlinks_within(self.root, directory, label="Tushare dependency store path")
        return directory

    def read_manifest(self, generation_id: str, *, dataset: str) -> dict[str, Any]:
        path = self._directory(dataset, generation_id) / "manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("Tushare dependency manifest is missing or malformed") from exc
        validate_contract("tushare_dependency_snapshot.v1.json", manifest)
        if manifest["dataset"] != dataset:
            raise ContractError("Tushare dependency manifest dataset mismatch")
        if _generation_id(
            dataset, manifest["query"], manifest["data_artifact"]["checksum_sha256"]
        ) != generation_id or manifest["generation_id"] != generation_id:
            raise ContractError("Tushare dependency generation identity mismatch")
        return manifest

    def read_frame(self, generation_id: str, *, dataset: str) -> tuple[dict[str, Any], pd.DataFrame]:
        manifest = self.read_manifest(generation_id, dataset=dataset)
        path = self._directory(dataset, generation_id) / manifest["data_artifact"]["path"]
        if not path.is_file() or path.is_symlink():
            raise ContractError("Tushare dependency Parquet artifact is unavailable")
        artifact = path.read_bytes()
        if hashlib.sha256(artifact).hexdigest() != manifest["data_artifact"]["checksum_sha256"]:
            raise ContractError("Tushare dependency artifact checksum mismatch")
        try:
            frame = pd.read_parquet(io.BytesIO(artifact))
        except (ImportError, OSError, ValueError) as exc:
            raise ContractError("Tushare dependency Parquet artifact is unreadable") from exc
        if len(frame) != manifest["row_count"] or frame.columns.tolist() != manifest["columns"]:
            raise ContractError("Tushare dependency Parquet shape mismatch")
        return manifest, frame
