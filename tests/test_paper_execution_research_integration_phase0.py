from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tests.review_key import REVIEWER_V3_PRIVATE_KEY

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from uq.contracts.gate_contracts import canonical_json
from uq.contracts.model_layer import (
    ModelContractLoader,
    research_contract_identities,
    research_stage_plan_v3_sha256,
)
from uq.errors import ContractError
from uq.research_chain.contracts import verify_stage_plan_review

ROOT = Path(__file__).resolve().parents[1]
V3_FIXTURES = ROOT / "evidence/paper-execution-research-integration/phase-0/fixtures"


def _load(name: str) -> dict:
    path = V3_FIXTURES / name
    assert path.is_file(), f"missing persisted fixture: {path}"
    return json.loads(path.read_text())


def _sign_v3_review(stage_plan_sha256: str, *, review_type: str = "research_stage_plan_v3_activation") -> dict:
    unsigned = {
        "review_type": review_type,
        "subject_generation_id": stage_plan_sha256,
        "subject_manifest_digest_sha256": stage_plan_sha256,
        "review_status": "approved",
        "reviewer": "repository-owner",
        "key_id": "research-stage-plan-reviewer-v3-2026-09",
    }
    key = serialization.load_pem_private_key(REVIEWER_V3_PRIVATE_KEY.read_text().encode(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    signature = key.sign(canonical_json(unsigned)).hex()
    return {**unsigned, "review_signature_sha256": signature}


def test_v3_request_contract_and_fixtures_validate() -> None:
    valid = _load("research_run_request_v3-valid.json")
    ModelContractLoader.validate("research_run_request_v3", valid)
    assert valid["request_content_generation_id"] == research_contract_identities(
        valid, schema_name="research_run_request_v3"
    )[0]

    with pytest.raises(ContractError):
        ModelContractLoader.validate(
            "research_run_request_v3",
            _load("research_run_request_v3-negative-unknown-template-field.json"),
        )
    with pytest.raises(ContractError):
        ModelContractLoader.validate(
            "research_run_request_v3",
            _load("research_run_request_v3-negative-missing-calendar-binding.json"),
        )


def test_v3_request_rejects_schema_version_2_review_type() -> None:
    request = _load("research_run_request_v3-valid.json")
    request["stage_plan_review"]["review_type"] = "research_stage_plan_v2_activation"
    with pytest.raises(ContractError):
        ModelContractLoader.validate("research_run_request_v3", request)


def test_v3_stage_plan_digest_is_stable_and_sensitive() -> None:
    expected = research_stage_plan_v3_sha256()
    canonical_payload = json.dumps(
        {
            "schema_version": "v3",
            "stage_plan": [
                "resolve_request", "factor_computation", "dataset_preparation", "qlib_export",
                "model_training", "prediction_publication", "portfolio_construction",
                "backtest_execution", "paper_execution", "result_reconciliation",
            ],
        },
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    assert expected == hashlib.sha256(canonical_payload).hexdigest()
    assert expected not in {"0" * 64, research_stage_plan_v3_sha256.__doc__}


def test_v3_activation_review_is_verified_and_sensitive() -> None:
    stage_plan = research_stage_plan_v3_sha256()
    review = _sign_v3_review(stage_plan)
    verify_stage_plan_review(review, stage_plan_sha256=stage_plan)
    with pytest.raises(ContractError, match="signature mismatch"):
        verify_stage_plan_review({**review, "review_signature_sha256": "f" * 128}, stage_plan_sha256=stage_plan)
    with pytest.raises(ContractError, match="subject mismatch"):
        verify_stage_plan_review(review, stage_plan_sha256="0" * 64)
    wrong_type = {
        **_sign_v3_review(stage_plan),
        "review_type": "research_stage_plan_v2_activation",
        "key_id": "research-stage-plan-reviewer-v1-2026-09",
    }
    with pytest.raises(ContractError, match="signature mismatch"):
        verify_stage_plan_review(wrong_type, stage_plan_sha256=stage_plan)


def test_v3_request_rejects_v2_stage_plan_digest() -> None:
    request = _load("research_run_request_v3-valid.json")
    v2_digest = hashlib.sha256(json.dumps(
        {
            "schema_version": "v2",
            "stage_plan": [
                "resolve_request", "factor_computation", "dataset_preparation", "qlib_export",
                "model_training", "prediction_publication", "portfolio_construction",
                "backtest_execution", "result_reconciliation",
            ],
        },
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    request["stage_plan_sha256"] = v2_digest
    request["stage_plan_review"] = _sign_v3_review(v2_digest)
    request["request_content_generation_id"] = "0" * 64
    request["manifest_digest_sha256"] = "0" * 64
    request["request_content_generation_id"], request["manifest_digest_sha256"] = research_contract_identities(
        request, schema_name="research_run_request_v3"
    )
    from uq.research_chain.resolver import ResearchChainRequestResolver
    with pytest.raises(Exception) as excinfo:
        ResearchChainRequestResolver({}).resolve(request)
    assert "stage plan digest" in str(excinfo.value)
