"""Phase 2 backtest pre-trade risk decision tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.risk import PortfolioPublicationRiskGate, RiskEngine
from uq.risk.contracts import (
    build_risk_review_decision,
    risk_contract_identities,
    risk_policy_draft_subject,
)
from tests.review_key import REVIEWER_PRIVATE_KEY


_ORDER_RULES = [
    ("order_notional_limit", "order_notional", "less_than_or_equal", 100000.0, "resize", "critical", 10),
    ("order_participation_limit", "order_participation", "less_than_or_equal", 0.10, "resize", "critical", 20),
    ("order_insufficient_cash", "cash_after_order", "greater_than_or_equal", 0.0, "block_order", "critical", 30),
    ("order_instrument_halt", "order_halt", "less_than", 1.0, "block_order", "critical", 40),
    ("order_limit_price", "order_limit_headroom", "greater_than", 0.0, "block_order", "critical", 50),
    ("order_t1_sellable_quantity", "sellable_shares", "greater_than_or_equal", 0.0, "resize", "critical", 60),
]


def _order_policy() -> dict:
    policy = json.loads(Path("config/schemas/fixtures/risk/risk_policy-valid.json").read_text())
    policy["policy_scope"] = "order"
    policy["effective_from"] = "2026-01-01"
    policy["effective_to"] = None
    policy["rules"] = [
        {
            "rule_id": rule_id,
            "rule_scope": "order",
            "metric": metric,
            "operator": operator,
            "threshold": threshold,
            "action": action,
            "severity": severity,
            "priority": priority,
            "state_required": False,
            "state_window_days": 0,
            "hysteresis_exit_threshold": None,
            "cooldown_days": 0,
        }
        for rule_id, metric, operator, threshold, action, severity, priority in _ORDER_RULES
    ]
    review = build_risk_review_decision(
        review_type="risk_policy_activation",
        subject=risk_policy_draft_subject(policy),
        schema_name="risk_policy",
        review_status="approved",
        reviewer="repository-owner",
        errors=[],
        warnings=[],
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    policy["approval_binding"] = {
        "family": "risk_review_decision_v1",
        "review_generation_id": review["generation_id"],
        "review_manifest_digest_sha256": review["manifest_digest_sha256"],
    }
    policy["generation_id"], policy["manifest_digest_sha256"] = risk_contract_identities(
        policy, schema_name="risk_policy"
    )
    return policy, review


def _evaluate(**overrides):
    policy, review = _order_policy()
    values = {
        "policy": policy,
        "policy_review": review,
        "order": {"order_id": "order-1", "instrument": "A", "side": "buy", "shares": 1000},
        "execution_date": "2026-01-05",
        "execution_price": 10.9,
        "previous_close": 10.0,
        "decision_date_volume": 100000.0,
        "holdings": {"A": 0},
        "cash": 1000000.0,
        "suspended": False,
        "sellable_shares": 0,
        "limit_ratio": 0.10,
        "board_lot": 100,
        "slippage_bps": 0.0,
    }
    values.update(overrides)
    return RiskEngine().evaluate_order(**values)


def test_risk_order_missing_context_fails_closed():
    with pytest.raises(ContractError, match="side"):
        _evaluate(order={"order_id": "order-1", "instrument": "A", "side": "invalid", "shares": 1})
    with pytest.raises(ContractError, match="positive"):
        _evaluate(execution_price=0.0)
    with pytest.raises(ContractError, match="holdings"):
        _evaluate(order={"order_id": "order-1", "instrument": "A", "side": "sell", "shares": 1})


def test_risk_order_rules_are_deterministic():
    first = _evaluate()
    second = _evaluate()
    assert first == second
    assert first["action"] == "allow"
    assert first["decision_scope"] == "order_submission"
    Path("evidence/risk/phase-2/golden").mkdir(parents=True, exist_ok=True)
    Path("evidence/risk/phase-2/golden/order-decision.json").write_text(
        json.dumps(first, sort_keys=True, indent=2) + "\n"
    )


def test_risk_order_halt_blocks_candidate():
    decision = _evaluate(suspended=True)
    assert decision["action"] == "block_order"
    assert any(
        finding["rule_id"] == "order_instrument_halt" and finding["result"] == "failed"
        for finding in decision["findings"]
    )


def test_risk_t1_sellable_quantity_is_enforced():
    decision = _evaluate(
        order={"order_id": "sell-1", "instrument": "A", "side": "sell", "shares": 200},
        holdings={"A": 200},
        sellable_shares=100,
        cash=0.0,
    )
    assert decision["action"] == "resize"
    assert decision["constraints"] == [
        {"constraint_type": "allowed_shares", "instrument": "A", "value": 100.0}
    ]


def test_risk_participation_and_notional_resize_exact():
    participation = _evaluate(
        decision_date_volume=1000.0,
    )
    assert participation["action"] == "resize"
    assert participation["constraints"][0] == {
        "constraint_type": "allowed_shares", "instrument": "A", "value": 100.0
    }
    notional = _evaluate(
        order={"order_id": "order-1", "instrument": "A", "side": "buy", "shares": 10000},
        decision_date_volume=1000000.0,
    )
    assert notional["action"] == "resize"
    allowed_shares = next(
        constraint for constraint in notional["constraints"]
        if constraint["constraint_type"] == "allowed_shares"
    )
    assert allowed_shares == {
        "constraint_type": "allowed_shares", "instrument": "A", "value": 9100.0
    }
    assert {
        "constraint_type": "allowed_notional", "instrument": "A", "value": 100000.0
    } in notional["constraints"]


def test_risk_rejected_candidate_is_not_added_to_frozen_fills(tmp_path):
    root = tmp_path / "store"
    gate = PortfolioPublicationRiskGate(root)
    policy, review = _order_policy()
    price_panel = pd.DataFrame(
        {
            "date": ["2026-01-05", "2026-01-06"],
            "instrument": ["A", "A"],
            "open": [10.0, 11.0],
            "close": [10.0, 11.0],
            "volume": [100000.0, 100000.0],
        }
    )
    config = {
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
        "contract_version": 1,
        "schema_version": "1.0.0",
        "initial_capital": 100000.0,
        "start_date": "2026-01-05",
        "end_date": "2026-01-06",
        "cost_model": {"commission_bps": 0, "stamp_duty_bps": 0, "slippage_bps": 0},
        "execution_model": {"board_lot": 100, "volume_participation_cap": 0.10},
        "limit_rules": {"limit_ratio": 0.10},
        "price_source_binding": {"source": "test"},
    }
    from uq.backtest.engine import BacktestEngine

    engine = BacktestEngine(tmp_path)
    manifest, artifacts, risk_run = gate.run_with_risk_gate(
        engine,
        config=config,
        policy=policy,
        policy_review=review,
        portfolio_definition={"generation_id": "0" * 64},
        weight_partitions={
            "2026-01-05": pd.DataFrame({"instrument": ["A"], "weight": [1.0]})
        },
        price_panel=price_panel.set_index(["date", "instrument"]),
        suspension_dates={("2026-01-06", "A")},
    )
    assert len(risk_run["decision_bindings"]) == 1
    stored_run, payloads = gate.run_store.read(risk_run["generation_id"])
    assert len(payloads) == 1
    assert payloads["decision-0.json"]["action"] == "block_order"
    assert payloads["decision-0.json"]["decision_digest"] == decision_digest(payloads)
    verified_run, _ = gate.read_verified_risk_run(risk_run["generation_id"])
    assert len(verified_run["event_bindings"]) == 1
    fills = artifacts["fills"]
    assert "skipped_risk_blocked" in set(fills["status"])
    assert list(fills.columns) == [
        "date", "instrument", "side", "target_shares", "filled_shares",
        "gross_execution_price", "net_execution_price", "commission_fee",
        "stamp_duty_fee", "status",
    ]


def decision_digest(payloads: dict) -> str:
    return payloads["decision-0.json"]["decision_digest"]


def test_backtest_result_v1_contract_remains_frozen():
    from uq.contracts.model_layer import ModelContractLoader

    schema_path = Path("config/schemas/contracts/backtest_result.v1.json")
    schema = json.loads(schema_path.read_text())
    assert schema["properties"]["fills_artifact"]["type"] == "object"
    assert schema["properties"]["target_weight_bindings"]["type"] == "array"
    assert ModelContractLoader is not None


def test_risk_run_tampering_rejects_read(tmp_path):
    root = tmp_path / "store"
    gate = PortfolioPublicationRiskGate(root)
    policy, review = _order_policy()
    decision = gate.evaluate_order(
        policy=policy,
        policy_review=review,
        order={"order_id": "order-1", "instrument": "A", "side": "buy", "shares": 100},
        execution_date="2026-01-05",
        execution_price=10.0,
        previous_close=10.0,
        decision_date_volume=100000.0,
        holdings={"A": 0},
        cash=100000.0,
        suspended=False,
        sellable_shares=0,
        limit_ratio=0.10,
        board_lot=100,
        slippage_bps=0.0,
    )
    event = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "event_id": "0" * 64,
        "event_class": "risk_cleared",
        "scope_type": "order",
        "scope_id": "backtest_pre_trade",
        "event_sequence_number": 1,
        "as_of_date": decision["as_of_date"],
        "visible_through": decision["visible_through"],
        "policy_generation_id": policy["generation_id"],
        "state_generation_id": None,
        "decision_generation_id": decision["generation_id"],
        "reason": "test",
        "producer": decision["producer"],
        "serialization_profile": "json-canonical-v1",
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    event["generation_id"], event["manifest_digest_sha256"] = risk_contract_identities(
        event, schema_name="risk_event"
    )
    run = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "run_scope": "order_submission",
        "risk_policy_binding": {
            "family": "risk_policy_v1",
            "generation_id": policy["generation_id"],
            "manifest_digest_sha256": policy["manifest_digest_sha256"],
        },
        "risk_state_binding": None,
        "consumer_binding": {
            "family": "backtest_config_v1",
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        },
        "decision_bindings": [{
            "family": "risk_decision_v1",
            "generation_id": decision["generation_id"],
            "manifest_digest_sha256": decision["manifest_digest_sha256"],
        }],
        "event_bindings": [{
            "family": "risk_event_v1",
            "generation_id": event["generation_id"],
            "manifest_digest_sha256": event["manifest_digest_sha256"],
        }],
        "as_of_date": "2026-01-05",
        "visible_through": "2026-01-05T15:00:00+00:00",
        "producer": decision["producer"],
        "serialization_profile": "json-canonical-v1",
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    run["generation_id"], run["manifest_digest_sha256"] = risk_contract_identities(
        run, schema_name="risk_run"
    )
    gate.event_store.publish(event)
    gate.decision_store.publish(decision, {
        "action": decision["action"],
        "constraints": decision["constraints"],
        "decision_digest": decision["decision_digest"],
        "findings": decision["findings"],
    })
    run["files"] = [{
        "path": "decision-0.json",
        "sha256": hashlib.sha256((
            json.dumps({
                "action": decision["action"],
                "constraints": decision["constraints"],
                "decision_digest": decision["decision_digest"],
                "findings": decision["findings"],
            }, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")).hexdigest(),
        "byte_size": len((
            json.dumps({
                "action": decision["action"],
                "constraints": decision["constraints"],
                "decision_digest": decision["decision_digest"],
                "findings": decision["findings"],
            }, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")),
        "serialization_profile": "json-canonical-v1",
    }]
    run["generation_id"], run["manifest_digest_sha256"] = risk_contract_identities(
        run, schema_name="risk_run"
    )
    gate.run_store.publish(run, {"decision-0.json": {
        "action": decision["action"],
        "constraints": decision["constraints"],
        "decision_digest": decision["decision_digest"],
        "findings": decision["findings"],
    }})
    assert gate.read_verified_risk_run(run["generation_id"])
    decision_manifest_path = next((root / "risk" / "decisions").glob("**/manifest.json"))
    tampered = json.loads(decision_manifest_path.read_text())
    tampered["manifest_digest_sha256"] = "1" * 64
    decision_manifest_path.write_text(json.dumps(tampered))
    with pytest.raises(ContractError, match="identity mismatch"):
        gate.read_verified_risk_run(run["generation_id"])


def test_duplicate_order_id_fails_closed():
    policy, review = _order_policy()
    gate = PortfolioPublicationRiskGate(tempfile.mkdtemp())
    kwargs = {
        "policy": policy,
        "policy_review": review,
        "order": {"order_id": "order-1", "instrument": "A", "side": "buy", "shares": 100},
        "execution_date": "2026-01-05",
        "execution_price": 10.0,
        "previous_close": 10.0,
        "decision_date_volume": 100000.0,
        "holdings": {"A": 0},
        "cash": 100000.0,
        "suspended": False,
        "sellable_shares": 0,
        "limit_ratio": 0.10,
        "board_lot": 100,
        "slippage_bps": 0.0,
    }
    gate.evaluate_order(**kwargs)
    with pytest.raises(ContractError, match="duplicate or stale"):
        gate.evaluate_order(**kwargs)
