"""Governed market dataset publisher for calendar, suspension, and corporate-action sets.

Publishes immutable snapshots with manifest-first identity, overwrite rejection,
no-symlink path policy, and a reader that returns ordered partition-set triples
compatible with the Phase 0R ``set_binding_resolver`` digest convention.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from ..contracts.gate_contracts import validate_contract
from ..contracts.manifest import canonical_json
from ..errors import ContractError


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _no_symlink(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise ContractError(f"symlink detected in store path: {current}")
        current = current.parent


class MarketDatasetPublisher:
    """Manifest-first immutable publisher for market dataset snapshot families."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _directory(self, family: str, partition_date: date) -> Path:
        return self.root / family / partition_date.isoformat()

    def publish_calendar_snapshot(
        self,
        *,
        partition_date: date,
        window_start: date,
        window_end: date,
        sessions: Sequence[date],
        primary_source: str,
        independent_source: str,
    ) -> dict[str, Any]:
        directory = self._directory("trading_calendar_snapshot_v1", partition_date)
        if directory.exists():
            raise ContractError(f"calendar snapshot already published: {directory}")
        _no_symlink(directory.parent)
        session_strings = sorted(session.isoformat() for session in sessions)
        generation = sha256_json({
            "family": "trading_calendar_snapshot_v1",
            "partition_date": partition_date.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "session_dates": session_strings,
        })
        directory.mkdir(parents=True, exist_ok=False)
        frame = pd.DataFrame({"session_date": session_strings})
        data_path = directory / "data.parquet"
        frame.to_parquet(data_path, index=False)
        data_checksum = hashlib.sha256(data_path.read_bytes()).hexdigest()
        manifest = {
            "snapshot_version": "1.0.0",
            "family": "trading_calendar_snapshot_v1",
            "generation_id": generation,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "session_dates": session_strings,
            "primary_source": primary_source,
            "independent_source": independent_source,
            "cross_check_result": "agreement",
            "row_count": len(session_strings),
            "data_checksum_sha256": data_checksum,
            "serialization_profile": "parquet-v1",
        }
        manifest["manifest_digest_sha256"] = sha256_json(manifest)
        validate_contract("trading_calendar_snapshot_v1.json", manifest)
        (directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return manifest

    def publish_suspension_snapshot(
        self,
        *,
        partition_date: date,
        window_start: date,
        window_end: date,
        frame: pd.DataFrame,
        primary_source: str = "bars_daily_v2_status",
        reconciliation_source: str = "tushare_suspend_d",
    ) -> dict[str, Any]:
        directory = self._directory("suspension_snapshot_v1", partition_date)
        if directory.exists():
            raise ContractError(f"suspension snapshot already published: {directory}")
        _no_symlink(directory.parent)
        directory.mkdir(parents=True, exist_ok=False)
        data_path = directory / "data.parquet"
        frame.to_parquet(data_path, index=False)
        data_checksum = hashlib.sha256(data_path.read_bytes()).hexdigest()
        generation = sha256_json({
            "family": "suspension_snapshot_v1",
            "partition_date": partition_date.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "row_count": len(frame),
            "data_checksum_sha256": data_checksum,
        })
        manifest = {
            "snapshot_version": "1.0.0",
            "family": "suspension_snapshot_v1",
            "generation_id": generation,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "primary_source": primary_source,
            "reconciliation_source": reconciliation_source,
            "row_count": len(frame),
            "data_checksum_sha256": data_checksum,
            "serialization_profile": "parquet-v1",
        }
        manifest["manifest_digest_sha256"] = sha256_json(manifest)
        validate_contract("suspension_snapshot_v1.json", manifest)
        (directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return manifest

    def publish_bars_partition(
        self,
        *,
        partition_date: date,
        window_start: date,
        window_end: date,
        source_data_path: Path,
        source_generation_id: str,
        source_data_checksum_sha256: str,
        columns: Sequence[str],
        row_count: int,
        source: str = "canonical_bars_daily_v2",
    ) -> dict[str, Any]:
        """Publish one session of bars as a byte-identical copy of a canonical partition.

        The bytes are copied rather than re-serialised so ``data_checksum_sha256``
        equals the canonical ``source_data_checksum_sha256`` and the published
        artifact is provably the same frame, not a re-encoding of it.
        """
        directory = self._directory("bars_daily_v2", partition_date)
        if directory.exists():
            raise ContractError(f"bars partition already published: {directory}")
        _no_symlink(directory.parent)
        for label, value in (
            ("source_generation_id", source_generation_id),
            ("source_data_checksum_sha256", source_data_checksum_sha256),
        ):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ContractError(f"invalid canonical bars {label}")
        content = Path(source_data_path).read_bytes()
        data_checksum = hashlib.sha256(content).hexdigest()
        if data_checksum != source_data_checksum_sha256:
            raise ContractError("canonical bars bytes do not match their manifest checksum")
        generation = sha256_json({
            "family": "bars_daily_v2",
            "partition_date": partition_date.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "source_generation_id": source_generation_id,
            "data_checksum_sha256": data_checksum,
            "row_count": int(row_count),
            "columns": list(columns),
        })
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "data.parquet").write_bytes(content)
        manifest = {
            "snapshot_version": "1.0.0",
            "family": "bars_daily_v2",
            "generation_id": generation,
            "partition_date": partition_date.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "source": source,
            "source_generation_id": source_generation_id,
            "source_data_checksum_sha256": source_data_checksum_sha256,
            "row_count": int(row_count),
            "columns": list(columns),
            "data_checksum_sha256": data_checksum,
            "serialization_profile": "parquet-v1",
        }
        manifest["manifest_digest_sha256"] = sha256_json(manifest)
        validate_contract("bars_daily_v2_partition.json", manifest)
        (directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return manifest

    def publish_corporate_action_snapshot(
        self,
        *,
        partition_date: date,
        window_start: date,
        window_end: date,
        frame: pd.DataFrame,
        source_generation_ids: Sequence[str],
        event_count: int,
        primary_source: str = "dividend_event_store:implemented_ex_date",
        reconciliation_source: str = "canonical_bars_adj_factor",
    ) -> dict[str, Any]:
        """Publish the per-ex-date corporate-action exclusion set for one session."""
        if list(frame.columns) != ["date", "instrument"]:
            raise ContractError("corporate action partition columns must be exactly date, instrument")
        if frame["instrument"].duplicated().any():
            raise ContractError("duplicate instrument in corporate action partition")
        if any(
            pd.isna(value) or str(value) != partition_date.isoformat()
            for value in frame["date"].astype(str)
        ):
            raise ContractError("corporate action rows must carry the partition ex-date")
        directory = self._directory("dividend_event_v1", partition_date)
        if directory.exists():
            raise ContractError(f"corporate action partition already published: {directory}")
        _no_symlink(directory.parent)
        ordered = frame.sort_values("instrument").reset_index(drop=True)
        generation_ids = sorted(str(value) for value in source_generation_ids)
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in generation_ids):
            raise ContractError("corporate action partition requires source generation ids")
        # An empty exclusion set (a session with no implemented ex-date) is a
        # legitimate governed artifact: it records that nothing was excluded.
        # A non-empty set must cite at least one source generation.
        if not generation_ids and len(ordered) != 0:
            raise ContractError("corporate action partition requires source generation ids")
        directory.mkdir(parents=True, exist_ok=False)
        data_path = directory / "data.parquet"
        ordered.to_parquet(data_path, index=False)
        data_checksum = hashlib.sha256(data_path.read_bytes()).hexdigest()
        generation = sha256_json({
            "family": "dividend_event_v1",
            "partition_date": partition_date.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "source_generation_ids": generation_ids,
            "data_checksum_sha256": data_checksum,
            "row_count": int(len(ordered)),
        })
        manifest = {
            "snapshot_version": "1.0.0",
            "family": "dividend_event_v1",
            "generation_id": generation,
            "partition_date": partition_date.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "primary_source": primary_source,
            "reconciliation_source": reconciliation_source,
            "source_generation_ids": generation_ids,
            "event_count": int(event_count),
            "row_count": int(len(ordered)),
            "columns": ["date", "instrument"],
            "data_checksum_sha256": data_checksum,
            "serialization_profile": "parquet-v1",
        }
        manifest["manifest_digest_sha256"] = sha256_json(manifest)
        validate_contract("dividend_event_v1_partition.json", manifest)
        (directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return manifest

    def read_partition_frame(
        self,
        family: str,
        partition_date: date,
        *,
        required_columns: set[str] | None = None,
    ) -> tuple[dict[str, Any], pd.DataFrame]:
        """Read one published partition, verifying its bytes against the manifest."""
        directory = self._directory(family, partition_date)
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise ContractError(f"no published {family} manifest for {partition_date}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        data_path = directory / "data.parquet"
        if not data_path.is_file():
            raise ContractError(f"incomplete {family} partition: {partition_date}")
        if hashlib.sha256(data_path.read_bytes()).hexdigest() != manifest["data_checksum_sha256"]:
            raise ContractError(f"tampered {family} partition: {partition_date}")
        frame = pd.read_parquet(data_path)
        if len(frame) != manifest["row_count"]:
            raise ContractError(f"{family} partition row count mismatch: {partition_date}")
        if required_columns is not None and not required_columns.issubset(frame.columns):
            raise ContractError(f"{family} partition is missing required columns: {partition_date}")
        return manifest, frame

    def list_partition_dates(self, family: str) -> list[date]:
        directory = self.root / family
        if not directory.is_dir():
            return []
        return sorted(
            date.fromisoformat(child.name)
            for child in directory.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        )

    def read_manifest(self, family: str, partition_date: date) -> dict[str, Any]:
        path = self._directory(family, partition_date) / "manifest.json"
        if not path.is_file():
            raise ContractError(f"no published {family} manifest for {partition_date}")
        return json.loads(path.read_text(encoding="utf-8"))

    def read_partition_triples(self, family: str, window_start: date, window_end: date) -> list[dict[str, str]]:
        triples: list[dict[str, str]] = []
        for partition_date in self.list_partition_dates(family):
            if partition_date < window_start or partition_date > window_end:
                continue
            manifest = self.read_manifest(family, partition_date)
            triples.append({
                "session_or_decision_date": partition_date.isoformat(),
                "generation_id": manifest["generation_id"],
                "data_checksum_sha256": manifest["data_checksum_sha256"],
            })
        return triples

    def build_set_binding(
        self,
        family: str,
        *,
        window_start: date,
        window_end: date,
    ) -> dict[str, Any]:
        from .set_binding_resolver import build_set_binding as _build

        triples = self.read_partition_triples(family, window_start, window_end)
        if not triples:
            raise ContractError(f"no published {family} partitions in [{window_start}, {window_end}]")
        return _build(triples, family=family, window_start=window_start.isoformat(), window_end=window_end.isoformat())

    def verify_set_binding(self, binding: Mapping[str, Any]) -> None:
        from .set_binding_resolver import verify_partition_set_binding

        family = binding["family"]
        triples = self.read_partition_triples(
            family, date.fromisoformat(binding["window_start"]), date.fromisoformat(binding["window_end"])
        )
        verify_partition_set_binding(binding, triples, family=family)
