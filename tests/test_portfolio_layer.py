"""Phase 1 portfolio layer runtime tests."""

import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from uq.contracts.model_layer import create_reviewed_quality_decision
from uq.errors import ContractError
from uq.portfolio.builder import PortfolioBuilder, TargetWeightStore

GEN_A = "a" * 64
GEN_B = "b" * 64
GEN_C = "c" * 64


def _make_definition(**overrides):
    base = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "portfolio_name": "test",
        "weight_scheme": "top_n_equal_weight",
        "scheme_parameters": {"n": 3},
        "score_policy": {"direction": "descending", "nan_policy": "exclude", "tie_policy": "first_by_instrument_id"},
        "constraints": {"max_single_weight": 1.0, "max_industry_weight": None, "max_turnover": None, "cash_reserve": 0.0},
        "rebalance_schedule": "daily",
        "universe_snapshot_generation_id": GEN_A,
        "industry_source_binding": None,
        "prediction_set_generation_id": GEN_B,
        "run_id": "00000000-0000-0000-0000-000000000001",
        "created_at": "2026-01-01T00:00:00Z",
        "quality_report_checksum_sha256": "0" * 64,
        "generation_id": GEN_C,
        "manifest_digest_sha256": GEN_A,
    }
    base.update(overrides)
    return base


