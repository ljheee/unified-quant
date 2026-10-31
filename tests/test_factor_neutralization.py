from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tests.review_key import REVIEWER_PRIVATE_KEY

from uq.errors import ContractError
from uq.models.factor_neutralization import (
    FactorNeutralizationPreprocessorBuilder,
    neutralize_factor_panel,
)
from uq.models.factor_exposures import align_exposure_panel, normalize_exposure_panel


def _inputs():
    instruments = list("ABCDEFGH")
    industries = ["tech", "tech", "tech", "tech", "finance", "finance", "finance", "finance"]
    market_caps = np.array([10., 18., 35., 60., 12., 25., 42., 90.])
    liquidities = np.array([4., 7., 11., 20., 6., 14., 17., 33.])
    residuals = np.array([.7, -.4, .2, -.5, -.3, .6, -.2, -.1])
    industry_effect = np.array([1.5 if item == "tech" else -2.0 for item in industries])
    values = industry_effect + 1.7 * np.log(market_caps) - .6 * np.log(liquidities) + residuals
    factors = pd.DataFrame({
        "instrument": instruments,
        "datetime": ["2025-01-02"] * len(instruments),
        "factor": values,
        "factor_two": values * -2 + np.arange(len(values)) * .1,
    })
    exposures = pd.DataFrame({
        "instrument": instruments,
        "datetime": ["2025-01-02"] * len(instruments),
        "industry": industries,
        "market_cap": market_caps,
        "liquidity": liquidities,
    })
    return factors, exposures


def test_neutralization_removes_pit_industry_size_and_liquidity_effects():
    factors, exposures = _inputs()
    # The exposure row order is deliberately different; joins are by session/name.
    exposures = exposures.iloc[::-1].reset_index(drop=True)

    result = neutralize_factor_panel(
        factors,
        exposures,
        features=["factor", "factor_two"],
        controls=["industry", "market_cap", "liquidity"],
    )

    assert result[["instrument", "datetime"]].equals(factors[["instrument", "datetime"]])
    residual = result["factor"].to_numpy()
    assert residual[:4].mean() == pytest.approx(0., abs=1e-10)
    assert residual[4:].mean() == pytest.approx(0., abs=1e-10)
    ordered_exposures = exposures.sort_values("instrument")
    assert np.corrcoef(
        residual, np.log(ordered_exposures["market_cap"])
    )[0, 1] == pytest.approx(0., abs=1e-10)
    assert np.corrcoef(
        residual, np.log1p(ordered_exposures["liquidity"])
    )[0, 1] == pytest.approx(0., abs=1e-10)


def test_neutralization_preserves_null_factor_values_and_returns_float_columns():
    factors, exposures = _inputs()
    factors = factors[["instrument", "datetime", "factor"]].copy()
    exposures = exposures[["instrument", "datetime", "industry", "market_cap"]].copy()
    factors.loc[0, "factor"] = np.nan

    result = neutralize_factor_panel(
        factors,
        exposures,
        features=["factor"],
        controls=["industry", "market_cap"],
    )

    assert pd.isna(result.loc[0, "factor"])
    assert result["factor"].dtype == "float64"
    assert np.isfinite(result.loc[1:, "factor"]).all()


def test_liquidity_control_accepts_zero_using_log1p():
    factors, exposures = _inputs()
    factors = factors[["instrument", "datetime", "factor"]].copy()
    exposures = exposures[["instrument", "datetime", "industry", "liquidity"]].copy()
    exposures["liquidity"] = [0., 1., 2., 3., 1., 4., 6., 9.]

    result = neutralize_factor_panel(
        factors,
        exposures,
        features=["factor"],
        controls=["industry", "liquidity"],
    )

    assert np.isfinite(result["factor"]).all()


@pytest.mark.parametrize("mutate,pattern", [
    (lambda f, e: (f, e.iloc[:-1].copy()), "cover exactly the same keys"),
    (lambda f, e: (f, pd.concat([e, e.iloc[[0]]], ignore_index=True)), "keys contain duplicates"),
    (lambda f, e: (f, e.assign(market_cap=np.nan)), "cannot be null"),
    (lambda f, e: (f, e.assign(industry=" ")), "industry labels"),
    (lambda f, e: (f, e.assign(market_cap=0.)), "finite and positive"),
    (lambda f, e: (f.assign(factor=np.inf), e), "non-finite values"),
])
def test_neutralization_rejects_misaligned_or_invalid_inputs(mutate, pattern):
    factors, exposures = mutate(*_inputs())
    factors = factors[["instrument", "datetime", "factor"]].copy()
    if "liquidity" in exposures:
        exposures = exposures.drop(columns="liquidity")
    with pytest.raises(ContractError, match=pattern):
        neutralize_factor_panel(
            factors,
            exposures,
            features=["factor"],
            controls=["industry", "market_cap"],
        )


