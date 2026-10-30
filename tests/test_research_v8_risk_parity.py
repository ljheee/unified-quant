from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.model_layer import (
    ModelContractLoader,
    RESEARCH_STAGE_ORDER_V3,
    _validate_stage_record_order,
    research_contract_identities,
)
from uq.backtest.execution_facts import ExecutionFactsStore, facts_binding
from uq.errors import ContractError
from uq.portfolio.risk_model import PortfolioRiskReturnsStore
from uq.research_chain.adapters import _synthesized_portfolio_definition
from uq.research_chain.contracts import research_request_schema_name
from uq.research_chain.resolver import (
    ResearchChainRequestResolver,
    ResearchResolutionError,
    ResolvedExecutionPlan,
    research_stage_plan_v6_sha256,
    stage_plan_for_request,
)
from uq.research_chain.runner import ResearchChainRunner
from tests.test_research_v7_lightgbm import _build_v7_request


def _build_v8_risk_request() -> dict:
    request = _build_v7_request()
    request["contract_version"] = 8
    request["schema_version"] = "8.0.0"
    template = request["portfolio_definition_template"]
    template.update({
        "contract_version": 3,
        "template_version": "3.0.0",
        "weight_scheme": "risk_parity",
        "scheme_parameters": {"n": 3, "lookback_sessions": 60},
        "risk_model_binding": {
            "family": "portfolio_risk_returns_v1",
            "generation_id": "a" * 64,
            "manifest_digest_sha256": "b" * 64,
        },
    })
    template["template_generation_id"] = "0" * 64
    template["template_manifest_digest_sha256"] = "0" * 64
    generation, digest = research_contract_identities(
        template, schema_name="portfolio_definition_template"
    )
    template["template_generation_id"] = generation
    template["template_manifest_digest_sha256"] = digest
    return _rebind_request(request)


def _rebind_request(request: dict) -> dict:
    template = request["portfolio_definition_template"]
    template["template_generation_id"] = "0" * 64
    template["template_manifest_digest_sha256"] = "0" * 64
    generation, digest = research_contract_identities(
        template, schema_name="portfolio_definition_template"
    )
    template["template_generation_id"] = generation
    template["template_manifest_digest_sha256"] = digest
    request["request_content_generation_id"] = "0" * 64
    request["manifest_digest_sha256"] = "0" * 64
    generation, digest = research_contract_identities(
        request, schema_name="research_run_request_v8"
    )
    request["request_content_generation_id"] = generation
    request["manifest_digest_sha256"] = digest
    return request


def test_v8_routes_reviewed_risk_parity_template_and_keeps_v6_stage_plan() -> None:
    request = _build_v8_risk_request()
    assert research_request_schema_name(request) == "research_run_request_v8"
    ModelContractLoader.validate("research_run_request_v8", request)
    assert stage_plan_for_request(request) == stage_plan_for_request({"contract_version": 7})
    plan = ResolvedExecutionPlan(
        request=request,
        request_manifest_digest_sha256=request["manifest_digest_sha256"],
        stage_plan_sha256=research_stage_plan_v6_sha256(),
        stage_bindings=(),
        resolved_execution_plan_sha256="c" * 64,
    )
    runner = object.__new__(ResearchChainRunner)
    runner._validate_plan(plan)
    definition = _synthesized_portfolio_definition(
        plan,
        template=request["portfolio_definition_template"],
        prediction_generation_id="d" * 64,
        universe_generation_id="e" * 64,
        created_at="2026-01-30T07:00:00+00:00",
    )
    ModelContractLoader.validate("portfolio_definition", definition)
    assert definition["contract_version"] == 3
    assert definition["risk_model_binding"] == request["portfolio_definition_template"]["risk_model_binding"]


def test_v8_result_uses_the_existing_paper_stage_order() -> None:
    records = [
        {"stage": stage, "status": "passed", "failure_reason": None, "output_bindings": []}
        for stage in RESEARCH_STAGE_ORDER_V3
    ]
    _validate_stage_record_order(
        records,
        require_complete=True,
        payload={
            "contract_version": 8,
            "final_status": "passed",
            "readback_status": {"test": "passed"},
        },
    )


