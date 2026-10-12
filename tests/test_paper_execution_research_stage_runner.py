"""Phase 2/3 integration: the paper stage driven through its owning stores.

These tests exercise the production adapter path (not the test-only manifest
assembly in ``test_paper_execution_phase2/3``): governed market data is
published to a real ``MarketDatasetPublisher`` store, the adapter publishes the
config/plan/result/state chain for two decision dates, and the stage state is
published through ``FileResearchRunStore``.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.model_layer import create_reviewed_quality_decision, research_contract_identities
from uq.errors import ContractError
from uq.execution.market_dataset_publisher import MarketDatasetPublisher
from uq.execution.market_source import PublishedPaperMarketData
from uq.execution.stage_adapter import PaperExecutionStageAdapter
from uq.execution.stores import (
    ExecutionConfigStore,
    ExecutionResultStore,
    OrderPlanStore,
    PaperPortfolioStateStore,
)
from uq.research_chain.resolver import FileResearchRunStore, ResolvedExecutionPlan
from tests.review_key import REVIEWER_PRIVATE_KEY

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "evidence/paper-execution-research-integration/phase-0r/fixtures/paper_execution_template_v2-valid.json"

_CHECKS = {
    "execution_config_v1": [
        "schema_valid", "market_data_binding_resolved", "state_mode_valid",
        "execution_policy_within_bounds",
    ],
    "order_plan_v1": [
        "target_weights_binding_resolved", "row_count_and_keys_valid",
        "cash_feasible_planning", "t1_quantity_valid",
    ],
    "execution_result_v1": [
        "payload_readback_valid", "fee_reconciliation_valid",
        "lineage_generations_resolved", "reconciliation_complete",
    ],
    "paper_portfolio_state_v1": [
        "holdings_readback_valid", "cash_non_negative",
        "state_lineage_resolved", "state_reconciliation_complete",
    ],
}
_FAMILY_BY_KEY = {
    "execution_config": "execution_config_v1",
    "order_plan": "order_plan_v1",
    "execution_result": "execution_result_v1",
    "paper_portfolio_state": "paper_portfolio_state_v1",
}


def _quality_source() -> dict:
    def make(family: str):
        def resolve(subject_generation_id: str) -> dict:
            return create_reviewed_quality_decision(
                binding_type=family,
                policy="reject_all",
                status="passed",
                checks=[
                    {"name": name, "threshold": True, "observed": True, "level": "error", "result": "passed"}
                    for name in _CHECKS[family]
                ],
                errors=[],
                warnings=[],
                producer_code_fingerprint="a" * 64,
                private_key_pem=REVIEWER_PRIVATE_KEY,
                subject_generation_id=subject_generation_id,
            )
        return resolve
    return {key: make(family) for key, family in _FAMILY_BY_KEY.items()}


def _publish_market(root: Path, sessions: list[date]) -> MarketDatasetPublisher:
    publisher = MarketDatasetPublisher(root)
    publisher.publish_calendar_snapshot(
        partition_date=sessions[-1],
        window_start=sessions[0],
        window_end=sessions[-1],
        sessions=sessions,
        primary_source="test:trade_cal",
        independent_source="not_checked",
    )
    for session in sessions:
        bars = pd.DataFrame({
            "instrument": ["000001.XSHE", "600000.XSHG"],
            "session_date": [session, session],
            "open": [10.2, 20.4],
            "high": [10.4, 20.8],
            "low": [10.1, 20.2],
            "close": [10.0, 20.0],
            "volume": [1_000_000.0, 2_000_000.0],
            "amount": [1.02e7, 4.08e7],
            "status": ["trading", "trading"],
            "limit_up": [11.0, 22.0],
            "limit_down": [9.0, 18.0],
            "adj_factor": [1.0, 1.0],
        })
        data_path = session.isoformat() + ".parquet"
        target = root / "staging" / data_path
        target.parent.mkdir(parents=True, exist_ok=True)
        bars.to_parquet(target, index=False)
        import hashlib
        publisher.publish_bars_partition(
            partition_date=session,
            window_start=sessions[0],
            window_end=sessions[-1],
            source_data_path=target,
            source_generation_id=hashlib.sha256(session.isoformat().encode()).hexdigest(),
            source_data_checksum_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            columns=list(bars.columns),
            row_count=len(bars),
        )
        publisher.publish_suspension_snapshot(
            partition_date=session,
            window_start=sessions[0],
            window_end=sessions[-1],
            frame=pd.DataFrame({"instrument": [], "session_date": [], "status": []}),
        )
        publisher.publish_corporate_action_snapshot(
            partition_date=session,
            window_start=sessions[0],
            window_end=sessions[-1],
            frame=pd.DataFrame({"date": [], "instrument": []}),
            source_generation_ids=[],
            event_count=0,
        )
    return publisher


def _template(publisher: MarketDatasetPublisher, sessions: list[date]) -> dict:
    template = json.loads(TEMPLATE.read_text())
    template["initial_state"] = {
        "cash": 1_000_000.0, "holdings": [], "valuation_price_basis": "decision_close_estimate",
    }
    template["market_data_binding"] = publisher.build_set_binding(
        "bars_daily_v2", window_start=sessions[0], window_end=sessions[-1]
    )
    template["calendar_binding"] = publisher.build_set_binding(
        "trading_calendar_snapshot_v1", window_start=sessions[0], window_end=sessions[-1]
    )
    template["suspension_binding"] = publisher.build_set_binding(
        "suspension_snapshot_v1", window_start=sessions[0], window_end=sessions[-1]
    )
    template["corporate_action_binding"] = publisher.build_set_binding(
        "dividend_event_v1", window_start=sessions[0], window_end=sessions[-1]
    )
    return template


def _target_manifest(target: pd.DataFrame, decision_date: str) -> dict:
    from uq.contracts.model_layer import model_manifest_identities
    manifest = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "decision_date": decision_date,
        "portfolio_definition_generation_id": "1" * 64,
        "prediction_set_generation_id": "2" * 64,
        "universe_snapshot_generation_id": "3" * 64,
        "instrument_count": len(target),
        "total_stock_weight": float(target["weight"].sum()),
        "cash_reserve": 0.0,
        "previous_target_weights_generation_id": None,
        "weights_file": "data.parquet",
        "weights_checksum_sha256": "4" * 64,
        "columns": ["instrument", "weight"],
        "dtypes": {"instrument": "string", "weight": "float64"},
        "row_count": len(target),
        "key_uniqueness": "instrument",
        "logical_fingerprint": "5" * 64,
        "serialization_profile_id": "parquet-v1",
        "run_id": "0" * 8,
        "created_at": "2026-01-01T00:00:00+00:00",
        "quality_report_checksum_sha256": "6" * 64,
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    manifest["generation_id"], manifest["manifest_digest_sha256"] = model_manifest_identities(
        manifest, schema_name="target_weights"
    )
    return manifest


def _risk_source():
    """A governed-looking order-submission decision bound to the config generation."""
    from uq.risk.contracts import risk_contract_identities

    def source(decision_date: str, config_generation_id: str, context) -> dict:
        decision = json.loads(
            (ROOT / "config/schemas/fixtures/paper-execution/risk_decision-valid.json").read_text()
        )
        decision["decision_scope"] = "order_submission"
        decision["action"] = "allow"
        decision["as_of_date"] = decision_date
        decision["visible_through"] = f"{context['execution_date']}T15:00:00+08:00"
        decision["binding_config_generation_id"] = config_generation_id
        decision["generation_id"], decision["manifest_digest_sha256"] = risk_contract_identities(
            decision, schema_name="risk_decision"
        )
        return decision

    return source


def _plan() -> ResolvedExecutionPlan:
    request = {
        "contract_version": 5,
        "request_content_generation_id": "a" * 64,
        "run_id": "11111111-1111-1111-1111-111111111111",
    }
    return ResolvedExecutionPlan(
        request=request,
        request_manifest_digest_sha256="b" * 64,
        stage_plan_sha256="c" * 64,
        stage_bindings=(),
        resolved_execution_plan_sha256="d" * 64,
    )


def test_paper_stage_publishes_ordered_4n_bindings_across_two_dates(tmp_path: Path) -> None:
    sessions = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    publisher = _publish_market(tmp_path / "market", sessions)
    template = _template(publisher, sessions)
    paper_root = tmp_path / "paper"
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    run_store = FileResearchRunStore(tmp_path / "runs")

    from types import SimpleNamespace

    adapter = PaperExecutionStageAdapter(
        template,
        market=PublishedPaperMarketData(
            publisher, window_start=sessions[0], window_end=sessions[-1]
        ),
        config_store=ExecutionConfigStore(paper_root),
        plan_store=OrderPlanStore(paper_root),
        result_store=ExecutionResultStore(paper_root),
        state_store=PaperPortfolioStateStore(paper_root),
    )

    decision_dates = [sessions[0].isoformat(), sessions[1].isoformat()]
    frames = {
        decision_date: pd.DataFrame({"instrument": ["000001.XSHE"], "weight": [0.5]})
        for decision_date in decision_dates
    }
    manifests = {
        decision_date: _target_manifest(frame, decision_date)
        for decision_date, frame in frames.items()
    }
    portfolio_result = SimpleNamespace(
        definition_manifest={"generation_id": "1" * 64},
        target_weight_manifests=manifests,
        target_weight_frames=frames,
    )

    result = adapter.run(
        _plan(),
        portfolio_stage_result=portfolio_result,
        decision_dates=decision_dates,
        market=adapter.market,
        quality_decisions=_quality_source(),
        run_store=run_store,
        risk_decision_source=_risk_source(),
        runner_identity={"code_fingerprint": "e" * 64, "environment_profile": "locked-test", "lock_digest_sha256": "f" * 64},
        created_at="2026-01-01T00:00:00+00:00",
    )

    assert result.decision_dates == decision_dates
    assert len(result.bindings) == 8
    assert [b["output_family"] for b in result.bindings[:4]] == [
        "execution_config", "order_plan", "execution_result", "paper_portfolio_state",
    ]
    # Continuation state must bind the first date's published state.
    second_config = adapter.config_store.read(result.bindings[4]["generation_id"])
    assert second_config["state_mode"] == "continuation"
    assert second_config["input_state_binding"]["generation_id"] == result.bindings[3]["generation_id"]

    # The published stage state carries the ordered bindings and the run store accepts it.
    state = run_store.read_state(
        "a" * 64, "11111111-1111-1111-1111-111111111111", "paper_execution",
        result.published_state.manifest_digest_sha256,
    )
    assert state["stage_records"][-1]["stage"] == "paper_execution"
    assert len(state["stage_records"][-1]["output_bindings"]) == 8


def test_paper_stage_is_idempotent_and_rejects_republish(tmp_path: Path) -> None:
    sessions = [date(2026, 1, 5), date(2026, 1, 6)]
    publisher = _publish_market(tmp_path / "market", sessions)
    template = _template(publisher, sessions)
    paper_root = tmp_path / "paper"
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    run_store = FileResearchRunStore(tmp_path / "runs")
    from types import SimpleNamespace

    adapter = PaperExecutionStageAdapter(
        template,
        market=PublishedPaperMarketData(publisher, window_start=sessions[0], window_end=sessions[-1]),
        config_store=ExecutionConfigStore(paper_root),
        plan_store=OrderPlanStore(paper_root),
        result_store=ExecutionResultStore(paper_root),
        state_store=PaperPortfolioStateStore(paper_root),
    )
    decision_date = sessions[0].isoformat()
    frame = pd.DataFrame({"instrument": ["000001.XSHE"], "weight": [0.5]})
    portfolio_result = SimpleNamespace(
        definition_manifest={"generation_id": "1" * 64},
        target_weight_manifests={decision_date: _target_manifest(frame, decision_date)},
        target_weight_frames={decision_date: frame},
    )
    kwargs = dict(
        portfolio_stage_result=portfolio_result,
        decision_dates=[decision_date],
        market=adapter.market,
        quality_decisions=_quality_source(),
        run_store=run_store,
        risk_decision_source=_risk_source(),
        runner_identity={"code_fingerprint": "e" * 64, "environment_profile": "locked-test", "lock_digest_sha256": "f" * 64},
        created_at="2026-01-01T00:00:00+00:00",
    )
    adapter.run(_plan(), **kwargs)
    with pytest.raises(ContractError):
        adapter.run(_plan(), **kwargs)
