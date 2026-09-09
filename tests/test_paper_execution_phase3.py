from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.model_layer import (
    model_manifest_identities,
    paper_execution_identities,
)
from uq.errors import ContractError
from uq.execution.engine import PaperExecutionEngine
from uq.execution.planner import PaperOrderPlanner
from uq.execution.state import PaperPortfolioStateBuilder
from uq.execution.stores import (
    ExecutionConfigStore,
    ExecutionResultStore,
    OrderPlanStore,
    PaperPortfolioStateStore,
)
from uq.risk.contracts import risk_contract_identities
from uq.risk.publication import ConfigPublicationBinding
from tests.review_key import REVIEWER_PRIVATE_KEY
from tests.test_paper_execution_phase2 import (
    _config,
    _decision_prices,
    _execution_market,
    _decision_for_review,
    _publish_config,
    _publish_plan,
    _recompute,
    _target,
    _unsigned,
)

RISK_FIXTURE = Path("config/schemas/fixtures/paper-execution/risk_decision-valid.json")
CONTINUATION_OPENING_CASH = 54988.3
_STATE_QUALITY_CHECKS = [
    "holdings_readback_valid", "cash_non_negative",
    "state_lineage_resolved", "state_reconciliation_complete",
]


def _publish_continuation_config(
    tmp_path: Path, previous_state_manifest: dict, target: pd.DataFrame | None = None
) -> tuple[dict, dict]:
    config, _ = _publish_config(tmp_path)
    config["decision_date"] = "2026-01-07"
    config["execution_date"] = "2026-01-08"
    config["state_mode"] = "continuation"
    config["initial_state"] = None
    config["input_state_binding"] = {
        "family": "paper_portfolio_state_v1",
        "generation_id": previous_state_manifest["generation_id"],
        "manifest_digest_sha256": previous_state_manifest["manifest_digest_sha256"],
        "as_of_date": previous_state_manifest["state_date"],
    }
    if target is None:
        target = _target()
    target_manifest = _target_manifest(target, config)
    config["target_weights_binding"] = {
        "family": "target_weights_v1",
        "generation_id": target_manifest["generation_id"],
        "manifest_digest_sha256": target_manifest["manifest_digest_sha256"],
    }
    config = _recompute(config)
    risk = _converged_risk(config)
    unsigned = _unsigned("execution_config_v1", config["generation_id"])
    decision = _decision_for_review(unsigned, "execution_config_v1", config["generation_id"])
    store = ExecutionConfigStore(tmp_path)
    store.publish(
        config,
        quality_decision=decision,
        risk_decision=risk,
        config_publication=ConfigPublicationBinding(
            decision=risk, config_generation_id=config["generation_id"]
        ),
    )
    return store.read(config["generation_id"]), target_manifest


def _converged_risk(config: dict) -> dict:
    risk = json.loads(RISK_FIXTURE.read_text())
    risk["decision_scope"] = "order_submission"
    risk["action"] = "allow"
    risk["as_of_date"] = config["decision_date"]
    risk["visible_through"] = f"{config['execution_date']}T15:00:00+08:00"
    for _ in range(8):
        risk["binding_config_generation_id"] = config["generation_id"]
        risk["generation_id"], risk["manifest_digest_sha256"] = risk_contract_identities(
            risk, schema_name="risk_decision"
        )
        binding = {
            "family": "risk_decision_v1",
            "generation_id": risk["generation_id"],
            "manifest_digest_sha256": risk["manifest_digest_sha256"],
        }
        if config.get("risk_decision_binding") == binding:
            return risk
        config["risk_decision_binding"] = binding
        config["generation_id"], config["manifest_digest_sha256"] = paper_execution_identities(
            config, schema_name="execution_config"
        )
    raise AssertionError("risk decision did not converge")


def _publish_continuation_plan(
    root: Path,
    config: dict,
    target: pd.DataFrame,
    holdings: list[dict],
    *,
    opening_cash: float,
) -> tuple[dict, pd.DataFrame]:
    risk = _converged_risk(config)
    frame, residual = PaperOrderPlanner().plan(
        config,
        target,
        holdings,
        _decision_prices(),
        opening_cash=opening_cash,
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
        "ordered_cash_residual_sequence": residual,
        "run_id": config["run_id"],
        "created_at": config["created_at"],
        "quality_report_checksum_sha256": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
        "generation_id": "0" * 64,
    }
    store = OrderPlanStore(root)
    unsigned = _unsigned("order_plan_v1", config["generation_id"])
    decision = _decision_for_review(unsigned, "order_plan_v1", config["generation_id"])
    store.publish(manifest, frame, quality_decision=decision, risk_decision=risk)
    return store.read(manifest["generation_id"])


