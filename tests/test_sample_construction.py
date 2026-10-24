"""Locks for the independent training-sample construction artifact.

The split under test is deliberate:

* sample construction may inspect labels and remove untrainable rows;
* feature preprocessing receives only the selected feature frame and remains
  label-agnostic and stateless.

That is the Qlib split expressed as two governed artifacts rather than one
preprocessing flag: `DropnaLabel` is a learn-only sample filter, while the
cross-sectional transform is shared by learning and inference.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.model_layer import create_reviewed_quality_decision
from uq.errors import ContractError
from uq.models.sample_construction import (
    SampleConstructionBuilder,
    select_training_sample,
)
from uq.research_chain.owning_contracts import SampleConstructionStore
from tests.review_key import REVIEWER_PRIVATE_KEY


def _factor_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "instrument": ["A", "A", "A", "B", "B"],
        "datetime": pd.to_datetime(
            ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-06", "2026-01-07"]
        ),
        "f1": [1.0, 2.0, 3.0, 4.0, 5.0],
        "f2": [0.1, 0.2, 0.3, 0.4, 0.5],
    })


def _labels() -> pd.DataFrame:
    return pd.DataFrame({
        "instrument": ["A", "A", "A", "B", "B"],
        "decision_date": pd.to_datetime(
            ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-06", "2026-01-07"]
        ),
        "label": [0.01, None, 0.02, 0.03, None],
    })


def _decision(generation: str) -> dict:
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
        errors=[],
        warnings=[],
        producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
        subject_generation_id=generation,
    )


def _build_manifest(frame: pd.DataFrame, *, rows_before: int) -> dict:
    return SampleConstructionBuilder(
        sample_name="sample-v1", semantic_version="1.0.0"
    ).build(
        frame,
        window_start="2026-01-05",
        window_end="2026-01-07",
        source_factor_set="basic",
        source_factor_version="2.0.0",
        source_factor_generation_ids=["a" * 64],
        source_factor_partition_set_digest_sha256="b" * 64,
        source_label_set_name="return_5d",
        source_label_generation_id="c" * 64,
        source_label_manifest_digest_sha256="d" * 64,
        source_label_data_checksum_sha256="e" * 64,
        source_universe_snapshot_generation_id="f" * 64,
        source_index_membership_set_digest_sha256="1" * 64,
        ordered_features=["f1", "f2"],
        missing_policy="drop_unlabeled_rows",
        rows_before_selection=rows_before,
        dropped_rows={},
        dropped_labels={"rows": 2, "instruments": 2, "null_labels": 2, "absent_keys": 0},
    )


def test_sample_construction_drops_null_labels_before_preprocessing() -> None:
    selected, dropped_rows, dropped_labels = select_training_sample(
        _factor_frame(), _labels(), ordered_features=["f1", "f2"],
        missing_policy="drop_unlabeled_rows",
    )
    assert len(selected) == 3
    assert selected[["instrument", "datetime"]].to_dict("records") == [
        {"instrument": "A", "datetime": pd.Timestamp("2026-01-05")},
        {"instrument": "A", "datetime": pd.Timestamp("2026-01-07")},
        {"instrument": "B", "datetime": pd.Timestamp("2026-01-06")},
    ]
    assert selected["label"].tolist() == [0.01, 0.02, 0.03]
    assert dropped_rows == {}
    assert dropped_labels["rows"] == 2
    assert dropped_labels["null_labels"] == 2
    assert dropped_labels["absent_keys"] == 0


def test_sample_construction_drops_feature_nulls_and_label_nulls() -> None:
    factor = _factor_frame()
    factor.loc[0, "f1"] = float("nan")
    selected, dropped_rows, dropped_labels = select_training_sample(
        factor, _labels(), ordered_features=["f1", "f2"],
        missing_policy="drop_unlabeled_rows",
    )
    assert dropped_rows["rows"] == 1
    assert dropped_labels["rows"] == 2
    assert len(selected) == 2
    assert not selected[["f1", "f2", "label"]].isna().any().any()


def test_drop_incomplete_rows_still_refuses_a_null_label() -> None:
    with pytest.raises(ContractError, match="refuses null or absent labels"):
        select_training_sample(
            _factor_frame(), _labels(), ordered_features=["f1", "f2"],
            missing_policy="drop_incomplete_rows",
        )


def test_an_absent_label_key_is_removed_under_drop_unlabeled_rows() -> None:
    labels = _labels()[_labels()["instrument"] == "A"]
    selected, _, dropped_labels = select_training_sample(
        _factor_frame(), labels, ordered_features=["f1", "f2"],
        missing_policy="drop_unlabeled_rows",
    )
    assert "B" not in set(selected["instrument"])
    assert dropped_labels["absent_keys"] == 2


def test_sample_construction_publishes_a_governed_frame(tmp_path: Path) -> None:
    selected, dropped_rows, dropped_labels = select_training_sample(
        _factor_frame(), _labels(), ordered_features=["f1", "f2"],
        missing_policy="drop_unlabeled_rows",
    )
    manifest = _build_manifest(selected, rows_before=5)
    manifest["dropped_rows"] = dropped_rows
    manifest["dropped_labels"] = dropped_labels
    # Rebuild after the audit is known: the audit participates in the identity.
    manifest = SampleConstructionBuilder(
        sample_name="sample-v1", semantic_version="1.0.0"
    ).build(
        selected,
        window_start="2026-01-05",
        window_end="2026-01-07",
        source_factor_set="basic",
        source_factor_version="2.0.0",
        source_factor_generation_ids=["a" * 64],
        source_factor_partition_set_digest_sha256="b" * 64,
        source_label_set_name="return_5d",
        source_label_generation_id="c" * 64,
        source_label_manifest_digest_sha256="d" * 64,
        source_label_data_checksum_sha256="e" * 64,
        source_universe_snapshot_generation_id="f" * 64,
        source_index_membership_set_digest_sha256="1" * 64,
        ordered_features=["f1", "f2"],
        missing_policy="drop_unlabeled_rows",
        rows_before_selection=5,
        dropped_rows=dropped_rows,
        dropped_labels=dropped_labels,
    )
    store = SampleConstructionStore(tmp_path / "governed")
    published = store.publish(
        manifest, selected, quality_decision=_decision(manifest["generation_id"])
    )
    read_manifest, read_frame = store.read_frame(published["generation_id"])
    assert read_manifest["row_count"] == 3
    assert read_manifest["rows_before_selection"] == 5
    assert read_manifest["quality_report_checksum_sha256"]
    pd.testing.assert_frame_equal(read_frame, selected)


def test_a_tampered_sample_frame_is_refused(tmp_path: Path) -> None:
    selected, dropped_rows, dropped_labels = select_training_sample(
        _factor_frame(), _labels(), ordered_features=["f1", "f2"],
        missing_policy="drop_unlabeled_rows",
    )
    manifest = SampleConstructionBuilder(
        sample_name="sample-v1", semantic_version="1.0.0"
    ).build(
        selected,
        window_start="2026-01-05",
        window_end="2026-01-07",
        source_factor_set="basic",
        source_factor_version="2.0.0",
        source_factor_generation_ids=["a" * 64],
        source_factor_partition_set_digest_sha256="b" * 64,
        source_label_set_name="return_5d",
        source_label_generation_id="c" * 64,
        source_label_manifest_digest_sha256="d" * 64,
        source_label_data_checksum_sha256="e" * 64,
        source_universe_snapshot_generation_id="f" * 64,
        source_index_membership_set_digest_sha256="1" * 64,
        ordered_features=["f1", "f2"],
        missing_policy="drop_unlabeled_rows",
        rows_before_selection=5,
        dropped_rows=dropped_rows,
        dropped_labels=dropped_labels,
    )
    store = SampleConstructionStore(tmp_path / "governed")
    store.publish(manifest, selected, quality_decision=_decision(manifest["generation_id"]))
    data_path = (
        tmp_path / "governed" / "sample_construction"
        / f"generation={manifest['generation_id']}" / "data.parquet"
    )
    tampered = selected.copy()
    tampered.loc[0, "label"] = 99.0
    tampered.to_parquet(data_path, index=False)
    with pytest.raises(ContractError, match="tampered sample construction"):
        store.read_frame(manifest["generation_id"])
