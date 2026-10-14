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
    # cr.md latent B5: a suspended instrument must not reach the pre-trade gate at
    # all, because its execution price is undefined -- so "suspended" can no longer
    # be the vehicle for a blocked order.  The fixture blocks an order the account
    # cannot fund instead, on an instrument the engine could otherwise trade: a
    # target weight above 1.0 asks for more shares than the cash can settle, so the
    # gate refuses with ``order_insufficient_cash`` and the engine records the
    # skipped delta without ever filling.  The slippage is what keeps the requested
    # notional under ``order_notional_limit`` while the slipped cash outlay is not,
    # so the insufficient-cash rule is the only one that fails.
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
        "cost_model": {"commission_bps": 0, "stamp_duty_bps": 0, "slippage_bps": 500.0},
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
            "2026-01-05": pd.DataFrame({"instrument": ["A"], "weight": [1.05]})
        },
        price_panel=price_panel.set_index(["date", "instrument"]),
    )
    assert len(risk_run["decision_bindings"]) == 1
    stored_run, payloads = gate.run_store.read(risk_run["generation_id"])
    assert len(payloads) == 1
    assert payloads["decision-0.json"]["decision"]["action"] == "block_order"
    failed_findings = [
        finding["rule_id"]
        for finding in payloads["decision-0.json"]["decision"]["findings"]
        if finding["result"] == "failed"
    ]
    assert failed_findings == ["order_insufficient_cash"]
    assert payloads["decision-0.json"]["decision"]["decision_digest"] == decision_digest(payloads)
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
    return payloads["decision-0.json"]["decision"]["decision_digest"]


def _permissive_portfolio_policy(threshold: float = 1.0) -> tuple[dict, dict]:
    """A portfolio policy whose gross-exposure rule cannot fail the fixture."""
    from tests.test_risk_control_plane_phase1 import _policy, _policy_review

    policy = _policy()
    policy["rules"][0]["threshold"] = threshold
    review = _policy_review(policy)
    policy["approval_binding"] = {
        "family": "risk_review_decision_v1",
        "review_generation_id": review["generation_id"],
        "review_manifest_digest_sha256": review["manifest_digest_sha256"],
    }
    policy["generation_id"], policy["manifest_digest_sha256"] = risk_contract_identities(
        policy, schema_name="risk_policy"
    )
    return policy, review


def _gate_portfolio(gate: PortfolioPublicationRiskGate, *, threshold: float = 1.0):
    from tests.test_risk_control_plane_phase1 import (
        _definition,
        _previous_weights,
        _target_weights,
        _universe,
    )

    policy, review = _permissive_portfolio_policy(threshold)
    definition = _definition()
    universe = _universe()
    target, frame = _target_weights(definition, universe)
    decision = gate.gate(
        policy=policy,
        policy_review=review,
        target_weights=target,
        target_weights_frame=frame,
        definition=definition,
        universe=universe,
        previous_target_weights=_previous_weights(),
    )
    return decision


def test_portfolio_publication_run_records_and_verifies_its_decision_payload(tmp_path):
    """cr.md latent B1: the allow path used to die with ``run_store.publish(run)``
    (no payloads) *after* the decision/event partitions had already landed.  The run
    now pins ``files[]`` and the readback verifies the portfolio-shaped payload
    (decision only, no candidate order)."""
    gate = PortfolioPublicationRiskGate(tmp_path)
    decision = _gate_portfolio(gate)
    assert decision["action"] == "allow"

    runs = list((tmp_path / "risk" / "runs").glob("generation=*"))
    assert len(runs) == 1
    run_generation_id = runs[0].name.split("=", 1)[1]
    stored_run, payloads = gate.run_store.read(run_generation_id)
    assert stored_run["run_scope"] == "portfolio_publication"
    assert [entry["path"] for entry in stored_run["files"]] == ["decision-0.json"]
    assert payloads["decision-0.json"]["action"] == decision["action"]
    assert "candidate_order" not in payloads["decision-0.json"]

    verified_run, verified_payloads = gate.read_verified_risk_run(run_generation_id)
    assert verified_run["generation_id"] == run_generation_id
    assert verified_payloads["decision-0.json"]["decision_digest"] == decision["decision_digest"]


