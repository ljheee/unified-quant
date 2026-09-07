"""Risk Control Plane Phase 4 Research Chain integration tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from uq.contracts.gate_contracts import canonical_json
from uq.contracts.model_layer import (
    ModelContractLoader,
    research_contract_identities as research_identities,
    research_stage_plan_sha256,
    research_stage_plan_v2_sha256,
    sha256_json,
)
from uq.errors import ContractError
from uq.research_chain.contracts import verify_stage_plan_review
from uq.risk.contracts import build_risk_review_decision
from uq.risk.contracts import risk_contract_identities as risk_identities
from uq.risk.contracts import risk_policy_draft_subject
from uq.research_chain.resolver import ResearchChainRequestResolver, ResolvedExecutionPlan
from uq.research_chain.runner import ResearchChainRunner
from tests.review_key import REVIEWER_PRIVATE_KEY

ROOT = Path(__file__).resolve().parents[1]
V1_REQUEST = ROOT / "evidence/research-chain/phase-0/fixtures/research_run_request-valid.json"

_REVIEW_FIELDS = (
    "review_type", "subject_generation_id", "subject_manifest_digest_sha256",
    "review_status", "reviewer", "key_id",
)


def _zero() -> str:
    return "0" * 64


def _risk_binding(action: str = "allow") -> dict:
    decision_binding = {
        "family": "risk_decision_v1",
        "generation_id": _zero(),
        "manifest_digest_sha256": _zero(),
        "action": action,
        "decision_scope": "portfolio_publication",
        "decision_digest": _zero(),
    }
    return {
        "policy_binding": {
            "family": "risk_policy_v1", "generation_id": _zero(),
            "manifest_digest_sha256": _zero(),
        },
        "policy_review_binding": {
            "family": "risk_review_decision_v1", "generation_id": _zero(),
            "manifest_digest_sha256": _zero(),
        },
        "state_binding": {
            "family": "risk_state_v1", "generation_id": _zero(),
            "manifest_digest_sha256": _zero(),
        },
        "run_binding": {
            "family": "risk_run_v1", "generation_id": _zero(),
            "manifest_digest_sha256": _zero(),
        },
        "decision_binding": decision_binding,
        "event_bindings": [{
            "family": "risk_event_v1", "generation_id": _zero(),
            "manifest_digest_sha256": _zero(),
        }],
    }


def _signed_review(stage_plan_sha256: str, *, signature: str | None = None) -> dict:
    unsigned = {
        "review_type": "research_stage_plan_v2_activation",
        "subject_generation_id": stage_plan_sha256,
        "subject_manifest_digest_sha256": stage_plan_sha256,
        "review_status": "approved",
        "reviewer": "repository-owner",
        "key_id": "research-stage-plan-reviewer-v1-2026-09",
    }
    if signature is None:
        key = serialization.load_pem_private_key(REVIEWER_PRIVATE_KEY.read_text().encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise AssertionError("test reviewer key is not Ed25519")
        signature = key.sign(canonical_json(unsigned)).hex()
    return {**unsigned, "review_signature_sha256": signature}


def _signed_policy_evidence() -> tuple[dict, dict]:
    policy = json.loads((FIXTURES / "risk_policy-valid.json").read_text())
    draft = risk_policy_draft_subject(policy)
    policy_review = build_risk_review_decision(
        review_type="risk_policy_activation",
        subject=draft,
        schema_name="risk_policy",
        review_status="approved",
        reviewer="repository-owner",
        errors=[],
        warnings=[],
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    policy["approval_binding"] = {
        "family": "risk_review_decision_v1",
        "review_generation_id": policy_review["generation_id"],
        "review_manifest_digest_sha256": policy_review["manifest_digest_sha256"],
    }
    _identity(policy, "risk_policy")
    return policy, policy_review


def _v2_request(action: str = "allow") -> dict:
    request = json.loads(V1_REQUEST.read_text())
    policy, policy_review = _signed_policy_evidence()
    stage_plan = research_stage_plan_v2_sha256()
    request["contract_version"] = 2
    request["schema_version"] = "2.0.0"
    request["risk_binding"] = {
        **_risk_binding(action),
        "policy_review_binding": _binding_from(policy_review, "risk_review_decision_v1"),
    }
    request["policy_review"] = policy_review
    request["stage_plan_sha256"] = stage_plan
    request["stage_plan_review"] = _signed_review(stage_plan)
    request["request_content_generation_id"] = _zero()
    request["manifest_digest_sha256"] = _zero()
    request["request_content_generation_id"], request["manifest_digest_sha256"] = research_identities(
        request, schema_name="research_run_request_v2"
    )
    return request


FIXTURES = ROOT / "config/schemas/fixtures/risk"


def _identity(payload: dict, schema_name: str) -> dict:
    if schema_name == "research_run_request_v2":
        payload["generation_id"], payload["manifest_digest_sha256"] = research_identities(
            payload, schema_name=schema_name
        )
    else:
        payload["generation_id"], payload["manifest_digest_sha256"] = risk_identities(
            payload, schema_name=schema_name
        )
    return payload


def _binding_from(document: dict, family: str) -> dict:
    return {
        "family": family,
        "generation_id": document["generation_id"],
        "manifest_digest_sha256": document["manifest_digest_sha256"],
    }


def _risk_evidence(action: str) -> tuple[dict, dict]:
    policy, policy_review = _signed_policy_evidence()
    state = json.loads((FIXTURES / "risk_state-valid.json").read_text())
    event = json.loads((FIXTURES / "risk_event-valid.json").read_text())
    decision = json.loads((FIXTURES / "risk_decision-valid.json").read_text())
    decision["action"] = action
    decision["risk_policy_binding"] = _binding_from(policy, "risk_policy_v1")
    decision["risk_state_binding"] = _binding_from(state, "risk_state_v1")
    decision["decision_digest"] = sha256_json({
        "action": decision["action"],
        "constraints": decision["constraints"],
        "findings": decision["findings"],
    })
    _identity(decision, "risk_decision")

    policy_binding = _binding_from(policy, "risk_policy_v1")
    state_binding = _binding_from(state, "risk_state_v1")
    decision_binding = {
        **_binding_from(decision, "risk_decision_v1"),
        "action": decision["action"],
        "decision_scope": decision["decision_scope"],
        "decision_digest": decision["decision_digest"],
    }
    event_binding = _binding_from(event, "risk_event_v1")

    run = json.loads((FIXTURES / "risk_run-valid.json").read_text())
    run["run_scope"] = "portfolio_publication"
    run["risk_policy_binding"] = policy_binding
    run["risk_state_binding"] = state_binding
    run["decision_bindings"] = [_binding_from(decision, "risk_decision_v1")]
    run["event_bindings"] = [event_binding]
    _identity(run, "risk_run")

    request = _v2_request(action)
    request["risk_binding"] = {
        "policy_binding": policy_binding,
        "policy_review_binding": _binding_from(policy_review, "risk_review_decision_v1"),
        "state_binding": state_binding,
        "run_binding": _binding_from(run, "risk_run_v1"),
        "decision_binding": decision_binding,
        "event_bindings": [event_binding],
    }
    _identity(request, "research_run_request_v2")
    documents = {
        "policy": policy,
        "policy_review": policy_review,
        "state": state,
        "run": run,
        "decision": decision,
        "events": [event],
    }
    return request, documents


def _plan(request: dict) -> ResolvedExecutionPlan:
    return ResolvedExecutionPlan(
        request=request,
        request_manifest_digest_sha256=request["manifest_digest_sha256"],
        stage_plan_sha256=request["stage_plan_sha256"],
        stage_bindings=(),
        resolved_execution_plan_sha256=_zero(),
    )


def test_research_request_v1_stage_plan_is_frozen():
    request = json.loads(V1_REQUEST.read_text())
    ModelContractLoader.validate("research_run_request", request)
    assert request["stage_plan_sha256"] == research_stage_plan_sha256()


def test_research_request_v2_requires_risk_decision():
    request = _v2_request()
    ModelContractLoader.validate("research_run_request_v2", request)
    assert request["stage_plan_sha256"] == research_stage_plan_v2_sha256()
    assert request["risk_binding"]["decision_binding"]["action"] == "allow"
    missing = dict(request)
    missing.pop("risk_binding")
    missing["request_content_generation_id"] = _zero()
    missing["manifest_digest_sha256"] = _zero()
    with pytest.raises(ContractError, match="risk_binding"):
        ModelContractLoader.validate("research_run_request_v2", missing)


def test_research_stage_stops_on_rejected_decision():
    allowed_request, allowed_documents = _risk_evidence("allow")
    rejected_request, rejected_documents = _risk_evidence("block")
    runner = ResearchChainRunner.__new__(ResearchChainRunner)
    runner._enforce_risk_gate(_plan(allowed_request), risk_documents=allowed_documents)
    with pytest.raises(ContractError, match="stopped by rejected risk decision"):
        runner._enforce_risk_gate(
            _plan(rejected_request), risk_documents=rejected_documents
        )


def test_research_v2_requires_signed_policy_review():
    request, documents = _risk_evidence("allow")
    runner = ResearchChainRunner.__new__(ResearchChainRunner)
    runner._enforce_risk_gate(_plan(request), risk_documents=documents)
    with pytest.raises(ContractError, match="risk evidence is incomplete"):
        runner._enforce_risk_gate(
            _plan(request),
            risk_documents={key: value for key, value in documents.items() if key != "policy_review"},
        )
    tampered_review = {
        **documents["policy_review"],
        "review_status": "rejected",
    }
    with pytest.raises(ContractError, match="identity mismatch"):
        runner._enforce_risk_gate(
            _plan(request), risk_documents={**documents, "policy_review": tampered_review}
        )
    def tampered_signature(review: dict, signature: str) -> dict:
        unsigned = {
            "contract_version": review["contract_version"],
            "schema_version": review["schema_version"],
            "review_type": review["review_type"],
            "subject_generation_id": review["subject_generation_id"],
            "subject_manifest_digest_sha256": review["subject_manifest_digest_sha256"],
            "subject_content_sha256": review["subject_content_sha256"],
            "review_status": review["review_status"],
            "reviewer": review["reviewer"],
            "key_id": review["key_id"],
            "policy": review["policy"],
            "reviewed_at_utc": review["reviewed_at_utc"],
            "errors": review["errors"],
            "warnings": review["warnings"],
        }
        generation, digest = risk_identities(
            {
                **unsigned,
                "review_signature_sha256": signature,
                "generation_id": "0" * 64,
                "manifest_digest_sha256": "0" * 64,
            },
            schema_name="risk_review_decision",
        )
        return {
            **unsigned,
            "review_signature_sha256": signature,
            "generation_id": generation,
            "manifest_digest_sha256": digest,
        }

    forged_review = tampered_signature(documents["policy_review"], "f" * 128)
    with pytest.raises(ContractError, match="review signature mismatch"):
        runner._enforce_risk_gate(
            _plan(request), risk_documents={**documents, "policy_review": forged_review}
        )


def test_research_runner_cannot_sign_risk_reviews():
    stage_plan_sha256 = research_stage_plan_v2_sha256()
    review = _signed_review(stage_plan_sha256)
    verify_stage_plan_review(review, stage_plan_sha256=stage_plan_sha256)
    tampered = {**review, "review_signature_sha256": "f" * 128}
    with pytest.raises(ContractError, match="signature mismatch"):
        verify_stage_plan_review(tampered, stage_plan_sha256=stage_plan_sha256)
    runner_methods = {
        name for name in dir(ResearchChainRunner)
        if callable(getattr(ResearchChainRunner, name, None))
        and ("sign" in name or "create_review" in name)
    }
    assert runner_methods == set()
