"""v6 dataset stage consumes the selected sample, not raw factor rows.

This is the integration lock for the scheme-B boundary: the stage verifies the
sample artifact's factor/label/universe lineage, feeds its selected frame into
the label-agnostic preprocessing transform, and publishes the dataset from that
frame. The unit tests on `select_training_sample` alone would not prove the
stage actually uses it.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from uq.contracts.model_layer import create_reviewed_quality_decision
from uq.models.sample_construction import SampleConstructionBuilder
from uq.errors import ContractError
from uq.research_chain.adapters import _merge_labels, _stage_binding
from uq.research_chain.owning_contracts import SampleConstructionStore
from uq.research_chain.resolver import ResolvedStageBinding
from tests.review_key import REVIEWER_PRIVATE_KEY
from tests.test_research_chain_phase2 import _execute_decisions, _prepare_dataset_store


def _sample_decision(generation: str) -> dict:
    return create_reviewed_quality_decision(
        binding_type="sample_construction_v1",
        policy="pit_label_join_v1",
        status="passed",
        checks=[
            {"name": "key_reconciliation", "threshold": 0, "observed": 0,
             "level": "error", "result": "passed"},
            {"name": "label_join_reconciliation", "threshold": 0, "observed": 0,
             "level": "error", "result": "passed"},
            {"name": "row_selection_reconciliation", "threshold": 0, "observed": 0,
             "level": "error", "result": "passed"},
            {"name": "output_readback", "threshold": 0, "observed": 1,
             "level": "error", "result": "passed"},
        ],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
        subject_generation_id=generation,
    )


def test_merge_labels_preserves_factor_order_without_wide_merge(monkeypatch) -> None:
    factor_frame = pd.DataFrame({
        "instrument": ["000002.XSHE", "000001.XSHE", "000003.XSHE"],
        "datetime": pd.to_datetime(["2026-01-06", "2026-01-05", "2026-01-07"]),
        "feature": [2.0, 1.0, 3.0],
    })
    label_frame = pd.DataFrame({
        "instrument": ["000001.XSHE", "000002.XSHE", "000004.XSHE"],
        "decision_date": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-08"]),
        "label": [0.1, 0.2, 0.4],
    })
    original = factor_frame.copy(deep=True)

    def unexpected_wide_merge(*args, **kwargs):
        raise AssertionError("label alignment must not create an intermediate wide merge frame")

    monkeypatch.setattr(pd.DataFrame, "merge", unexpected_wide_merge)
    actual = _merge_labels(
        factor_frame, label_frame, label_generation_id="a" * 64
    )

    expected = original.copy()
    expected["label"] = [0.2, 0.1, float("nan")]
    pd.testing.assert_frame_equal(actual, expected)
    pd.testing.assert_frame_equal(factor_frame, original)
    assert np.shares_memory(
        actual["feature"].to_numpy(), factor_frame["feature"].to_numpy()
    )


def test_merge_labels_rejects_duplicate_factor_keys() -> None:
    factors = pd.DataFrame({
        "instrument": ["000001.XSHE", "000001.XSHE"],
        "datetime": pd.to_datetime(["2026-01-05", "2026-01-05"]),
        "feature": [1.0, 2.0],
    })
    labels = pd.DataFrame({
        "instrument": ["000001.XSHE"],
        "decision_date": pd.to_datetime(["2026-01-05"]),
        "label": [0.1],
    })

    with pytest.raises(ContractError, match="duplicate factor keys"):
        _merge_labels(factors, labels, label_generation_id="a" * 64)


def test_v6_dataset_stage_consumes_the_sample_artifact(tmp_path: Path, monkeypatch) -> None:
    adapter, plan, factor_store = _prepare_dataset_store(tmp_path)
    factor_binding = _stage_binding(
        plan, stage="factor_computation", output_family="factor_partition"
    )
    _, factor_frame = factor_store.read_partition(factor_binding.generation_id)
    label_binding = _stage_binding(
        plan, stage="dataset_preparation", output_family="label_set"
    )
    label_manifest, label_frame = adapter.label_store.read_frame(
        label_binding.generation_id
    )
    preprocessing = adapter.preprocessing_store.read_manifest(
        _stage_binding(
            plan, stage="dataset_preparation", output_family="feature_preprocessing"
        ).generation_id
    )
    ordered_features = list(preprocessing["ordered_features"])
    keys = factor_frame[["instrument", "datetime"]].copy()
    keys["decision_date"] = pd.to_datetime(keys["datetime"]).dt.tz_localize(None)
    labels = label_frame.copy()
    labels["decision_date"] = pd.to_datetime(labels["decision_date"]).dt.tz_localize(None)
    sample_frame = keys.merge(
        labels, on=["instrument", "decision_date"], how="left", validate="one_to_one"
    )
    assert not sample_frame["label"].isna().any()
    sample_frame = factor_frame.merge(
        sample_frame[["instrument", "datetime", "label"]],
        on=["instrument", "datetime"], how="left", validate="one_to_one",
    )[["instrument", "datetime", *ordered_features, "label"]]

    source_factor_digest = "1" * 64
    sample_manifest = SampleConstructionBuilder(
        sample_name="v6-sample", semantic_version="1.0.0"
    ).build(
        sample_frame,
        window_start=plan.request["window_start_date"],
        window_end=plan.request["window_end_date"],
        source_factor_set="basic",
        source_factor_version="1.0.0",
        source_factor_generation_ids=[factor_binding.generation_id],
        source_factor_partition_set_digest_sha256=source_factor_digest,
        source_label_set_name=label_manifest["name"],
        source_label_generation_id=label_manifest["generation_id"],
        source_label_manifest_digest_sha256=label_manifest["manifest_digest_sha256"],
        source_label_data_checksum_sha256=label_manifest["data_checksum_sha256"],
        source_universe_snapshot_generation_id=_stage_binding(
            plan, stage="factor_computation", output_family="universe_snapshot"
        ).generation_id,
        source_index_membership_set_digest_sha256="2" * 64,
        ordered_features=ordered_features,
        missing_policy="fail_closed",
        rows_before_selection=len(factor_frame),
        dropped_rows={},
        dropped_labels={},
    )
    sample_store = SampleConstructionStore(tmp_path / "governed")
    published_sample = sample_store.publish(
        sample_manifest,
        sample_frame,
        quality_decision=_sample_decision(sample_manifest["generation_id"]),
    )
    adapter.sample_construction_store = sample_store

    request = json.loads(json.dumps(plan.request))
    request["contract_version"] = 6
    request["index_membership_binding"] = {
        "binding_type": "index_membership_set",
        "universe_id": "research-whitelist",
        "index_code": "000985.CSI",
        "window_start": request["window_start_date"],
        "window_end": request["window_end_date"],
        "set_digest_sha256": "2" * 64,
    }
    v6_plan = dataclasses.replace(
        plan,
        request=request,
        stage_bindings=(
            *plan.stage_bindings,
            ResolvedStageBinding(
                stage="dataset_preparation",
                output_family="sample_construction",
                generation_id=sample_manifest["generation_id"],
                manifest_digest_sha256=published_sample["manifest_digest_sha256"],
                data_checksum_sha256=published_sample["data_checksum_sha256"],
                quality_decision_checksum_sha256=published_sample[
                    "quality_report_checksum_sha256"
                ],
            ),
        ),
    )

    def unexpected_raw_factor_read(*args, **kwargs):
        raise AssertionError("v6 dataset preparation must consume the reviewed sample artifact")

    monkeypatch.setattr(adapter, "_read_factor_frame", unexpected_raw_factor_read)
    monkeypatch.setattr(
        adapter,
        "_factor_partition_set",
        lambda *args, **kwargs: {
            "set_digest_sha256": source_factor_digest,
            "partitions": [{"generation_id": factor_binding.generation_id}],
        },
    )
    decisions = _execute_decisions(v6_plan)
    decisions["sample_construction_quality_decision"] = _sample_decision(
        sample_manifest["generation_id"]
    )
    result = adapter.run(
        v6_plan,
        runner_identity={
            "code_fingerprint": "0" * 64,
            "environment_profile": "locked-test",
            "lock_digest_sha256": "0" * 64,
        },
        **decisions,
        created_at="2026-01-30T07:00:00+00:00",
    )
    assert result.manifest["row_count"] == len(sample_frame)
    assert set(result.frame["instrument"]) == set(sample_frame["instrument"])
    output_bindings = result.published_state and json.loads(
        result.published_state.manifest_path.read_text()
    )["stage_records"][-1]["output_bindings"]
    assert any(
        binding["output_family"] == "sample_construction"
        and binding["generation_id"] == sample_manifest["generation_id"]
        for binding in output_bindings
    )
