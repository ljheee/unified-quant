from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from ..contracts.model_layer import AcceptedFactorIndexContract
from ..errors import ContractError
from ..factors.store import read_factor_partition


class AcceptedFactorIndexRuntime(AcceptedFactorIndexContract):
    """Runtime implementation backed by a published FactorStore root.

    Scans immutable factor partitions on disk and returns entries only for
    partitions whose manifests pass full identity/checksum/quality validation
    via ``read_factor_partition``.
    """

    def __init__(self, store_root: Path | str) -> None:
        super().__init__()
        self.store_root = Path(store_root)
        self._factors_dir = self.store_root / "factors"
        self._frames: dict[str, pd.DataFrame] = {}
        self._tampered_generations: set[str] = set()

    def _scan_partitions(self) -> list[dict[str, Any]]:
        if not self._factors_dir.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for manifest_path in sorted(self._factors_dir.rglob("manifest.json")):
            partition = manifest_path.parent
            try:
                manifest = json.loads(manifest_path.read_text())
                frame = read_factor_partition(partition)
            except (json.JSONDecodeError, OSError, ContractError) as exc:
                self._tampered_generations.add(manifest_path.parent.name)
                raise ContractError(
                    f"tampered or invalid accepted factor partition: {partition}: {exc}"
                ) from exc
            entry = {
                "factor_set": manifest["factor_set"],
                "factor_version": manifest["factor_version"],
                "partition_date": manifest["partition_date"],
                "generation_id": manifest["generation_id"],
                "manifest_digest_sha256": manifest["manifest_digest_sha256"],
                "data_checksum_sha256": manifest["data_checksum_sha256"],
                "quality_status": manifest["quality"]["status"],
                "universe_snapshot_generation_id": (
                    manifest.get("universe_snapshot") or {}
                ).get("artifact_generation_id"),
            }
            if entry["quality_status"] == "rejected":
                continue
            entries.append(entry)
            self.register_verified_generation(manifest["generation_id"])
            self._frames[manifest["generation_id"]] = frame
        return entries

    def list(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        request = dict(query)
        request.setdefault("pagination", {"limit": 10000})
        from ..contracts.gate_contracts import validate_contract
        validate_contract("accepted_factor_index_query.v1.json", request)
        self._validate_cursor(request)
        all_entries = self._scan_partitions()
        filters = request.get("filters", {})
        filtered = [
            e for e in all_entries
            if (not filters.get("factor_set") or e["factor_set"] == filters["factor_set"])
            and (not filters.get("factor_version") or e["factor_version"] == filters["factor_version"])
            and (not filters.get("generation_id") or e["generation_id"] == filters["generation_id"])
            and (not filters.get("date_from") or e["partition_date"] >= filters["date_from"])
            and (not filters.get("date_to") or e["partition_date"] <= filters["date_to"])
        ]
        ordering = request["ordering"]
        sort_keys_map = {
            "factor_set": lambda e: e["factor_set"],
            "factor_version": lambda e: e["factor_version"],
            "partition_date": lambda e: e["partition_date"],
            "generation_id": lambda e: e["generation_id"],
        }
        filtered.sort(key=lambda e: tuple(sort_keys_map[field](e) for field in ordering))
        limit = request["pagination"]["limit"]
        cursor = request["pagination"].get("after_sort_key")
        if cursor is not None:
            cursor_values = dict(zip(ordering, cursor))
            filtered = [
                e for e in filtered
                if tuple(sort_keys_map[field](e) for field in ordering) > tuple(cursor_values.values())
            ]
        page = filtered[:limit]
        return page

    def index(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        return self.list(query)

    def read(self, generation_id: str) -> pd.DataFrame:
        """Read the verified factor partition data by generation ID."""
        if generation_id in self._tampered_generations:
            raise ContractError(f"generation {generation_id[:12]}... has tampered or invalid partition data")
        if generation_id not in self._verified_generations:
            raise ContractError("generation not verified as accepted; call list/index first")
        return self._frames[generation_id]
