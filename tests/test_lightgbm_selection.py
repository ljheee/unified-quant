from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from uq.contracts.model_layer import model_manifest_identities
from uq.errors import ContractError
from uq.models.lightgbm_selection import select_lightgbm_candidate


def _manifest() -> dict:
    digest = "0" * 64
    result = {
        "contract_version": 1,
        "dataset_name": "lightgbm_fixture",
        "semantic_version": "1.0.0",
        "ordered_features": ["signal", "noise"],
        "factor_set": "fixture",
        "factor_version": "1.0.0",
        "factor_generation_ids": [digest],
        "label_set_name": "return_5d",
        "label_generation_id": digest,
        "universe_snapshot_generation_id": digest,
        "split_policy": {
            "purge_trading_days": 1,
            "embargo_trading_days": 1,
            "splits": [
                {"name": "train", "start_date": "2026-01-05", "end_date": "2026-01-16"},
                {"name": "validation", "start_date": "2026-01-26", "end_date": "2026-01-30"},
                {"name": "test", "start_date": "2026-02-09", "end_date": "2026-02-13"},
            ],
        },
        "missing_policy": "drop_unlabeled_rows",
        "row_count": 999,
        "input_feature_schema_generation_id": None,
        "input_feature_schema_manifest_digest_sha256": None,
        "input_feature_schema_path": None,
        "data_checksum_sha256": digest,
        "logical_fingerprint": digest,
        "code_fingerprint": digest,
        "serialization_profile_id": "uq-parquet-v1",
        "quality_report_checksum_sha256": digest,
        "run_id": "00000000-0000-4000-8000-000000000001",
        "created_at": "2026-01-01T00:00:00+00:00",
        "generation_id": digest,
        "manifest_digest_sha256": digest,
    }
    generation, manifest_digest = model_manifest_identities(
        result,
        schema_name="model_dataset",
        exclude_fields={
            "quality_report_checksum_sha256",
            "logical_fingerprint",
            "input_feature_schema_manifest_digest_sha256",
        },
    )
    result["generation_id"] = generation
    result["manifest_digest_sha256"] = manifest_digest
    return result


def _frame(test_label: float = 0.0) -> pd.DataFrame:
    rng = np.random.RandomState(23)
    dates = pd.bdate_range("2026-01-05", "2026-02-13")
    rows = []
    for day in dates:
        for instrument in range(30):
            signal = rng.normal()
            noise = rng.normal()
            label = 0.04 * signal + 0.002 * noise
            if day.strftime("%Y-%m-%d") >= "2026-02-09":
                label = test_label
            rows.append((f"S{instrument:02}", day, signal, noise, label))
    return pd.DataFrame(rows, columns=["instrument", "datetime", "signal", "noise", "label"])


def _run(frame: pd.DataFrame):
    pytest.importorskip("lightgbm")
    return select_lightgbm_candidate(
        dataset_manifest=_manifest(),
        dataset_frame=frame,
        candidates=[{"n_estimators": 8, "num_leaves": 5, "learning_rate": 0.1, "min_child_samples": 5}],
        seed=19,
        thread_count=1,
    )


def test_test_labels_do_not_change_selected_candidate_or_test_scores() -> None:
    ordinary = _run(_frame(test_label=0.0))
    altered = _run(_frame(test_label=1_000_000.0))

    assert ordinary.receipt["test_labels_used_for_selection"] is False
    assert ordinary.receipt["selected_parameters"] == altered.receipt["selected_parameters"]
    assert ordinary.receipt["trials"] == altered.receipt["trials"]
    pd.testing.assert_frame_equal(ordinary.test_predictions, altered.test_predictions)
    assert ordinary.model_text == altered.model_text


def test_candidate_budget_and_feature_order_are_enforced() -> None:
    with pytest.raises(ContractError, match="trial budget"):
        select_lightgbm_candidate(
            dataset_manifest=_manifest(), dataset_frame=_frame(), candidates=[{}, {}, {}, {}]
        )
    with pytest.raises(ContractError, match="feature columns"):
        select_lightgbm_candidate(
            dataset_manifest=_manifest(), dataset_frame=_frame(), feature_columns=["noise", "signal"]
        )


@pytest.mark.parametrize(("parameter", "value"), [("seed", True), ("seed", "7"),
                                                     ("thread_count", True), ("thread_count", 1.5)])
def test_seed_and_thread_count_require_real_integers(parameter, value) -> None:
    with pytest.raises(ContractError, match="seed must be non-negative"):
        select_lightgbm_candidate(dataset_manifest=_manifest(), dataset_frame=_frame(),
                                  **{parameter: value})