def _publish_initial_config(root: Path, target: pd.DataFrame) -> tuple[dict, dict]:
    config = _config()
    target_manifest = _target_manifest(target, config)
    config["target_weights_binding"] = {
        "family": "target_weights_v1",
        "generation_id": target_manifest["generation_id"],
        "manifest_digest_sha256": target_manifest["manifest_digest_sha256"],
    }
    risk = _converged_risk(config)
    generation = config["generation_id"]
    unsigned = _unsigned("execution_config_v1", generation)
    decision = _decision_for_review(unsigned, "execution_config_v1", generation)
    store = ExecutionConfigStore(root)
    store.publish(
        config,
        quality_decision=decision,
        risk_decision=risk,
        config_publication=ConfigPublicationBinding(
            decision=risk, config_generation_id=generation
        ),
    )
    return store.read(generation), target_manifest


def _publish_initial_state(
    tmp_path: Path,
    *,
    target: pd.DataFrame | None = None,
) -> tuple[dict, pd.DataFrame, dict, dict, pd.DataFrame, dict]:
    root = tmp_path / "initial"
    root.mkdir()
    if target is None:
        target = _target()
    config, target_manifest = _publish_initial_config(root, target)
    plan_manifest, plan_frame = _publish_plan(root, config, target)
    result, result_manifest = PaperExecutionEngine().execute(
        config,
        plan_manifest,
        plan_frame,
        target,
        config["initial_state"]["holdings"],
        _execution_market(),
        _decision_prices(),
        opening_cash=config["initial_state"]["cash"],
    )
    result_store = ExecutionResultStore(root)
    unsigned = _unsigned("execution_result_v1", result_manifest["generation_id"])
    quality = _decision_for_review(unsigned, "execution_result_v1", result_manifest["generation_id"])
    result_store.publish(result_manifest, result, quality_decision=quality)
    result_manifest, result = result_store.read(result_manifest["generation_id"])
    frame, manifest = PaperPortfolioStateBuilder().build(
        config,
        result_manifest=result_manifest,
        result_frame=result,
        plan_manifest=plan_manifest,
        plan_frame=plan_frame,
        target_weights_manifest=target_manifest,
        target_weights_frame=target,
        execution_market=_execution_market(),
        decision_prices=_decision_prices(),
        input_holdings=config["initial_state"]["holdings"],
        opening_cash=config["initial_state"]["cash"],
    )
    return config, frame, manifest, plan_manifest, plan_frame, result_manifest, result


def _target_manifest(target: pd.DataFrame, config: dict) -> dict:
    records = target.to_dict(orient="records")
    manifest = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "decision_date": config["decision_date"],
        "portfolio_definition_generation_id": "1" * 64,
        "prediction_set_generation_id": "2" * 64,
        "universe_snapshot_generation_id": "3" * 64,
        "instrument_count": len(target),
        "total_stock_weight": float(target["weight"].sum()),
        "cash_reserve": 0.0,
        "previous_target_weights_generation_id": None,
        "weights_file": "data.parquet",
        "weights_checksum_sha256": "4" * 64,
        "columns": ["instrument", "weight"],
        "dtypes": {"instrument": "string", "weight": "float64"},
        "row_count": len(target),
        "key_uniqueness": "instrument",
        "logical_fingerprint": "5" * 64,
        "serialization_profile_id": "parquet-v1",
        "run_id": config["run_id"],
        "created_at": config["created_at"],
        "quality_report_checksum_sha256": "6" * 64,
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    manifest["generation_id"], manifest["manifest_digest_sha256"] = model_manifest_identities(
        manifest, schema_name="target_weights"
    )
    return manifest


