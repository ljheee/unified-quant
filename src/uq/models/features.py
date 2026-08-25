from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from ..contracts.model_layer import ModelContractLoader, model_manifest_identities, sha256_json
from ..errors import ContractError


class FeatureSchemaBuilder:
    """Build a feature schema subcontract from a factor frame."""

    def __init__(self, *, schema_version: str = "1.0.0") -> None:
        self.schema_version = schema_version

    def build(self, frame: pd.DataFrame, *, source_factor_set: str, source_factor_version: str) -> dict[str, Any]:
        if frame.empty:
            raise ContractError("cannot build feature schema from empty frame")
        key_columns = {"instrument", "datetime"}
        feature_columns = [c for c in frame.columns if c not in key_columns]
        if not feature_columns:
            raise ContractError("no feature columns found in frame")
        columns = []
        for name in feature_columns:
            dtype = str(frame[name].dtype)
            mapped = "float64" if "float" in dtype else ("int64" if "int" in dtype else ("bool" if "bool" in dtype else "string"))
            columns.append({
                "name": name,
                "source_factor": name,
                "factor_set": source_factor_set,
                "factor_version": source_factor_version,
                "dtype": mapped,
                "unit": "dimensionless",
                "nullable": bool(frame[name].isna().any()),
                "null_semantics": "native_null",
                "transform_status": "identity",
                "forbidden_transforms": [],
                "fingerprint_normalization": "round_12",
            })
        manifest: dict[str, Any] = {
            "contract_version": 1,
            "schema_version": self.schema_version,
            "columns": columns,
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation_id, manifest_digest = model_manifest_identities(manifest, schema_name="feature_schema")
        manifest["generation_id"] = generation_id
        manifest["manifest_digest_sha256"] = manifest_digest
        ModelContractLoader.validate("feature_schema", manifest)
        return manifest


class FeatureSchemaValidator:
    @staticmethod
    def validate_against_frame(schema: dict[str, Any], frame: pd.DataFrame) -> None:
        ModelContractLoader.validate("feature_schema", schema)
        expected_names = [col["name"] for col in schema["columns"]]
        key_order = [c for c in frame.columns if c in {"instrument", "datetime"}]
        actual_features = [c for c in frame.columns if c not in {"instrument", "datetime"}]
        if key_order != ["instrument", "datetime"]:
            raise ContractError("dataset key column order must be instrument then datetime")
        if expected_names != [c for c in actual_features if c in expected_names]:
            raise ContractError(
                f"feature schema columns missing or reordered: expected {expected_names}, got {actual_features}"
            )
        for col_def in schema["columns"]:
            name = col_def["name"]
            if name not in frame.columns:
                raise ContractError(f"feature column {name} missing from frame")
            actual_dtype = str(frame[name].dtype)
            expected_dtype = col_def["dtype"]
            type_matches = (
                (expected_dtype == "float64" and "float" in actual_dtype)
                or (expected_dtype == "int64" and "int" in actual_dtype and "float" not in actual_dtype)
                or (expected_dtype == "bool" and "bool" in actual_dtype)
                or (expected_dtype == "string" and ("object" in actual_dtype or "str" in actual_dtype))
            )
            if not type_matches:
                raise ContractError(f"dtype mismatch for {name}: expected {expected_dtype}, got {actual_dtype}")
