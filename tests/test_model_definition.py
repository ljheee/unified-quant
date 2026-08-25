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
        builder = ModelDefinitionBuilder()
        manifest = builder.build(
            algorithm="regularized_linear",
            hyperparameters={"alpha": 1.0},
            seed_policy={"base_seed": 42, "derivation": "fixed"},
            **_common(),
        )
        assert len(manifest["generation_id"]) == 64
        assert manifest["status"] == "reviewed"

    def test_reject_unsupported_algorithm(self) -> None:
        builder = ModelDefinitionBuilder()
        with pytest.raises(ContractError, match="unsupported algorithm"):
            builder.build(algorithm="xgboost", hyperparameters={}, seed_policy={"base_seed": 0, "derivation": "fixed"}, **_common())

    def test_reject_empty_metrics(self) -> None:
        builder = ModelDefinitionBuilder()
        common = _common()
        common["metrics"] = []
        with pytest.raises(ContractError, match="at least one metric"):
            builder.build(algorithm="regularized_linear", hyperparameters={"alpha": 1.0}, seed_policy={"base_seed": 42, "derivation": "fixed"}, **common)

    def test_changed_hyperparams_new_generation(self) -> None:
        builder = ModelDefinitionBuilder()
        m1 = builder.build(algorithm="regularized_linear", hyperparameters={"alpha": 1.0}, seed_policy={"base_seed": 42, "derivation": "fixed"}, **_common())
        m2 = builder.build(algorithm="regularized_linear", hyperparameters={"alpha": 2.0}, seed_policy={"base_seed": 42, "derivation": "fixed"}, **_common())
        assert m1["generation_id"] != m2["generation_id"]


class TestMetricReport:
    def test_ic_computation(self) -> None:
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
        preds = df[["instrument", "decision_date", "score"]].rename(columns={"score": "score"})
        actuals = df[["instrument", "decision_date", "label"]].rename(columns={"label": "label"})
        report = MetricReport.compute(preds, actuals, metric_definitions=[{"name": "ic", "direction": "maximize"}])
        assert len(report["results"]) == 1
        assert -1 <= report["results"][0]["value"] <= 1
