"""Risk Control Plane Phase 0 contract tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from tests.review_key import REVIEWER_PRIVATE_KEY

from uq.errors import ContractError
from uq.risk.contracts import (
    RISK_CONTRACT_NAMES,
    build_risk_review_decision,
    risk_contract_identities,
    risk_stable_content_id,
    validate_risk_contract,
    validate_enforceable_de_risk_contract,
    validate_industry_membership_prerequisite,
    validate_risk_event_sequence,
    risk_policy_draft_subject,
    validate_risk_policy_governance,
    validate_risk_file_path,
    validate_risk_file_payload,
    validate_risk_manifest,
    validate_risk_policy_scope_compatibility,
    verify_risk_review_decision,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "config/schemas/fixtures/risk"
GOLDEN = ROOT / "evidence/risk/phase-0/golden/risk-contract-identities.json"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _family_payload(schema_name: str) -> dict:
    return _fixture(f"{schema_name}-valid")


def test_all_risk_contract_fixtures_are_valid():
    for schema_name in RISK_CONTRACT_NAMES:
        payload = _family_payload(schema_name)
        if schema_name == "risk_review_trust_anchor":
            validate_risk_contract(schema_name, payload)
        else:
            validate_risk_manifest(schema_name, payload)


def test_all_risk_contract_negative_fixtures_fail():
    for path in FIXTURES.glob("*-negative.json"):
        schema_name = path.name.replace("-negative.json", "").replace("_v1", "")
        payload = json.loads(path.read_text())
        with pytest.raises(Exception):
            validate_risk_contract(schema_name, payload)
        if schema_name == "risk_de_risk_contract":
            with pytest.raises(ContractError):
                validate_enforceable_de_risk_contract(payload, action="de_risk")


def test_risk_generation_excludes_run_metadata():
    payload = _family_payload("risk_decision")
    generation, digest = risk_contract_identities(payload, schema_name="risk_decision")
    run_local = copy.deepcopy(payload)
    run_local["producer"]["run_id"] = "00000000-0000-0000-0000-000000000002"
    run_local["producer"]["request_id"] = "00000000-0000-0000-0000-000000000003"
    run_local["producer"]["created_at"] = "2026-09-06T01:00:00Z"
    run_local_generation, run_local_digest = risk_contract_identities(
        run_local, schema_name="risk_decision"
    )
    assert run_local_generation == generation
    assert run_local_digest != digest


def test_risk_identity_changes_on_each_binding():
    payload = _family_payload("risk_decision")
    generation, digest = risk_contract_identities(payload, schema_name="risk_decision")
    for field in ("risk_policy_binding", "risk_state_binding", "input_bindings"):
        changed = copy.deepcopy(payload)
        if field == "input_bindings":
            changed[field][0]["generation_id"] = "1" * 64
        elif changed[field] is None:
            changed[field] = {
                "family": "risk_state_v1",
                "generation_id": "1" * 64,
                "manifest_digest_sha256": "2" * 64,
            }
        else:
            changed[field]["generation_id"] = "1" * 64
        changed_generation, changed_digest = risk_contract_identities(
            changed, schema_name="risk_decision"
        )
        assert changed_generation != generation
        assert changed_digest != digest


def test_risk_manifest_digest_rejects_tampering():
    payload = _family_payload("risk_state")
    tampered = copy.deepcopy(payload)
    tampered["upstream_bindings"][0]["generation_id"] = "2" * 64
    with pytest.raises(ContractError, match="identity mismatch"):
        validate_risk_manifest("risk_state", tampered)


def test_risk_artifact_bytes_require_checksum_match(tmp_path: Path):
    payload = _family_payload("risk_state")
    content = b'{"row": 1}\n'
    path = "state.json"
    file_entry = payload["files"][0]
    file_entry["path"] = path
    file_entry["sha256"] = "0" * 64
    file_entry["byte_size"] = len(content)
    file_entry["serialization_profile"] = "json-canonical-v1"
    with pytest.raises(ContractError, match="tampered risk artifact bytes"):
        validate_risk_file_payload(payload, path=path, content=content)
    file_entry["byte_size"] = len(content) + 1
    with pytest.raises(ContractError, match="byte size mismatch"):
        validate_risk_file_payload(payload, path=path, content=content)


def test_risk_file_path_rejects_escape():
    for path in ("/state.json", "../state.json", "a/../../state.json"):
        with pytest.raises(ContractError, match="unsafe|non-canonical"):
            validate_risk_file_path(path)


def test_risk_review_decision_requires_external_trust_anchor():
    payload = _family_payload("risk_policy")
    subject = risk_policy_draft_subject(payload)
    review = build_risk_review_decision(
        review_type="risk_policy_activation",
        subject=subject,
        schema_name="risk_policy",
        review_status="approved",
        reviewer="repository-owner",
        errors=[],
        warnings=[],
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    verify_risk_review_decision(
        review,
        expected_review_type="risk_policy_activation",
        expected_subject_generation_id=subject["generation_id"],
        expected_subject_manifest_digest_sha256=subject["manifest_digest_sha256"],
        expected_subject_content_sha256=risk_stable_content_id(
            subject, schema_name="risk_policy"
        ),
    )
    with pytest.raises(ContractError, match="signature mismatch|unregistered"):
        verify_risk_review_decision(
            {**review, "review_signature_sha256": "f" * 128},
            expected_review_type="risk_policy_activation",
            expected_subject_generation_id=subject["generation_id"],
            expected_subject_manifest_digest_sha256=subject["manifest_digest_sha256"],
            expected_subject_content_sha256=risk_stable_content_id(
                subject, schema_name="risk_policy"
            ),
        )


def test_approved_policy_binds_verified_external_review_decision():
    policy = _family_payload("risk_policy")
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
    validate_risk_policy_governance(policy, review)
    with pytest.raises(ContractError):
        validate_risk_policy_governance(policy, {**review, "review_status": "rejected"})


def test_risk_rule_scope_compatibility_matrix():
    payload = _family_payload("risk_policy")
    validate_risk_policy_scope_compatibility(payload)
    cross_scope = copy.deepcopy(payload)
    cross_scope["policy_scope"] = "strategy"
    with pytest.raises(ContractError, match="incompatible|cross-scope"):
        validate_risk_policy_scope_compatibility(cross_scope)


def test_industry_limit_requires_membership_contract():
    payload = _family_payload("risk_policy")
    payload["rules"].append({
        **payload["rules"][0],
        "rule_id": "portfolio_industry_limit",
        "metric": "industry_weight",
        "threshold": 0.25,
    })
    with pytest.raises(ContractError, match="industry_membership_binding"):
        validate_industry_membership_prerequisite(payload)


def test_risk_event_sequence_rejects_duplicate_or_stale_entries():
    event = _family_payload("risk_event")
    with pytest.raises(ContractError):
        validate_risk_event_sequence([event, {**event, "event_id": event["event_id"]}])
    stale = copy.deepcopy(event)
    stale["event_id"] = "2" * 64
    stale["event_sequence_number"] = event["event_sequence_number"]
    stale["generation_id"], stale["manifest_digest_sha256"] = risk_contract_identities(
        stale, schema_name="risk_event"
    )
    with pytest.raises(ContractError, match="stale or duplicate"):
        validate_risk_event_sequence([event, stale])


def test_enforceable_de_risk_contract_fails_closed():
    payload = _family_payload("risk_de_risk_contract")
    with pytest.raises(ContractError, match="not enforceable"):
        validate_enforceable_de_risk_contract(payload, action="de_risk")
    payload["status"] = "approved"
    payload["execution_available"] = True
    validate_enforceable_de_risk_contract(payload, action="de_risk")
    with pytest.raises(ContractError, match="does not support"):
        validate_enforceable_de_risk_contract(payload, action="flatten")


def test_phase0_golden_identities_are_persisted():
    golden = json.loads(GOLDEN.read_text())
    assert golden["golden_version"] == 1
    for schema_name in RISK_CONTRACT_NAMES:
        if schema_name == "risk_review_trust_anchor":
            continue
        payload = _family_payload(schema_name)
        generation, digest = risk_contract_identities(payload, schema_name=schema_name)
        assert golden["identities"][schema_name] == {
            "generation_id": generation,
            "manifest_digest_sha256": digest,
        }
