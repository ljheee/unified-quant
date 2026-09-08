from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

import pandas as pd
import pytest

from uq.contracts.model_layer import (
    ModelContractLoader,
    create_reviewed_quality_decision,
    review_signature,
)
from uq.errors import ContractError
from uq.execution.planner import PaperOrderPlanner
from uq.execution.stores import ExecutionConfigStore, OrderPlanStore
from uq.risk.contracts import risk_contract_identities
from uq.risk.publication import ConfigPublicationBinding
from tests.review_key import REVIEWER_PRIVATE_KEY

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "config/schemas/fixtures/paper-execution"
RISK_FIXTURE = ROOT / "config/schemas/fixtures/risk/risk_decision-valid.json"


def _config() -> dict:
    return json.loads((FIXTURES / "execution_config-valid.json").read_text())


def _recompute(config: dict) -> dict:
    from uq.contracts.model_layer import paper_execution_identities
    config["generation_id"], config["manifest_digest_sha256"] = paper_execution_identities(
        config, schema_name="execution_config"
    )
    return config


def _prices() -> dict:
    return {
        "000001.XSHE": {"open": 10.2, "high": 10.4, "low": 10.1, "close": 10.0, "volume": 1000000.0, "status": "trading", "limit_up": 11.0, "limit_down": 9.0}
    }


def _target() -> pd.DataFrame:
    return pd.DataFrame({"instrument": ["000001.XSHE"], "weight": [0.5]})


def _decision(config: dict) -> dict:
    decision = json.loads(RISK_FIXTURE.read_text())
    decision["decision_scope"] = "order_submission"
    decision["action"] = "allow"
    decision["binding_config_generation_id"] = config["generation_id"]
    return decision