def _initial_state_manifest(
    config: dict,
    result_manifest: dict,
    target_manifest: dict,
) -> dict:
    manifest = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "execution_id": config["execution_id"],
        "state_date": config["execution_date"],
        "state_mode": "initial",
        "cash": result_manifest["closing_cash"],
        "data_file": "data.parquet",
        "data_checksum_sha256": "7" * 64,
        "columns": PaperPortfolioStateBuilder.COLUMNS,
        "dtypes": PaperPortfolioStateBuilder.DTYPES,
        "row_count": 1,
        "key_uniqueness": "instrument",
        "logical_fingerprint": "8" * 64,
        "serialization_profile_id": "parquet-v1",
        "target_weights_binding": config["target_weights_binding"],
        "previous_state_binding": None,
        "execution_result_binding": {
            "family": "execution_result_v1",
            "generation_id": result_manifest["generation_id"],
            "manifest_digest_sha256": result_manifest["manifest_digest_sha256"],
        },
        "risk_decision_binding": config["risk_decision_binding"],
        "market_data_binding": {
            "family": config["market_data_binding"]["dataset_family"],
            "generation_id": config["market_data_binding"]["generation_id"],
            "manifest_digest_sha256": config["market_data_binding"]["manifest_digest_sha256"],
        },
        "run_id": config["run_id"],
        "created_at": config["created_at"],
        "quality_report_checksum_sha256": "9" * 64,
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    manifest["generation_id"], manifest["manifest_digest_sha256"] = paper_execution_identities(
        manifest, schema_name="paper_portfolio_state"
    )
    return manifest


def test_state_rejects_valuation_reconciliation_mismatch(tmp_path: Path) -> None:
    config, frame, manifest, plan_manifest, plan_frame, result_manifest, result = _publish_initial_state(tmp_path)
    tampered = copy.deepcopy(result_manifest)
    tampered["opening_portfolio_value"] += 1.0
    tampered["generation_id"], tampered["manifest_digest_sha256"] = paper_execution_identities(
        tampered, schema_name="execution_result"
    )
    with pytest.raises(ContractError, match="opening portfolio value"):
        PaperPortfolioStateBuilder().build(
            config,
            result_manifest=tampered,
            result_frame=result,
            plan_manifest=plan_manifest,
            plan_frame=plan_frame,
            target_weights_manifest=_target_manifest(_target(), config),
            target_weights_frame=_target(),
            execution_market=_execution_market(),
            decision_prices=_decision_prices(),
            input_holdings=config["initial_state"]["holdings"],
            opening_cash=config["initial_state"]["cash"],
        )


def test_state_rejects_aggregate_reconciliation_mismatch(tmp_path: Path) -> None:
    config, frame, manifest, plan_manifest, plan_frame, result_manifest, result = _publish_initial_state(tmp_path)
    tampered = copy.deepcopy(result_manifest)
    tampered["aggregate_reconciliation"]["filled_buy_quantity"] += 1
    tampered["generation_id"], tampered["manifest_digest_sha256"] = paper_execution_identities(
        tampered, schema_name="execution_result"
    )
    with pytest.raises(ContractError, match="filled_buy_quantity"):
        PaperPortfolioStateBuilder().build(
            config,
            result_manifest=tampered,
            result_frame=result,
            plan_manifest=plan_manifest,
            plan_frame=plan_frame,
            target_weights_manifest=_target_manifest(_target(), config),
            target_weights_frame=_target(),
            execution_market=_execution_market(),
            decision_prices=_decision_prices(),
            input_holdings=config["initial_state"]["holdings"],
            opening_cash=config["initial_state"]["cash"],
        )


def test_zero_order_retained_holding_publishes_state(tmp_path: Path) -> None:
    matching_weight = 1000 * 10.0 / (100000.0 + 1000 * 10.0)
    config, frame, manifest, _, _, result_manifest, result = _publish_initial_state(
        tmp_path, target=_target(weight=matching_weight)
    )
    assert len(result) == 0
    assert frame["quantity"].iloc[0] == 1000
    assert frame["buy_locked_quantity"].iloc[0] == 0
    assert frame["market_value"].iloc[0] == pytest.approx(10000.0)
    assert manifest["row_count"] == 1