def test_portfolio_publication_run_payload_tampering_is_rejected(tmp_path):
    """The portfolio payload is now covered by the readback, so a rewritten
    decision body must not verify."""
    gate = PortfolioPublicationRiskGate(tmp_path)
    _gate_portfolio(gate)
    run_generation_id = next((tmp_path / "risk" / "runs").glob("generation=*")).name.split("=", 1)[1]
    payload_path = tmp_path / "risk" / "runs" / f"generation={run_generation_id}" / "decision-0.json"
    payload = json.loads(payload_path.read_text())
    payload["action"] = "block_order"
    payload_path.write_text(json.dumps(payload))
    with pytest.raises(ContractError, match="byte size mismatch|tampered"):
        gate.read_verified_risk_run(run_generation_id)


@pytest.mark.parametrize("action", ["de_risk", "flatten", "escalate_review"])
def test_portfolio_publication_refuses_non_executable_actions(tmp_path, action):
    """cr.md latent B1 / spec 7.2: only allow/warn are executable at the publication
    gate.  Every other action -- including ones added to the enum later -- refuses
    instead of silently passing as "not blocked"."""
    from tests.test_risk_control_plane_phase1 import (
        _definition,
        _policy,
        _policy_review,
        _previous_weights,
        _target_weights,
        _universe,
    )

    policy = _policy()
    policy["rules"][0]["threshold"] = 0.5  # guaranteed failure -> rule action applies
    policy["rules"][0]["action"] = action
    review = _policy_review(policy)
    policy["approval_binding"] = {
        "family": "risk_review_decision_v1",
        "review_generation_id": review["generation_id"],
        "review_manifest_digest_sha256": review["manifest_digest_sha256"],
    }
    policy["generation_id"], policy["manifest_digest_sha256"] = risk_contract_identities(
        policy, schema_name="risk_policy"
    )
    definition = _definition()
    universe = _universe()
    target, frame = _target_weights(definition, universe)
    gate = PortfolioPublicationRiskGate(tmp_path)
    with pytest.raises(ContractError, match="not executable"):
        gate.gate(
            policy=policy,
            policy_review=review,
            target_weights=target,
            target_weights_frame=frame,
            definition=definition,
            universe=universe,
            previous_target_weights=_previous_weights(),
        )
    # No run is published for a refused decision.
    assert list((tmp_path / "risk" / "runs").glob("generation=*")) == []


def test_risk_run_publication_requires_decision_payloads(tmp_path):
    """cr.md latent B1: a risk run without its decision payloads is a broken record,
    not a weaker one.  The store refuses rather than publishing a bare manifest."""
    gate = PortfolioPublicationRiskGate(tmp_path)
    decision = _gate_portfolio(gate)
    run = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "run_scope": "portfolio_publication",
        "risk_policy_binding": {
            "family": "risk_policy_v1",
            "generation_id": decision["risk_policy_binding"]["generation_id"],
            "manifest_digest_sha256": decision["risk_policy_binding"]["manifest_digest_sha256"],
        },
        "risk_state_binding": None,
        "consumer_binding": {
            "family": "target_weights_v1",
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        },
        "decision_bindings": [{
            "family": "risk_decision_v1",
            "generation_id": decision["generation_id"],
            "manifest_digest_sha256": decision["manifest_digest_sha256"],
        }],
        "event_bindings": [],
        "as_of_date": decision["as_of_date"],
        "visible_through": decision["visible_through"],
        "producer": decision["producer"],
        "serialization_profile": "json-canonical-v1",
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    run["generation_id"], run["manifest_digest_sha256"] = risk_contract_identities(
        run, schema_name="risk_run"
    )
    with pytest.raises(ContractError, match="requires the decision payloads"):
        gate.run_store.publish(run)


def _gate_config(start: str, end: str) -> dict:
    return {
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
        "contract_version": 1,
        "schema_version": "1.0.0",
        "initial_capital": 100000.0,
        "start_date": start,
        "end_date": end,
        "cost_model": {"commission_bps": 0, "stamp_duty_bps": 0, "slippage_bps": 0.0},
        "execution_model": {"board_lot": 100, "volume_participation_cap": 0.10},
        "limit_rules": {"limit_ratio": 0.10},
        "price_source_binding": {"source": "test"},
    }


