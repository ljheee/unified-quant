from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import date
from pathlib import Path

from ..contracts.schema import Schema
from ..errors import ContractError
from ..store.reader import ManifestFirstReader


class QlibExporter:
    def __init__(self, canonical_root: Path, qlib_root: Path, exporter_version: str = "0.1.0") -> None:
        self.canonical_root = canonical_root
        self.qlib_root = qlib_root
        self.exporter_version = exporter_version
        self.reader = ManifestFirstReader(canonical_root)

    def export_partition_snapshot(self, schema: Schema, partition_date: date) -> Path:
        frame = self.reader.read(schema, partition_date)
        snapshot = self.qlib_root / schema.dataset / f"v{partition_date.strftime('%Y%m%d')}.{uuid.uuid4().hex[:8]}"
        if snapshot.exists():
            raise ContractError(f"Qlib snapshot already exists: {snapshot}")
        staging = snapshot.with_name(snapshot.name + ".staging")
        staging.mkdir(parents=True)
        try:
            output = staging / "features.parquet"
            frame.to_parquet(output, index=False)
            checksum = hashlib.sha256(output.read_bytes()).hexdigest()
            input_manifest_path = self.canonical_root / "canonical" / schema.dataset / schema.version / f"date={partition_date.isoformat()}" / "manifest.json"
            manifest = {
                "snapshot": snapshot.name,
                "exporter_version": self.exporter_version,
                "input_manifest": json.loads(input_manifest_path.read_text()),
                "output_checksum_sha256": checksum,
                "row_count": len(frame),
            }
            (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
            shutil.move(staging, snapshot)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return snapshot
