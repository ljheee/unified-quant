from __future__ import annotations

import json
from pathlib import Path

import pytest

from uq.contracts.model_layer import (
    ModelContractLoader,
    research_contract_identities,
    research_stage_plan_v3_sha256,
    research_stage_plan_v4_sha256,
    research_stage_plan_v5_sha256,
)
from uq.errors import ContractError
from uq.execution.quality_checks import paper_binding_types, paper_quality_checks_for, validate_exact_quality_checks
from uq.execution.set_binding_resolver import build_set_binding, set_digest_sha256, verify_partition_set_binding
from uq.research_chain.contracts import verify_stage_plan_review

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "evidence/paper-execution-research-integration/phase-0r/golden/research-stage-plans.json"


def _load_golden() -> dict:
    assert GOLDEN.is_file(), f"missing golden vector file: {GOLDEN}"
    return json.loads(GOLDEN.read_text())


def test_research_stage_plan_golden_vectors_are_persisted() -> None:
    golden = _load_golden()
    assert golden["stage_plans"]["v3"] == research_stage_plan_v3_sha256()
    assert golden["stage_plans"]["v4"] == research_stage_plan_v4_sha256()
    assert golden["stage_plans"]["v5"] == research_stage_plan_v5_sha256()
    assert golden["stage_plans"]["v3"] != golden["stage_plans"]["v5"]


def test_paper_execution_template_v2_and_request_v5_validate() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from uq.contracts.gate_contracts import canonical_json

    template = {
        "template_version": "2.0.0",
        "status": "reviewed",
        "mode": "paper",
        "market": "cn_a",
        "state_mode_policy": "initial_then_continuation",
        "lot_size": 100,
        "quantity_policy": {
            "board_lot": 100, "minimum_order_quantity": 100, "partial_fills": False,
            "sell_before_buy": True, "enforce_t1": True, "minimum_holding_rule": "preserve_one_lot",
            "insufficient_cash_policy": "round_down_to_lot",
        },
        "fee_policy": {
            "buy_commission_rate": 0.0003, "sell_commission_rate": 0.0003,
            "sell_stamp_tax_rate": 0.001, "transfer_fee_rate": 0.00001,
            "minimum_commission": 5.0, "currency_precision": 2,
        },
        "price_policy": {
            "order_pricing_basis": "decision_close_estimate",
            "limit_tolerance_absolute": 0.01, "limit_tolerance_relative": 0.001,
            "execution_window": "open",
        },
        "initial_state": {"cash": 1000000.0, "holdings": [], "valuation_price_basis": "decision_close_estimate"},
        "market_data_binding": {
            "family": "bars_daily_v2", "window_start": "2025-01-01", "window_end": "2025-12-31",
            "set_digest_sha256": "a" * 64, "member_count": 1,
        },
        "calendar_binding": {
            "family": "trading_calendar_snapshot_v1", "window_start": "2025-01-01",
            "window_end": "2025-12-31", "set_digest_sha256": "b" * 64, "member_count": 1,
        },
        "suspension_binding": {
            "family": "suspension_snapshot_v1", "window_start": "2025-01-01",
            "window_end": "2025-12-31", "set_digest_sha256": "c" * 64, "member_count": 1,
        },
        "corporate_action_binding": {
            "family": "dividend_event_v1", "window_start": "2025-01-01",
            "window_end": "2025-12-31", "set_digest_sha256": "d" * 64, "member_count": 1,
        },
        "risk_policy_binding": {
            "family": "risk_policy_v1", "generation_id": "e" * 64, "manifest_digest_sha256": "f" * 64,
        },
        "data_delay_policy": "delayed_replay",
        "data_delay_reason": "T+1 execution on open",
        "serialization_profiles": {"manifest": "json-canonical-v1", "payload": "parquet-v1"},
    }
    ModelContractLoader.validate("paper_execution_template_v2", template)


def test_request_v5_validates() -> None:
    fixture_dir = ROOT / "evidence/paper-execution-research-integration/phase-0r/fixtures"
    request_path = fixture_dir / "research_run_request_v5-valid.json"
    assert request_path.is_file()
    request = json.loads(request_path.read_text())
    ModelContractLoader.validate("research_run_request_v5", request)
    assert request["request_content_generation_id"] == research_contract_identities(
        request, schema_name="research_run_request_v5"
    )[0]