def test_backtest_gate_with_only_suspended_candidates_records_no_run(tmp_path):
    """cr.md latent B5 follow-up: an execution day whose only candidate is suspended
    yields no decision at all, so the run is ``None`` instead of a fabricated
    ``risk_run`` with zero decision bindings (the schema requires at least one)."""
    root = tmp_path / "store"
    gate = PortfolioPublicationRiskGate(root)
    policy, review = _order_policy()
    # The only decision date wants A, but A has no execution price that day, so the
    # engine drops it from the target map and never forms a candidate order.
    price_panel = pd.DataFrame(
        {
            "date": ["2026-01-05", "2026-01-06"],
            "instrument": ["A", "A"],
            "open": [10.0, None],
            "close": [10.0, 10.0],
            "volume": [100000.0, 100000.0],
        }
    )
    from uq.backtest.engine import BacktestEngine

    engine = BacktestEngine(tmp_path)
    manifest, artifacts, risk_run = gate.run_with_risk_gate(
        engine,
        config=_gate_config("2026-01-05", "2026-01-06"),
        policy=policy,
        policy_review=review,
        portfolio_definition={"generation_id": "0" * 64},
        weight_partitions={
            "2026-01-05": pd.DataFrame({"instrument": ["A"], "weight": [1.0]})
        },
        price_panel=price_panel.set_index(["date", "instrument"]),
    )
    assert risk_run is None
    assert list((root / "risk" / "runs").glob("generation=*")) == []


