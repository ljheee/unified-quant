from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd

from ..contracts.manifest import load_manifest_with_schema, sha256_bytes
from ..contracts.schema import Schema
from ..errors import ContractError


class ManifestFirstReader:
    """Only read published partitions through valid manifests."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self, schema: Schema, partition_date: date, verify_checksum: bool = True) -> pd.DataFrame:
        partition = self.root / "canonical" / schema.dataset / schema.version / f"date={partition_date.isoformat()}"
        data_path = partition / "data.parquet"
        manifest_path = partition / "manifest.json"
        if not manifest_path.is_file() or not data_path.is_file():
            raise ContractError(f"unpublished or incomplete partition: {partition}")

        manifest = load_manifest_with_schema(json.loads(manifest_path.read_text(encoding="utf-8")))
        expected_anchor = sha256_bytes(manifest["generation_id"].encode("ascii"))
        if manifest.get("trust_anchor_sha256") != expected_anchor:
            raise ContractError("manifest trust anchor mismatch")
        if manifest.get("dataset") != schema.dataset or manifest.get("schema_version") != schema.version:
            raise ContractError("manifest does not match requested schema")
        if verify_checksum:
            actual_checksum = hashlib.sha256(data_path.read_bytes()).hexdigest()
            if manifest.get("data_checksum_sha256") != actual_checksum:
                raise ContractError("data checksum mismatch")
        frame = pd.read_parquet(data_path)
        if len(frame) != int(manifest.get("row_count", -1)):
            raise ContractError("manifest row count mismatch")
        if list(frame.columns) != list(manifest.get("columns", [])):
            raise ContractError("manifest column order mismatch")
        for name, dtype in manifest.get("dtypes", {}).items():
            if name in frame and str(frame[name].dtype) != dtype:
                raise ContractError(f"manifest dtype mismatch: {name}")
        schema.validate(frame)
        return frame