def test_request_v5_rejects_superseded_stage_plan() -> None:
    fixture_dir = ROOT / "evidence/paper-execution-research-integration/phase-0r/fixtures"
    neg1_path = fixture_dir / "research_run_request_v5-negative-v3-review-type.json"
    neg2_path = fixture_dir / "research_run_request_v5-negative-wrong-stage-plan.json"
    assert neg1_path.is_file() and neg2_path.is_file()
    neg1 = json.loads(neg1_path.read_text())
    with pytest.raises(ContractError):
        ModelContractLoader.validate("research_run_request_v5", neg1)
    neg2 = json.loads(neg2_path.read_text())
    with pytest.raises(ContractError, match="stage plan review subject mismatch"):
        verify_stage_plan_review(
            neg2["stage_plan_review"], stage_plan_sha256=neg2["stage_plan_sha256"]
        )
    # The refusal checks never exercise the fixtures' own identities; lock them
    # so an edit without an identity refresh fails instead of silently drifting.
    for negative in (neg1, neg2):
        generation, digest = research_contract_identities(
            negative, schema_name="research_run_request_v5"
        )
        assert negative["request_content_generation_id"] == generation
        assert negative["manifest_digest_sha256"] == digest


def test_request_v5_rejects_v1_template() -> None:
    template_v1_path = ROOT / "config/schemas/contracts/paper_execution_template.v1.json"
    assert template_v1_path.is_file()
    v1_schema = json.loads(template_v1_path.read_text())
    assert "mode" not in v1_schema.get("properties", {})


def test_request_v5_stage_plan_digest_is_distinct() -> None:
    v5_digest = research_stage_plan_v5_sha256()
    assert v5_digest != research_stage_plan_v3_sha256()
    assert v5_digest != research_stage_plan_v4_sha256()


def test_state_and_result_widening_is_compatibility_preserving() -> None:
    state_schema_path = ROOT / "config/schemas/contracts/research_run_state.v1.json"
    result_schema_path = ROOT / "config/schemas/contracts/research_run_result.v1.json"
    state_schema = json.loads(state_schema_path.read_text())
    result_schema = json.loads(result_schema_path.read_text())
    assert state_schema["properties"]["contract_version"]["enum"] == [1, 3, 4, 5, 6]
    assert result_schema["properties"]["contract_version"]["enum"] == [1, 3, 4, 5, 6]


def test_set_binding_digest_recomputation_and_drift() -> None:
    triples = [
        {"session_or_decision_date": "2025-06-02", "generation_id": "a" * 64, "data_checksum_sha256": "b" * 64},
        {"session_or_decision_date": "2025-06-03", "generation_id": "c" * 64, "data_checksum_sha256": "d" * 64},
    ]
    binding = build_set_binding(
        triples, family="bars_daily_v2", window_start="2025-06-01", window_end="2025-06-30",
    )
    assert binding["set_digest_sha256"] == set_digest_sha256(triples)
    verify_partition_set_binding(binding, triples, family="bars_daily_v2")

    tampered = [dict(t) for t in triples]
    tampered[1]["data_checksum_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="partition set digest mismatch"):
        verify_partition_set_binding(binding, tampered, family="bars_daily_v2")


def test_paper_quality_checks_match_registry_allowed_checks() -> None:
    types = paper_binding_types()
    assert len(types) == 4
    for binding_type in types:
        checks = paper_quality_checks_for(binding_type)
        assert checks
    report = {"checks": [{"name": check} for check in paper_quality_checks_for("execution_config_v1")]}
    validate_exact_quality_checks(report, "execution_config_v1")
    with pytest.raises(ContractError, match="do not exactly match"):
        bad = {"checks": [{"name": "wrong_check"}]}
        validate_exact_quality_checks(bad, "execution_config_v1")


def test_template_v2_requires_initial_state_or_bound_state() -> None:
    schema_path = ROOT / "config/schemas/contracts/paper_execution_template_v2.json"
    schema = json.loads(schema_path.read_text())
    assert "initial_state" in schema["required"]
    initial_state = schema["properties"]["initial_state"]
    assert "oneOf" in initial_state or "anyOf" in initial_state


def test_paper_execution_negative_fixtures_fail_closed() -> None:
    fixtures_dir = ROOT / "evidence/paper-execution-research-integration/phase-0r/fixtures"
    valid_path = fixtures_dir / "paper_execution_template_v2-valid.json"
    assert valid_path.is_file(), f"missing valid fixture: {valid_path}"
    valid = json.loads(valid_path.read_text())
    ModelContractLoader.validate("paper_execution_template_v2", valid)

    neg1_path = fixtures_dir / "paper_execution_template_v2-negative-unknown-field.json"
    neg2_path = fixtures_dir / "paper_execution_template_v2-negative-tampered-set-digest.json"
    assert neg1_path.is_file() and neg2_path.is_file()
    for neg_path in (neg1_path, neg2_path):
        neg = json.loads(neg_path.read_text())
        if "unknown_field" in neg:
            with pytest.raises(ContractError):
                ModelContractLoader.validate("paper_execution_template_v2", neg)
        else:
            binding = neg["market_data_binding"]
            triples = [
                {"session_or_decision_date": "2025-06-02", "generation_id": "a" * 64, "data_checksum_sha256": "b" * 64},
            ]
            with pytest.raises(ContractError, match="partition set digest mismatch"):
                verify_partition_set_binding(binding, triples, family=binding["family"])