def _unsigned(family: str, checks: list[str], generation: str) -> dict:
    return {
        "binding_type": family,
        "checks": [
            {"name": name, "threshold": True, "observed": True, "level": "error", "result": "passed"}
            for name in checks
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


def _decision_for_review(unsigned: dict, binding_type: str, generation: str) -> dict:
    unsigned = copy.deepcopy(unsigned)
    unsigned["binding_type"] = binding_type
    signature = review_signature(unsigned, private_key_pem=REVIEWER_PRIVATE_KEY)
    return {**unsigned, "review_signature_sha256": signature}


def _publish_config(tmp_path: Path) -> tuple[dict, Path]:
    store = ExecutionConfigStore(tmp_path)
    config = _config()
    risk = _decision(config)
    risk["binding_config_generation_id"] = config["generation_id"]
    risk["generation_id"], risk["manifest_digest_sha256"] = risk_contract_identities(
        risk, schema_name="risk_decision"
    )
    config["risk_decision_binding"] = {
        "family": "risk_decision_v1",
        "generation_id": risk["generation_id"],
        "manifest_digest_sha256": risk["manifest_digest_sha256"],
    }
    config = _recompute(config)
    generation = config["generation_id"]
    unsigned = _unsigned("execution_config_v1", [
        "schema_valid", "market_data_binding_resolved", "state_mode_valid", "execution_policy_within_bounds"
    ], generation)
    decision = _decision_for_review(unsigned, "execution_config_v1", generation)
    binding = ConfigPublicationBinding(decision=risk, config_generation_id=generation)
    store.publish(config, quality_decision=decision, risk_decision=risk, config_publication=binding)
    readback = store.read(config["generation_id"])
    assert readback["execution_id"] == config["execution_id"]
    return readback, store.directory / config["execution_id"] / f"generation={generation}"



def test_order_plan_is_deterministic_sell_before_buy(tmp_path: Path) -> None:
    config = _config()
    config["state_mode"] = "continuation"
    config["input_state_binding"] = {
        "family": "paper_portfolio_state_v1", "generation_id": "1" * 64,
        "manifest_digest_sha256": "2" * 64,
        "as_of_date": config["decision_date"],
    }
    config["initial_state"] = None
    holdings = [{
        "instrument": "000001.XSHE", "quantity": 1200, "average_cost": 9.0,
        "buy_locked_quantity": 200, "sellable_quantity": 1000,
    }, {
        "instrument": "600000.XSHG", "quantity": 1000, "average_cost": 8.0,
        "buy_locked_quantity": 0, "sellable_quantity": 1000,
    }]
    config = _recompute(config)
    prices = _prices()
    prices["600000.XSHG"] = {"open": 8.2, "high": 8.4, "low": 8.1, "close": 8.0, "volume": 1000000.0, "status": "trading", "limit_up": 8.8, "limit_down": 7.2}
    target = pd.DataFrame({"instrument": ["000001.XSHE"], "weight": [0.7]})
    frame, residual = PaperOrderPlanner().plan(config, target, holdings, prices, opening_cash=100000.0)
    sells = frame[frame["side"] == "sell"]
    buys = frame[frame["side"] == "buy"]
    assert not sells.empty and not buys.empty
    assert frame.iloc[0]["side"] == "sell"
    assert frame["order_state"].eq("planned").all()
    assert frame["requested_quantity"].mod(100).eq(0).all()
    assert sells["limit_price"].eq(8.0).all() and buys["limit_price"].eq(10.0).all()
    assert sells["requested_quantity"].le(sells["sellable_quantity"]).all()
    assert residual == list(frame.loc[frame["side"] == "buy", "plan_sequence"])


def test_t1_locked_inventory_cannot_be_sold_on_same_session() -> None:
    config = _config()
    config["state_mode"] = "continuation"
    config["input_state_binding"] = {
        "family": "paper_portfolio_state_v1", "generation_id": "1" * 64,
        "manifest_digest_sha256": "2" * 64, "as_of_date": config["execution_date"],
    }
    config["initial_state"] = None
    config = _recompute(config)
    ModelContractLoader.validate("execution_config", config)
    holdings = [{
        "instrument": "000001.XSHE", "quantity": 1000, "average_cost": 9.0,
        "buy_locked_quantity": 1000, "sellable_quantity": 1000,
    }]
    # Force a sell target by removing the target row; planner closes to zero.
    target = pd.DataFrame({"instrument": ["000001.XSHE"], "weight": [0.0]})
    frame, _ = PaperOrderPlanner().plan(config, target, holdings, _prices(), opening_cash=100000.0)
    assert frame["side"].eq("sell").all()
    assert frame["sellable_quantity"].eq(0).all()
    assert frame["requested_quantity"].eq(0).all()


def test_insufficient_cash_rounds_buy_down_to_lot() -> None:
    config = _config()
    config["initial_state"]["cash"] = 100.0
    config["initial_state"]["holdings"] = []
    config = _recompute(config)
    frame, _ = PaperOrderPlanner().plan(
        config, _target(), None, _prices(), opening_cash=100.0,
    )
    # The target delta is represented even when no lot is feasible.
    assert len(frame) == 1
    assert frame["requested_quantity"].eq(0).all()


def test_insufficient_cash_reject_policy_fails_closed() -> None:
    config = _config()
    config["quantity_policy"]["insufficient_cash_policy"] = "reject"
    config["initial_state"]["cash"] = 100.0
    config["initial_state"]["holdings"] = []
    config = _recompute(config)
    target = pd.DataFrame({"instrument": ["000001.XSHE"], "weight": [1.0]})
    prices = {"000001.XSHE": {"close": 1.0}}
    with pytest.raises(ContractError, match="insufficient cash"):
        PaperOrderPlanner().plan(config, target, None, prices, opening_cash=100.0)


def test_risk_rejection_blocks_config_publication(tmp_path: Path) -> None:
    store = ExecutionConfigStore(tmp_path)
    config = _config()
    decision = json.loads(RISK_FIXTURE.read_text())
    decision["decision_scope"] = "order_submission"
    decision["action"] = "block"
    decision["binding_config_generation_id"] = config["generation_id"]
    decision["generation_id"], decision["manifest_digest_sha256"] = risk_contract_identities(
        decision, schema_name="risk_decision"
    )
    config["risk_decision_binding"] = {
        "family": "risk_decision_v1",
        "generation_id": decision["generation_id"],
        "manifest_digest_sha256": decision["manifest_digest_sha256"],
    }
    config = _recompute(config)
    generation = config["generation_id"]
    unsigned = _unsigned("execution_config_v1", [
        "schema_valid", "market_data_binding_resolved", "state_mode_valid", "execution_policy_within_bounds"
    ], generation)
    quality = _decision_for_review(unsigned, "execution_config_v1", generation)
    binding = ConfigPublicationBinding(
        decision=decision, config_generation_id=generation, require_executable=False
    )
    with pytest.raises(ContractError):
        store.publish(
            config,
            quality_decision=quality,
            risk_decision=decision,
            config_publication=binding,
        )
    assert list(tmp_path.rglob("manifest.json")) == []


def test_continuation_empty_holdings_and_future_as_of_are_supported() -> None:
    config = _config()
    config["state_mode"] = "continuation"
    config["input_state_binding"] = {
        "family": "paper_portfolio_state_v1", "generation_id": "1" * 64,
        "manifest_digest_sha256": "2" * 64,
        "as_of_date": config["decision_date"],
    }
    config["initial_state"] = None
    config = _recompute(config)
    frame, _ = PaperOrderPlanner().plan(
        config, pd.DataFrame(columns=["instrument", "weight"]), [], _prices(), opening_cash=100000.0,
    )
    assert frame.empty


def test_tampered_manifest_identity_fails_read(tmp_path: Path) -> None:
    config, partition = _publish_config(tmp_path)
    store = ExecutionConfigStore(tmp_path)
    manifest = json.loads((partition / "manifest.json").read_text())
    manifest["execution_date"] = "2026-01-07"
    (partition / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with pytest.raises(ContractError, match="stable generation mismatch"):
        store.read(config["generation_id"])


def test_quality_report_symlink_fails_read(tmp_path: Path) -> None:
    config, _ = _publish_config(tmp_path)
    store = ExecutionConfigStore(tmp_path)
    partition = tmp_path / "execution-configs" / config["execution_id"] / f"generation={config['generation_id']}"
    manifest = json.loads((partition / "manifest.json").read_text())
    outside = tmp_path / "outside-quality-report.json"
    outside.write_text("{}")
    report_path = tmp_path / "external_quality_reviews" / ("b" * 64 + ".json")
    report_path.symlink_to(outside)
    manifest["quality_report_checksum_sha256"] = "b" * 64
    from uq.contracts.model_layer import paper_execution_identities
    manifest["generation_id"], manifest["manifest_digest_sha256"] = paper_execution_identities(
        manifest, schema_name="execution_config"
    )
    (partition / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    with pytest.raises(ContractError, match="symbolic links are forbidden"):
        store.read(config["generation_id"])


def test_zero_quantity_target_residual_is_preserved(tmp_path: Path) -> None:
    config = _config()
    config["initial_state"]["cash"] = 100.0
    config["initial_state"]["holdings"] = []
    config = _recompute(config)
    target = pd.DataFrame({"instrument": ["000001.XSHE"], "weight": [1.0]})
    prices = {"000001.XSHE": {"close": 1.0}}
    frame, _ = PaperOrderPlanner().plan(config, target, None, prices, opening_cash=100.0)
    assert len(frame) == 1
    assert frame["side"].eq("buy").all()
    assert frame["requested_quantity"].eq(0).all()
    assert frame["reason"].eq("cash_residual").all()


def test_tampered_order_plan_payload_fails_read(tmp_path: Path) -> None:
    config, _ = _publish_config(tmp_path)
    store = OrderPlanStore(tmp_path)
    frame, residual = PaperOrderPlanner().plan(
        config, _target(), None, _prices(), opening_cash=config["initial_state"]["cash"]
    )
    generation = config["generation_id"]
    unsigned = _unsigned("order_plan_v1", [
        "target_weights_binding_resolved", "row_count_and_keys_valid",
        "cash_feasible_planning", "t1_quantity_valid",
    ], generation)
    decision = _decision_for_review(unsigned, "order_plan_v1", generation)
    manifest = {
        "contract_version": 1, "schema_version": "1.0.0", "execution_id": config["execution_id"],
        "state_mode": config["state_mode"], "execution_config_generation_id": config["generation_id"],
        "execution_date": config["execution_date"], "decision_date": config["decision_date"],
        "data_file": "data.parquet", "data_checksum_sha256": "0" * 64,
        "columns": PaperOrderPlanner.COLUMNS, "dtypes": PaperOrderPlanner.DTYPES,
        "row_count": int(len(frame)), "key_uniqueness": ["plan_sequence"],
        "logical_fingerprint": "3" * 64, "serialization_profile_id": "parquet-v1",
        "target_weights_binding": config["target_weights_binding"],
        "input_state_binding": config["input_state_binding"],
        "initial_state_provenance_sha256": config["initial_state"]["provenance_sha256"],
        "market_data_binding": {"family": "bars_daily", "generation_id": "3" * 64, "manifest_digest_sha256": "4" * 64},
        "calendar_binding": config["calendar_binding"],
        "suspension_binding": config["suspension_binding"],
        "corporate_action_binding": config["corporate_action_binding"],
        "risk_decision_binding": config["risk_decision_binding"],
        "ordered_cash_residual_sequence": residual,
        "run_id": config["run_id"], "created_at": config["created_at"],
        "quality_report_checksum_sha256": "0" * 64,
        "manifest_digest_sha256": "0" * 64, "generation_id": "0" * 64,
    }
    partition = store.publish(manifest, frame, quality_decision=decision)
    generation_id = json.loads((partition / "manifest.json").read_text())["generation_id"]
    data_path = partition / "data.parquet"
    data_path.write_bytes(data_path.read_bytes()[:-1])
    with pytest.raises(ContractError, match="payload checksum"):
        store.read(generation_id)
