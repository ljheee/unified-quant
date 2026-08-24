from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from ..contracts.schema import Schema
from ..contracts.manifest import anchor_generation_id, sha256_json, schema_file_checksum
from ..errors import ContractError


class CanonicalStore:
    """Publish immutable canonical partitions with manifest-first reads."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.code_fingerprint = sha256_json({"component": "CanonicalStore", "version": 1})
        
    def publish(
        self,
        schema: Schema,
        partition_date: date,
        frame: pd.DataFrame,
        lineage: dict[str, Any],
        source_versions: dict[str, str],
        quality_checksum: str = "",
        raw_artifacts: dict[str, object] | None = None,
    ) -> Path:
        schema.validate(frame)
        partition = (
            self.root / "canonical" / schema.dataset / schema.version
            / f"date={partition_date.isoformat()}"
        )
        if partition.exists():
            raise ContractError(f"immutable partition already published: {partition}")

        lock_path = self.root / ".locks" / f"{schema.dataset}.{schema.version}.{partition_date.isoformat()}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if partition.exists():
                raise ContractError(f"immutable partition already published: {partition}")
            staging = partition.with_name(partition.name + f".staging.{uuid.uuid4().hex}")
            staging.mkdir(parents=True)
            try:
                data_path = staging / "data.parquet"
                frame.sort_values(list(schema.sort_key)).to_parquet(data_path, index=False)
                checksum = self._sha256(data_path)
                restored = pd.read_parquet(data_path)
                schema.validate(restored)
                schema_checksum = schema_file_checksum(schema.source_path)
                manifest = {
                    "manifest_version": 1,
                    "dataset": schema.dataset,
                    "schema_version": schema.version,
                    "partition_date": partition_date.isoformat(),
                    "row_count": len(frame),
                    "columns": list(restored.columns),
                    "dtypes": {name: str(dtype) for name, dtype in restored.dtypes.items()},
                    "data_checksum_sha256": checksum,
                    "schema_checksum_sha256": schema_checksum,
                    "code_fingerprint": self.code_fingerprint,
                    "run_id": str(uuid.uuid4()),
                    "created_at": pd.Timestamp.now(tz="UTC").isoformat(),
                    "lineage": lineage,
                    "source_versions": source_versions,
                    "quality_report_checksum": quality_checksum,
                    "raw_artifacts": raw_artifacts or {},
                }
                manifest["generation_id"] = sha256_json({
                    key: value for key, value in manifest.items() if key != "generation_id"
                })
                manifest["trust_anchor_sha256"] = anchor_generation_id(manifest["generation_id"])
                manifest_path = staging / "manifest.json"
                manifest_path.write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2), encoding="utf-8")
                self._fsync_tree(staging)
                os.rename(staging, partition)
                self._fsync_dir(partition.parent)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return partition / "data.parquet"

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _fsync_tree(root: Path) -> None:
        for path in root.rglob("*"):
            if path.is_file():
                fd = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        CanonicalStore._fsync_dir(root)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
