"""Deterministic per-session cross-sectional factor residualization."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence
import uuid

import numpy as np
import pandas as pd

from ..errors import ContractError
from ..contracts.model_layer import ModelContractLoader, model_manifest_identities, sha256_json
from .feature_preprocessing import _frame_fingerprint
from .factor_exposures import exposure_values_for_neutralization, normalize_exposure_panel

_SUPPORTED_CONTROLS = {"industry", "market_cap", "liquidity"}


def neutralize_factor_panel(
    factors: pd.DataFrame,
    exposures: pd.DataFrame,
    *,
    features: Sequence[str],
    controls: Sequence[str],
) -> pd.DataFrame:
    """Residualize each factor against point-in-time cross-sectional controls.

    ``factors`` must have ``instrument``, ``datetime`` and the selected feature
    columns. ``exposures`` must have the same key rows plus the requested control
    columns. Industry is expanded to sorted one-hot columns. Market cap and
    Market cap is log-transformed; liquidity uses ``log1p`` so a zero-turnover
    observation remains representable. Numeric controls are standardized within
    each session. OLS is
    fitted independently by session; the result is the residual for each factor.
    Null factor values remain null, and the input row/key order is preserved.

    This function performs no source lookup or approval. Callers must construct
    the exposure frame from a point-in-time governed store and bind its identity
    into the resulting preprocessing artifact before using residuals for model
    training.
    """
    feature_names = list(features)
    control_names = list(controls)
    if (not feature_names or len(feature_names) != len(set(feature_names))
            or any(not isinstance(name, str) or not name.strip() for name in feature_names)):
        raise ContractError("factor neutralization features must be nonempty unique names")
    if (not control_names or len(control_names) != len(set(control_names))
            or set(control_names) - _SUPPORTED_CONTROLS):
        raise ContractError("factor neutralization controls must be unique supported controls")
    if set(feature_names) & {"instrument", "datetime", *control_names}:
        raise ContractError("factor neutralization feature conflicts with a key or control")
    if not isinstance(factors, pd.DataFrame) or factors.empty:
        raise ContractError("factor neutralization requires a non-empty factor frame")
    if not isinstance(exposures, pd.DataFrame) or exposures.empty:
        raise ContractError("factor neutralization requires a non-empty exposure frame")
    factor_columns = ["instrument", "datetime", *feature_names]
    exposure_columns = ["instrument", "datetime", *control_names]
    if factors.columns.tolist() != factor_columns:
        raise ContractError("factor neutralization frame columns do not match declared features")
    if exposures.columns.tolist() != exposure_columns:
        raise ContractError("factor neutralization exposure columns do not match declared controls")

    factor_frame = factors.copy().reset_index(drop=True)
    exposure_frame = exposures.copy().reset_index(drop=True)
    for frame, label in ((factor_frame, "factor"), (exposure_frame, "exposure")):
        if frame[["instrument", "datetime"]].isna().any().any():
            raise ContractError(f"factor neutralization {label} keys cannot be null")
        if frame.duplicated(["instrument", "datetime"]).any():
            raise ContractError(f"factor neutralization {label} keys contain duplicates")
        if not frame["instrument"].map(
            lambda value: isinstance(value, str) and bool(value.strip())
        ).all():
            raise ContractError(f"factor neutralization {label} instruments must be non-empty strings")
        normalized_dates = []
        for value in frame["datetime"]:
            if not isinstance(value, str):
                raise ContractError(f"factor neutralization {label} dates must be ISO strings")
            try:
                parsed = pd.Timestamp(value)
            except (TypeError, ValueError) as exc:
                raise ContractError(f"factor neutralization {label} date is invalid") from exc
            if parsed.strftime("%Y-%m-%d") != value:
                raise ContractError(f"factor neutralization {label} dates must be canonical sessions")
            normalized_dates.append(value)
        frame["datetime"] = normalized_dates
    factor_keys = factor_frame[["instrument", "datetime"]].sort_values(
        ["datetime", "instrument"], kind="mergesort"
    ).reset_index(drop=True)
    exposure_keys = exposure_frame[["instrument", "datetime"]].sort_values(
        ["datetime", "instrument"], kind="mergesort"
    ).reset_index(drop=True)
    if not factor_keys.equals(exposure_keys):
        raise ContractError("factor and exposure frames must cover exactly the same keys")

    for name in feature_names:
        values = factor_frame[name]
        if not pd.api.types.is_numeric_dtype(values) or pd.api.types.is_bool_dtype(values):
            raise ContractError(f"factor neutralization feature must be numeric: {name}")
        non_null = values.dropna().to_numpy(dtype=float)
        if not np.isfinite(non_null).all():
            raise ContractError(f"factor neutralization feature contains non-finite values: {name}")
    for name in control_names:
        values = exposure_frame[name]
        if name == "industry":
            if not values.map(lambda value: isinstance(value, str) and bool(value.strip())).all():
                raise ContractError("factor neutralization industry labels must be non-empty strings")
        else:
            if not pd.api.types.is_numeric_dtype(values) or pd.api.types.is_bool_dtype(values):
                raise ContractError(f"factor neutralization control must be numeric: {name}")
            if values.isna().any():
                raise ContractError(f"factor neutralization control cannot be null: {name}")
            numbers = values.to_numpy(dtype=float, na_value=np.nan)
            invalid_sign = numbers < 0 if name == "liquidity" else numbers <= 0
            if not np.isfinite(numbers).all() or invalid_sign.any():
                requirement = "non-negative" if name == "liquidity" else "positive"
                raise ContractError(
                    f"factor neutralization control must be finite and {requirement}: {name}"
                )

    output = factor_frame.copy()
    for name in feature_names:
        output[name] = output[name].astype("float64")
    exposure_index = exposure_frame.set_index(["datetime", "instrument"])
    for session in sorted(factor_frame["datetime"].unique()):
        positions = np.flatnonzero(factor_frame["datetime"].to_numpy() == session)
        session_rows = factor_frame.iloc[positions]
        session_exposures = exposure_index.loc[
            [(session, instrument) for instrument in session_rows["instrument"]]
        ].reset_index(drop=True)
        design_columns: list[np.ndarray] = []
        if "industry" in control_names:
            categories = sorted(session_exposures["industry"].astype(str).str.strip().unique())
            industry_values = session_exposures["industry"].astype(str).str.strip().to_numpy()
            design_columns.extend((industry_values == category).astype(float) for category in categories)
        else:
            design_columns.append(np.ones(len(session_rows), dtype=float))
        for name in ("market_cap", "liquidity"):
            if name not in control_names:
                continue
            values = session_exposures[name].to_numpy(dtype=float)
            logged = np.log1p(values) if name == "liquidity" else np.log(values)
            deviation = float(logged.std(ddof=0))
            if not np.isfinite(deviation) or deviation <= 0:
                raise ContractError(f"factor neutralization control has no cross-sectional variation: {name}")
            design_columns.append((logged - float(logged.mean())) / deviation)
        design = np.column_stack(design_columns)

        for feature in feature_names:
            values = session_rows[feature].to_numpy(dtype=float, na_value=np.nan)
            valid = np.isfinite(values)
            if not valid.any():
                continue
            matrix = design[valid]
            target = values[valid]
            if len(target) <= matrix.shape[1]:
                raise ContractError(
                    f"factor neutralization has insufficient observations on {session} for {feature}"
                )
            coefficients, _, rank, _ = np.linalg.lstsq(matrix, target, rcond=None)
            if rank != matrix.shape[1]:
                raise ContractError(
                    f"factor neutralization controls are collinear on {session} for {feature}"
                )
            residual = target - matrix @ coefficients
            if not np.isfinite(residual).all():
                raise ContractError(f"factor neutralization produced non-finite residuals: {feature}")
            output.loc[positions[valid], feature] = residual
    return output


class FactorNeutralizationPreprocessorBuilder:
    """Build/replay a PIT-bound feature_preprocessing.v2 artifact."""

    def __init__(
        self,
        *,
        preprocess_name: str,
        semantic_version: str,
        controls: Sequence[str],
        code_fingerprint: str | None = None,
    ) -> None:
        if not isinstance(preprocess_name, str) or not preprocess_name:
            raise ContractError("neutralization preprocess_name is required")
        if not isinstance(semantic_version, str) or not semantic_version:
            raise ContractError("neutralization semantic_version is required")
        self.preprocess_name = preprocess_name
        self.semantic_version = semantic_version
        self.controls = list(controls)
        self.code_fingerprint = code_fingerprint or sha256_json({
            "component": "FactorNeutralizationPreprocessorBuilder",
            "version": 1,
            "controls": self.controls,
        })

    @staticmethod
    def _neutralizer_input(frame: pd.DataFrame) -> pd.DataFrame:
        normalized = frame.copy().reset_index(drop=True)
        dates: list[str] = []
        for value in normalized["datetime"]:
            try:
                parsed = pd.Timestamp(value)
            except (TypeError, ValueError) as exc:
                raise ContractError("factor neutralization dates are invalid") from exc
            if parsed.tzinfo is not None:
                raise ContractError("factor neutralization dates must be timezone-naive sessions")
            dates.append(parsed.strftime("%Y-%m-%d"))
        normalized["datetime"] = dates
        return normalized

    def transform(self, frame: pd.DataFrame, exposures: pd.DataFrame) -> pd.DataFrame:
        normalized_exposures = normalize_exposure_panel(exposures, self.controls)
        factor_input = self._neutralizer_input(frame)
        exposure_input = exposure_values_for_neutralization(
            normalized_exposures, controls=self.controls
        )
        output = neutralize_factor_panel(
            factor_input,
            exposure_input,
            features=list(frame.columns[2:]),
            controls=self.controls,
        )
        # Restore the input's datetime representation after the OLS helper has
        # canonicalized dates to strings. Factor and label stores use pandas
        # timestamps in Research Chain datasets.
        output["datetime"] = frame["datetime"].reset_index(drop=True)
        return output.sort_values(
            ["instrument", "datetime"], kind="mergesort"
        ).reset_index(drop=True)

    def build(
        self,
        input_frame: pd.DataFrame,
        output_frame: pd.DataFrame,
        *,
        input_factor_set: str,
        input_factor_version: str,
        input_factor_generation_ids: list[str],
        ordered_features: list[str],
        input_feature_schema: dict,
        output_feature_schema: dict,
        exposure_manifest: dict,
        exposure_frame: pd.DataFrame,
    ) -> dict:
        if input_frame.empty or output_frame.empty:
            raise ContractError("neutralization requires non-empty input and output frames")
        if list(input_frame.columns) != ["instrument", "datetime", *ordered_features]:
            raise ContractError("neutralization input columns do not match ordered features")
        if list(output_frame.columns) != ["instrument", "datetime", *ordered_features]:
            raise ContractError("neutralization output columns do not match ordered features")
        if not input_factor_generation_ids:
            raise ContractError("neutralization requires factor partition generations")
        normalized_exposure = normalize_exposure_panel(exposure_frame, self.controls)
        if (
            exposure_manifest.get("contract_version") != 1
            or exposure_manifest.get("decision_time_policy") != "session_close_asia_shanghai_v1"
            or exposure_manifest.get("controls") != self.controls
        ):
            raise ContractError("neutralization exposure manifest policy mismatch")
        exposure_generation = exposure_manifest.get("generation_id")
        exposure_digest = exposure_manifest.get("manifest_digest_sha256")
        exposure_checksum = exposure_manifest.get("data_checksum_sha256")
        for value in (exposure_generation, exposure_digest, exposure_checksum):
            if not isinstance(value, str) or len(value) != 64:
                raise ContractError("neutralization exposure binding is incomplete")
        ModelContractLoader.validate("feature_schema", input_feature_schema)
        ModelContractLoader.validate("feature_schema_v2", output_feature_schema)
        if input_feature_schema.get("contract_version") != 1 or output_feature_schema.get("contract_version") != 2:
            raise ContractError("neutralization requires feature schema v1 input and v2 output")
        self._validate_schema(input_feature_schema, ordered_features, "identity", "input")
        self._validate_schema(output_feature_schema, ordered_features, "neutralized", "output")
        from .features import FeatureSchemaValidator

        FeatureSchemaValidator.validate_against_frame(input_feature_schema, input_frame)
        FeatureSchemaValidator.validate_against_frame(output_feature_schema, output_frame)
        expected_output = self.transform(input_frame.copy(), normalized_exposure)
        try:
            pd.testing.assert_frame_equal(
                output_frame.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True),
                expected_output,
                check_exact=False,
                rtol=1e-12,
                atol=1e-12,
            )
        except AssertionError as exc:
            raise ContractError("neutralization output does not match declared PIT OLS transform") from exc
        manifest = {
            "contract_version": 2,
            "preprocess_name": self.preprocess_name,
            "semantic_version": self.semantic_version,
            "input_factor_set": input_factor_set,
            "input_factor_version": input_factor_version,
            "input_factor_generation_ids": list(input_factor_generation_ids),
            "input_frame_sha256": _frame_fingerprint(input_frame),
            "input_feature_schema_generation_id": input_feature_schema["generation_id"],
            "input_feature_schema_manifest_digest_sha256": input_feature_schema["manifest_digest_sha256"],
            "output_feature_schema_generation_id": output_feature_schema["generation_id"],
            "output_feature_schema_manifest_digest_sha256": output_feature_schema["manifest_digest_sha256"],
            "ordered_features": list(ordered_features),
            "key_columns": ["instrument", "datetime"],
            "transform": "neutralize_factor_panel",
            "policy": {
                "type": "pit_factor_neutralization_v1",
                "null_result": "preserve_null",
                "control_order": self.controls,
                "design": "per_session_ols_residual_v1",
                "visibility_policy": "session_close_asia_shanghai_v1",
            },
            "group_columns": ["datetime"],
            "factor_exposure_binding": {
                "generation_id": exposure_generation,
                "manifest_digest_sha256": exposure_digest,
                "data_checksum_sha256": exposure_checksum,
            },
            "output_frame_row_count": len(output_frame),
            "output_frame_sha256": _frame_fingerprint(output_frame),
            "code_fingerprint": self.code_fingerprint,
            "serialization_profile_id": "parquet-snappy-v1",
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "quality_report_checksum_sha256": "0" * 64,
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation, digest = model_manifest_identities(
            manifest,
            schema_name="feature_preprocessing_v2",
            exclude_fields={"input_feature_schema_manifest_digest_sha256", "quality_report_checksum_sha256"},
        )
        manifest["generation_id"] = generation
        manifest["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("feature_preprocessing_v2", manifest)
        return manifest

    @staticmethod
    def _validate_schema(schema: dict, features: list[str], status: str, label: str) -> None:
        columns = schema.get("columns")
        if not isinstance(columns, list) or [item.get("name") for item in columns] != features:
            raise ContractError(f"neutralization {label} feature schema does not match features")
        if any(item.get("transform_status") != status for item in columns):
            raise ContractError(f"neutralization {label} transform status mismatch")
