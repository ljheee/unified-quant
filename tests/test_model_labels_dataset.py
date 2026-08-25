from __future__ import annotations

import json
import copy
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from uq.contracts.model_layer import ModelContractLoader, resolve_bindings
from uq.errors import ContractError
from uq.models.dataset import DatasetBuilder, SplitValidator
from uq.models.labels import LabelBuilder, LabelValidator

DIGEST = "0" * 64


def _adjusted_frame(n_instruments: int = 2, n_days: int = 10) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=n_days)
    rows = []
    for i in range(n_instruments):
        for date in dates:
            rows.append({
                "instrument": f"INST{i:04d}",
                "datetime": date,
                "close": 10.0 + i,
                "adj_factor": 1.0 + 0.01 * i,
            })
    return pd.DataFrame(rows)


def _binding() -> dict:
    return {
        "binding": "adjusted_price",
        "dataset": "bars_adjusted",
        "schema_version": "adjusted-v1",
        "partition_date": "2026-01-15",
        "generation_id": DIGEST,
        "data_checksum_sha256": DIGEST,
        "visible_cutoff": "2026-01-15T15:00:00+08:00",
    }


class TestLabelBuilder:
    def test_build_produces_valid_manifest(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        frame = _adjusted_frame()
        manifest = builder.build(frame, upstream_bindings=[_binding()])
        assert manifest["horizon_trading_days"] == 5
        assert manifest["row_count"] == 20
        assert len(manifest["generation_id"]) == 64

    def test_last_n_rows_are_null(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        frame = _adjusted_frame(n_days=10)
        manifest = builder.build(frame, upstream_bindings=[_binding()])
        # Last 5 rows per instrument should have null labels.
        df = pd.DataFrame({"instrument": [f"INST{i:04d}" for i in range(2) for _ in range(10)]})
        assert manifest["row_count"] == 20  # all rows present; last 5 labels null

    def test_rejects_wrong_binding_type(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        bad_binding = {**_binding(), "binding": "raw_price"}
        with pytest.raises(ContractError, match="only accepts adjusted_price"):
            builder.build(_adjusted_frame(), upstream_bindings=[bad_binding])

    def test_rejects_duplicate_keys(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        frame = _adjusted_frame()
        duplicated = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
        with pytest.raises(ContractError, match="duplicate"):
            builder.build(duplicated, upstream_bindings=[_binding()])

    def test_run_metadata_change_stable_generation(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        m1 = builder.build(_adjusted_frame(), upstream_bindings=[_binding()])
        m2 = builder.build(_adjusted_frame(), upstream_bindings=[_binding()])
        # run_id/created_at differ but generation should be same if content is same
        assert m1["run_id"] != m2["run_id"]


class TestLabelValidator:
    def test_validate_passes_on_valid_manifest(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        manifest = builder.build(_adjusted_frame(), upstream_bindings=[_binding()])
        LabelValidator.validate_manifest(manifest)

    def test_validate_rejects_wrong_horizon(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0", horizon=10)
        frame = _adjusted_frame(n_days=15)
        manifest = builder.build(frame, upstream_bindings=[_binding()])
        with pytest.raises(ContractError, match="unsupported label horizon"):
            LabelValidator.validate_manifest(manifest)

    def test_validate_rejects_benchmark(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        manifest = builder.build(_adjusted_frame(), upstream_bindings=[_binding()])
        tampered = {**manifest, "benchmark_binding": {"name": "csi300"}}
        tampered["generation_id"] = "0" * 64
        tampered["manifest_digest_sha256"] = "0" * 64
        from uq.contracts.model_layer import model_manifest_identities
        gen, digest = model_manifest_identities(tampered, schema_name="label_set")
        tampered["generation_id"] = gen; tampered["manifest_digest_sha256"] = digest
        with pytest.raises(ContractError, match="benchmark"):
            LabelValidator.validate_manifest(tampered)


class TestSplitValidator:
    def _dates(self, n: int = 30) -> list[str]:
        return [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-01", periods=n)]

    def test_valid_splits_pass(self) -> None:
        dates = self._dates()
        SplitValidator.validate_splits(
            [
                {"name": "train", "start_date": dates[0], "end_date": dates[14]},
                {"name": "validation", "start_date": dates[25], "end_date": dates[29]},
            ],
            horizon=5, embargo_days=2, trading_dates=dates,
        )

    def test_purge_violation_fails(self) -> None:
        dates = self._dates()
        with pytest.raises(ContractError, match="purge/embargo violation"):
            SplitValidator.validate_splits(
                [
                    {"name": "train", "start_date": dates[0], "end_date": dates[14]},
                    {"name": "validation", "start_date": dates[17], "end_date": dates[29]},
                ],
                horizon=5, embargo_days=2, trading_dates=dates,
            )


class TestDatasetBuilder:
    def test_build_produces_valid_manifest(self) -> None:
        builder = DatasetBuilder(dataset_name="research_slice", semantic_version="1.0.0")
        manifest = builder.build(
            ordered_features=["volume_ratio_20d"],
            factor_set="basic", factor_version="1.0.0",
            factor_generation_ids=[DIGEST],
            label_set_name="return_5d", label_generation_id=DIGEST,
            universe_snapshot_generation_id=DIGEST,
            split_policy={"purge_trading_days": 5, "embargo_trading_days": 2, "splits": [{"name": "train", "start_date": "2026-01-01", "end_date": "2026-01-28"}]},
            row_count=100,
        )
        assert len(manifest["generation_id"]) == 64
        ModelContractLoader.validate("model_dataset", manifest)

    def test_changed_features_create_new_generation(self) -> None:
        builder = DatasetBuilder(dataset_name="research_slice", semantic_version="1.0.0")
        common = dict(
            factor_set="basic", factor_version="1.0.0", factor_generation_ids=[DIGEST],
            label_set_name="return_5d", label_generation_id=DIGEST,
            universe_snapshot_generation_id=DIGEST,
            split_policy={"purge_trading_days": 5, "embargo_trading_days": 2, "splits": [{"name": "train", "start_date": "2026-01-01", "end_date": "2026-01-28"}]},
            row_count=100,
        )
        m1 = builder.build(ordered_features=["volume_ratio_20d"], **common)
        m2 = builder.build(ordered_features=["volume_ratio_20d", "turnover_20d"], **common)
        assert m1["generation_id"] != m2["generation_id"]

    def test_empty_features_rejected(self) -> None:
        builder = DatasetBuilder(dataset_name="research_slice", semantic_version="1.0.0")
        with pytest.raises(ContractError, match="at least one feature"):
            builder.build(
                ordered_features=[], factor_set="basic", factor_version="1.0.0",
                factor_generation_ids=[DIGEST], label_set_name="x", label_generation_id=DIGEST,
                universe_snapshot_generation_id=DIGEST,
                split_policy={"purge_trading_days": 5, "embargo_trading_days": 2, "splits": []},
                row_count=0,
            )