def test_neutralization_requires_full_rank_with_residual_degrees_of_freedom():
    factors, exposures = _inputs()
    factors = factors[["instrument", "datetime", "factor"]].copy()
    exposures = exposures[["instrument", "datetime", "industry", "market_cap"]].copy()
    # Size is constant inside each industry, so it is collinear with the industry dummies.
    exposures["market_cap"] = exposures["industry"].map({"tech": 10., "finance": 20.})
    with pytest.raises(ContractError, match="controls are collinear"):
        neutralize_factor_panel(
            factors,
            exposures,
            features=["factor"],
            controls=["industry", "market_cap"],
        )

    selected = [0, 4, 1]
    small = factors.iloc[selected].reset_index(drop=True)
    small_exposures = exposures.iloc[selected].reset_index(drop=True)
    with pytest.raises(ContractError, match="insufficient observations"):
        neutralize_factor_panel(
            small,
            small_exposures,
            features=["factor"],
            controls=["industry", "market_cap"],
        )


def test_neutralization_rejects_unsupported_controls_and_noncanonical_dates():
    factors, exposures = _inputs()
    with pytest.raises(ContractError, match="supported controls"):
        neutralize_factor_panel(
            factors,
            exposures.assign(momentum=1.),
            features=["factor"],
            controls=["momentum"],
        )

    factors = factors[["instrument", "datetime", "factor"]].copy()
    exposures = exposures[["instrument", "datetime", "industry", "market_cap"]].copy()
    malformed = factors.copy()
    malformed["datetime"] = "2025-1-2"
    malformed_exposures = exposures.copy()
    malformed_exposures["datetime"] = "2025-1-2"
    with pytest.raises(ContractError, match="canonical sessions"):
        neutralize_factor_panel(
            malformed,
            malformed_exposures,
            features=["factor"],
            controls=["industry", "market_cap"],
        )


def _pit_exposure_rows():
    factors, raw_exposures = _inputs()
    factors = factors[["instrument", "datetime", "factor"]].copy()
    raw_exposures = raw_exposures[["instrument", "datetime", "industry", "market_cap"]].copy()
    raw_exposures["available_at"] = "2025-01-02T07:00:00Z"
    return factors, normalize_exposure_panel(raw_exposures, ["industry", "market_cap"])


def _neutralization_schemas(frame, output):
    from uq.contracts.model_layer import model_manifest_identities
    from uq.models.features import FeatureSchemaBuilder

    input_schema = FeatureSchemaBuilder().build(
        frame, source_factor_set="basic", source_factor_version="1.0.0"
    )
    output_schema = FeatureSchemaBuilder(contract_version=2).build(
        output, source_factor_set="basic", source_factor_version="1.0.0"
    )
    for column in output_schema["columns"]:
        column["transform_status"] = "neutralized"
    output_schema["generation_id"], output_schema["manifest_digest_sha256"] = model_manifest_identities(
        output_schema, schema_name="feature_schema_v2"
    )
    return input_schema, output_schema


def _test_quality_policy():
    import uq.contracts.model_layer as model_layer
    registry = model_layer.ModelQualityReviewRegistry()
    expected = {
        "factor_exposure_panel_v1": {
            "allowed_checks": [
                "key_uniqueness", "control_values_valid", "pit_visibility_valid",
                "source_approval_present", "readback_checksum",
            ],
            "allowed_statuses": ["passed"],
            "policy": "reject_all",
        },
        "feature_preprocessing_v2": {
            "allowed_checks": [
                "key_reconciliation", "row_count_reconciliation",
                "exposure_binding_reconciliation", "pit_visibility_reconciliation",
                "output_readback",
            ],
            "allowed_statuses": ["passed"],
            "policy": "pit_factor_neutralization_v1",
        },
    }
    for binding_type, policy in expected.items():
        assert registry.bindings[binding_type] == policy
    return model_layer, registry


def test_neutralization_review_policies_are_active_and_pinned_by_trust_anchor():
    from uq.contracts.model_layer import ModelQualityReviewRegistry, ModelQualityReviewTrustAnchor

    registry = ModelQualityReviewRegistry()
    assert registry.bindings["factor_exposure_panel_v1"]["allowed_statuses"] == ["passed"]
    assert registry.bindings["feature_preprocessing_v2"]["allowed_statuses"] == ["passed"]
    anchor = ModelQualityReviewTrustAnchor()
    assert anchor.key_id == "model-quality-reviewer-v1-2026-09"


def _signed_decision(model_layer, binding_type, policy, checks):
    def provide(generation_id):
        return model_layer.create_reviewed_quality_decision(
            binding_type=binding_type,
            policy=policy,
            status="passed",
            checks=[{
                "name": name,
                "threshold": 0,
                "observed": 0,
                "level": "error",
                "result": "passed",
            } for name in checks],
            errors=[],
            warnings=[],
            producer_code_fingerprint="0" * 64,
            private_key_pem=REVIEWER_PRIVATE_KEY,
            subject_generation_id=generation_id,
        )
    return provide