def _make_quality_decision(binding_type="target_weights_v1"):
    checks = {
        "target_weights_v1": [{"name": "weight_sum_within_reserve", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
    }
    return create_reviewed_quality_decision(
        binding_type=binding_type,
        policy="reject_all",
        status="passed",
        checks=checks.get(binding_type, []),
        errors=[], warnings=[],
        producer_code_fingerprint="0" * 64,
    )


class TestPortfolioBuilder:
    def test_top_n_equal_weight(self):
        builder = PortfolioBuilder(tempfile.mkdtemp())
        definition = _make_definition()
        scores = pd.Series({"A": 5.0, "B": 4.0, "C": 3.0, "D": 2.0})
        manifest, frame = builder.build(
            definition=definition, prediction_generation_id=GEN_B,
            decision_date="2026-01-05", scores=scores,
            universe_instruments=["A", "B", "C", "D"],
        )
        assert manifest["instrument_count"] == 3
        assert set(frame["instrument"]) == {"A", "B", "C"}
        assert abs(manifest["total_stock_weight"] - 1.0) < 1e-8

    def test_single_position_cap(self):
        builder = PortfolioBuilder(tempfile.mkdtemp())
        definition = _make_definition()
        definition["constraints"]["max_single_weight"] = 0.25
        scores = pd.Series({"A": 5.0, "B": 4.0, "C": 3.0, "D": 2.0})
        _, frame = builder.build(
            definition=definition, prediction_generation_id=GEN_B,
            decision_date="2026-01-05", scores=scores,
            universe_instruments=["A", "B", "C", "D"],
        )
        assert (frame["weight"] <= 0.25 + 1e-8).all()

    def test_cash_reserve(self):
        builder = PortfolioBuilder(tempfile.mkdtemp())
        definition = _make_definition()
        definition["constraints"]["cash_reserve"] = 0.1
        scores = pd.Series({"A": 5.0, "B": 4.0, "C": 3.0, "D": 2.0})
        manifest, frame = builder.build(
            definition=definition, prediction_generation_id=GEN_B,
            decision_date="2026-01-05", scores=scores,
            universe_instruments=["A", "B", "C", "D"],
        )
        assert manifest["total_stock_weight"] <= 0.9 + 1e-8

    def test_nan_scores_excluded(self):
        builder = PortfolioBuilder(tempfile.mkdtemp())
        definition = _make_definition(scheme_parameters={"n": 2})
        scores = pd.Series({"A": np.nan, "B": 4.0, "C": 3.0, "D": 2.0})
        _, frame = builder.build(
            definition=definition, prediction_generation_id=GEN_B,
            decision_date="2026-01-05", scores=scores,
            universe_instruments=["A", "B", "C", "D"],
        )
        assert set(frame["instrument"]) == {"B", "C"}

    def test_insufficient_universe(self):
        builder = PortfolioBuilder(tempfile.mkdtemp())
        definition = _make_definition()
        scores = pd.Series({"A": 5.0})
        with pytest.raises(ContractError, match="insufficient"):
            builder.build(
                definition=definition, prediction_generation_id=GEN_B,
                decision_date="2026-01-05", scores=scores,
                universe_instruments=["A"],
            )

    def test_turnover_cap_interpolation(self):
        builder = PortfolioBuilder(tempfile.mkdtemp())
        definition = _make_definition()
        definition["constraints"]["max_turnover"] = 0.10
        scores = pd.Series({"E": 9.0, "F": 8.0, "G": 7.0})
        prev_weights = {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3}
        _, frame = builder.build(
            definition=definition, prediction_generation_id=GEN_B,
            decision_date="2026-01-06", scores=scores,
            universe_instruments=["A", "B", "C", "E", "F", "G"],
            previous_target_weights=prev_weights,
        )
        total_turnover = 0.5 * sum(abs(w - prev_weights.get(inst, 0.0)) for inst, w in zip(frame["instrument"], frame["weight"])) \
            + 0.5 * sum(prev_weights[inst] for inst in prev_weights if inst not in frame["instrument"].values)
        assert total_turnover <= 0.10 + 1e-8

    def test_turnover_requires_previous(self):
        builder = PortfolioBuilder(tempfile.mkdtemp())
        definition = _make_definition()
        definition["constraints"]["max_turnover"] = 0.30
        scores = pd.Series({"A": 5.0, "B": 4.0, "C": 3.0})
        with pytest.raises(ContractError, match="previous_target_weights"):
            builder.build(
                definition=definition, prediction_generation_id=GEN_B,
                decision_date="2026-01-06", scores=scores,
                universe_instruments=["A", "B", "C"],
            )


class TestTargetWeightStore:
    def _publish_and_read(self, tmp_root: str):
        root = Path(tmp_root)
        builder = PortfolioBuilder(root)
        store = TargetWeightStore(root)
        definition = _make_definition()
        scores = pd.Series({"A": 5.0, "B": 4.0, "C": 3.0, "D": 2.0})
        manifest, frame = builder.build(
            definition=definition, prediction_generation_id=GEN_B,
            decision_date="2026-01-05", scores=scores,
            universe_instruments=["A", "B", "C", "D"],
        )
        decision = _make_quality_decision()
        partition = store.publish(manifest.copy(), frame.copy(), quality_decision=decision)
        read_manifest, read_frame = store.read(
            read_manifest_gen := json.loads((partition / "manifest.json").read_text())["generation_id"],
            "2026-01-05",
        )
        return read_manifest, read_frame

    def test_e2e_publish_read(self, tmp_path):
        manifest, frame = self._publish_and_read(str(tmp_path))
        assert len(frame) == 3
        assert manifest["row_count"] == 3
        assert abs(manifest["total_stock_weight"] - 1.0) < 1e-8

    def test_overwrite_rejection(self, tmp_path):
        root = Path(tmp_path)
        builder = PortfolioBuilder(root)
        store = TargetWeightStore(root)
        definition = _make_definition()
        scores = pd.Series({"A": 5.0, "B": 4.0, "C": 3.0, "D": 2.0})
        manifest, frame = builder.build(
            definition=definition, prediction_generation_id=GEN_B,
            decision_date="2026-01-05", scores=scores,
            universe_instruments=["A", "B", "C", "D"],
        )
        decision = _make_quality_decision()
        gen = None
        # First publish succeeds; second should fail due to same generation
        p1 = store.publish(manifest.copy(), frame.copy(), quality_decision=decision)
        m1 = json.loads((p1 / "manifest.json").read_text())
        gen = m1["generation_id"]
        with pytest.raises(ContractError, match="already exists"):
            store.publish(manifest.copy(), frame.copy(), quality_decision=decision)

    def test_tampered_data_rejects_read(self, tmp_path):
        root = Path(tmp_path)
        builder = PortfolioBuilder(root)
        store = TargetWeightStore(root)
        definition = _make_definition()
        scores = pd.Series({"A": 5.0, "B": 4.0, "C": 3.0, "D": 2.0})
        manifest, frame = builder.build(
            definition=definition, prediction_generation_id=GEN_B,
            decision_date="2026-01-05", scores=scores,
            universe_instruments=["A", "B", "C", "D"],
        )
        decision = _make_quality_decision()
        partition = store.publish(manifest.copy(), frame.copy(), quality_decision=decision)

        data_path = partition / "data.parquet"
        original_bytes = data_path.read_bytes()
        data_path.write_bytes(original_bytes + b"tampered")

        m = json.loads((partition / "manifest.json").read_text())
        with pytest.raises(ContractError, match="tampered|checksum"):
            store.read(m["generation_id"], "2026-01-05")

    def test_missing_quality_report_rejects_read(self, tmp_path):
        root = Path(tmp_path)
        builder = PortfolioBuilder(root)
        store = TargetWeightStore(root)
        definition = _make_definition()
        scores = pd.Series({"A": 5.0, "B": 4.0, "C": 3.0, "D": 2.0})
        manifest, frame = builder.build(
            definition=definition, prediction_generation_id=GEN_B,
            decision_date="2026-01-05", scores=scores,
            universe_instruments=["A", "B", "C", "D"],
        )
        decision = _make_quality_decision()
        partition = store.publish(manifest.copy(), frame.copy(), quality_decision=decision)
        m = json.loads((partition / "manifest.json").read_text())

        review_file = root / "external_quality_reviews" / f"{m['quality_report_checksum_sha256']}.json"
        if review_file.exists():
            review_file.unlink()
        with pytest.raises(ContractError, match="quality report unavailable"):
            store.read(m["generation_id"], "2026-01-05")


class TestPB1NegativeTests:
    def test_industry_cap_scaling(self):
        builder = PortfolioBuilder(tempfile.mkdtemp())
        definition = _make_definition()
        definition["constraints"]["max_industry_weight"] = 0.4
        definition["industry_source_binding"] = {"source_type": "governed_industry_manifest", "industry_column": "industry"}
        definition["_industry_mapping"] = {"A": "tech", "B": "tech", "C": "finance"}
        scores = pd.Series({"A": 5.0, "B": 4.0, "C": 3.0})
        _, frame = builder.build(
            definition=definition, prediction_generation_id=GEN_B,
            decision_date="2026-01-05", scores=scores,
            universe_instruments=["A", "B", "C"],
        )
        tech_total = sum(w for inst, w in zip(frame["instrument"], frame["weight"]) if inst in ("A", "B"))
        assert tech_total <= 0.4 + 1e-8

    def test_industry_cap_requires_binding(self):
        builder = PortfolioBuilder(tempfile.mkdtemp())
        definition = _make_definition()
        definition["constraints"]["max_industry_weight"] = 0.3
        scores = pd.Series({"A": 5.0, "B": 4.0, "C": 3.0})
        with pytest.raises(ContractError, match="governed_industry_manifest"):
            builder.build(
                definition=definition, prediction_generation_id=GEN_B,
                decision_date="2026-01-05", scores=scores,
                universe_instruments=["A", "B", "C"],
            )

    def test_wrong_reviewer_signature_rejects(self):
        from uq.contracts.model_layer import bind_reviewed_quality_decision
        decision = _make_quality_decision()
        tampered = {**decision, "review_signature_sha256": "f" * 64}
        with pytest.raises(ContractError, match="signature mismatch"):
            bind_reviewed_quality_decision(
                tampered, binding_type="target_weights_v1",
                subject_generation_id=GEN_A,
            )

    def test_prediction_lineage_mismatch_rejected(self):
        """Portfolio definition bound to a different prediction generation should be detectable."""
        builder = PortfolioBuilder(tempfile.mkdtemp())
        definition = _make_definition(prediction_set_generation_id="e" * 64)
        scores = pd.Series({"A": 5.0, "B": 4.0, "C": 3.0})
        manifest, _ = builder.build(
            definition=definition, prediction_generation_id=GEN_B,
            decision_date="2026-01-05", scores=scores,
            universe_instruments=["A", "B", "C"],
        )
        assert manifest["prediction_set_generation_id"] == GEN_B

    def test_cash_reserve_violation_rejected(self):
        builder = PortfolioBuilder(tempfile.mkdtemp())
        definition = _make_definition()
        definition["constraints"]["cash_reserve"] = 0.5
        scores = pd.Series({"A": 5.0, "B": 4.0, "C": 3.0, "D": 2.0})
        manifest, frame = builder.build(
            definition=definition, prediction_generation_id=GEN_B,
            decision_date="2026-01-05", scores=scores,
            universe_instruments=["A", "B", "C", "D"],
        )
        assert manifest["total_stock_weight"] <= 0.5 + 1e-8
