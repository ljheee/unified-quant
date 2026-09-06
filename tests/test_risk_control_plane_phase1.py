"""Phase 1 portfolio risk decision tests."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.gate_contracts import adjustment_snapshot_generation
from uq.contracts.model_layer import model_manifest_identities, sha256_json
from uq.errors import ContractError
from uq.risk import (
    PortfolioPublicationRiskGate,
    RiskDecisionStore,
    RiskEventStore,
    RiskPolicyStore,
    RiskRunStore,
    RiskStateStore,
    RiskEngine,
)
from uq.risk.contracts import (
    build_risk_review_decision,
    risk_contract_identities,
    risk_policy_draft_subject,
)
from tests.review_key import REVIEWER_PRIVATE_KEY


GEN_DEFINITION = hashlib.sha256(b"definition").hexdigest()
GEN_PREDICTION = hashlib.sha256(b"prediction").hexdigest()
GEN_UNIVERSE = hashlib.sha256(b"universe").hexdigest()


def _sign_policy(policy: dict) -> dict:
    policy["generation_id"], policy["manifest_digest_sha256"] = risk_contract_identities(
        policy, schema_name="risk_policy"
    )
    return policy


def _policy_review(policy: dict) -> dict:
    return build_risk_review_decision(
        review_type="risk_policy_activation",
        subject=risk_policy_draft_subject(policy),
        schema_name="risk_policy",
        review_status="approved",
        reviewer="repository-owner",
        errors=[],
        warnings=[],
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def _policy() -> dict:
    policy = json.loads(Path("config/schemas/fixtures/risk/risk_policy-valid.json").read_text())
    policy["effective_from"] = "2026-01-01"
    policy["effective_to"] = None
    policy["rules"][0]["rule_id"] = "portfolio_gross_exposure_limit"
    policy["rules"][0]["metric"] = "gross_stock_exposure"
    policy["rules"][0]["operator"] = "less_than_or_equal"
    policy["rules"][0]["threshold"] = 0.90
    policy["rules"][0]["action"] = "block"
    policy["rules"][0]["severity"] = "critical"
    policy["rules"][0]["priority"] = 10
    policy["rules"][0]["state_required"] = False
    review = _policy_review(policy)
    policy["approval_binding"] = {
        "family": "risk_review_decision_v1",
        "review_generation_id": review["generation_id"],
        "review_manifest_digest_sha256": review["manifest_digest_sha256"],
    }
    return _sign_policy(policy)


def _universe() -> dict:
    universe = {
        "universe_version": 1,
        "universe_id": "research",
        "source": "test",
        "snapshot_time": "2026-01-04T15:00:00+00:00",
        "visibility_time": "2026-01-04T15:00:00+00:00",
        "valid_from": "2026-01-05",
        "valid_to": None,
        "members_artifact": {"path": "members.json", "checksum_sha256": "0" * 64},
        "membership_evidence": "test fixture",
    }
    universe["generation_id"] = adjustment_snapshot_generation(
        {key: value for key, value in universe.items() if key != "generation_id"}
    )
    return universe


def _definition() -> dict:
    universe = _universe()
    definition = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "portfolio_name": "research",
        "weight_scheme": "top_n_equal_weight",
        "scheme_parameters": {"n": 2},
        "score_policy": {"direction": "descending", "nan_policy": "exclude", "tie_policy": "first_by_instrument_id"},
        "constraints": {
            "max_single_weight": 0.5,
            "max_industry_weight": None,
            "max_turnover": None,
            "cash_reserve": 0.0,
        },
        "rebalance_schedule": "daily",
        "universe_snapshot_generation_id": universe["generation_id"],
        "industry_source_binding": {"source_type": "none"},
        "prediction_set_generation_id": GEN_PREDICTION,
        "run_id": "00000000-0000-0000-0000-000000000001",
        "created_at": "1970-01-01T00:00:00+00:00",
        "quality_report_checksum_sha256": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
        "generation_id": "0" * 64,
    }
    definition["generation_id"], definition["manifest_digest_sha256"] = model_manifest_identities(
        definition, schema_name="portfolio_definition"
    )
    return definition


def _target_weights(
    definition: dict | None = None,
    universe: dict | None = None,
    previous_generation: str | None = None,
) -> tuple[dict, pd.DataFrame]:
    definition = definition or _definition()
    universe = universe or _universe()
    frame = pd.DataFrame({"instrument": ["A", "B"], "weight": [0.6, 0.35]})
    weights = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "decision_date": "2026-01-05",
        "portfolio_definition_generation_id": definition["generation_id"],
        "prediction_set_generation_id": definition["prediction_set_generation_id"],
        "universe_snapshot_generation_id": universe["generation_id"],
        "instrument_count": 2,
        "total_stock_weight": 0.95,
        "cash_reserve": 0.05,
        "weights_file": "data.parquet",
        "weights_checksum_sha256": hashlib.sha256(b"test").hexdigest(),
        "columns": ["instrument", "weight"],
        "dtypes": {"instrument": "string", "weight": "float64"},
        "row_count": 2,
        "key_uniqueness": "instrument",
        "logical_fingerprint": hashlib.sha256(b"logical").hexdigest(),
        "serialization_profile_id": "parquet-v1",
        "run_id": "00000000-0000-0000-0000-000000000001",
        "created_at": "1970-01-01T00:00:00+00:00",
        "quality_report_checksum_sha256": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
        "generation_id": "0" * 64,
        "previous_target_weights_generation_id": previous_generation,
    }
    weights["logical_fingerprint"] = sha256_json({
        "instruments": sorted(frame["instrument"].astype(str).tolist()),
        "weights": [round(float(value), 12) for value in frame.sort_values("instrument")["weight"].tolist()],
    })
    weights["generation_id"], weights["manifest_digest_sha256"] = model_manifest_identities(
        weights, schema_name="target_weights"
    )
    return weights, frame


def _previous_weights() -> dict[str, float]:
    return {"A": 0.3, "B": 0.3}


def _evaluate(**overrides):
    policy = overrides.pop("policy", _policy())
    if "policy_review" not in overrides:
        overrides["policy_review"] = _policy_review(policy)
    definition = overrides.pop("definition", _definition())
    universe = overrides.pop("universe", _universe())
    if "target_weights" in overrides:
        target, frame = overrides.pop("target_weights")
    else:
        target, frame = _target_weights(definition, universe)
    return RiskEngine().evaluate_portfolio(
        policy=policy,
        target_weights=target,
        target_weights_frame=frame,
        definition=definition,
        universe=universe,
        as_of_date=overrides.pop("as_of_date", "2026-01-05"),
        **overrides,
    )


def test_risk_portfolio_decision_is_deterministic():
    first = _evaluate()
    second = _evaluate()
    assert first == second
    assert first["action"] == "block"
    assert first["findings"][0]["observed"] == 0.95
    Path("evidence/risk/phase-1/golden").mkdir(parents=True, exist_ok=True)
    Path("evidence/risk/phase-1/golden/portfolio-decision.json").write_text(
        json.dumps(first, sort_keys=True, indent=2) + "\n"
    )


def test_risk_portfolio_missing_inputs_fail_closed():
    with pytest.raises(ContractError, match="policy"):
        _evaluate(policy={})
    definition = _definition()
    universe = _universe()
    target, frame = _target_weights(definition, universe)
    with pytest.raises(ContractError, match="generation mismatch"):
        _evaluate(
            definition=definition,
            universe=universe,
            target_weights=({**target, "generation_id": "1" * 64}, frame),
        )
    with pytest.raises(ContractError, match="generation mismatch"):
        _evaluate(
            definition={**definition, "generation_id": "2" * 64},
            universe=universe,
        )
    with pytest.raises(ContractError, match="universe definition"):
        _evaluate(
            definition=definition,
            universe={**universe, "generation_id": "3" * 64},
        )


def test_risk_portfolio_state_required_fails_closed():
    policy = _policy()
    policy["rules"][0]["state_required"] = True
    review = _policy_review(policy)
    policy["approval_binding"] = {
        "family": "risk_review_decision_v1",
        "review_generation_id": review["generation_id"],
        "review_manifest_digest_sha256": review["manifest_digest_sha256"],
    }
    policy = _sign_policy(policy)
    with pytest.raises(ContractError, match="risk state is required"):
        _evaluate(policy=policy, policy_review=review)


def test_risk_portfolio_action_ranking_is_deterministic():
    policy = _policy()
    policy["rules"] = [
        {
            "rule_id": "portfolio_gross_exposure_limit",
            "rule_scope": "portfolio",
            "metric": "gross_stock_exposure",
            "operator": "less_than_or_equal",
            "threshold": 0.5,
            "action": "warn",
            "severity": "critical",
            "priority": 100,
            "state_required": False,
        },
        {
            "rule_id": "portfolio_single_name_limit",
            "rule_scope": "portfolio",
            "metric": "max_single_weight",
            "operator": "less_than_or_equal",
            "threshold": 0.5,
            "action": "block",
            "severity": "error",
            "priority": 1,
            "state_required": False,
        },
    ]
    review = _policy_review(policy)
    policy["approval_binding"] = {
        "family": "risk_review_decision_v1",
        "review_generation_id": review["generation_id"],
        "review_manifest_digest_sha256": review["manifest_digest_sha256"],
    }
    policy = _sign_policy(policy)
    decision = _evaluate(policy=policy, policy_review=review)
    assert decision["action"] == "block"


def test_risk_portfolio_resize_formula_is_exact():
    policy = _policy()
    policy["rules"][0]["action"] = "resize"
    review = _policy_review(policy)
    policy["approval_binding"] = {
        "family": "risk_review_decision_v1",
        "review_generation_id": review["generation_id"],
        "review_manifest_digest_sha256": review["manifest_digest_sha256"],
    }
    policy = _sign_policy(policy)
    decision = _evaluate(
        policy=policy,
        policy_review=review,
        previous_target_weights=_previous_weights(),
    )
    assert decision["action"] == "resize"
    assert decision["constraints"][0]["value"] == 0.9
    assert decision["findings"][0]["observed"] == 0.95


def test_risk_portfolio_overlapping_policies_fail_closed():
    policy = _policy()
    policy["rules"].append(copy.deepcopy(policy["rules"][0]))
    with pytest.raises(ContractError, match="cross-scope|identity"):
        _evaluate(policy=policy)


def test_risk_industry_limit_disabled_without_contract():
    policy = _policy()
    policy["rules"][0]["rule_id"] = "portfolio_industry_limit"
    policy["rules"][0]["metric"] = "industry_weight"
    policy = _sign_policy(policy)
    with pytest.raises(ContractError, match="industry_membership_binding"):
        _evaluate(policy=policy)


def test_risk_stores_are_immutable_and_readable(tmp_path: Path):
    policy = _policy()
    policy_partition = RiskPolicyStore(tmp_path).publish(policy)
    assert policy_partition.exists()
    with pytest.raises(ContractError, match="already exists"):
        RiskPolicyStore(tmp_path).publish(policy)
    assert RiskPolicyStore(tmp_path).read(policy["generation_id"]) == policy

    decision = _evaluate()
    payload = {
        "action": decision["action"],
        "constraints": decision["constraints"],
        "decision_digest": decision["decision_digest"],
        "findings": decision["findings"],
    }
    RiskDecisionStore(tmp_path).publish(decision, payload)
    with pytest.raises(ContractError, match="already exists"):
        RiskDecisionStore(tmp_path).publish(decision, payload)
    assert RiskDecisionStore(tmp_path).read(decision["generation_id"])[0] == decision


def test_risk_portfolio_publication_gate_rejects_block(tmp_path: Path):
    gate = PortfolioPublicationRiskGate(tmp_path)
    with pytest.raises(ContractError, match="publication blocked"):
        gate.gate(
            policy=_policy(),
            policy_review=_policy_review(_policy()),
            target_weights=_target_weights(definition=_definition(), universe=_universe())[0],
            target_weights_frame=_target_weights(definition=_definition(), universe=_universe())[1],
            definition=_definition(),
            universe=_universe(),
            previous_target_weights=_previous_weights(),
        )
    decision = RiskDecisionStore(tmp_path).read(
        sorted((tmp_path / "risk" / "decisions").glob("*/date=*/generation=*"))[0].name.split("=", 1)[1]
    )[0]
    assert decision["action"] == "block"
    assert RiskPolicyStore(tmp_path).read(_policy()["generation_id"])
    assert RiskDecisionStore(tmp_path).read(decision["generation_id"])[0]["action"] == "block"
    assert list((tmp_path / "risk" / "events").glob("generation=*")) == []
    assert list((tmp_path / "risk" / "runs").glob("generation=*")) == []