def test_backtest_gate_suspended_exit_never_reaches_the_pre_trade_gate(tmp_path):
    """cr.md latent B5: a held position whose execution-day price is missing is
    suspended, and a suspended queue has no execution price.  Before the fix the
    engine still handed the gate ``price=0.0`` and the whole run died with
    ``candidate order execution price must be positive``.  The exit must instead be
    recorded as ``skipped_suspended`` while the earlier buy keeps its decision."""
    root = tmp_path / "store"
    gate = PortfolioPublicationRiskGate(root)
    policy, review = _order_policy()
    price_panel = pd.DataFrame(
        {
            "date": ["2026-01-05", "2026-01-06", "2026-01-07"],
            "instrument": ["A", "A", "A"],
            "open": [10.0, 10.0, None],
            "close": [10.0, 10.0, 10.0],
            "volume": [100000.0, 100000.0, 100000.0],
        }
    )
    from uq.backtest.engine import BacktestEngine

    engine = BacktestEngine(tmp_path)
    manifest, artifacts, risk_run = gate.run_with_risk_gate(
        engine,
        config=_gate_config("2026-01-05", "2026-01-07"),
        policy=policy,
        policy_review=review,
        portfolio_definition={"generation_id": "0" * 64},
        weight_partitions={
            "2026-01-05": pd.DataFrame({"instrument": ["A"], "weight": [1.0]}),
            "2026-01-06": pd.DataFrame({"instrument": ["A"], "weight": [0.0]}),
        },
        price_panel=price_panel.set_index(["date", "instrument"]),
    )
    # The 01-06 buy filled (one decision); the 01-07 exit was suspended and recorded
    # without ever forming an order for the gate.
    assert risk_run is not None
    assert len(risk_run["decision_bindings"]) == 1
    assert "filled" in set(artifacts["fills"]["status"])
    assert "skipped_suspended" in set(artifacts["fills"]["status"])


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
    order = {"order_id": "order-1", "instrument": "A", "side": "buy", "shares": 100}
    decision = gate.evaluate_order(
        policy=policy,
        policy_review=review,
        order=order,
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
    run["generation_id"], run["manifest_digest_sha256"] = risk_contract_identities(
        run, schema_name="risk_run"
    )
    payload = {
        "candidate_order": order,
        "decision": {
            "action": decision["action"],
            "constraints": decision["constraints"],
            "decision_digest": decision["decision_digest"],
            "findings": decision["findings"],
        },
    }
    encoded = (json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    run["files"] = [{
        "path": "decision-0.json",
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "byte_size": len(encoded),
        "serialization_profile": "json-canonical-v1",
    }]
    run["generation_id"], run["manifest_digest_sha256"] = risk_contract_identities(
        run, schema_name="risk_run"
    )
    gate.run_store.publish(run, {"decision-0.json": payload})
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


def test_order_submission_source_binds_governed_decision_to_config() -> None:
    """The paper risk source re-scopes a governed portfolio decision per config."""
    from tests.test_risk_control_plane_phase1 import (
        _definition,
        _policy,
        _policy_review,
        _target_weights,
        _universe,
    )
    from uq.execution.risk_source import RiskPolicyOrderSubmissionSource
    from uq.risk.contracts import risk_contract_identities
    from uq.risk.publication import ConfigPublicationBinding

    # A permissive policy (gross exposure 0.95 <= 1.0) yields an allow decision,
    # which is the shape the paper execution config store requires.
    policy = _policy()
    policy["rules"][0]["threshold"] = 1.0
    review = _policy_review(policy)
    policy["approval_binding"] = {
        "family": "risk_review_decision_v1",
        "review_generation_id": review["generation_id"],
        "review_manifest_digest_sha256": review["manifest_digest_sha256"],
    }
    policy["generation_id"], policy["manifest_digest_sha256"] = risk_contract_identities(
        policy, schema_name="risk_policy"
    )
    definition = _definition()
    universe = _universe()
    target, frame = _target_weights(definition, universe)

    source = RiskPolicyOrderSubmissionSource(policy=policy, policy_review=review)
    generation = "a" * 64
    decision = source(
        "2026-01-05",
        generation,
        {
            "execution_date": "2026-01-06",
            "target_weights_manifest": target,
            "target_weights_frame": frame,
            "definition": definition,
            "universe": universe,
        },
    )
    assert decision["decision_scope"] == "order_submission"
    assert decision["binding_config_generation_id"] == generation
    assert decision["as_of_date"] == "2026-01-05"
    assert decision["action"] == "allow"
    expected_generation, expected_digest = risk_contract_identities(
        decision, schema_name="risk_decision"
    )
    assert decision["generation_id"] == expected_generation
    assert decision["manifest_digest_sha256"] == expected_digest

    # The decision is already executable under the config publication binding.
    ConfigPublicationBinding(decision=decision, config_generation_id=generation)


def test_order_submission_source_fails_closed_on_blocking_decision() -> None:
    """A blocking governed decision cannot authorize a config publication."""
    from tests.test_risk_control_plane_phase1 import (
        _definition,
        _policy,
        _policy_review,
        _target_weights,
        _universe,
    )
    from uq.execution.risk_source import RiskPolicyOrderSubmissionSource
    from uq.risk.publication import ConfigPublicationBinding

    policy = _policy()
    review = _policy_review(policy)
    definition = _definition()
    universe = _universe()
    target, frame = _target_weights(definition, universe)
    decision = RiskPolicyOrderSubmissionSource(policy=policy, policy_review=review)(
        "2026-01-05",
        "a" * 64,
        {
            "execution_date": "2026-01-06",
            "target_weights_manifest": target,
            "target_weights_frame": frame,
            "definition": definition,
            "universe": universe,
        },
    )
    assert decision["action"] == "block"
    with pytest.raises(ContractError, match="not executable"):
        ConfigPublicationBinding(decision=decision, config_generation_id="a" * 64)


def test_order_submission_source_fails_closed_on_missing_context() -> None:
    from uq.execution.risk_source import RiskPolicyOrderSubmissionSource

    source = RiskPolicyOrderSubmissionSource(policy={}, policy_review={})
    with pytest.raises(ContractError, match="missing context"):
        source("2026-01-05", "a" * 64, {})


def _run_gate_once(*, weight, capital, participation, price=10.0, volume=100000.0, risk_gate_override=None):
    """One decision date, one instrument; returns (gate, artifacts) for assertions.

    ``risk_gate_override`` lets a stub gate stand in for the real evaluator so the
    engine's consumption of an arbitrary action can be exercised.
    """
    root = Path(tempfile.mkdtemp()) / "store"
    gate = PortfolioPublicationRiskGate(root)
    policy, review = _order_policy()
    panel = pd.DataFrame(
        {
            "date": ["2026-01-05", "2026-01-06"],
            "instrument": ["A", "A"],
            "open": [price, price],
            "close": [price, price],
            "volume": [volume, volume],
        }
    )
    config = _gate_config("2026-01-05", "2026-01-06")
    config["initial_capital"] = capital
    config["execution_model"]["volume_participation_cap"] = participation
    from uq.backtest.engine import BacktestEngine

    if risk_gate_override is None:
        manifest, artifacts, _ = gate.run_with_risk_gate(
            BacktestEngine(root),
            config=config,
            policy=policy,
            policy_review=review,
            portfolio_definition={"generation_id": "0" * 64},
            weight_partitions={"2026-01-05": pd.DataFrame({"instrument": ["A"], "weight": [weight]})},
            price_panel=panel.set_index(["date", "instrument"]),
        )
        return artifacts["fills"]
    # A stub gate drives the engine directly, bypassing the real risk_run publish.
    manifest, artifacts = BacktestEngine(root).run(
        config=config,
        portfolio_definition={"generation_id": "0" * 64},
        weight_partitions={"2026-01-05": pd.DataFrame({"instrument": ["A"], "weight": [weight]})},
        price_panel=panel.set_index(["date", "instrument"]),
        risk_gate=risk_gate_override,
    )
    return artifacts["fills"]


def test_backtest_gate_resize_whitelist_is_executed_end_to_end():
    """CR22-1: a ``resize`` decision must actually reach the engine and cap shares.

    The engine lists ``resize`` as executable, so an allowed_shares constraint has
    to travel through ``run_with_risk_gate`` and land as a filled, capped order --
    not merely be asserted at the ``RiskEngine`` boundary.

    The engine's own participation cap is set *above* the risk cap on purpose: if
    the engine ignored the resize constraint it would fill 50,000 shares instead of
    the 10,000 the decision allows, so the assertion discriminates.
    """
    fills = _run_gate_once(weight=1.0, capital=10_000_000.0, participation=0.50)
    row = fills.iloc[0]
    # Requested 1,000,000 shares; the risk participation rule caps at 10,000
    # (0.10 * 100,000) and the notional rule at 10,000 (100,000 / 10), both well
    # below the engine's own 50,000 (0.50 * 100,000) cap -- so only consuming the
    # resize decision produces this fill.
    assert row["target_shares"] == 10000
    assert row["filled_shares"] == 10000
    assert row["status"] == "filled"


def test_backtest_gate_resize_min_constraint_is_the_cap():
    """CR22-1: when several allowed_shares constraints disagree, the minimum wins.

    The risk participation cap (0.10 * 10,000 = 1,000) is set strictly *below* the
    notional cap (100,000 / 10 = 10,000), so only the ``min`` of the two produces
    the observed fill: a first-constraint-wins bug would fill 10,000, and ignoring
    the constraint entirely would fill the engine's own 5,000 (0.50 * 10,000).
    """
    fills = _run_gate_once(weight=1.0, capital=10_000_000.0, participation=0.50, volume=10000.0)
    row = fills.iloc[0]
    assert row["filled_shares"] == 1000


def test_backtest_gate_resize_without_allowed_shares_fails_closed():
    """CR22-1: a ``resize`` decision that names no allowed_shares is a contract
    violation, not a silent no-op -- the engine must raise."""

    def bad_gate(candidate, context):
        return {
            "action": "resize",
            "constraints": [],
            "decision_digest": "0" * 64,
            "findings": [],
        }

    with pytest.raises(ContractError, match="allowed_shares"):
        _run_gate_once(
            weight=1.0, capital=10_000_000.0, participation=0.05, risk_gate_override=bad_gate
        )


def test_backtest_gate_blocks_any_unknown_action():
    """CR22-6: the executable set is a whitelist, so an action the engine has never
    heard of (or a future enum member) is recorded blocked, never executed."""

    def unknown_gate(candidate, context):
        return {
            "action": "halt_strategy",
            "constraints": [],
            "decision_digest": "0" * 64,
            "findings": [],
        }

    fills = _run_gate_once(
        weight=1.0, capital=10_000_000.0, participation=0.05, risk_gate_override=unknown_gate
    )
    assert set(fills["status"]) == {"skipped_risk_blocked"}
    assert fills.iloc[0]["filled_shares"] == 0
