"""Risk Control Plane Phase 3 stateful loss-control tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uq.errors import ContractError
from uq.risk import evaluate_strategy_state
from uq.risk.contracts import (
    build_risk_review_decision,
    risk_contract_identities,
    risk_de_risk_contract_draft_subject,
    risk_exception_draft_subject,
    risk_policy_draft_subject,
)
from uq.risk.stores import make_binding
from tests.review_key import REVIEWER_PRIVATE_KEY

GOLDEN = Path("evidence/risk/phase-3/golden/state-sequence.json")


def _sign_policy(policy: dict) -> dict:
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


def _policy(**overrides) -> tuple[dict, dict]:
    policy = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "policy_id": "strategy-loss-control",
        "policy_version": "1.0.0",
        "policy_scope": "strategy",
        "activation_status": "approved",
        "effective_from": "2026-01-01",
        "effective_to": None,
        "rules": [{
            "rule_id": "strategy_drawdown_trigger",
            "rule_scope": "strategy",
            "metric": "strategy_drawdown",
            "operator": "greater_than_or_equal",
            "threshold": overrides.pop("threshold", 0.20),
            "action": "flatten",
            "severity": "critical",
            "priority": 10,
            "state_required": True,
            "state_window_days": 20,
            "hysteresis_exit_threshold": overrides.pop("hysteresis_exit_threshold", 0.10),
            "cooldown_days": overrides.pop("cooldown_days", 2),
        }],
        "industry_membership_binding": None,
        "approval_binding": {
            "family": "risk_review_decision_v1",
            "review_generation_id": "0" * 64,
            "review_manifest_digest_sha256": "0" * 64,
        },
        "producer": {
            "producer_code_fingerprint": "0" * 64,
            "run_id": "00000000-0000-0000-0000-000000000001",
            "created_at": "1970-01-01T00:00:00+00:00",
        },
        "serialization_profile": "json-canonical-v1",
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    return _sign_policy(policy)


def _contract() -> tuple[dict, dict]:
    contract = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "contract_id": "strategy-loss-control",
        "consumer_type": "portfolio",
        "status": "approved",
        "supported_actions": ["flatten"],
        "transition_semantics": "replace_weight_map",
        "execution_available": True,
        "review_binding": {
            "family": "risk_review_decision_v1",
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        },
        "producer": {
            "producer_code_fingerprint": "0" * 64,
            "run_id": "00000000-0000-0000-0000-000000000001",
            "created_at": "1970-01-01T00:00:00+00:00",
        },
        "serialization_profile": "json-canonical-v1",
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    review = build_risk_review_decision(
        review_type="risk_de_risk_contract_activation",
        subject=risk_de_risk_contract_draft_subject(contract),
        schema_name="risk_de_risk_contract",
        review_status="approved",
        reviewer="repository-owner",
        errors=[],
        warnings=[],
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    contract["review_binding"] = make_binding(review, family="risk_review_decision_v1")
    contract["generation_id"], contract["manifest_digest_sha256"] = risk_contract_identities(
        contract, schema_name="risk_de_risk_contract"
    )
    return contract, review


def _exception(policy: dict, *, expires_at: str) -> tuple[dict, dict]:
    exception = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "exception_id": "strategy-flatten-exception",
        "scope_type": "strategy",
        "scope_id": policy["policy_id"],
        "policy_binding": make_binding(policy, family="risk_policy_v1"),
        "rule_id": "strategy_drawdown_trigger",
        "permitted_action": "flatten",
        "status": "approved",
        "expires_at": expires_at,
        "reason": "temporary execution transition",
        "evidence_bindings": [],
        "review_binding": {
            "family": "risk_review_decision_v1",
            "review_generation_id": "0" * 64,
            "review_manifest_digest_sha256": "0" * 64,
        },
        "producer": {
            "producer_code_fingerprint": "0" * 64,
            "run_id": "00000000-0000-0000-0000-000000000001",
            "created_at": "1970-01-01T00:00:00+00:00",
        },
        "serialization_profile": "json-canonical-v1",
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    review = build_risk_review_decision(
        review_type="risk_exception_grant",
        subject=risk_exception_draft_subject(exception),
        schema_name="risk_exception",
        review_status="approved",
        reviewer="repository-owner",
        errors=[],
        warnings=[],
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    exception["review_binding"] = make_binding(review, family="risk_review_decision_v1")
    exception["generation_id"], exception["manifest_digest_sha256"] = risk_contract_identities(
        exception, schema_name="risk_exception"
    )
    return exception, review


CALENDAR = {
    "family": "trading_calendar_v1",
    "generation_id": "a" * 64,
    "manifest_digest_sha256": "b" * 64,
}


def _observation(day: str, value: float, visible: str) -> dict:
    return {
        "as_of_date": day,
        "visible_through": visible,
        "observation_generation_id": "c" * 64,
        "metrics": {"strategy_drawdown": value, "strategy_volatility": value / 2.0},
    }


def _evaluate(observations: list[dict], **overrides):
    policy, policy_review = overrides.pop("policy", _policy())
    contract_override = overrides.pop("contract", "default")
    contract, contract_review = (None, None) if contract_override is None else _contract()
    return evaluate_strategy_state(
        policy=policy,
        policy_review=policy_review,
        observations=observations,
        calendar_binding=CALENDAR,
        de_risk_contract=contract,
        de_risk_review=contract_review,
        **overrides,
    )


def test_risk_state_transition_is_deterministic():
    observations = [
        _observation("2026-01-05", 0.25, "2026-01-05T16:00:00+00:00"),
        _observation("2026-01-06", 0.24, "2026-01-06T16:00:00+00:00"),
        _observation("2026-01-07", 0.09, "2026-01-07T16:00:00+00:00"),
    ]
    first = _evaluate(observations)
    second = _evaluate(observations)
    assert first == second
    assert first["state"]["state_status"] == "updated"
    assert [event["event_class"] for event in first["events"]] == [
        "risk_triggered",
        "state_snapshot_published",
        "state_snapshot_published",
        "risk_cleared",
        "state_snapshot_published",
    ]
    assert first["events"][0]["event_sequence_number"] == 1
    assert first["events"][-1]["event_sequence_number"] == 5
    assert [item["event_class"] for item in first["events"]].count("risk_triggered") == 1
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(first, sort_keys=True, indent=2) + "\n")


def test_risk_hysteresis_and_cooldown_are_exact():
    observations = [
        _observation("2026-01-05", 0.25, "2026-01-05T16:00:00+00:00"),
        _observation("2026-01-06", 0.15, "2026-01-06T16:00:00+00:00"),
        _observation("2026-01-07", 0.25, "2026-01-07T16:00:00+00:00"),
        _observation("2026-01-08", 0.15, "2026-01-08T16:00:00+00:00"),
        _observation("2026-01-09", 0.09, "2026-01-09T16:00:00+00:00"),
        _observation("2026-01-10", 0.25, "2026-01-10T16:00:00+00:00"),
    ]
    result = _evaluate(observations)
    assert result["state_payload"]["active"] is True
    assert result["state_payload"]["consecutive_trigger_days"] == 1
    assert [event["event_class"] for event in result["events"]].count("risk_triggered") == 2
    assert [event["event_class"] for event in result["events"]].count("risk_cleared") == 1


def test_risk_late_visibility_creates_new_generation():
    first = _evaluate([_observation("2026-01-05", 0.25, "2026-01-05T16:00:00+00:00")])
    late = _evaluate(
        [_observation("2026-01-05", 0.26, "2026-01-06T10:00:00+00:00")],
        prior_state=first["state"],
        prior_payload=first["state_payload"],
    )
    assert late["state"]["generation_id"] != first["state"]["generation_id"]
    assert late["state"]["supersedes_state_generation_id"] == first["state"]["generation_id"]
    assert late["state"]["as_of_date"] == first["state"]["as_of_date"]
    with pytest.raises(ContractError, match="strictly later"):
        _evaluate(
            [_observation("2026-01-05", 0.26, "2026-01-05T16:00:00+00:00")],
            prior_state=first["state"],
            prior_payload=first["state_payload"],
        )


def test_risk_exception_expiry_restores_enforcement():
    policy, policy_review = _policy()
    contract, contract_review = _contract()
    active_exception, exception_review = _exception(policy, expires_at="2026-01-06")
    suppressed = _evaluate(
        [_observation("2026-01-05", 0.25, "2026-01-05T16:00:00+00:00")],
        policy=(policy, policy_review),
        contract=(contract, contract_review),
        exception=active_exception,
        exception_review=exception_review,
    )
    assert suppressed["action"] == "allow"
    assert "exception_applied" in {event["event_class"] for event in suppressed["events"]}

    expired_exception, expired_review = _exception(policy, expires_at="2026-01-04")
    restored = _evaluate(
        [_observation("2026-01-05", 0.25, "2026-01-05T16:00:00+00:00")],
        policy=(policy, policy_review),
        contract=(contract, contract_review),
        exception=expired_exception,
        exception_review=expired_review,
    )
    assert restored["action"] == "flatten"
    assert "exception_expired" in {event["event_class"] for event in restored["events"]}


def test_risk_missing_de_risk_contract_fails_closed():
    with pytest.raises(ContractError, match="requires a de-risk contract"):
        _evaluate(
            [_observation("2026-01-05", 0.25, "2026-01-05T16:00:00+00:00")],
            contract=None,
        )


def test_risk_prior_state_payload_tampering_fails_closed():
    first = _evaluate([_observation("2026-01-05", 0.25, "2026-01-05T16:00:00+00:00")])
    tampered_payload = {**first["state_payload"], "active": False}
    with pytest.raises(ContractError, match="prior risk state payload identity mismatch"):
        _evaluate(
            [_observation("2026-01-06", 0.09, "2026-01-06T16:00:00+00:00")],
            prior_state=first["state"],
            prior_payload=tampered_payload,
        )


def test_risk_exception_policy_lineage_mismatch_fails_closed():
    bound_policy, _ = _policy(threshold=0.20)
    evaluated_policy, evaluated_policy_review = _policy(threshold=0.30)
    exception, exception_review = _exception(bound_policy, expires_at="2026-01-06")
    with pytest.raises(ContractError, match="policy lineage mismatch"):
        _evaluate(
            [_observation("2026-01-05", 0.25, "2026-01-05T16:00:00+00:00")],
            policy=(evaluated_policy, evaluated_policy_review),
            exception=exception,
            exception_review=exception_review,
        )


def test_risk_state_event_sequence_is_monotonic_across_generations():
    first = _evaluate([_observation("2026-01-05", 0.25, "2026-01-05T16:00:00+00:00")])
    second = _evaluate(
        [_observation("2026-01-06", 0.24, "2026-01-06T16:00:00+00:00")],
        prior_state=first["state"],
        prior_payload=first["state_payload"],
    )
    first_last = first["events"][-1]["event_sequence_number"]
    second_last = second["events"][-1]["event_sequence_number"]
    assert first["state_payload"]["last_event_sequence_number"] == first_last
    assert second["state_payload"]["last_event_sequence_number"] == second_last
    assert second["events"][0]["event_sequence_number"] == first_last + 1
    assert second_last > first_last
