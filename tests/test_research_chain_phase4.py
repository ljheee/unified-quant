from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pandas as pd
import pytest

from tests.review_key import REVIEWER_PRIVATE_KEY
from tests.test_research_chain_phase2 import (
    RUN_ID,
    _prepare_dataset_store,
)
from uq.backtest.engine import BacktestEngine, BacktestResultStore
from uq.contracts.model_layer import (
    bind_reviewed_quality_decision,
    create_reviewed_quality_decision,
    model_manifest_identities,
    research_contract_identities,
)
from uq.errors import ContractError
from uq.portfolio.builder import PortfolioBuilder, TargetWeightStore
from uq.research_chain import BacktestStageAdapter, FileResearchRunStore, PortfolioStageAdapter
from uq.research_chain.adapters import PredictionStageResult
from uq.research_chain.contracts import PublishedState
from uq.research_chain.owning_contracts import BacktestConfigStore
from uq.research_chain.resolver import ResolvedStageBinding

DIGEST = "0" * 64
RUNNER = {"code_fingerprint": DIGEST, "environment_profile": "locked-test", "lock_digest_sha256": DIGEST}
DATES = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
INSTRUMENTS = ["INST0", "INST1", "INST2"]


def _definition_decision(generation_id: str = "0" * 64) -> dict:
    decision = create_reviewed_quality_decision(
        binding_type="portfolio_definition_v1", policy="reject_all", status="passed",
        checks=[
            {"name": "weight_scheme_valid", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "constraints_within_bounds", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "universe_binding_resolved", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
        ], errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    report, _ = bind_reviewed_quality_decision(
        decision, binding_type="portfolio_definition_v1",
        subject_generation_id=generation_id,
    )
    return {
        "contract_version": 1, "schema_version": "1.0.0",
        "binding_type": "portfolio_definition_v1",
        "subject_generation_id": generation_id,
        "subject_manifest_digest_sha256": None,
        "owning_report": report,
        "decision_checksum_sha256": report["report_checksum_sha256"],
        "provider_id": "external-model-quality-reviewer-v1",
        "trust_anchor_id": report["key_id"],
    }


def _weights_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="target_weights_v1", policy="reject_all", status="passed",
        checks=[
            {"name": "weight_sum_within_reserve", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "no_nan_inf_weights", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "instruments_in_universe", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
        ], errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def _result_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="backtest_result_v1", policy="reject_all", status="passed",
        checks=[
            {"name": "equity_curve_finite_positive", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "turnover_non_negative", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "lineage_generations_resolved", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
        ], errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def _price_panel() -> pd.DataFrame:
    rows = []
    for date_index, date in enumerate(DATES):
        for instrument_index, instrument in enumerate(INSTRUMENTS):
            base = 20.0 + instrument_index * 5.0
            drift = 1.01 ** date_index
            rows.append({
                "date": date, "instrument": instrument,
                "open": round(base * drift, 4), "close": round(base * drift * 1.005, 4),
                "volume": 1_000_000.0,
            })
    return pd.DataFrame(rows).set_index(["date", "instrument"])


def _backtest_config(universe_generation_id: str, price_checksum: str) -> dict:
    config = {
        "contract_version": 1, "schema_version": "1.0.0",
        "backtest_name": "research-baseline", "start_date": DATES[0], "end_date": DATES[-1],
        "execution_model": {
            "type": "daily_t1_open", "board_lot": 100,
            "sellable_quantity_rule": "prior_day_holding_only", "volume_participation_cap": 0.5,
        },
        "cost_model": {"commission_bps": 2.5, "stamp_duty_bps": 5.0, "slippage_bps": 1.0},
        "limit_rules": {"limit_ratio": 0.1, "prev_close_source": "raw_prev_close", "adjustment_basis": "raw"},
        "calendar_binding": {"generation_id": "a" * 64, "checksum_sha256": "b" * 64},
        "price_source_binding": {"dataset_generation_id": "c" * 64, "data_checksum_sha256": price_checksum},
        "universe_binding": {"snapshot_generation_id": universe_generation_id},
        "corporate_action_binding": {"dataset_generation_id": "d" * 64, "data_checksum_sha256": "e" * 64},
        "suspension_binding": {"dataset_generation_id": "f" * 64, "data_checksum_sha256": DIGEST},
        "initial_capital": 1_000_000.0, "run_id": RUN_ID,
        "created_at": "2026-01-30T07:00:00+00:00",
        "quality_report_checksum_sha256": DIGEST,
        "generation_id": DIGEST, "manifest_digest_sha256": DIGEST,
    }
    config["generation_id"], config["manifest_digest_sha256"] = model_manifest_identities(
        config, schema_name="backtest_config"
    )
    return config


def _prediction_result() -> PredictionStageResult:
    frame = pd.DataFrame({
        "instrument": INSTRUMENTS,
        "score": [0.9, 0.5, 0.1],
    })
    manifest = {
        "generation_id": "9" * 64,
        "manifest_digest_sha256": "a" * 64,
        "data_checksum_sha256": "b" * 64,
        "created_at": "2026-01-30T07:00:00+00:00",
    }
    return PredictionStageResult(
        manifest=manifest, frame=frame,
        published_state=PublishedState(Path("unused"), manifest["manifest_digest_sha256"]),
    )


def _extended_plan(tmp_path: Path, prediction_generation_id: str, config_generation_id: str, config_digest: str):
    dataset_adapter, original_plan, _ = _prepare_dataset_store(tmp_path)
    universe_binding = next(
        binding for binding in original_plan.stage_bindings if binding.output_family == "universe_snapshot"
    )
    full_request = json.loads(
        (Path(__file__).parents[1] / "evidence/research-chain/phase-0/fixtures/research_run_request-valid.json").read_text()
    )
    full_request["portfolio_definition_template"]["scheme_parameters"]["n"] = 2
    full_request["portfolio_definition_template"]["constraints"]["max_single_weight"] = 0.6
    full_request["backtest_config_binding"].update({
        "generation_id": config_generation_id, "manifest_digest_sha256": config_digest,
    })
    full_request["request_content_generation_id"] = DIGEST
    full_request["manifest_digest_sha256"] = DIGEST
    full_request["request_content_generation_id"], full_request["manifest_digest_sha256"] = research_contract_identities(
        full_request, schema_name="research_run_request"
    )
    request = {
        **full_request,
        "request_content_generation_id": full_request["request_content_generation_id"],
        "manifest_digest_sha256": full_request["manifest_digest_sha256"],
        "run_id": RUN_ID,
        "universe_id": "research-whitelist",
    }
    prediction_binding = ResolvedStageBinding(
        stage="prediction_publication", output_family="prediction_set",
        generation_id=prediction_generation_id, manifest_digest_sha256="a" * 64,
        data_checksum_sha256="b" * 64,
    )
    config_binding = ResolvedStageBinding(
        stage="backtest_execution", output_family="backtest_config",
        generation_id=config_generation_id, manifest_digest_sha256=config_digest,
        data_checksum_sha256="d" * 64,
    )
    plan = dataclasses.replace(original_plan, request=request)
    plan = dataclasses.replace(plan, stage_bindings=(*plan.stage_bindings, prediction_binding, config_binding))
    return dataset_adapter, plan, universe_binding


def _temp_root(tmp_path: Path) -> Path:
    return tmp_path / "governed_inputs"


def _definition_generation(plan, universe_generation_id: str) -> str:
    template = plan.request["portfolio_definition_template"]
    definition = {
        "contract_version": 1, "schema_version": "1.0.0",
        "portfolio_name": template["portfolio_name"],
        "weight_scheme": template["weight_scheme"],
        "scheme_parameters": template["scheme_parameters"],
        "score_policy": template["score_policy"],
        "constraints": template["constraints"],
        "rebalance_schedule": template["rebalance_schedule"],
        "universe_snapshot_generation_id": universe_generation_id,
        "industry_source_binding": template["industry_source_binding"],
        "prediction_set_generation_id": "9" * 64,
        "run_id": RUN_ID, "created_at": "2026-01-30T07:00:00+00:00",
        "quality_report_checksum_sha256": DIGEST,
        "manifest_digest_sha256": DIGEST, "generation_id": DIGEST,
    }
    generation, _ = model_manifest_identities(definition, schema_name="portfolio_definition")
    return generation


def _publish_backtest_config(root: Path, universe_generation_id: str, price_checksum: str) -> dict:
    config = _backtest_config(universe_generation_id, price_checksum)
    directory = root / "backtest_configs" / f"generation={config['generation_id']}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(config, sort_keys=True))
    return config


def _prepare_portfolio(tmp_path: Path, decision_dates: list[str] | None = None):
    dataset_adapter, plan, universe_binding = _extended_plan(tmp_path, "9" * 64, "c" * 64, "c" * 64)
    config = _publish_backtest_config(tmp_path, universe_binding.generation_id, "b" * 64)
    plan = dataclasses.replace(plan, stage_bindings=tuple(
        ResolvedStageBinding(
            stage="backtest_execution", output_family="backtest_config",
            generation_id=config["generation_id"],
            manifest_digest_sha256=config["manifest_digest_sha256"],
            data_checksum_sha256=config["price_source_binding"]["data_checksum_sha256"],
        ) if binding.output_family == "backtest_config" else binding
        for binding in plan.stage_bindings
    ))
    definition_generation = _definition_generation(plan, universe_binding.generation_id)
    portfolio = PortfolioStageAdapter(
        universe_store=dataset_adapter.universe_store,
        portfolio_builder=PortfolioBuilder(tmp_path),
        target_weight_store=TargetWeightStore(tmp_path),
        run_store=FileResearchRunStore(tmp_path),
    )
    result = portfolio.run(
        plan, prediction_stage_result=_prediction_result(),
        decision_dates=decision_dates or ["2026-01-05"],
        definition_quality_decision=_definition_decision(definition_generation),
        weights_quality_decision=_weights_decision(), runner_identity=RUNNER,
        created_at="2026-01-30T07:00:00+00:00",
    )
    return plan, portfolio, result, config


def test_portfolio_stage_binds_prediction_and_universe(tmp_path: Path) -> None:
    _, _, portfolio_result, _ = _prepare_portfolio(tmp_path)
    definition = portfolio_result.definition_manifest
    assert definition["prediction_set_generation_id"] == "9" * 64
    manifest = portfolio_result.target_weight_manifests["2026-01-05"]
    assert manifest["prediction_set_generation_id"] == "9" * 64
    assert manifest["previous_target_weights_generation_id"] is None
    assert portfolio_result.published_state.manifest_path.is_file()
    assert len(portfolio_result.target_weight_frames["2026-01-05"]) == 2


def test_portfolio_chain_links_previous_target_generation(tmp_path: Path) -> None:
    _, _, result, _ = _prepare_portfolio(tmp_path, ["2026-01-05", "2026-01-06"])
    first = result.target_weight_manifests["2026-01-05"]
    second = result.target_weight_manifests["2026-01-06"]
    assert second["previous_target_weights_generation_id"] == first["generation_id"]


def test_portfolio_prediction_binding_mismatch_fails(tmp_path: Path) -> None:
    _, plan, universe_binding = _extended_plan(tmp_path, "9" * 64, "c" * 64, "c" * 64)
    _publish_backtest_config(tmp_path, universe_binding.generation_id, "b" * 64)
    wrong = _prediction_result()
    wrong.manifest["manifest_digest_sha256"] = "e" * 64
    adapter = PortfolioStageAdapter(
        universe_store=None, portfolio_builder=PortfolioBuilder(tmp_path),
        target_weight_store=TargetWeightStore(tmp_path), run_store=FileResearchRunStore(tmp_path),
    )
    with pytest.raises(ContractError, match="portfolio prediction manifest digest mismatch"):
        adapter.run(
            plan, prediction_stage_result=wrong, decision_dates=["2026-01-05"],
            definition_quality_decision=_definition_decision("f" * 64),
            weights_quality_decision=_weights_decision(), runner_identity=RUNNER,
        )


def test_portfolio_weight_overwrite_fails(tmp_path: Path) -> None:
    dataset_adapter, plan, universe_binding = _extended_plan(tmp_path, "9" * 64, "c" * 64, "c" * 64)
    _publish_backtest_config(tmp_path, universe_binding.generation_id, "b" * 64)
    adapter = PortfolioStageAdapter(
        universe_store=dataset_adapter.universe_store,
        portfolio_builder=PortfolioBuilder(tmp_path),
        target_weight_store=TargetWeightStore(tmp_path), run_store=FileResearchRunStore(tmp_path),
    )
    definition_generation = _definition_generation(plan, universe_binding.generation_id)
    adapter.run(
        plan, prediction_stage_result=_prediction_result(), decision_dates=["2026-01-05"],
        definition_quality_decision=_definition_decision(definition_generation),
        weights_quality_decision=_weights_decision(), runner_identity=RUNNER,
        created_at="2026-01-30T07:00:00+00:00",
    )
    with pytest.raises(ContractError, match="target-weight partition already exists"):
        adapter.run(
            plan, prediction_stage_result=_prediction_result(), decision_dates=["2026-01-05"],
            definition_quality_decision=_definition_decision(definition_generation),
            weights_quality_decision=_weights_decision(), runner_identity=RUNNER,
            created_at="2026-01-30T07:00:00+00:00",
        )


def test_wrong_portfolio_quality_decision_fails(tmp_path: Path) -> None:
    _, plan, universe_binding = _extended_plan(tmp_path, "9" * 64, "c" * 64, "c" * 64)
    _publish_backtest_config(tmp_path, universe_binding.generation_id, "b" * 64)
    adapter = PortfolioStageAdapter(
        universe_store=None, portfolio_builder=PortfolioBuilder(tmp_path),
        target_weight_store=TargetWeightStore(tmp_path), run_store=FileResearchRunStore(tmp_path),
    )
    with pytest.raises(ContractError, match="portfolio stage requires a bound owning quality report"):
        adapter.run(
            plan, prediction_stage_result=_prediction_result(), decision_dates=["2026-01-05"],
            definition_quality_decision=_weights_decision(),
            weights_quality_decision=_weights_decision(), runner_identity=RUNNER,
        )


def test_backtest_stage_publishes_ordered_weight_lineage(tmp_path: Path) -> None:
    plan, portfolio, portfolio_result, config = _prepare_portfolio(tmp_path)
    backtest = BacktestStageAdapter(
        backtest_engine=BacktestEngine(tmp_path),
        backtest_result_store=BacktestResultStore(tmp_path),
        backtest_config_store=BacktestConfigStore(tmp_path),
        run_store=portfolio.run_store,
    )
    result = backtest.run(
        plan, portfolio_stage_result=portfolio_result, price_panel=_price_panel(),
        suspension_dates=None, corporate_action_instruments=None,
        quality_decision=_result_decision(), runner_identity=RUNNER,
    )
    bindings = result.result_manifest["target_weight_bindings"]
    assert [binding["decision_date"] for binding in bindings] == ["2026-01-05"]
    assert bindings[0]["generation_id"] == portfolio_result.target_weight_manifests["2026-01-05"]["generation_id"]
    assert result.result_manifest["portfolio_definition_generation_id"] == portfolio_result.definition_manifest["generation_id"]
    assert result.result_manifest["backtest_config_generation_id"] == config["generation_id"]
    assert result.published_state.manifest_path.is_file()
    assert set(result.result_artifacts) == {"equity_curve", "daily_metrics", "fills"}
    assert len(result.result_artifacts["equity_curve"]) == len(DATES)
    assert len(result.result_artifacts["fills"]) > 0


def test_backtest_rejects_corporate_action_overlap(tmp_path: Path) -> None:
    plan, portfolio, portfolio_result, config = _prepare_portfolio(tmp_path)
    backtest = BacktestStageAdapter(
        backtest_engine=BacktestEngine(tmp_path),
        backtest_result_store=BacktestResultStore(tmp_path),
        backtest_config_store=BacktestConfigStore(tmp_path), run_store=portfolio.run_store,
    )
    with pytest.raises(ContractError, match="corporate-action instruments"):
        backtest.run(
            plan, portfolio_stage_result=portfolio_result, price_panel=_price_panel(),
            suspension_dates=None, corporate_action_instruments={"INST0"},
            quality_decision=_result_decision(), runner_identity=RUNNER,
        )


def test_backtest_rejects_tampered_price_panel(tmp_path: Path) -> None:
    plan, portfolio, portfolio_result, config = _prepare_portfolio(tmp_path)
    prices = _price_panel()
    prices.loc[(DATES[1], "INST0"), "close"] = 0.0
    backtest = BacktestStageAdapter(
        backtest_engine=BacktestEngine(tmp_path),
        backtest_result_store=BacktestResultStore(tmp_path),
        backtest_config_store=BacktestConfigStore(tmp_path), run_store=portfolio.run_store,
    )
    with pytest.raises(ContractError, match="invalid close price"):
        backtest.run(
            plan, portfolio_stage_result=portfolio_result, price_panel=prices,
            suspension_dates=None, corporate_action_instruments=None,
            quality_decision=_result_decision(), runner_identity=RUNNER,
        )
