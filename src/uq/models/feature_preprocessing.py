from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import pandas as pd

from ..contracts.model_layer import ModelContractLoader, model_manifest_identities, sha256_json
from ..errors import ContractError


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    from ..factors.raw_price import logical_fingerprint

    canonical = frame.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True)
    return logical_fingerprint(canonical)


class FeaturePreprocessorBuilder:
    """Build a stateless, cross-sectional preprocessing manifest.

    A single manifest must declare one transform type. This prevents the common
    order-of-operations ambiguity that makes multi-step pipelines hard to audit.
    """

    def __init__(
        self,
        *,
        preprocess_name: str,
        semantic_version: str,
        transform: Literal["standardize_cross_section", "rank_cross_section"],
        min_group_observations: int = 2,
        code_fingerprint: str | None = None,
    ) -> None:
        self.preprocess_name = preprocess_name
        self.semantic_version = semantic_version
        self.transform_type = transform
        if min_group_observations < 2:
            raise ContractError("cross-sectional preprocessing requires at least two observations")
        self.min_group_observations = min_group_observations
        self.code_fingerprint = code_fingerprint or sha256_json({
            "component": "FeaturePreprocessorBuilder",
            "version": 1,
            "transform": transform,
        })

    def build(
        self,
        input_frame: pd.DataFrame,
        output_frame: pd.DataFrame,
        *,
        input_factor_set: str,
        input_factor_version: str,
        input_factor_generation_ids: list[str],
        ordered_features: list[str],
    ) -> dict[str, Any]:
        if input_frame.empty or output_frame.empty:
            raise ContractError("preprocessing requires non-empty input and output frames")
        self.validate_frame_keys(input_frame)
        self.validate_frame_keys(output_frame)
        if not ordered_features or len(ordered_features) != len(set(ordered_features)):
            raise ContractError("ordered_features must be non-empty and unique")
        if not input_factor_generation_ids:
            raise ContractError("preprocessing requires at least one input factor generation")
        if list(input_frame.columns) != ["instrument", "datetime", *ordered_features]:
            raise ContractError("input frame columns do not match ordered features")
        if list(output_frame.columns) != ["instrument", "datetime", *ordered_features]:
            raise ContractError("output frame columns do not match ordered features")
        expected_output = self._apply(input_frame.copy(), ordered_features)
        pd.testing.assert_frame_equal(output_frame, expected_output, check_exact=False, rtol=1e-12, atol=1e-12)

        manifest: dict[str, Any] = {
            "contract_version": 1,
            "preprocess_name": self.preprocess_name,
            "semantic_version": self.semantic_version,
            "input_factor_set": input_factor_set,
            "input_factor_version": input_factor_version,
            "input_factor_generation_ids": list(input_factor_generation_ids),
            "input_frame_sha256": _frame_fingerprint(input_frame),
            "ordered_features": list(ordered_features),
            "key_columns": ["instrument", "datetime"],
            "transform": self.transform_type,
            "policy": {
                "type": "cross_sectional_stateless_v1",
                "null_result": "preserve_null",
                "min_group_observations": self.min_group_observations,
            },
            "group_columns": ["datetime"],
            "output_frame_row_count": len(output_frame),
            "output_frame_sha256": _frame_fingerprint(output_frame),
            "code_fingerprint": self.code_fingerprint,
            "serialization_profile_id": "parquet-snappy-v1",
            "quality_report_checksum_sha256": "0" * 64,
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation_id, manifest_digest = model_manifest_identities(
            manifest, schema_name="feature_preprocessing", exclude_fields={"quality_report_checksum_sha256"}
        )
        manifest["generation_id"] = generation_id
        manifest["manifest_digest_sha256"] = manifest_digest
        ModelContractLoader.validate("feature_preprocessing", manifest)
        return manifest

    def validate_frame_keys(self, frame: pd.DataFrame) -> None:
        if list(frame.columns[:2]) != ["instrument", "datetime"]:
            raise ContractError("preprocessing frame keys must be instrument then datetime")
        if frame.empty:
            raise ContractError("preprocessing frame is empty")
        if frame.duplicated(["instrument", "datetime"]).any():
            raise ContractError("duplicate preprocessing keys")
        if frame["instrument"].isna().any() or frame["datetime"].isna().any():
            raise ContractError("null preprocessing keys")

    def transform(self, frame: pd.DataFrame, ordered_features: list[str] | None = None) -> pd.DataFrame:
        return self._apply(frame, ordered_features).sort_values(
            ["instrument", "datetime"], kind="mergesort"
        ).reset_index(drop=True)

    def _apply(self, frame: pd.DataFrame, ordered_features: list[str] | None = None) -> pd.DataFrame:
        features = ordered_features if ordered_features is not None else list(frame.columns[2:])
        output = frame.copy()
        group = output.groupby("datetime", sort=False, dropna=False)
        for name in features:
            if name not in output.columns:
                raise ContractError(f"feature column {name} is absent")
            if name in {"instrument", "datetime"}:
                raise ContractError(f"feature column {name} conflicts with key column")
            series = output[name]
            if not (pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series)):
                raise ContractError(f"cross-sectional preprocessing requires float64/int64 feature: {name}")
            if self.transform_type == "standardize_cross_section":
                values = group[name].transform(lambda values: self._standardize(values))
            else:
                values = group[name].transform(lambda values: self._rank(values))
            output[name] = values.astype("float64")
        return output

    def _standardize(self, values: pd.Series) -> pd.Series:
        finite = values.dropna()
        if len(finite) < self.min_group_observations:
            raise ContractError("cross-sectional group has fewer than minimum observations")
        mean = float(finite.mean())
        std = float(finite.std(ddof=0))
        if not pd.api.types.is_scalar(std) or std <= 0.0:
            raise ContractError("cross-sectional standard deviation is non-positive")
        result = (values - mean) / std
        result[values.isna()] = float("nan")
        return result

    def _rank(self, values: pd.Series) -> pd.Series:
        finite = values.dropna()
        if len(finite) < self.min_group_observations:
            raise ContractError("cross-sectional group has fewer than minimum observations")
        result = values.rank(method="average", na_option="keep", ascending=True)
        count = float(len(finite))
        result = result.combine(values.isna(), lambda rank, missing: float("nan") if missing else (rank - 1.0) / (count - 1.0))
        return result
