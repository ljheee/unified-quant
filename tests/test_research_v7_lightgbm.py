"""Research request v7 routes reviewed LightGBM definitions through training."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from uq.contracts.model_layer import (
    ModelContractLoader,
    research_contract_identities,
    research_stage_plan_v6_sha256,
)
from uq.errors import ContractError
from uq.research_chain.contracts import research_request_schema_name, verify_stage_plan_review
from uq.research_chain.adapters import _synthesized_portfolio_definition
from uq.research_chain.resolver import ResolvedExecutionPlan, stage_plan_for_request
from uq.research_chain.runner import ResearchChainRunner

ROOT = Path(__file__).resolve().parents[1]
V5_REQUEST = ROOT / (
    "evidence/paper-execution-research-integration/phase-0r/fixtures/"
    "research_run_request_v5-valid.json"
)


def _build_v7_request() -> dict:
    request = json.loads(V5_REQUEST.read_text())
    request["contract_version"] = 7
    request["schema_version"] = "7.0.0"
    request["sample_construction_binding"] = {
        "family": "sample_construction",
        "generation_id": "a" * 64,
        "manifest_digest_sha256": "b" * 64,
    }
    request["stage_plan_sha256"] = research_stage_plan_v6_sha256()
    review_unsigned = {
        "review_type": "research_stage_plan_v6_activation",
        "subject_generation_id": research_stage_plan_v6_sha256(),
        "subject_manifest_digest_sha256": research_stage_plan_v6_sha256(),
        "review_status": "approved",
        "reviewer": "test-v7-request",
        "key_id": "research-stage-plan-reviewer-v6-test-2026-10",
    }
    test_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("02" * 32))
    request["stage_plan_review"] = {
        **review_unsigned,
        "review_signature_sha256": test_key.sign(
            json.dumps(review_unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hex(),
    }
    template = request["model_definition_template"]
    template.update({
        "contract_version": 2,
        "template_version": "2.0.0",
        "algorithm": "qlib_lightgbm",
        "serializer_version": "joblib-v1",
    })
    template["template_generation_id"] = "0" * 64
    template["template_manifest_digest_sha256"] = "0" * 64
    template_generation, template_digest = research_contract_identities(
        template, schema_name="model_definition_template"
    )
    template["template_generation_id"] = template_generation
    template["template_manifest_digest_sha256"] = template_digest
    portfolio_template = request["portfolio_definition_template"]
    portfolio_template.update({
        "contract_version": 2,
        "template_version": "2.0.0",
        "weight_scheme": "score_weighted",
        "scheme_parameters": {"n": 3, "temperature": 0.5},
    })
    portfolio_template["template_generation_id"] = "0" * 64
    portfolio_template["template_manifest_digest_sha256"] = "0" * 64
    portfolio_generation, portfolio_digest = research_contract_identities(
        portfolio_template, schema_name="portfolio_definition_template"
    )
    portfolio_template["template_generation_id"] = portfolio_generation
    portfolio_template["template_manifest_digest_sha256"] = portfolio_digest
    request["request_content_generation_id"] = "0" * 64
    request["manifest_digest_sha256"] = "0" * 64
    generation, digest = research_contract_identities(
        request, schema_name="research_run_request_v7"
    )
    request["request_content_generation_id"] = generation
    request["manifest_digest_sha256"] = digest
    return request


def test_v7_request_selects_lightgbm_and_reuses_the_unchanged_v6_stage_plan() -> None:
    request = _build_v7_request()
    assert research_request_schema_name(request) == "research_run_request_v7"
    ModelContractLoader.validate("research_run_request_v7", request)
    verify_stage_plan_review(
        request["stage_plan_review"], stage_plan_sha256=request["stage_plan_sha256"]
    )
    plan = ResolvedExecutionPlan(
        request=request,
        request_manifest_digest_sha256=request["manifest_digest_sha256"],
        stage_plan_sha256=request["stage_plan_sha256"],
        stage_bindings=(),
        resolved_execution_plan_sha256="0" * 64,
    )
    runner = object.__new__(ResearchChainRunner)
    runner._validate_plan(plan)
    assert stage_plan_for_request(request) == stage_plan_for_request({"contract_version": 6})
    assert request["model_definition_template"]["algorithm"] == "qlib_lightgbm"
    definition = _synthesized_portfolio_definition(
        plan,
        template=request["portfolio_definition_template"],
        prediction_generation_id="c" * 64,
        universe_generation_id="d" * 64,
        created_at="2026-01-30T07:00:00+00:00",
    )
    ModelContractLoader.validate("portfolio_definition", definition)
    assert definition["contract_version"] == 2
    assert definition["weight_scheme"] == "score_weighted"


def test_v7_cli_dry_run_routes_and_publishes_the_v7_request(tmp_path: Path) -> None:
    from uq.research_chain.cli import _dry_run

    request = _build_v7_request()
    result = _dry_run(request, data_root=tmp_path)
    assert result["status"] == "dry_run_published"
    assert Path(result["manifest_path"]).is_file()


def test_v7_model_stage_builds_a_v2_lightgbm_definition() -> None:
    request = _build_v7_request()
    bindings = [
        SimpleNamespace(output_family=family, generation_id=(str(index) * 64)[:64],
                        manifest_digest_sha256="1" * 64)
        for index, family in enumerate((
            "model_dataset", "label_set", "universe_snapshot", "factor_partition",
            "feature_preprocessing", "qlib_dataset_export", "qlib_init_receipt",
        ), start=1)
    ]
    plan = SimpleNamespace(request=request, stage_bindings=bindings)

    class _PreprocessingStore:
        @staticmethod
        def read_manifest(_generation: str) -> dict:
            return {
                "output_feature_schema_generation_id": "a" * 64,
                "ordered_features": ["feature_a", "feature_b"],
            }

    class _Exporter:
        @staticmethod
        def read(_dataset_name: str, _generation: str):
            return ({"generation_id": "b" * 64}, None)

    class _ModelAdapter:
        last_result = None

        def run(self, *args, **kwargs):
            self.definition = kwargs["definition"]
            return SimpleNamespace()

    runner = object.__new__(ResearchChainRunner)
    runner.dataset_adapter = SimpleNamespace(preprocessing_store=_PreprocessingStore())
    runner.export_adapter = SimpleNamespace(exporter=_Exporter(), last_result=None)
    runner.model_adapter = _ModelAdapter()
    runner._read_receipt = lambda _binding: {"generation_id": "c" * 64}
    runner._run_model(
        plan,
        runner_identity={"code_fingerprint": "d" * 64},
        quality_decisions={"model_run": {}},
        created_at="2026-01-30T07:00:00+00:00",
        artifact_decision_provider=lambda _generation: {},
    )
    assert runner.model_adapter.definition["contract_version"] == 2
    assert runner.model_adapter.definition["algorithm"] == "qlib_lightgbm"
    assert runner.model_adapter.definition["serializer_version"] == "joblib-v1"


def test_v7_rejects_linear_or_v1_model_templates() -> None:
    request = _build_v7_request()
    request["model_definition_template"]["algorithm"] = "qlib_linear"
    request["manifest_digest_sha256"] = "0" * 64
    request["request_content_generation_id"] = "0" * 64
    generation, digest = research_contract_identities(
        request, schema_name="research_run_request_v7"
    )
    request["request_content_generation_id"] = generation
    request["manifest_digest_sha256"] = digest
    with pytest.raises(ContractError):
        ModelContractLoader.validate("research_run_request_v7", request)

    old_template = copy.deepcopy(request["model_definition_template"])
    old_template["contract_version"] = 1
    old_template["template_version"] = "1.0.0"
    with pytest.raises(ContractError):
        # The request schema pins the v2 template shape even when the embedded
        # template's own values appear internally consistent.
        from uq.contracts.gate_contracts import validate_contract

        validate_contract("model_definition_template.v2.json", old_template)