def test_pit_exposure_panel_publication_and_preprocessing_v2_readback(tmp_path):
    from uq.models.dataset_writer import DatasetWriter
    from uq.research_chain.owning_contracts import (
        FactorExposureStore,
        FeaturePreprocessingStore,
        FeatureSchemaStore,
    )

    model_layer, _ = _test_quality_policy()
    factors, exposures = _pit_exposure_rows()
    exposure_store = FactorExposureStore(tmp_path / "governed")
    exposure_manifest = exposure_store.publish(
        exposures,
        controls=["industry", "market_cap"],
        source_id="offline-reviewed-source",
        source_approval_ref="review-record-2025-01",
        quality_decision=_signed_decision(
            model_layer,
            "factor_exposure_panel_v1",
            "reject_all",
            ["key_uniqueness", "control_values_valid", "pit_visibility_valid", "source_approval_present", "readback_checksum"],
        ),
    )
    read_exposure_manifest, read_exposures = exposure_store.read_frame(
        exposure_manifest["generation_id"]
    )
    assert read_exposure_manifest["source_approval_ref"] == "review-record-2025-01"
    assert len(read_exposures) == len(exposures)

    aligned = align_exposure_panel(factors, read_exposures, ["industry", "market_cap"])
    builder = FactorNeutralizationPreprocessorBuilder(
        preprocess_name="basic-neutralized-v2",
        semantic_version="1.0.0",
        controls=["industry", "market_cap"],
    )
    output = builder.transform(factors, aligned)
    input_schema, output_schema = _neutralization_schemas(factors, output)
    feature_schema_store = FeatureSchemaStore(tmp_path / "governed")
    input_schema = feature_schema_store.publish(input_schema)
    output_schema = feature_schema_store.publish(output_schema)
    assert feature_schema_store.read_schema(output_schema["generation_id"]) == output_schema
    preprocessing = builder.build(
        factors,
        output,
        input_factor_set="basic",
        input_factor_version="1.0.0",
        input_factor_generation_ids=["4" * 64],
        ordered_features=["factor"],
        input_feature_schema=input_schema,
        output_feature_schema=output_schema,
        exposure_manifest=read_exposure_manifest,
        exposure_frame=aligned,
    )
    preprocessing_store = FeaturePreprocessingStore(tmp_path / "governed")
    published_preprocessing = preprocessing_store.publish(
        preprocessing,
        quality_decision=_signed_decision(
            model_layer,
            "feature_preprocessing_v2",
            "pit_factor_neutralization_v1",
            ["key_reconciliation", "row_count_reconciliation", "exposure_binding_reconciliation", "pit_visibility_reconciliation", "output_readback"],
        ),
    )
    assert preprocessing_store.read_manifest(preprocessing["generation_id"]) == published_preprocessing

    from uq.research_chain.adapters import _prepare_preprocessed_frame

    replay_input, replay_output = _prepare_preprocessed_frame(
        factors,
        published_preprocessing,
        input_schema,
        output_schema,
        label_generation_id="0" * 64,
        exposure_frame=aligned,
    )
    pd.testing.assert_frame_equal(replay_input, factors)
    pd.testing.assert_frame_equal(replay_output, output)

    dataset_manifest = {
        "feature_preprocessing_generation_id": preprocessing["generation_id"],
        "factor_set": "basic",
        "factor_version": "1.0.0",
        "factor_generation_ids": ["4" * 64],
        "input_feature_schema_generation_id": input_schema["generation_id"],
        "input_feature_schema_manifest_digest_sha256": input_schema["manifest_digest_sha256"],
        "input_feature_schema_path": "feature_schemas/input.json",
    }
    DatasetWriter(tmp_path)._validate_preprocessing(
        dataset_manifest,
        output,
        output_schema,
        published_preprocessing,
        factors,
        input_schema,
        output,
        aligned,
        read_exposure_manifest,
    )
    with pytest.raises(ContractError, match="does not match"):
        DatasetWriter(tmp_path)._validate_preprocessing(
            dataset_manifest,
            output,
            output_schema,
            published_preprocessing,
            factors,
            input_schema,
            output,
            aligned.assign(market_cap=aligned["market_cap"].iloc[::-1].to_numpy()),
            read_exposure_manifest,
        )


@pytest.mark.parametrize("available_at", [
    "2025-01-02T07:00:00",  # no timezone
    "2025-01-02T15:00:01+08:00",  # after the decision close
])
def test_exposure_panel_rejects_non_pit_availability(available_at):
    _, exposures = _pit_exposure_rows()
    changed = exposures.assign(available_at=available_at)
    with pytest.raises(ContractError, match="timezone|visible"):
        normalize_exposure_panel(changed, ["industry", "market_cap"])


def test_neutralization_exposure_publish_fails_closed_without_anchored_review_policy(tmp_path):
    from uq.research_chain.owning_contracts import FactorExposureStore

    _, exposures = _pit_exposure_rows()
    with pytest.raises(ContractError, match="quality decision"):
        FactorExposureStore(tmp_path).publish(
            exposures,
            controls=["industry", "market_cap"],
            source_id="offline-source",
            source_approval_ref="approval-reference",
            quality_decision={},
        )
