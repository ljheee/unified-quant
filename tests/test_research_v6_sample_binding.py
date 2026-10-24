"""v6 is v5 plus an explicit sample-construction binding.

The version boundary is the point: a v5 request must not acquire label-aware
row selection by editing its existing preprocessing binding, and a v6 request
must not execute without the selected-sample artifact.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from uq.contracts.model_layer import (
    ModelContractLoader,
    research_contract_identities,
    research_stage_plan_v6_sha256,
)
from uq.errors import ContractError
from uq.research_chain.contracts import research_request_schema_name

ROOT = Path(__file__).resolve().parents[1]
V5 = ROOT / "evidence/paper-execution-research-integration/phase-0r/fixtures/research_run_request_v5-valid.json"


def _v6_request() -> dict:
    request = json.loads(V5.read_text())
    request["contract_version"] = 6
    request["schema_version"] = "6.0.0"
    request["sample_construction_binding"] = {
        "family": "sample_construction",
        "generation_id": "a" * 64,
        "manifest_digest_sha256": "b" * 64,
    }
    request["stage_plan_sha256"] = research_stage_plan_v6_sha256()
    request["stage_plan_review"]["review_type"] = "research_stage_plan_v6_activation"
    request["request_content_generation_id"] = "0" * 64
    request["manifest_digest_sha256"] = "0" * 64
    generation, digest = research_contract_identities(
        request, schema_name="research_run_request_v6"
    )
    request["request_content_generation_id"] = generation
    request["manifest_digest_sha256"] = digest
    return request


def test_v6_routes_to_its_own_schema_and_requires_the_sample_binding() -> None:
    request = _v6_request()
    assert research_request_schema_name(request) == "research_run_request_v6"
    ModelContractLoader.validate("research_run_request_v6", request)
    missing = copy.deepcopy(request)
    missing.pop("sample_construction_binding")
    missing["request_content_generation_id"] = "0" * 64
    missing["manifest_digest_sha256"] = "0" * 64
    generation, digest = research_contract_identities(
        missing, schema_name="research_run_request_v6"
    )
    missing["request_content_generation_id"] = generation
    missing["manifest_digest_sha256"] = digest
    with pytest.raises(ContractError, match="sample_construction_binding"):
        ModelContractLoader.validate("research_run_request_v6", missing)


def test_v5_cannot_claim_v6_semantics() -> None:
    request = json.loads(V5.read_text())
    request["sample_construction_binding"] = {
        "family": "sample_construction",
        "generation_id": "a" * 64,
        "manifest_digest_sha256": "b" * 64,
    }
    with pytest.raises(ContractError, match="Additional properties"):
        ModelContractLoader.validate("research_run_request_v5", request)


def test_v6_and_v5_have_distinct_stage_plan_digests() -> None:
    from uq.contracts.model_layer import research_stage_plan_v5_sha256

    assert research_stage_plan_v6_sha256() != research_stage_plan_v5_sha256()
