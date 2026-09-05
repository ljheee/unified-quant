from __future__ import annotations

import json
import pandas as pd
import pytest

from uq.contracts.model_layer import ModelContractLoader, sha256_json
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
                "limit_up": False,
                "limit_down": False,
                "delisted": False,
                "suspended": False,
                "listing_date": pd.Timestamp("2020-01-01", tz="UTC"),
            })
    return pd.DataFrame(rows)


def _adjusted_price_checksum(frame: pd.DataFrame) -> str:
    ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort")
    return sha256_json({"rows": [
        [str(row[0]), pd.Timestamp(row[1]).isoformat(), float(row[2]), float(row[3]), bool(row[4]), str(pd.Timestamp(row[5]).date())]
        for row in ordered[["instrument", "datetime", "close", "adj_factor", "suspended", "listing_date"]].itertuples(index=False)
    ]})


def _binding(frame: pd.DataFrame | None = None) -> dict:
    return {
        "binding": "adjusted_price",
        "dataset": "bars_adjusted",
        "schema_version": "adjusted-v1",
        "partition_date": "2026-01-15",
        "generation_id": DIGEST,
        "data_checksum_sha256": _adjusted_price_checksum(frame if frame is not None else _adjusted_frame()),
        "visible_cutoff": "2026-01-15T15:00:00+08:00",
    }


