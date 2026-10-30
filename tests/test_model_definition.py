from __future__ import annotations

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.models.definition import MetricReport, ModelDefinitionBuilder


DIGEST = "0" * 64


def _common() -> dict:
    return {
        "model_set": "baseline",
        "model_version": "1.0.0",
        "feature_schema_generation_id": DIGEST,
        "compatible_dataset_versions": ["1.0.0"],
        "metrics": [{"name": "ic", "direction": "maximize"}],
        "selection_rule": "maximum validation ic",
    }


class TestModelDefinitionBuilder:
    def test_build_linear_model(self) -> None:
        builder = ModelDefinitionBuilder(run_content_generation_id=DIGEST, reviewed=True)
        manifest = builder.build(
            algorithm="regularized_linear",
            hyperparameters={"alpha": 1.0},
            seed_policy={"base_seed": 42, "derivation": "fixed"},
            **_common(),
        )
        assert len(manifest["generation_id"]) == 64
        assert manifest["status"] == "reviewed"

    def test_reject_unsupported_algorithm(self) -> None:
        builder = ModelDefinitionBuilder(run_content_generation_id=DIGEST, reviewed=True)
        with pytest.raises(ContractError, match="unsupported algorithm"):
            builder.build(algorithm="xgboost", hyperparameters={}, seed_policy={"base_seed": 0, "derivation": "fixed"}, **_common())

    def test_build_lightgbm_uses_version_two_contract(self) -> None:
        builder = ModelDefinitionBuilder(run_content_generation_id=DIGEST, reviewed=True)
        manifest = builder.build(
            algorithm="qlib_lightgbm",
            hyperparameters={"n_estimators": 20, "num_leaves": 7},
            seed_policy={"base_seed": 42, "derivation": "fixed"},
            contract_version=2,
            **_common(),
        )
        assert manifest["contract_version"] == 2
        assert manifest["serializer_version"] == "joblib-v1"
        assert len(manifest["generation_id"]) == 64

    def test_lightgbm_requires_version_two_and_joblib(self) -> None:
        builder = ModelDefinitionBuilder(run_content_generation_id=DIGEST, reviewed=True)
        with pytest.raises(ContractError, match="unsupported algorithm"):
            builder.build(
                algorithm="qlib_lightgbm", hyperparameters={},
                seed_policy={"base_seed": 42, "derivation": "fixed"}, **_common(),
            )
        with pytest.raises(ContractError, match="requires serializer"):
            builder.build(
                algorithm="qlib_lightgbm", hyperparameters={},
                seed_policy={"base_seed": 42, "derivation": "fixed"},
                serializer_version="json-numpy-v1", contract_version=2, **_common(),
            )

    def test_reject_empty_metrics(self) -> None:
        builder = ModelDefinitionBuilder(run_content_generation_id=DIGEST, reviewed=True)
        common = _common()
        common["metrics"] = []
        with pytest.raises(ContractError, match="at least one metric"):
            builder.build(algorithm="regularized_linear", hyperparameters={"alpha": 1.0}, seed_policy={"base_seed": 42, "derivation": "fixed"}, **common)

    def test_changed_hyperparams_new_generation(self) -> None:
        builder = ModelDefinitionBuilder(run_content_generation_id=DIGEST, reviewed=True)
        m1 = builder.build(algorithm="regularized_linear", hyperparameters={"alpha": 1.0}, seed_policy={"base_seed": 42, "derivation": "fixed"}, **_common())
        m2 = builder.build(algorithm="regularized_linear", hyperparameters={"alpha": 2.0}, seed_policy={"base_seed": 42, "derivation": "fixed"}, **_common())
        assert m1["generation_id"] != m2["generation_id"]