def test_continuation_state_resets_prior_buy_lock(tmp_path: Path) -> None:
    config, prior_frame, previous_state_manifest, _, _, result_manifest, result = _publish_initial_state(tmp_path)
    continuation_root = tmp_path / "continuation"
    continuation_root.mkdir()
    target = _target()
    continuation_config, target_manifest = _publish_continuation_config(
        continuation_root, previous_state_manifest, target
    )
    plan_manifest, plan_frame = _publish_continuation_plan(
        continuation_root,
        continuation_config,
        target,
        prior_frame.to_dict(orient="records"),
        opening_cash=CONTINUATION_OPENING_CASH,
    )
    next_result, next_result_manifest = PaperExecutionEngine().execute(
        continuation_config,
        plan_manifest,
        plan_frame,
        target,
        prior_frame.to_dict(orient="records"),
        _execution_market(),
        _decision_prices(),
        opening_cash=CONTINUATION_OPENING_CASH,
    )
    next_result_store = ExecutionResultStore(continuation_root)
    unsigned = _unsigned("execution_result_v1", next_result_manifest["generation_id"])
    next_result_quality = _decision_for_review(
        unsigned, "execution_result_v1", next_result_manifest["generation_id"]
    )
    next_result_store.publish(next_result_manifest, next_result, quality_decision=next_result_quality)
    next_result_manifest, next_result = next_result_store.read(next_result_manifest["generation_id"])
    with pytest.raises(ContractError, match="prior state payload"):
        PaperPortfolioStateBuilder().build(
            continuation_config,
            result_manifest=next_result_manifest,
            result_frame=next_result,
            plan_manifest=plan_manifest,
            plan_frame=plan_frame,
            target_weights_manifest=target_manifest,
            target_weights_frame=target,
            execution_market=_execution_market(),
            decision_prices=_decision_prices(),
            input_holdings=prior_frame.to_dict(orient="records"),
            opening_cash=CONTINUATION_OPENING_CASH,
            previous_state_manifest=previous_state_manifest,
        )
    state_frame, next_state = PaperPortfolioStateBuilder().build(
        continuation_config,
        result_manifest=next_result_manifest,
        result_frame=next_result,
        plan_manifest=plan_manifest,
        plan_frame=plan_frame,
        target_weights_manifest=target_manifest,
        target_weights_frame=target,
        execution_market=_execution_market(),
        decision_prices=_decision_prices(),
        input_holdings=prior_frame.to_dict(orient="records"),
        opening_cash=CONTINUATION_OPENING_CASH,
        previous_state_manifest=previous_state_manifest,
        previous_state_frame=prior_frame,
    )
    assert int(state_frame["buy_locked_quantity"].iloc[0]) == 0
    assert int(state_frame["sellable_quantity"].iloc[0]) == int(state_frame["quantity"].iloc[0])
    assert next_state["previous_state_binding"]["generation_id"] == previous_state_manifest["generation_id"]


def test_state_rejects_t1_locked_sell_plan(tmp_path: Path) -> None:
    config, _frame, _manifest, plan_manifest, plan_frame, result_manifest, _result = _publish_initial_state(tmp_path)
    plan_frame.loc[0, "side"] = "sell"
    plan_frame.loc[0, "requested_quantity"] = 4500
    plan_frame.loc[0, "target_delta_shares"] = -4500
    plan_manifest["logical_fingerprint"] = "7" * 64
    plan_manifest["generation_id"], plan_manifest["manifest_digest_sha256"] = paper_execution_identities(
        plan_manifest, schema_name="order_plan"
    )
    result_manifest["order_plan_generation_id"] = plan_manifest["generation_id"]
    result_manifest["generation_id"], result_manifest["manifest_digest_sha256"] = paper_execution_identities(
        result_manifest, schema_name="execution_result"
    )
    with pytest.raises(ContractError, match="sell exceeds sellable quantity"):
        PaperPortfolioStateBuilder().build(
            config,
            result_manifest=result_manifest,
            result_frame=_result,
            plan_manifest=plan_manifest,
            plan_frame=plan_frame,
            target_weights_manifest=_target_manifest(_target(), config),
            target_weights_frame=_target(),
            execution_market=_execution_market(),
            decision_prices=_decision_prices(),
            input_holdings=config["initial_state"]["holdings"],
            opening_cash=config["initial_state"]["cash"],
        )


def test_state_evolution_updates_quantity_cost_and_t1_lock(tmp_path: Path) -> None:
    config, frame, manifest, _, _, result_manifest, result = _publish_initial_state(tmp_path)
    assert frame["quantity"].iloc[0] == 5500
    assert frame["buy_locked_quantity"].iloc[0] == 4500
    assert frame["sellable_quantity"].iloc[0] == 1000
    assert frame["market_value"].iloc[0] == pytest.approx(5500 * 10.0)
    assert manifest["cash"] == pytest.approx(result_manifest["closing_cash"])
    assert manifest["execution_result_binding"]["generation_id"] == result_manifest["generation_id"]


