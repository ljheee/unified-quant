from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pandas as pd
import pytest

from uq.contracts.model_layer import (
    ModelContractLoader,
    paper_execution_identities,
    review_signature,
)
from uq.errors import ContractError
from uq.execution.engine import PaperExecutionEngine
from uq.execution.planner import PaperOrderPlanner
from uq.execution.stores import ExecutionConfigStore, ExecutionResultStore, OrderPlanStore
from uq.risk.contracts import risk_contract_identities
from uq.risk.publication import ConfigPublicationBinding
from tests.review_key import REVIEWER_PRIVATE_KEY

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "config/schemas/fixtures/paper-execution"
RISK_FIXTURE = ROOT / "config/schemas/fixtures/risk/risk_decision-valid.json"


def _config() -> dict:
    return json.loads((FIXTURES / "execution_config-valid.json").read_text())


def _recompute(config: dict) -> dict:
    config["generation_id"], config["manifest_digest_sha256"] = paper_execution_identities(
        config, schema_name="execution_config"
    )
    return config


def _target(weight: float = 0.5) -> pd.DataFrame:
    return pd.DataFrame({"instrument": ["000001.XSHE"], "weight": [weight]})


def _decision_prices() -> dict:
    return {"000001.XSHE": {"close": 10.0}}


def _execution_market(status: str = "trading", open_price: float = 10.2) -> dict:
    return {"000001.XSHE": {
        "open": open_price, "high": max(open_price + 0.2, 10.4), "low": min(open_price - 0.2, 10.1),
        "close": 10.0, "volume": 1000000.0, "status": status,
        "limit_up": 11.0, "limit_down": 9.0,
    }}


_QUALITY_CHECKS_BY_FAMILY = {
    "execution_config_v1": [
        "schema_valid", "market_data_binding_resolved", "state_mode_valid",
        "execution_policy_within_bounds",
    ],
    "order_plan_v1": [
        "target_weights_binding_resolved", "row_count_and_keys_valid",
        "cash_feasible_planning", "t1_quantity_valid",
    ],
    "execution_result_v1": [
        "payload_readback_valid", "fee_reconciliation_valid",
        "lineage_generations_resolved", "reconciliation_complete",
    ],
}