class TestMetricReport:
    def _frame(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        dates = pd.bdate_range("2026-01-05", periods=3)
        rows = []
        for d in dates:
            for i in range(10):
                rows.append({
                    "instrument": f"INST{i}",
                    "decision_date": d,
                    "score": float(i) + hash(str(d)) % 100 / 10000,
                    "label": float(i) * 0.01,
                })
        df = pd.DataFrame(rows)
        preds = df[["instrument", "decision_date", "score"]]
        actuals = df[["instrument", "decision_date", "label"]]
        return preds, actuals

    def test_ic_defaults_to_cross_sectional_mean(self) -> None:
        # cr.md B11: the IC family is cross-sectional by default -- each decision
        # date is one cross-section, correlated separately, then averaged.
        preds, actuals = self._frame()
        report = MetricReport.compute(
            preds, actuals, metric_definitions=[{"name": "ic", "direction": "maximize"}]
        )
        assert len(report["results"]) == 1
        result = report["results"][0]
        assert result["basis"] == "cross_sectional"
        assert result["cross_section_count"] == 3
        # OBS-2: ``sample_count`` is the row count of the merged frame; nothing
        # asserted it before, so it could drift unnoticed.
        assert result["sample_count"] == 30
        assert -1 <= result["value"] <= 1

    def test_cross_sectional_ic_is_the_mean_of_per_date_correlations(self) -> None:
        # Scores/labels rank-identical within every date, so each per-date Pearson
        # correlation is 1.0 and the cross-sectional IC is exactly 1.0.
        dates = pd.bdate_range("2026-01-05", periods=4)
        rows = []
        for d in dates:
            for i in range(8):
                rows.append({
                    "instrument": f"INST{i}", "decision_date": d,
                    "score": float(i), "label": float(i) * 0.5,
                })
        df = pd.DataFrame(rows)
        report = MetricReport.compute(
            df[["instrument", "decision_date", "score"]],
            df[["instrument", "decision_date", "label"]],
            metric_definitions=[{"name": "ic", "direction": "maximize"}],
        )
        assert report["results"][0]["value"] == pytest.approx(1.0)
        assert report["results"][0]["cross_section_count"] == 4

    def test_pooled_ic_is_opt_in_and_differs_from_cross_sectional(self) -> None:
        # A common daily shift makes the pooled correlation differ from the
        # cross-sectional mean: pooled folds the day effect in, cross-sectional
        # removes it.  ``ic_pooled`` is the explicit opt-in for the old behaviour.
        rows = []
        day_shift = {0: 0.0, 1: 10.0, 2: -10.0}
        for day_index, d in enumerate(pd.bdate_range("2026-01-05", periods=3)):
            for i in range(6):
                rows.append({
                    "instrument": f"INST{i}", "decision_date": d,
                    "score": float(i) * 0.1,
                    "label": float(i) * 0.1 + day_shift[day_index],
                })
        df = pd.DataFrame(rows)
        preds = df[["instrument", "decision_date", "score"]]
        actuals = df[["instrument", "decision_date", "label"]]
        report = MetricReport.compute(
            preds, actuals,
            metric_definitions=[
                {"name": "ic", "direction": "maximize"},
                {"name": "ic_pooled", "direction": "maximize"},
            ],
        )
        cross, pooled = report["results"]
        assert cross["basis"] == "cross_sectional"
        assert pooled["basis"] == "pooled"
        assert pooled["cross_section_count"] == 0
        assert pooled["sample_count"] == 18  # 3 dates * 6 instruments, pooled
        assert cross["sample_count"] == 18
        assert cross["value"] == pytest.approx(1.0)
        assert pooled["value"] < cross["value"]

    def test_rank_ic_variants_are_supported(self) -> None:
        preds, actuals = self._frame()
        report = MetricReport.compute(
            preds, actuals,
            metric_definitions=[
                {"name": "rank_ic", "direction": "maximize"},
                {"name": "rank_ic_pooled", "direction": "maximize"},
            ],
        )
        assert report["results"][0]["basis"] == "cross_sectional"
        assert report["results"][1]["basis"] == "pooled"

    def test_rank_ic_is_rank_correlation_not_pearson(self) -> None:
        # CR22-8: the shared fixture is linear, so its ranks equal its values and
        # rank_ic == ic -- the ``_correlate`` rank branch could be deleted and the
        # suite stayed green.  A strictly monotonic but non-linear score makes
        # Pearson and Spearman diverge, so the rank conversion is load-bearing.
        rows = []
        for i in range(8):
            rows.append({
                "instrument": f"INST{i}",
                "decision_date": pd.Timestamp("2026-01-05"),
                "score": float(i) ** 2,  # strictly increasing in i, non-linear
                "label": float(i),
            })
        df = pd.DataFrame(rows)
        report = MetricReport.compute(
            df[["instrument", "decision_date", "score"]],
            df[["instrument", "decision_date", "label"]],
            metric_definitions=[
                {"name": "ic", "direction": "maximize"},
                {"name": "rank_ic", "direction": "maximize"},
            ],
        )
        pearson, spearman = (result["value"] for result in report["results"])
        # Scores are a strictly increasing function of the labels, so the rank
        # correlation is exactly 1.0 while the Pearson correlation is not.
        assert spearman == pytest.approx(1.0)
        assert pearson == pytest.approx(0.961524, abs=1e-5)
        assert spearman != pytest.approx(pearson, abs=1e-6)

    def test_unsupported_metric_fails_closed(self) -> None:
        preds, actuals = self._frame()
        with pytest.raises(ContractError, match="unsupported metric"):
            MetricReport.compute(
                preds, actuals, metric_definitions=[{"name": "sharpe", "direction": "maximize"}]
            )

    def test_degenerate_cross_section_is_skipped_not_averaged(self) -> None:
        # A date whose scores are constant has an undefined correlation; it must be
        # skipped, not allowed to turn the mean into NaN.
        rows = []
        for day_index, d in enumerate(pd.bdate_range("2026-01-05", periods=3)):
            for i in range(4):
                rows.append({
                    "instrument": f"INST{i}", "decision_date": d,
                    "score": float(i) if day_index != 1 else 0.0,
                    "label": float(i) * 0.5,
                })
        df = pd.DataFrame(rows)
        report = MetricReport.compute(
            df[["instrument", "decision_date", "score"]],
            df[["instrument", "decision_date", "label"]],
            metric_definitions=[{"name": "ic", "direction": "maximize"}],
        )
        result = report["results"][0]
        assert result["cross_section_count"] == 2
        assert result["value"] == pytest.approx(1.0)

    def test_no_usable_cross_section_fails_closed(self) -> None:
        df = pd.DataFrame({
            "instrument": ["A", "B"],
            "decision_date": pd.to_datetime(["2026-01-05", "2026-01-05"]),
            "score": [1.0, 1.0],  # constant -> undefined correlation
            "label": [0.1, 0.2],
        })
        with pytest.raises(ContractError, match="no usable cross-section"):
            MetricReport.compute(
                df[["instrument", "decision_date", "score"]],
                df[["instrument", "decision_date", "label"]],
                metric_definitions=[{"name": "ic", "direction": "maximize"}],
            )
