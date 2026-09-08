from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

import pytest

from uq.contracts.model_layer import (
    ModelContractLoader,
    create_reviewed_quality_decision,
    paper_execution_identities,
)
from uq.errors import ContractError
from uq.research_chain.contracts import _QUALITY_BINDING_TYPES

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "config/schemas/fixtures/paper-execution"
GOLDEN = ROOT / "evidence/paper-execution/phase-0/identity-golden-vectors.json"
FAMILIES = ("execution_config", "order_plan", "execution_result", "paper_portfolio_state")
PAPER_BINDING_TYPES = {
    "execution_config_v1",
    "order_plan_v1",
    "execution_result_v1",
    "paper_portfolio_state_v1",
}


def _valid(family: str) -> dict:
    return json.loads((FIXTURES / f"{family}-valid.json").read_text())


@pytest.mark.parametrize("family", FAMILIES)
def test_representative_paper_contract_loads(family: str) -> None:
    payload = _valid(family)
    expected = paper_execution_identities(payload, schema_name=family)
    assert (payload["generation_id"], payload["manifest_digest_sha256"]) == expected
    assert ModelContractLoader.validate(family, payload) is None


@pytest.mark.parametrize(
    "path",
    sorted((FIXTURES).glob("*-negative-*.json")),
)
def test_paper_contract_negative_fixtures_fail_closed(path: Path) -> None:
    family = path.name.split("-negative-", 1)[0]
    payload = json.loads(path.read_text())
    with pytest.raises(ContractError):
        ModelContractLoader.validate(family, payload)


def test_every_paper_family_has_two_distinct_negative_fixtures() -> None:
    for family in FAMILIES:
        paths = sorted(FIXTURES.glob(f"{family}-negative-*.json"))
        assert len(paths) >= 2
        assert {json.loads(path.read_text()) != _valid(family) for path in paths} == {True}


def test_identity_golden_vectors_are_persisted_stable_and_sensitive() -> None:
    golden = json.loads(GOLDEN.read_text())
    for family in FAMILIES:
        payload = _valid(family)
        generation, digest = paper_execution_identities(payload, schema_name=family)
        assert golden["vectors"][family]["generation_id"] == generation
        assert golden["vectors"][family]["manifest_digest_sha256"] == digest
        metadata = copy.deepcopy(payload)
        metadata.update(
            run_id=str(uuid5(NAMESPACE_URL, "paper-execution-metadata")),
            created_at="2026-09-09T01:00:00+00:00",
            quality_report_checksum_sha256="f" * 64,
            generation_id="0" * 64,
            manifest_digest_sha256="0" * 64,
        )
        stable_generation, review_changed_digest = paper_execution_identities(
            metadata, schema_name=family
        )
        assert stable_generation == generation
        assert review_changed_digest != digest
        semantic = copy.deepcopy(payload)
        if family == "execution_config":
            semantic["lot_size"] += 100
        elif family == "order_plan":
            semantic["row_count"] += 1
        elif family == "execution_result":
            semantic["opening_cash"] += 1.0
        else:
            semantic["cash"] += 1.0
        semantic.update(generation_id="0" * 64, manifest_digest_sha256="0" * 64)
        assert paper_execution_identities(semantic, schema_name=family)[0] != generation


def test_paper_families_are_registered_for_quality_and_providers() -> None:
    from uq.contracts.model_layer import _MODEL_QUALITY_REVIEW_REGISTRY

    assert PAPER_BINDING_TYPES <= set(_MODEL_QUALITY_REVIEW_REGISTRY.bindings)
    assert PAPER_BINDING_TYPES <= _QUALITY_BINDING_TYPES
    for binding_type in PAPER_BINDING_TYPES:
        assert _MODEL_QUALITY_REVIEW_REGISTRY.bindings[binding_type]["policy"] == "reject_all"


def test_paper_config_can_bind_reviewed_quality_decision() -> None:
    from tests.review_key import REVIEWER_PRIVATE_KEY

    from uq.contracts.model_layer import bind_reviewed_quality_decision, verify_reviewed_quality_report_signature

    payload = _valid("execution_config")
    unsigned = {
        "binding_type": "execution_config_v1",
        "checks": [
            {
                "name": "schema_valid", "threshold": True, "observed": True,
                "level": "error", "result": "passed",
            },
            {
                "name": "state_mode_valid", "threshold": True, "observed": True,
                "level": "error", "result": "passed",
            },
            {
                "name": "market_data_binding_resolved", "threshold": True,
                "observed": True, "level": "error", "result": "passed",
            },
            {
                "name": "execution_policy_within_bounds", "threshold": True,
                "observed": True, "level": "error", "result": "passed",
            },
        ],
        "errors": [],
        "warnings": [],
        "key_id": "model-quality-reviewer-v1-2026-09",
        "policy": "reject_all",
        "producer_code_fingerprint": "a" * 64,
        "reviewer": "external-model-quality-reviewer-v1",
        "report_version": 2,
        "status": "passed",
    }
    from uq.contracts.model_layer import review_signature
    signature = review_signature(unsigned, private_key_pem=REVIEWER_PRIVATE_KEY)
    decision = {**unsigned, "review_signature_sha256": signature}
    report, checksum = bind_reviewed_quality_decision(
        decision,
        binding_type="execution_config_v1",
        subject_generation_id=payload["generation_id"],
        subject_content_sha256=payload["generation_id"],
    )
    verify_reviewed_quality_report_signature(report)
    assert checksum != payload["quality_report_checksum_sha256"]
    published = copy.deepcopy(payload)
    published["quality_report_checksum_sha256"] = checksum
    published.update(generation_id="0" * 64, manifest_digest_sha256="0" * 64)
    stable_generation, final_digest = paper_execution_identities(
        published, schema_name="execution_config"
    )
    assert stable_generation == payload["generation_id"]
    assert final_digest != payload["manifest_digest_sha256"]