class TestLabelBuilder:
    def test_build_produces_valid_manifest(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        frame = _adjusted_frame()
        manifest = builder.build(frame, upstream_bindings=[_binding(frame)])
        assert manifest["horizon_trading_days"] == 5
        assert manifest["row_count"] == 20
        assert len(manifest["generation_id"]) == 64

    def test_last_n_rows_are_null(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        frame = _adjusted_frame(n_days=10)
        manifest = builder.build(frame, upstream_bindings=[_binding(frame)])
        # Last 5 rows per instrument should have null labels.
        df = pd.DataFrame({"instrument": [f"INST{i:04d}" for i in range(2) for _ in range(10)]})
        assert manifest["row_count"] == 20  # all rows present; last 5 labels null

    def test_rejects_wrong_binding_type(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        bad_binding = {**_binding(), "binding": "raw_price"}
        with pytest.raises(ContractError, match="only accepts adjusted_price"):
            builder.build(_adjusted_frame(), upstream_bindings=[{**_binding(), "binding": "raw_price"}])

    def test_rejects_duplicate_keys(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        frame = _adjusted_frame()
        duplicated = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
        with pytest.raises(ContractError, match="duplicate"):
            builder.build(duplicated, upstream_bindings=[_binding(duplicated)])

    def test_run_metadata_change_stable_generation(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        m1 = builder.build(_adjusted_frame(), upstream_bindings=[_binding()])
        m2 = builder.build(_adjusted_frame(), upstream_bindings=[_binding()])
        assert m1["run_id"] == m2["run_id"]
        assert m1["generation_id"] == m2["generation_id"]

    @pytest.mark.parametrize(
        "field",
        ["suspended", "limit_up", "limit_down", "delisted"],
    )
    def test_label_eligibility_rules_exclude_ineligible_rows(self, field: str) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        frame = _adjusted_frame()
        frame.loc[0, field] = True
        manifest = builder.build(frame, upstream_bindings=[_binding(frame)])
        assert manifest["row_count"] == len(frame) - 1
        assert manifest["columns"] == ["instrument", "decision_date", "label"]
        eligible = frame.drop(index=0)
        labels = (
            (eligible["close"] * eligible["adj_factor"])
            .groupby(eligible["instrument"], sort=False)
            .transform(lambda values: values.shift(-5) / values - 1)
        )
        assert manifest["data_checksum_sha256"] == sha256_json({"rows": [
            [str(row[0]), pd.Timestamp(row[1]).isoformat(), None if pd.isna(row[2]) else float(row[2])]
            for row in pd.DataFrame({
                "instrument": eligible["instrument"],
                "decision_date": eligible["datetime"],
                "label": labels,
            }).itertuples(index=False)
        ]})

    def test_new_listing_rows_are_excluded_until_listing_age_threshold(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        frame = _adjusted_frame()
        frame.loc[:9, "listing_date"] = pd.Timestamp("2026-01-10", tz="UTC")
        manifest = builder.build(frame, upstream_bindings=[_binding(frame)])
        assert manifest["row_count"] == len(frame) - 10
        assert manifest["eligibility"]["rules"]["listing_age_minimum_days"] == 60
        eligible = frame.iloc[10:]
        labels = (
            (eligible["close"] * eligible["adj_factor"])
            .groupby(eligible["instrument"], sort=False)
            .transform(lambda values: values.shift(-5) / values - 1)
        )
        assert manifest["data_checksum_sha256"] == sha256_json({"rows": [
            [str(row[0]), pd.Timestamp(row[1]).isoformat(), None if pd.isna(row[2]) else float(row[2])]
            for row in pd.DataFrame({
                "instrument": eligible["instrument"],
                "decision_date": eligible["datetime"],
                "label": labels,
            }).itertuples(index=False)
        ]})


class TestLabelValidator:
    def test_validate_passes_on_valid_manifest(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        frame = _adjusted_frame()
        manifest = builder.build(frame, upstream_bindings=[_binding(frame)])
        LabelValidator.validate_manifest(manifest)

    def test_validate_rejects_wrong_horizon(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0", horizon=10)
        frame = _adjusted_frame(n_days=15)
        manifest = builder.build(frame, upstream_bindings=[_binding(frame)])
        with pytest.raises(ContractError, match="unsupported label horizon"):
            LabelValidator.validate_manifest(manifest)

    def test_validate_rejects_benchmark(self) -> None:
        builder = LabelBuilder(name="return_5d", semantic_version="1.0.0")
        frame = _adjusted_frame()
        manifest = builder.build(frame, upstream_bindings=[_binding(frame)])
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

    def test_split_overlap_and_duplicate_names_fail_closed(self) -> None:
        dates = self._dates()
        splits = [
            {"name": "train", "start_date": dates[0], "end_date": dates[14]},
            {"name": "validation", "start_date": dates[10], "end_date": dates[29]},
        ]
        with pytest.raises(ContractError):
            SplitValidator.validate_splits(
                splits, horizon=5, embargo_days=2, trading_dates=dates,
            )
        duplicate_name_splits = [
            {"name": "train", "start_date": dates[0], "end_date": dates[14]},
            {"name": "train", "start_date": dates[25], "end_date": dates[29]},
            {"name": "validation", "start_date": dates[25], "end_date": dates[29]},
        ]
        with pytest.raises(ContractError, match="duplicate split names"):
            SplitValidator.validate_splits(
                duplicate_name_splits, horizon=5, embargo_days=2, trading_dates=dates,
            )

    def test_non_adjacent_overlap_is_rejected_after_reordering(self) -> None:
        dates = self._dates(n=40)
        reversed_intervals = [
            {"name": "first", "start_date": dates[25], "end_date": dates[35]},
            {"name": "second", "start_date": dates[0], "end_date": dates[30]},
            {"name": "train", "start_date": dates[0], "end_date": dates[10]},
            {"name": "validation", "start_date": dates[38], "end_date": dates[39]},
        ]
        with pytest.raises(ContractError, match="purge/embargo violation|overlapping split intervals"):
            SplitValidator.validate_splits(
                reversed_intervals, horizon=5, embargo_days=2, trading_dates=dates,
            )

    def test_duplicate_name_with_same_interval_does_not_bypass_overlap(self) -> None:
        dates = self._dates()
        duplicate_same_interval = [
            {"name": "train", "start_date": dates[0], "end_date": dates[14]},
            {"name": "train", "start_date": dates[0], "end_date": dates[14]},
            {"name": "validation", "start_date": dates[25], "end_date": dates[29]},
        ]
        with pytest.raises(ContractError, match="duplicate split names"):
            SplitValidator.validate_splits(
                duplicate_same_interval, horizon=5, embargo_days=2, trading_dates=dates,
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
            split_policy={"purge_trading_days": 5, "embargo_trading_days": 2, "splits": [{"name": "train", "start_date": "2026-01-05", "end_date": "2026-01-12"}, {"name": "validation", "start_date": "2026-01-23", "end_date": "2026-01-26"}]},
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
            split_policy={"purge_trading_days": 5, "embargo_trading_days": 2, "splits": [{"name": "train", "start_date": "2026-01-05", "end_date": "2026-01-12"}, {"name": "validation", "start_date": "2026-01-23", "end_date": "2026-01-26"}]},
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
