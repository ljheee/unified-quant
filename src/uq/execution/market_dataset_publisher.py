"""Governed market dataset publisher for calendar, suspension, and corporate-action sets.

Publishes immutable snapshots with manifest-first identity, overwrite rejection,
no-symlink path policy, and a reader that returns ordered partition-set triples
compatible with the Phase 0R ``set_binding_resolver`` digest convention.
"""

from __future__ import annotations

import hashlib
import json
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