def test_v8_resolver_checks_risk_return_binding_and_prior_calendar_coverage(tmp_path: Path) -> None:
    request = _build_v8_risk_request()
    sessions = [f"2026-01-0{day}" for day in range(1, 6)]
    template = request["portfolio_definition_template"]
    template["scheme_parameters"]["lookback_sessions"] = 2
    template["risk_model_binding"] = {
        "family": "portfolio_risk_returns_v1",
        "generation_id": "f" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    request["window_start_date"] = sessions[2]
    request["window_end_date"] = sessions[4]
    manifest = {
        "manifest_digest_sha256": "0" * 64,
        "calendar_sessions": sessions,
        "observation_visibility": "available_after_session_close",
        "strict_prior_session_policy": True,
    }
    class StubRiskStore:
        def read(self, generation_id):
            assert generation_id == "f" * 64
            return manifest, None

    resolver = ResearchChainRequestResolver({"portfolio_risk_returns": StubRiskStore()})
    resolver._verify_v8_risk_returns(request)
    manifest["manifest_digest_sha256"] = "1" * 64
    with pytest.raises(ResearchResolutionError, match="manifest digest"):
        resolver._verify_v8_risk_returns(request)


def test_v8_industry_neutrality_requires_the_backtest_bound_point_in_time_facts(
    tmp_path: Path,
) -> None:
    sessions = [f"2026-01-0{day}" for day in range(1, 6)]
    instruments = ["000001.XSHG", "000002.XSHE", "000003.XSHG"]
    rows = []
    for day in sessions:
        for index, instrument in enumerate(instruments):
            rows.append({
                "date": day,
                "instrument": instrument,
                "visible_at": f"{day}T00:00:00+00:00",
                "board": "main",
                "is_st": False,
                "suspended": False,
                "has_price_limit": True,
                "limit_up": 11.0,
                "limit_down": 9.0,
                "limit_reference_price": 10.0,
                "raw_previous_close": 10.0,
                "buy_lot": 100,
                "min_buy_shares": 100,
                "sell_lot": 100,
                "industry": "technology" if index < 2 else "healthcare",
            })
    facts_manifest = ExecutionFactsStore(tmp_path).publish(
        pd.DataFrame(rows), source_id="reviewed-industry-source",
        source_approval_ref="industry-review-001",
    )

    class StubConfigStore:
        root = tmp_path

        def read_manifest(self, generation_id):
            return {"execution_facts_binding": facts_binding(facts_manifest)}

    request = _build_v8_risk_request()
    template = request["portfolio_definition_template"]
    template.update({
        "weight_scheme": "industry_neutral",
        "scheme_parameters": {"n": 3},
        "risk_model_binding": None,
        "industry_source_binding": {
            "source_type": "governed_execution_facts",
            "manifest_generation_id": facts_manifest["generation_id"],
            "industry_column": "industry",
        },
    })
    request["window_start_date"] = sessions[1]
    request["window_end_date"] = sessions[3]
    _rebind_request(request)
    ModelContractLoader.validate("research_run_request_v8", request)
    resolver = ResearchChainRequestResolver({"backtest_config": StubConfigStore()})
    resolver._verify_v8_industry_binding(request)

    template["industry_source_binding"]["manifest_generation_id"] = "0" * 64
    with pytest.raises(ResearchResolutionError, match="differ from the reviewed backtest config"):
        resolver._verify_v8_industry_binding(request)


def test_risk_returns_store_refuses_a_calendar_binding_that_does_not_match(tmp_path: Path) -> None:
    class CalendarStore:
        def read_manifest_by(self, generation_id, *, dataset_type):
            return {"manifest_digest_sha256": "9" * 64, "data_checksum_sha256": "8" * 64}

    store = PortfolioRiskReturnsStore(tmp_path, calendar_store=CalendarStore())
    with pytest.raises(ContractError, match="calendar binding"):
        store.publish(
            __import__("pandas").DataFrame({
                "date": ["2026-01-01", "2026-01-02"],
                "instrument": ["000001.XSHG", "000001.XSHG"],
                "return": [0.01, 0.02],
            }),
            calendar_sessions=["2026-01-01", "2026-01-02"],
            calendar_generation_id="1" * 64,
            calendar_manifest_digest_sha256="2" * 64,
            calendar_data_checksum_sha256="3" * 64,
            source_id="offline",
            source_approval_ref="review-1",
        )
