from __future__ import annotations

import copy
import json
import math
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
    # cr.md U9: the store derives the plan manifest generation at publish time,
    # so the decision must arrive as a per-subject provider.
    def decision(subject_generation_id: str) -> dict:
        return _decision_for_review(
            _unsigned("order_plan_v1", subject_generation_id), "order_plan_v1", subject_generation_id
        )
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
    execution_market: dict | None = None,
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
        _execution_market() if execution_market is None else execution_market,
        _decision_prices(),
        opening_cash=config["initial_state"]["cash"],
    )
    result_store = ExecutionResultStore(root)
    # cr.md U9: the store fills the payload checksum and derives the result
    # manifest generation at publish time, so the decision must arrive as a
    # per-subject provider.
    def quality(subject_generation_id: str) -> dict:
        return _decision_for_review(
            _unsigned("execution_result_v1", subject_generation_id),
            "execution_result_v1", subject_generation_id,
        )
    partition = result_store.publish(result_manifest, result, quality_decision=quality)
    published_generation = json.loads((partition / "manifest.json").read_text())["generation_id"]
    result_manifest, result = result_store.read(published_generation)
    frame, manifest = PaperPortfolioStateBuilder().build(
        config,
        result_manifest=result_manifest,
        result_frame=result,
        plan_manifest=plan_manifest,
        plan_frame=plan_frame,
        target_weights_manifest=target_manifest,
        target_weights_frame=target,
        execution_market=_execution_market() if execution_market is None else execution_market,
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
    # cr.md U9: the store derives the result manifest generation at publish
    # time, so the decision must arrive as a per-subject provider.
    def next_result_quality(subject_generation_id: str) -> dict:
        return _decision_for_review(
            _unsigned("execution_result_v1", subject_generation_id),
            "execution_result_v1", subject_generation_id,
        )
    next_partition = next_result_store.publish(
        next_result_manifest, next_result, quality_decision=next_result_quality
    )
    next_generation = json.loads((next_partition / "manifest.json").read_text())["generation_id"]
    next_result_manifest, next_result = next_result_store.read(next_generation)
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
    # cr.md U9: the store derives the state manifest generation at publish
    # time, so the decision must arrive as a per-subject provider.
    def decision(subject_generation_id: str) -> dict:
        return _decision_for_review(unsigned, "paper_portfolio_state_v1", subject_generation_id)
    partition = store.publish(manifest, frame, quality_decision=decision)
    published_generation = json.loads((partition / "manifest.json").read_text())["generation_id"]
    published_manifest, published_frame = store.read(published_generation)
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
    # cr.md U9: the store derives the state manifest generation at publish
    # time, so the decision must arrive as a per-subject provider.
    def decision(subject_generation_id: str) -> dict:
        return _decision_for_review(unsigned, "paper_portfolio_state_v1", subject_generation_id)
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


def test_a_suspended_holding_is_valued_at_the_previous_states_close(tmp_path: Path) -> None:
    """cr.md 8.67: a per-session panel has no row for a suspended name.

    The vendor partitions carry only the rows the vendor returned, so a genuinely
    suspended instrument is absent rather than marked non-trading -- the same shape
    the backtest engine used to refuse (cr.md 8.64).  A real full-A run hit this on
    605255.XSHG: held from 09-09, last carried on 09-23, then gone from the panel
    for the rest of the window while the suspension family correctly recorded it.

    The governed previous state already recorded what the position was worth, so
    the state builder carries that price forward instead of aborting the run.
    """
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

    def next_result_quality(subject_generation_id: str) -> dict:
        return _decision_for_review(
            _unsigned("execution_result_v1", subject_generation_id),
            "execution_result_v1", subject_generation_id,
        )

    next_partition = next_result_store.publish(
        next_result_manifest, next_result, quality_decision=next_result_quality
    )
    next_generation = json.loads((next_partition / "manifest.json").read_text())["generation_id"]
    next_result_manifest, next_result = next_result_store.read(next_generation)

    # The execution session carries no row at all for the held instrument.
    suspended_market = _execution_market()
    del suspended_market["000001.XSHE"]

    state_frame, _next_state = PaperPortfolioStateBuilder().build(
        continuation_config,
        result_manifest=next_result_manifest,
        result_frame=next_result,
        plan_manifest=plan_manifest,
        plan_frame=plan_frame,
        target_weights_manifest=target_manifest,
        target_weights_frame=target,
        execution_market=suspended_market,
        decision_prices=_decision_prices(),
        input_holdings=prior_frame.to_dict(orient="records"),
        opening_cash=CONTINUATION_OPENING_CASH,
        previous_state_manifest=previous_state_manifest,
        previous_state_frame=prior_frame,
    )
    held = state_frame[state_frame["instrument"] == "000001.XSHE"]
    assert not held.empty, "a suspended position must survive into the next state"
    quantity = int(held["quantity"].iloc[0])
    prior = prior_frame[prior_frame["instrument"] == "000001.XSHE"].iloc[0]
    expected_close = float(prior["market_value"]) / float(prior["quantity"])
    assert math.isclose(
        float(held["market_value"].iloc[0]), quantity * expected_close, rel_tol=0.0, abs_tol=1e-6
    ), "the position must be carried at the price the previous state recorded"


def test_a_suspended_holding_with_no_prior_state_still_fails_closed(tmp_path: Path) -> None:
    """The carry-forward must not become "invent a price".

    A first run has no previous state, so there is nothing to carry and the
    missing-close guard stays fully in force.
    """
    config, _frame, _manifest, plan_manifest, plan_frame, result_manifest, result = _publish_initial_state(tmp_path)
    market = _execution_market()
    del market["000001.XSHE"]
    with pytest.raises(ContractError, match="missing execution close"):
        PaperPortfolioStateBuilder().build(
            config,
            result_manifest=result_manifest,
            result_frame=result,
            plan_manifest=plan_manifest,
            plan_frame=plan_frame,
            target_weights_manifest=_target_manifest(_target(), config),
            target_weights_frame=_target(),
            execution_market=market,
            decision_prices=_decision_prices(),
            input_holdings=config["initial_state"]["holdings"],
            opening_cash=config["initial_state"]["cash"],
        )


def test_a_replayed_paper_stage_recovers_the_artifacts_it_already_published() -> None:
    """cr.md 8.68: the same caller-owns-idempotence rule, applied to paper stores.

    The paper stage publishes five artifacts per decision date (risk decision,
    execution config, order plan, execution result, next state).  A run that fails
    after publishing one of them retries against stores that already hold the
    same content-derived artifact; the stores keep refusing overwrites, and the
    stage turns that refusal into a readback of the same generation.  Without this
    a full-A run died on `risk decision partition already exists`.

    Locked at the helper boundary because the five call sites all go through it:
    a refusal naming *this* generation is recovered, and a refusal naming another
    one is re-raised so this run's lineage cannot attach to another run's bytes.
    """
    from uq.execution.stage_adapter import _publish_once

    generation = "a" * 64
    calls = {"n": 0}

    def publish() -> None:
        calls["n"] += 1
        raise ContractError(f"paper execution_result partition already exists: /x/generation={generation}")

    recovered = _publish_once(publish, expected_generation=generation)
    assert recovered == generation
    assert calls["n"] == 1, "the refusal must be tolerated, not retried blindly"

    # When the store derives the identity inside publish, the caller cannot know
    # it beforehand -- its manifest still holds the all-zero placeholder.  Passing
    # no expectation must still recover, and must report the id it found, or every
    # store-derived family (config, order plan, result, state) stays broken.
    store_derived = "c" * 64
    recovered_derived = _publish_once(
        lambda: (_ for _ in ()).throw(
            ContractError(f"paper order_plan partition already exists: /x/2025-09-10/generation={store_derived}")
        ),
    )
    assert recovered_derived == store_derived

    # A refusal for a different generation is somebody else's artifact.
    with pytest.raises(ContractError, match="already exists"):
        _publish_once(
            lambda: (_ for _ in ()).throw(
                ContractError(f"paper state partition already exists: /x/generation={'b' * 64}")
            ),
            expected_generation=generation,
        )

    # Any other refusal is a real failure.
    with pytest.raises(ContractError, match="tampered"):
        _publish_once(
            lambda: (_ for _ in ()).throw(ContractError("tampered execution result")),
            expected_generation=generation,
        )


def test_the_engine_values_a_suspended_holding_from_the_carried_price(tmp_path: Path) -> None:
    """cr.md 8.67: the engine's closing-book valuation is the other half.

    `_state_frame` and `ExecutionEngine._portfolio_value` both refuse a surviving
    holding with no close, and a suspended name has no row on the execution
    session.  Fixing only the state builder still left the run dying here -- the
    error text is identical, which is why the first diagnosis pointed at the wrong
    file.  The carried price must reach the engine too.
    """
    from uq.execution.engine import PaperExecutionEngine

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
    suspended_market = _execution_market()
    del suspended_market["000001.XSHE"]

    carried = {"000001.XSHE": 12.345}
    # Without the carried price the closing book cannot be valued.
    with pytest.raises(ContractError, match="missing execution close"):
        PaperExecutionEngine().execute(
            continuation_config, plan_manifest, plan_frame, target,
            prior_frame.to_dict(orient="records"), suspended_market, _decision_prices(),
            opening_cash=CONTINUATION_OPENING_CASH,
        )
    # With it, the run completes and the held quantity is valued at that price.
    _result, result_manifest = PaperExecutionEngine().execute(
        continuation_config, plan_manifest, plan_frame, target,
        prior_frame.to_dict(orient="records"), suspended_market, _decision_prices(),
        opening_cash=CONTINUATION_OPENING_CASH,
        carry_forward_prices=carried,
    )
    held_quantity = int(prior_frame[prior_frame["instrument"] == "000001.XSHE"]["quantity"].iloc[0])
    assert result_manifest["closing_portfolio_value"] >= held_quantity * 12.345 - 1e-6


def test_the_lineage_check_runs_before_the_t1_unlock() -> None:
    """cr.md 8.70: the check compares the whole book, so it must run first.

    The lineage comparison used to sit inside the per-holding validation loop,
    below the T+1 unlock that zeroes `buy_locked_quantity`.  On the second
    iteration every earlier instrument had already been unlocked, so the comparison
    saw a partially unlocked book and failed on the first name whose lock was still
    set -- `input holdings do not match previous state payload` on a name that
    matches perfectly.  A one-instrument fixture has no earlier instrument to
    unlock, which is why only the 20-name full-A run surfaced it.

    Driven at `_closing_holdings` because that is where the ordering lives; the
    full continuation chain adds nothing the ordering needs.
    """
    # Two instruments, both still carrying yesterday's buy lock.
    holdings = [
        {
            "instrument": "000001.XSHE", "quantity": 100, "average_cost": 10.0,
            "buy_locked_quantity": 100, "sellable_quantity": 0,
        },
        {
            "instrument": "000002.XSHE", "quantity": 200, "average_cost": 20.0,
            "buy_locked_quantity": 200, "sellable_quantity": 0,
        },
    ]
    # The previous state is exactly the same book, so the check must pass.
    previous = pd.DataFrame([
        {
            "instrument": "000001.XSHE", "quantity": 100, "average_cost": 10.0,
            "buy_locked_quantity": 100, "sellable_quantity": 0, "market_value": 1000.0,
        },
        {
            "instrument": "000002.XSHE", "quantity": 200, "average_cost": 20.0,
            "buy_locked_quantity": 200, "sellable_quantity": 0, "market_value": 4000.0,
        },
    ]).astype(PaperPortfolioStateBuilder.DTYPES)

    closed = PaperPortfolioStateBuilder()._closing_holdings(
        [dict(row) for row in holdings],
        pd.DataFrame(columns=PaperOrderPlanner.COLUMNS),
        same_session_prior_state=False,
        previous_state_frame=previous,
        previous_state_manifest={"row_count": 2},
    )
    # The unlock still happens -- it just no longer corrupts the check.
    assert all(int(row["buy_locked_quantity"]) == 0 for row in closed.values())
    assert {k: int(v["quantity"]) for k, v in closed.items()} == {
        "000001.XSHE": 100, "000002.XSHE": 200,
    }




def test_the_opening_valuation_uses_the_carried_price_and_still_reconciles() -> None:
    """cr.md 8.72: the opening valuation is reconciled, so both sides must agree.

    `_validate_valuation` recomputes the opening book value and compares it with
    the execution result's `opening_portfolio_value`.  A suspended holding has no
    row on the *decision* session's panel, so each side needs the same carried
    price; recomputing with a different one fails the reconciliation on a number
    that is individually fine.  With no carry at all it still fails closed.
    """
    from uq.execution.state import PaperPortfolioStateBuilder

    carried = {"000001.XSHE": 12.5}
    holdings = [{
        "instrument": "000001.XSHE", "quantity": 100, "average_cost": 10.0,
        "buy_locked_quantity": 0, "sellable_quantity": 100,
    }]
    # The closing half of the reconciliation reads `market_value`, so the state
    # frame has to carry a real value for it.
    state_frame = pd.DataFrame({
        "instrument": ["000001.XSHE"], "quantity": [100], "average_cost": [10.0],
        "buy_locked_quantity": [0], "sellable_quantity": [100], "market_value": [1250.0],
    }).astype(PaperPortfolioStateBuilder.DTYPES)
    # The result claims the opening book was valued with the carried price.
    result_manifest = {
        "opening_portfolio_value": 1250.0,
        "closing_cash": 0.0,
        "closing_portfolio_value": 1250.0,
    }

    # Agreeing prices reconcile.
    PaperPortfolioStateBuilder()._validate_valuation(
        result_manifest, opening_cash=0.0, input_holdings=holdings,
        decision_prices={}, state_frame=state_frame,
        carry_forward_prices=carried,
    )

    # A result that valued the holding differently does not reconcile.
    with pytest.raises(ContractError, match="does not reconcile"):
        PaperPortfolioStateBuilder()._validate_valuation(
            {**result_manifest, "opening_portfolio_value": 999.0},
            opening_cash=0.0, input_holdings=holdings,
            decision_prices={}, state_frame=state_frame,
            carry_forward_prices=carried,
        )

    # No carry and no panel row: still fail closed, no invented price.
    with pytest.raises(ContractError, match="missing decision close"):
        PaperPortfolioStateBuilder()._validate_valuation(
            result_manifest, opening_cash=0.0, input_holdings=holdings,
            decision_prices={}, state_frame=state_frame,
        )


def test_the_decision_nav_uses_the_carried_price(tmp_path: Path) -> None:
    """cr.md 8.72: `_decision_nav` was the fourth place with the same gap.

    The engine values the opening book three times -- decision NAV, opening
    portfolio value, closing portfolio value -- and only two of them took the
    carried price.  The decision NAV is computed first, so it was where the run
    actually died, while the state side had already accepted the same book.  The
    error text is byte-identical to the state builder's, which is why the first
    diagnosis pointed at the wrong file.
    """
    from uq.execution.engine import PaperExecutionEngine

    holdings = {
        "000001.XSHE": {
            "instrument": "000001.XSHE", "quantity": 100, "average_cost": 10.0,
            "buy_locked_quantity": 0, "sellable_quantity": 100,
        }
    }
    carried = {"000001.XSHE": 12.5}

    with pytest.raises(ContractError, match="missing decision close"):
        PaperExecutionEngine()._decision_nav(0.0, holdings, {})

    nav = PaperExecutionEngine()._decision_nav(
        0.0, holdings, {}, carry_forward_prices=carried
    )
    assert nav == 1250.0

    # And `execute` must actually pass it: driving the helper directly proves the
    # fallback works, not that the caller uses it.  Mutation of the call site kept
    # the suite green until this second half was added.
    config, prior_frame, previous_state_manifest, _, _, _, _ = _publish_initial_state(tmp_path)
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
    suspended_market = _execution_market()
    del suspended_market["000001.XSHE"]
    # The decision session's panel has no row either, so the decision NAV has to
    # come from the carry as well.
    _result, result_manifest = PaperExecutionEngine().execute(
        continuation_config,
        plan_manifest,
        plan_frame,
        target,
        prior_frame.to_dict(orient="records"),
        suspended_market,
        {},
        opening_cash=CONTINUATION_OPENING_CASH,
        carry_forward_prices={"000001.XSHE": 12.5},
    )
    assert result_manifest["opening_portfolio_value"] > 0