def _unsigned(family: str, generation: str) -> dict:
    return {
        "binding_type": family,
        "checks": [
            {"name": name, "threshold": True, "observed": True, "level": "error", "result": "passed"}
            for name in _QUALITY_CHECKS_BY_FAMILY[family]
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


def _decision_for_review(unsigned: dict, family: str, generation: str) -> dict:
    unsigned = copy.deepcopy(unsigned)
    unsigned["binding_type"] = family
    unsigned["review_signature_sha256"] = review_signature(unsigned, private_key_pem=REVIEWER_PRIVATE_KEY)
    return unsigned


def _publish_config(tmp_path: Path) -> tuple[dict, Path]:
    store = ExecutionConfigStore(tmp_path)
    config = _config()
    risk = json.loads(RISK_FIXTURE.read_text())
    risk["decision_scope"] = "order_submission"
    risk["action"] = "allow"
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
    unsigned = _unsigned("execution_config_v1", generation)
    quality = _decision_for_review(unsigned, "execution_config_v1", generation)
    binding = ConfigPublicationBinding(decision=risk, config_generation_id=generation)
    store.publish(
        config, quality_decision=quality, risk_decision=risk, config_publication=binding
    )
    return store.read(generation), store.directory / config["execution_id"] / f"generation={generation}"


def _publish_plan(tmp_path: Path, config: dict, target: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    holdings = config["initial_state"]["holdings"]
    frame, residual = PaperOrderPlanner().plan(
        config, target, None, _decision_prices(), opening_cash=config["initial_state"]["cash"]
    )
    manifest = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "execution_id": config["execution_id"],
        "state_mode": config["state_mode"],
        "execution_config_generation_id": config["generation_id"],
        "execution_date": config["execution_date"],
        "decision_date": config["decision_date"],
        "data_file": "data.parquet",
        "data_checksum_sha256": "0" * 64,
        "columns": PaperOrderPlanner.COLUMNS,
        "dtypes": PaperOrderPlanner.DTYPES,
        "row_count": int(len(frame)),
        "key_uniqueness": ["plan_sequence"],
        "logical_fingerprint": "3" * 64,
        "serialization_profile_id": "parquet-v1",
        "target_weights_binding": config["target_weights_binding"],
        "input_state_binding": config["input_state_binding"],
        "initial_state_provenance_sha256": config["initial_state"]["provenance_sha256"],
        "market_data_binding": {
            "family": config["market_data_binding"]["dataset_family"],
            "generation_id": config["market_data_binding"]["generation_id"],
            "manifest_digest_sha256": config["market_data_binding"]["manifest_digest_sha256"],
        },
        "calendar_binding": config["calendar_binding"],
        "suspension_binding": config["suspension_binding"],
        "corporate_action_binding": config["corporate_action_binding"],
        "risk_decision_binding": config["risk_decision_binding"],
        "ordered_cash_residual_sequence": residual,
        "run_id": config["run_id"],
        "created_at": config["created_at"],
        "quality_report_checksum_sha256": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
        "generation_id": "0" * 64,
    }
    store = OrderPlanStore(tmp_path)
    generation = config["generation_id"]
    unsigned = _unsigned("order_plan_v1", generation)
    decision = _decision_for_review(unsigned, "order_plan_v1", generation)
    store.publish(manifest, frame, quality_decision=decision)
    published, published_frame = store.read(json.loads((store.directory / config["execution_id"] / config["execution_date"] / f"generation={manifest['generation_id']}" / "manifest.json").read_text())["generation_id"])
    return published, published_frame


def _execute(
    config: dict,
    plan_manifest: dict,
    plan_frame: pd.DataFrame,
    target: pd.DataFrame,
    *,
    execution_market: dict | None = None,
    suspended: set[str] | None = None,
    corporate_action_excluded: set[str] | None = None,
):
    return PaperExecutionEngine().execute(
        config,
        plan_manifest,
        plan_frame,
        target,
        config["initial_state"]["holdings"],
        _execution_market() if execution_market is None else execution_market,
        _decision_prices(),
        opening_cash=config["initial_state"]["cash"],
        suspended_instruments=suspended or set(),
        corporate_action_excluded=corporate_action_excluded or set(),
    )


def test_execution_fills_all_or_none_and_records_fees(tmp_path: Path) -> None:
    config, _ = _publish_config(tmp_path)
    target = _target()
    plan_manifest, plan_frame = _publish_plan(tmp_path, config, target)
    result, manifest = _execute(config, plan_manifest, plan_frame, target)
    assert result["order_state"].eq("filled").all()
    assert result["reject_reason"].eq("none").all()
    assert result["filled_quantity"].gt(0).all()
    assert result["fee_amount"].gt(0).all()
    assert manifest["aggregate_reconciliation"]["filled_buy_quantity"] == int(result["filled_quantity"].sum())
    assert manifest["opening_cash"] == 100000.0
    assert manifest["closing_cash"] == pytest.approx(manifest["opening_cash"] + manifest["aggregate_reconciliation"]["net_cash_movement"])
    assert manifest["opening_portfolio_value"] == pytest.approx(110000.0)
    assert manifest["closing_portfolio_value"] == pytest.approx(manifest["opening_portfolio_value"] - manifest["aggregate_reconciliation"]["total_fee_amount"])


def test_decision_close_estimate_fills_at_plan_limit_price(tmp_path: Path) -> None:
    config, _ = _publish_config(tmp_path)
    target = _target()
    plan_manifest, plan_frame = _publish_plan(tmp_path, config, target)
    market = _execution_market()
    market["000001.XSHE"]["close"] = 10.5
    result, manifest = _execute(config, plan_manifest, plan_frame, target, execution_market=market)
    filled = result.loc[result["side"] == "buy"].iloc[0]
    assert result["order_state"].eq("filled").all()
    assert filled["price"] == float(plan_frame.loc[plan_frame["side"] == "buy", "limit_price"].iloc[0])


def test_non_trading_suspension_and_corporate_action_reject(tmp_path: Path) -> None:
    config, _ = _publish_config(tmp_path)
    target = _target()
    plan_manifest, plan_frame = _publish_plan(tmp_path, config, target)
    for kwargs in (
        {"execution_market": _execution_market(status="halted")},
        {"suspended": {"000001.XSHE"}},
        {"corporate_action_excluded": {"000001.XSHE"}},
    ):
        result, _ = _execute(config, plan_manifest, plan_frame, target, **kwargs)
        assert result["order_state"].eq("rejected").all()
        assert result["reject_reason"].eq("suspended").all()
        assert result["filled_quantity"].eq(0).all()
        assert result["price"].eq(0).all()


def test_limit_up_blocks_buy_and_limit_down_blocks_sell(tmp_path: Path) -> None:
    config, _ = _publish_config(tmp_path)
    target = _target()
    plan_manifest, plan_frame = _publish_plan(tmp_path, config, target)
    buy_market = _execution_market(open_price=10.0)
    buy_market["000001.XSHE"]["limit_up"] = 10.0
    result, _ = _execute(config, plan_manifest, plan_frame, target, execution_market=buy_market)
    assert result["reject_reason"].eq("limit_up").all()

    sell_config = _config()
    sell_config["initial_state"]["holdings"][0]["quantity"] = 1000
    sell_config = _recompute(sell_config)
    sell_target = _target(0.0)
    sell_plan, sell_frame = _publish_plan(tmp_path, sell_config, sell_target)
    market = _execution_market(open_price=10.0)
    market["000001.XSHE"]["limit_down"] = 10.1
    market["000001.XSHE"]["low"] = 9.9
    result, _ = _execute(sell_config, sell_plan, sell_frame, sell_target, execution_market=market)
    assert result["reject_reason"].eq("limit_down").all()


def test_missing_mandatory_market_data_is_typed(tmp_path: Path) -> None:
    config, _ = _publish_config(tmp_path)
    target = _target()
    plan_manifest, plan_frame = _publish_plan(tmp_path, config, target)
    market = _execution_market()
    del market["000001.XSHE"]["limit_up"]
    result, manifest = _execute(config, plan_manifest, plan_frame, target, execution_market=market)
    assert result["reject_reason"].eq("no_market_data").all()
    assert manifest["aggregate_reconciliation"]["rejected_quantity_by_reason"]["no_market_data"] == int(result["requested_quantity"].sum())
    assert manifest["closing_cash"] == manifest["opening_cash"]


def test_missing_execution_close_for_surviving_holding_fails(tmp_path: Path) -> None:
    config, _ = _publish_config(tmp_path)
    target = _target()
    plan_manifest, plan_frame = _publish_plan(tmp_path, config, target)
    with pytest.raises(ContractError, match="missing execution close"):
        _execute(config, plan_manifest, plan_frame, target, execution_market={})


def test_all_or_none_cash_after_planned_sells(tmp_path: Path) -> None:
    config, _ = _publish_config(tmp_path)
    config["state_mode"] = "continuation"
    config["input_state_binding"] = {
        "family": "paper_portfolio_state_v1", "generation_id": "1" * 64,
        "manifest_digest_sha256": "2" * 64,
        "as_of_date": config["decision_date"],
    }
    config["initial_state"] = None
    config = _recompute(config)
    target = pd.DataFrame({"instrument": ["600000.XSHG"], "weight": [1.0]})
    holdings = [{
        "instrument": "000001.XSHE", "quantity": 1000, "average_cost": 10.0,
        "buy_locked_quantity": 0, "sellable_quantity": 1000,
    }]
    decision_prices = {"000001.XSHE": {"close": 10.0}, "600000.XSHG": {"close": 100.0}}
    execution_market = {
        "000001.XSHE": {"open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0, "volume": 100000.0, "status": "trading", "limit_up": 11.0, "limit_down": 9.0},
        "600000.XSHG": {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100000.0, "status": "trading", "limit_up": 110.0, "limit_down": 90.0},
    }
    frame, _ = PaperOrderPlanner().plan(
        config, target, holdings, decision_prices, opening_cash=0.0,
    )
    buy_row = frame["side"].eq("buy")
    frame.loc[buy_row, "requested_quantity"] = 100
    frame.loc[buy_row, "reason"] = "target_rebalance"
    plan_manifest = {
        "generation_id": "3" * 64,
        "contract_version": 1,
        "schema_version": "1.0.0",
        "quality_report_checksum_sha256": "e" * 64,
        "execution_id": config["execution_id"],
        "state_mode": config["state_mode"],
        "execution_config_generation_id": config["generation_id"],
        "execution_date": config["execution_date"],
        "decision_date": config["decision_date"],
        "target_weights_binding": config["target_weights_binding"],
        "input_state_binding": config["input_state_binding"],
        "initial_state_provenance_sha256": None,
        "market_data_binding": {
            "family": config["market_data_binding"]["dataset_family"],
            "generation_id": config["market_data_binding"]["generation_id"],
            "manifest_digest_sha256": config["market_data_binding"]["manifest_digest_sha256"],
        },
        "calendar_binding": config["calendar_binding"],
        "suspension_binding": config["suspension_binding"],
        "corporate_action_binding": config["corporate_action_binding"],
        "risk_decision_binding": config["risk_decision_binding"],
        "ordered_cash_residual_sequence": [],
        "run_id": config["run_id"],
        "created_at": config["created_at"],
        "columns": PaperOrderPlanner.COLUMNS,
        "dtypes": PaperOrderPlanner.DTYPES,
        "row_count": len(frame),
        "key_uniqueness": ["plan_sequence"],
        "logical_fingerprint": "e" * 64,
        "serialization_profile_id": "parquet-v1",
        "data_file": "data.parquet",
        "data_checksum_sha256": "f" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    plan_manifest["generation_id"], plan_manifest["manifest_digest_sha256"] = paper_execution_identities(
        plan_manifest, schema_name="order_plan"
    )
    result, manifest = PaperExecutionEngine().execute(
        config,
        plan_manifest,
        frame,
        target,
        holdings,
        execution_market,
        decision_prices,
        opening_cash=0.0,
    )
    sells = result[result["side"] == "sell"]
    buys = result[result["side"] == "buy"]
    assert sells["order_state"].eq("filled").all()
    assert buys["order_state"].eq("rejected").all()
    assert buys["reject_reason"].eq("insufficient_cash").all()
    assert manifest["closing_cash"] == pytest.approx(sells["net_amount"].sum())


def test_incomplete_plan_lineage_fails(tmp_path: Path) -> None:
    config, _ = _publish_config(tmp_path)
    target = _target()
    plan_manifest, plan_frame = _publish_plan(tmp_path, config, target)
    plan_manifest["risk_decision_binding"]["generation_id"] = "f" * 64
    with pytest.raises(ContractError, match="order_plan stable generation mismatch"):
        _execute(config, plan_manifest, plan_frame, target)


def test_result_store_readback_rejects_payload_tampering(tmp_path: Path) -> None:
    config, _ = _publish_config(tmp_path)
    target = _target()
    plan_manifest, plan_frame = _publish_plan(tmp_path, config, target)
    result, manifest = _execute(config, plan_manifest, plan_frame, target)
    store = ExecutionResultStore(tmp_path)
    generation = config["generation_id"]
    unsigned = _unsigned("execution_result_v1", generation)
    decision = _decision_for_review(unsigned, "execution_result_v1", generation)
    partition = store.publish(manifest, result, quality_decision=decision)
    published_generation = json.loads((partition / "manifest.json").read_text())["generation_id"]
    data_path = partition / "data.parquet"
    data_path.write_bytes(data_path.read_bytes()[:-1])
    with pytest.raises(ContractError, match="payload checksum"):
        store.read(published_generation)
