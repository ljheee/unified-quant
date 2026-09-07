"""Production trust-anchor guard tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uq.contracts.gate_contracts import canonical_json
from uq.contracts.model_layer import ModelQualityReviewTrustAnchor
from uq.errors import ContractError
from uq.runtime import current_runtime_mode, require_production_review_key
from uq.risk.contracts import verify_risk_review_decision
from uq.research_chain.contracts import verify_stage_plan_review
from tests.review_key import REVIEWER_PRIVATE_KEY
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
TEST_KEY_HEX = "3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29"


def _sign(payload: dict) -> str:
    key = serialization.load_pem_private_key(
        REVIEWER_PRIVATE_KEY.read_text().encode(), password=None
    )
    assert isinstance(key, Ed25519PrivateKey)
    return key.sign(canonical_json(payload)).hex()


def test_runtime_mode_requires_declared_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UQ_RUNTIME_MODE", raising=False)
    assert current_runtime_mode() == "research"
    monkeypatch.setenv("UQ_RUNTIME_MODE", "production")
    assert current_runtime_mode() == "production"
    monkeypatch.setenv("UQ_RUNTIME_MODE", "broker")
    with pytest.raises(ContractError, match="UQ_RUNTIME_MODE"):
        current_runtime_mode()


def test_repository_test_key_is_rejected_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UQ_RUNTIME_MODE", "research")
    require_production_review_key(TEST_KEY_HEX, context="test")
    monkeypatch.setenv("UQ_RUNTIME_MODE", "production")
    with pytest.raises(ContractError, match="test-mode Ed25519 trust anchor"):
        require_production_review_key(TEST_KEY_HEX, context="test")


def test_risk_review_fails_closed_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = json.loads((ROOT / "config/schemas/fixtures/risk/risk_policy-valid.json").read_text())
    review = json.loads((ROOT / "config/schemas/fixtures/risk/risk_review_decision-valid.json").read_text())
    review["review_signature_sha256"] = _sign({
        key: review[key] for key in (
            "contract_version", "schema_version", "review_type",
            "subject_generation_id", "subject_manifest_digest_sha256",
            "subject_content_sha256", "review_status", "reviewer", "key_id",
            "policy", "reviewed_at_utc", "errors", "warnings",
        )
    })
    monkeypatch.setenv("UQ_RUNTIME_MODE", "research")
    verify_risk_review_decision(
        review,
        expected_review_type=review["review_type"],
        expected_subject_generation_id=review["subject_generation_id"],
        expected_subject_manifest_digest_sha256=review["subject_manifest_digest_sha256"],
        expected_subject_content_sha256=review["subject_content_sha256"],
    )
    monkeypatch.setenv("UQ_RUNTIME_MODE", "production")
    with pytest.raises(ContractError, match="test-mode Ed25519 trust anchor"):
        verify_risk_review_decision(
            review,
            expected_review_type=review["review_type"],
            expected_subject_generation_id=review["subject_generation_id"],
            expected_subject_manifest_digest_sha256=review["subject_manifest_digest_sha256"],
            expected_subject_content_sha256=review["subject_content_sha256"],
        )


def test_model_quality_anchor_fails_closed_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UQ_RUNTIME_MODE", "research")
    ModelQualityReviewTrustAnchor()
    monkeypatch.setenv("UQ_RUNTIME_MODE", "production")
    with pytest.raises(ContractError, match="test-mode Ed25519 trust anchor"):
        ModelQualityReviewTrustAnchor()