def test_state_store_publish_read_and_tamper_detection(tmp_path: Path) -> None:
    root = tmp_path / "initial"
    config, frame, manifest, _, _, _, _ = _publish_initial_state(tmp_path)
    store = PaperPortfolioStateStore(root)
    unsigned = {
        "binding_type": "paper_portfolio_state_v1",
        "checks": [
            {"name": name, "threshold": True, "observed": True, "level": "error", "result": "passed"}
            for name in _STATE_QUALITY_CHECKS
        ],
        "errors": [], "warnings": [], "key_id": "model-quality-reviewer-v1-2026-09",
        "policy": "reject_all", "producer_code_fingerprint": "a" * 64,
        "reviewer": "external-model-quality-reviewer-v1", "report_version": 2,
        "status": "passed",
    }
    decision = _decision_for_review(unsigned, "paper_portfolio_state_v1", manifest["generation_id"])
    partition = store.publish(manifest, frame, quality_decision=decision)
    published_manifest, published_frame = store.read(manifest["generation_id"])
    assert published_frame["quantity"].tolist() == [5500]
    assert partition.exists()

    data_path = partition / "data.parquet"
    data_path.write_bytes(data_path.read_bytes()[:-1])
    with pytest.raises(ContractError, match="payload checksum"):
        store.read(published_manifest["generation_id"])


def test_state_store_rejects_tampered_manifest_identity(tmp_path: Path) -> None:
    root = tmp_path / "initial"
    config, frame, manifest, _, _, _, _ = _publish_initial_state(tmp_path)
    store = PaperPortfolioStateStore(root)
    unsigned = {
        "binding_type": "paper_portfolio_state_v1",
        "checks": [
            {"name": name, "threshold": True, "observed": True, "level": "error", "result": "passed"}
            for name in _STATE_QUALITY_CHECKS
        ],
        "errors": [], "warnings": [], "key_id": "model-quality-reviewer-v1-2026-09",
        "policy": "reject_all", "producer_code_fingerprint": "a" * 64,
        "reviewer": "external-model-quality-reviewer-v1", "report_version": 2,
        "status": "passed",
    }
    decision = _decision_for_review(unsigned, "paper_portfolio_state_v1", manifest["generation_id"])
    partition = store.publish(manifest, frame, quality_decision=decision)
    tampered_manifest = json.loads((partition / "manifest.json").read_text())
    tampered_manifest["row_count"] += 1
    tampered_manifest["generation_id"], tampered_manifest["manifest_digest_sha256"] = paper_execution_identities(
        tampered_manifest, schema_name="paper_portfolio_state"
    )
    (partition / "manifest.json").write_text(json.dumps(tampered_manifest, sort_keys=True) + "\n")
    with pytest.raises(ContractError, match="bound to another generation"):
        store.read(manifest["generation_id"])


def test_state_rejects_mismatched_result_lineage(tmp_path: Path) -> None:
    config, frame, manifest, plan_manifest, _plan_frame, result_manifest, result = _publish_initial_state(tmp_path)
    mismatched_plan = copy.deepcopy(plan_manifest)
    mismatched_plan["generation_id"] = "f" * 64
    with pytest.raises(ContractError, match="order_plan stable generation mismatch"):
        PaperPortfolioStateBuilder().build(
            config,
            result_manifest=result_manifest,
            result_frame=result,
            plan_manifest=mismatched_plan,
            plan_frame=_plan_frame,
            target_weights_manifest=_target_manifest(_target(), config),
            target_weights_frame=_target(),
            execution_market=_execution_market(),
            decision_prices=_decision_prices(),
            input_holdings=config["initial_state"]["holdings"],
            opening_cash=config["initial_state"]["cash"],
        )


def test_state_requires_execution_close_for_surviving_holding(tmp_path: Path) -> None:
    config, frame, manifest, plan_manifest, _plan_frame, result_manifest, result = _publish_initial_state(tmp_path)
    market = _execution_market()
    del market["000001.XSHE"]["close"]
    with pytest.raises(ContractError, match="missing execution close"):
        PaperPortfolioStateBuilder().build(
            config,
            result_manifest=result_manifest,
            result_frame=result,
            plan_manifest=plan_manifest,
            plan_frame=_plan_frame,
            target_weights_manifest=_target_manifest(_target(), config),
            target_weights_frame=_target(),
            execution_market=market,
            decision_prices=_decision_prices(),
            input_holdings=config["initial_state"]["holdings"],
            opening_cash=config["initial_state"]["cash"],
        )
