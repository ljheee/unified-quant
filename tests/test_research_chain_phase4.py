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
from tests.test_research_chain_phase3 import prediction_decision
from uq.backtest.engine import BacktestEngine, BacktestResultStore
from uq.contracts.model_layer import (
    bind_reviewed_quality_decision,
    create_reviewed_quality_decision,
    model_manifest_identities,
    research_contract_identities,
    sha256_json,
)
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.errors import ContractError
from uq.models.definition import ModelDefinitionBuilder
from uq.models.predictions import PredictionBuilder
from uq.models.trainer import ArtifactStore, ModelTrainer
from uq.portfolio.builder import PortfolioBuilder, TargetWeightStore
from uq.research_chain import BacktestStageAdapter, FileResearchRunStore, PortfolioStageAdapter
from uq.research_chain.adapters import PredictionStageResult
from uq.research_chain.contracts import PublishedState
from uq.research_chain.owning_contracts import AdjustedPriceDatasetStore, BacktestConfigStore, BacktestMarketDatasetStore
from uq.research_chain.resolver import ResolvedStageBinding

DIGEST = "0" * 64
RUNNER = {"code_fingerprint": DIGEST, "environment_profile": "locked-test", "lock_digest_sha256": DIGEST}
DATES = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
INSTRUMENTS = ["INST0", "INST1", "INST2"]


def _definition_decision(generation_id: str = DIGEST) -> dict:
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


def _wrong_binding_type_decision(generation_id: str) -> dict:
    unsigned = create_reviewed_quality_decision(
        binding_type="target_weights_v1", policy="reject_all", status="passed",
        checks=[{"name": "weight_sum_within_reserve", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    report, _ = bind_reviewed_quality_decision(
        unsigned, binding_type="target_weights_v1", subject_generation_id=generation_id,
    )
    return {
        "contract_version": 1, "schema_version": "1.0.0",
        "binding_type": "target_weights_v1",
        "subject_generation_id": generation_id,
        "subject_manifest_digest_sha256": None,
        "owning_report": report,
        "decision_checksum_sha256": report["report_checksum_sha256"],
        "provider_id": "external-model-quality-reviewer-v1",
        "trust_anchor_id": report["key_id"],
    }


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


def _publish_model_artifact(root: Path) -> tuple[str, str, str]:
    import numpy as np

    dataset = pd.DataFrame({
        "instrument": [f"INST{index}" for index in range(4)],
        "datetime": pd.bdate_range("2026-01-01", periods=4),
        "volume_ratio_20d": np.linspace(-0.3, 0.3, 4),
        "label": np.linspace(-0.02, 0.02, 4),
    })
    definition = ModelDefinitionBuilder(run_content_generation_id=DIGEST, reviewed=True).build(
        algorithm="regularized_linear", hyperparameters={"alpha": 1.0},
        seed_policy={"base_seed": 42, "derivation": "fixed"},
        model_set="research-baseline", model_version="1.0.0",
        feature_schema_generation_id=DIGEST, compatible_dataset_versions=["1.0.0"],
        metrics=[{"name": "ic", "direction": "maximize"}], selection_rule="max ic",
    )
    trainer = ModelTrainer(root)
    manifest, artifact_bytes = trainer.train(
        definition=definition, dataset_frame=dataset,
        feature_columns=["volume_ratio_20d"], label_column="label",
    )
    artifact_generation = model_manifest_identities(
        {**manifest, "generation_id": DIGEST, "manifest_digest_sha256": DIGEST},
        schema_name="model_artifact", exclude_fields={"quality_report_checksum_sha256"},
    )[0]
    unsigned = create_reviewed_quality_decision(
        binding_type="model_artifact_v1", policy="reject_all", status="passed",
        checks=[{"name": "artifact_checksum", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    report, _ = bind_reviewed_quality_decision(
        unsigned, binding_type="model_artifact_v1", subject_generation_id=artifact_generation,
        subject_content_sha256=artifact_generation,
    )
    ArtifactStore(root).publish(manifest, artifact_bytes, quality_report=report)
    return (
        manifest["model_run_content_generation_id"], artifact_generation,
        manifest["artifact_checksum_sha256"],
    )


def _publish_prediction(
    root: Path,
    decision_date: str,
    artifact: tuple[str, str, str],
) -> tuple[dict, pd.DataFrame]:
    run_generation_id, artifact_generation, artifact_checksum = artifact
    builder = PredictionBuilder(root)
    scores = pd.DataFrame({"instrument": INSTRUMENTS, "score": [0.9, 0.5, 0.1]})
    manifest, artifact_bytes = builder.build(
        prediction_set_name=f"research_prediction_{decision_date}",
        model_artifact_generation_id=artifact_generation,
        model_artifact_checksum=artifact_checksum,
        input_dataset_generation_id=DIGEST,
        run_generation_id=run_generation_id,
        artifact_store=builder.artifact_store,
        decision_date=decision_date,
        scores=scores,
        eligibility_policy="reviewed-v1",
        eligibility_status="passed",
        quality_decision=prediction_decision(),
    )
    builder.publish(manifest, artifact_bytes)
    return builder.read(manifest["generation_id"], decision_date)


def _market_frame(dataset_type: str, rows: list[dict]) -> pd.DataFrame:
    columns = {
        "calendar": ["date"],
        "suspension": ["date", "instrument", "suspended"],
        "corporate_action": ["date", "instrument"],
    }[dataset_type]
    return pd.DataFrame(rows, columns=columns)


def _publish_market_data(root: Path, dataset_type: str, frame: pd.DataFrame) -> dict:
    generation = sha256_json({"dataset_type": dataset_type, "rows": frame.to_dict(orient="records")})
    directory = root / "backtest_market_data" / f"dataset={generation}"
    directory.mkdir(parents=True, exist_ok=True)
    data_path = directory / "data.parquet"
    frame.to_parquet(data_path, index=False)
    manifest = {
        "generation_id": generation,
        "dataset_type": dataset_type,
        "columns": frame.columns.tolist(),
        "row_count": len(frame),
        "data_checksum_sha256": file_sha256_bytes(data_path.read_bytes()),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    return manifest


def _publish_market_inputs(
    root: Path,
    universe_generation_id: str,
    price_generation_id: str,
    price_checksum: str,
    *,
    universe_override: str | None = None,
    suspension=None,
    corporate=None,
) -> dict:
    price_frame = AdjustedPriceDatasetStore(root).read_frame(price_generation_id)
    trading_dates = sorted(pd.to_datetime(price_frame["datetime"]).dt.strftime("%Y-%m-%d").unique().tolist())
    calendar_manifest = _publish_market_data(
        root, "calendar", _market_frame("calendar", [{"date": date} for date in trading_dates])
    )
    suspension_manifest = _publish_market_data(
        root, "suspension", _market_frame("suspension", suspension if suspension is not None else [])
    )
    corporate_manifest = _publish_market_data(
        root, "corporate_action", _market_frame("corporate_action", corporate if corporate is not None else [])
    )
    config = _backtest_config(
        universe_generation_id, price_generation_id, price_checksum,
        universe_override=universe_override,
        calendar_binding={"generation_id": calendar_manifest["generation_id"], "checksum_sha256": calendar_manifest["data_checksum_sha256"]},
        corporate_action_binding={"dataset_generation_id": corporate_manifest["generation_id"], "data_checksum_sha256": corporate_manifest["data_checksum_sha256"]},
        suspension_binding={"dataset_generation_id": suspension_manifest["generation_id"], "data_checksum_sha256": suspension_manifest["data_checksum_sha256"]},
    )
    directory = root / "backtest_configs" / f"generation={config['generation_id']}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(config, sort_keys=True))
    return config


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


def _backtest_config(
    universe_generation_id: str,
    price_generation_id: str,
    price_checksum: str,
    *,
    universe_override: str | None = None,
    calendar_binding: dict | None = None,
    corporate_action_binding: dict | None = None,
    suspension_binding: dict | None = None,
) -> dict:
    config = {
        "contract_version": 1, "schema_version": "1.0.0",
        "backtest_name": "research-baseline", "start_date": DATES[0], "end_date": DATES[-1],
        "execution_model": {
            "type": "daily_t1_open", "board_lot": 100,
            "sellable_quantity_rule": "prior_day_holding_only", "volume_participation_cap": 0.5,
        },
        "cost_model": {"commission_bps": 2.5, "stamp_duty_bps": 5.0, "slippage_bps": 1.0},
        "limit_rules": {"limit_ratio": 0.1, "prev_close_source": "raw_prev_close", "adjustment_basis": "raw"},
        "calendar_binding": calendar_binding or {"generation_id": "a" * 64, "checksum_sha256": "b" * 64},
        "price_source_binding": {"dataset_generation_id": price_generation_id, "data_checksum_sha256": price_checksum},
        "universe_binding": {"snapshot_generation_id": universe_override or universe_generation_id},
        "corporate_action_binding": corporate_action_binding or {"dataset_generation_id": "d" * 64, "data_checksum_sha256": "e" * 64},
        "suspension_binding": suspension_binding or {"dataset_generation_id": "f" * 64, "data_checksum_sha256": DIGEST},
        "initial_capital": 1_000_000.0, "run_id": RUN_ID,
        "created_at": "2026-01-30T07:00:00+00:00",
        "quality_report_checksum_sha256": DIGEST,
        "generation_id": DIGEST, "manifest_digest_sha256": DIGEST,
    }
    config["generation_id"], config["manifest_digest_sha256"] = model_manifest_identities(
        config, schema_name="backtest_config"
    )
    return config


def _publish_backtest_config(root: Path, universe_generation_id: str, price_checksum: str, **kwargs) -> dict:
    config = _backtest_config(universe_generation_id, price_checksum, **kwargs)
    directory = root / "backtest_configs" / f"generation={config['generation_id']}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(config, sort_keys=True))
    return config


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


def _definition_generation(plan, universe_generation_id: str, prediction_generation_id: str) -> str:
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
        "prediction_set_generation_id": prediction_generation_id,
        "run_id": RUN_ID, "created_at": "2026-01-30T07:00:00+00:00",
        "quality_report_checksum_sha256": DIGEST,
        "manifest_digest_sha256": DIGEST, "generation_id": DIGEST,
    }
    generation, _ = model_manifest_identities(definition, schema_name="portfolio_definition")
    return generation


def _prepare_portfolio(tmp_path: Path, decision_dates: list[str] | None = None, *, publish_all_predictions: bool = True):
    dates = decision_dates or ["2026-01-05"]
    dataset_adapter, plan, universe_binding = _extended_plan(tmp_path, "9" * 64, "c" * 64, "c" * 64)
    adjusted_binding = next(
        binding for binding in plan.stage_bindings
        if binding.output_family == "adjusted_price_dataset"
    )
    adjusted_store = dataset_price_store(tmp_path)
    adjusted_manifest = adjusted_store.read_manifest(adjusted_binding.generation_id)
    config = _publish_market_inputs(
        tmp_path, universe_binding.generation_id, adjusted_binding.generation_id,
        adjusted_manifest["data_checksum_sha256"],
    )
    artifact = _publish_model_artifact(tmp_path)
    prediction_manifests: dict[str, dict] = {}
    prediction_frames: dict[str, pd.DataFrame] = {}
    for decision_date in (DATES if publish_all_predictions else dates):
        prediction_manifests[decision_date], prediction_frames[decision_date] = _publish_prediction(
            tmp_path, decision_date, artifact
        )
    first_manifest = prediction_manifests[dates[0]]
    extra_prediction_bindings = tuple(
        ResolvedStageBinding(
            stage="prediction_publication",
            output_family="prediction_set",
            generation_id=prediction_manifests[date]["generation_id"],
            manifest_digest_sha256=prediction_manifests[date]["manifest_digest_sha256"],
            data_checksum_sha256=prediction_manifests[date]["data_checksum_sha256"],
        )
        for date in dates[1:]
    )
    plan = dataclasses.replace(plan, stage_bindings=tuple(
        ResolvedStageBinding(
            stage="prediction_publication" if binding.output_family == "prediction_set" else "backtest_execution" if binding.output_family == "backtest_config" else binding.stage,
            output_family=binding.output_family,
            generation_id=first_manifest["generation_id"] if binding.output_family == "prediction_set" else binding.generation_id,
            manifest_digest_sha256=first_manifest["manifest_digest_sha256"] if binding.output_family == "prediction_set" else binding.manifest_digest_sha256,
            data_checksum_sha256=first_manifest["data_checksum_sha256"] if binding.output_family == "prediction_set" else binding.data_checksum_sha256,
        ) if binding.output_family in {"prediction_set", "backtest_config"} else binding
        for binding in plan.stage_bindings
    ) + extra_prediction_bindings)
    plan = dataclasses.replace(plan, stage_bindings=tuple(
        ResolvedStageBinding(
            stage="backtest_execution", output_family="backtest_config",
            generation_id=config["generation_id"],
            manifest_digest_sha256=config["manifest_digest_sha256"],
            data_checksum_sha256=config["price_source_binding"]["data_checksum_sha256"],
        ) if binding.output_family == "backtest_config" else binding
        for binding in plan.stage_bindings
    ))
    definition_generation = _definition_generation(
        plan, universe_binding.generation_id, first_manifest["generation_id"]
    )
    portfolio = PortfolioStageAdapter(
        universe_store=dataset_adapter.universe_store,
        portfolio_builder=PortfolioBuilder(tmp_path),
        target_weight_store=TargetWeightStore(tmp_path),
        run_store=FileResearchRunStore(tmp_path),
        prediction_builder=PredictionBuilder(tmp_path),
    )
    first_result = PredictionStageResult(
        manifest=first_manifest,
        frame=prediction_frames[dates[0]],
        published_state=PublishedState(Path("unused"), first_manifest["manifest_digest_sha256"]),
    )
    result = portfolio.run(
        plan, prediction_stage_result=first_result,
        decision_dates=dates,
        prediction_generation_by_date={
            date: prediction_manifests[date]['generation_id']
            for date in dates
        },
        definition_quality_decision=_definition_decision(definition_generation),
        weights_quality_decision=_weights_decision(), runner_identity=RUNNER,
        created_at="2026-01-30T07:00:00+00:00",
    )
    return plan, portfolio, result, config


def test_portfolio_stage_binds_prediction_and_universe(tmp_path: Path) -> None:
    plan, _, portfolio_result, _ = _prepare_portfolio(tmp_path)
    prediction_generation = first_prediction_generation(plan)
    definition = portfolio_result.definition_manifest
    assert definition["prediction_set_generation_id"] == prediction_generation
    manifest = portfolio_result.target_weight_manifests["2026-01-05"]
    assert manifest["prediction_set_generation_id"] == prediction_generation
    assert manifest["previous_target_weights_generation_id"] is None
    assert portfolio_result.published_state.manifest_path.is_file()
    assert len(portfolio_result.target_weight_frames["2026-01-05"]) == 2


def first_prediction_generation(plan) -> str:
    return next(
        binding.generation_id for binding in plan.stage_bindings
        if binding.output_family == "prediction_set"
    )


def test_portfolio_chain_reads_daily_predictions(tmp_path: Path) -> None:
    _, _, result, _ = _prepare_portfolio(tmp_path, ["2026-01-05", "2026-01-06"])
    first = result.target_weight_manifests["2026-01-05"]
    second = result.target_weight_manifests["2026-01-06"]
    assert second["previous_target_weights_generation_id"] == first["generation_id"]
    assert first["prediction_set_generation_id"] != second["prediction_set_generation_id"]


def test_portfolio_prediction_binding_mismatch_fails(tmp_path: Path) -> None:
    plan, dataset_adapter, _, _ = _prepare_portfolio(tmp_path)
    adapter = PortfolioStageAdapter(
        universe_store=None, portfolio_builder=PortfolioBuilder(tmp_path),
        target_weight_store=TargetWeightStore(tmp_path), run_store=FileResearchRunStore(tmp_path),
        prediction_builder=PredictionBuilder(tmp_path),
    )
    with pytest.raises(ContractError, match="portfolio prediction manifest digest mismatch"):
        adapter.run(
            plan, prediction_stage_result=PredictionStageResult(
                manifest={
                    "generation_id": first_prediction_generation(plan),
                    "manifest_digest_sha256": "e" * 64,
                    "data_checksum_sha256": "f" * 64,
                    "created_at": "2026-01-30T07:00:00+00:00",
                },
                frame=pd.DataFrame({"instrument": INSTRUMENTS, "score": [0.9, 0.5, 0.1]}),
                published_state=PublishedState(Path("unused"), "e" * 64),
            ),
            decision_dates=["2026-01-05"],
            definition_quality_decision=_definition_decision("f" * 64),
            weights_quality_decision=_weights_decision(), runner_identity=RUNNER,
        )


def test_portfolio_missing_prediction_fails_closed(tmp_path: Path) -> None:
    plan, dataset_adapter, result, _ = _prepare_portfolio(tmp_path, ["2026-01-05"], publish_all_predictions=False)
    prediction_generation = first_prediction_generation(plan)
    prediction_manifest, _ = PredictionBuilder(tmp_path).read(prediction_generation, "2026-01-05")
    adapter = PortfolioStageAdapter(
        universe_store=dataset_adapter.universe_store, portfolio_builder=PortfolioBuilder(tmp_path),
        target_weight_store=TargetWeightStore(tmp_path), run_store=FileResearchRunStore(tmp_path),
        prediction_builder=PredictionBuilder(tmp_path),
    )
    frame = pd.DataFrame({"instrument": INSTRUMENTS, "score": [0.9, 0.5, 0.1]})
    with pytest.raises(ContractError, match="unpublished or incomplete prediction"):
        adapter.run(
            plan, prediction_stage_result=PredictionStageResult(
                manifest=prediction_manifest,
                frame=frame,
                published_state=PublishedState(Path("unused"), prediction_manifest["manifest_digest_sha256"]),
            ),
            decision_dates=["2026-01-06"],
            prediction_generation_by_date={"2026-01-06": "a" * 64},
            definition_quality_decision=_definition_decision(result.definition_manifest["generation_id"]),
            weights_quality_decision=_weights_decision(), runner_identity=RUNNER,
        )


def test_portfolio_tampered_prediction_fails_closed(tmp_path: Path) -> None:
    plan, dataset_adapter, result, _ = _prepare_portfolio(tmp_path)
    prediction_generation = first_prediction_generation(plan)
    prediction_manifest, prediction_frame = PredictionBuilder(tmp_path).read(
        prediction_generation, "2026-01-05"
    )
    prediction_path = next(
        (tmp_path / "predictions").glob(f"prediction_set={prediction_generation}/date=2026-01-05/data.parquet")
    )
    prediction_path.write_bytes(prediction_path.read_bytes() + b"tampered")
    adapter = PortfolioStageAdapter(
        universe_store=dataset_adapter.universe_store, portfolio_builder=PortfolioBuilder(tmp_path),
        target_weight_store=TargetWeightStore(tmp_path), run_store=FileResearchRunStore(tmp_path),
        prediction_builder=PredictionBuilder(tmp_path),
    )
    with pytest.raises(ContractError, match="tampered prediction data"):
        adapter.run(
            plan, prediction_stage_result=PredictionStageResult(
                manifest=prediction_manifest,
                frame=prediction_frame,
                published_state=result.published_state,
            ),
            decision_dates=["2026-01-05"],
            definition_quality_decision=_definition_decision(result.definition_manifest["generation_id"]),
            weights_quality_decision=_weights_decision(), runner_identity=RUNNER,
        )


def test_portfolio_universe_binding_mismatch_fails(tmp_path: Path) -> None:
    dataset_adapter, plan, universe_binding = _extended_plan(tmp_path, "9" * 64, "c" * 64, "c" * 64)
    adjusted_binding = next(
        binding for binding in plan.stage_bindings
        if binding.output_family == "adjusted_price_dataset"
    )
    adjusted_manifest = dataset_price_store(tmp_path).read_manifest(adjusted_binding.generation_id)
    config = _publish_market_inputs(
        tmp_path, "a" * 64, adjusted_binding.generation_id,
        adjusted_manifest["data_checksum_sha256"], universe_override="a" * 64,
    )
    artifact = _publish_model_artifact(tmp_path)
    manifest, _ = _publish_prediction(tmp_path, DATES[0], artifact)
    plan = dataclasses.replace(plan, stage_bindings=tuple(
        ResolvedStageBinding(
            stage="prediction_publication", output_family="prediction_set",
            generation_id=manifest["generation_id"],
            manifest_digest_sha256=manifest["manifest_digest_sha256"],
            data_checksum_sha256=manifest["data_checksum_sha256"],
        ) if binding.output_family == "prediction_set" else binding
        for binding in plan.stage_bindings
    ))
    adapter = PortfolioStageAdapter(
        universe_store=dataset_adapter.universe_store,
        portfolio_builder=PortfolioBuilder(tmp_path),
        target_weight_store=TargetWeightStore(tmp_path), run_store=FileResearchRunStore(tmp_path),
        prediction_builder=PredictionBuilder(tmp_path),
    )
    with pytest.raises(ContractError, match="portfolio quality decision is bound to another definition"):
        adapter.run(
            plan, prediction_stage_result=PredictionStageResult(
                manifest=manifest,
                frame=pd.DataFrame({"instrument": INSTRUMENTS, "score": [0.9, 0.5, 0.1]}),
                published_state=PublishedState(Path("unused"), manifest["manifest_digest_sha256"]),
            ),
            decision_dates=["2026-01-05"],
            definition_quality_decision=_definition_decision("f" * 64),
            weights_quality_decision=_weights_decision(), runner_identity=RUNNER,
        )


def test_portfolio_weight_overwrite_fails(tmp_path: Path) -> None:
    plan, portfolio, result, _ = _prepare_portfolio(tmp_path)
    definition_generation = result.definition_manifest["generation_id"]
    prediction_manifest, _ = PredictionBuilder(tmp_path).read(
        first_prediction_generation(plan), "2026-01-05"
    )
    prediction_result = PredictionStageResult(
        manifest=prediction_manifest,
        frame=pd.DataFrame({"instrument": INSTRUMENTS, "score": [0.9, 0.5, 0.1]}),
        published_state=result.published_state,
    )
    with pytest.raises(ContractError, match="target-weight partition already exists"):
        portfolio.run(
            plan, prediction_stage_result=prediction_result, decision_dates=["2026-01-05"],
            definition_quality_decision=_definition_decision(definition_generation),
            weights_quality_decision=_weights_decision(), runner_identity=RUNNER,
            created_at="2026-01-30T07:00:00+00:00",
        )


def test_wrong_portfolio_quality_decision_fails(tmp_path: Path) -> None:
    plan, dataset_adapter, result, _ = _prepare_portfolio(tmp_path)
    prediction_manifest, _ = PredictionBuilder(tmp_path).read(
        first_prediction_generation(plan), "2026-01-05"
    )
    adapter = PortfolioStageAdapter(
        universe_store=None, portfolio_builder=PortfolioBuilder(tmp_path),
        target_weight_store=TargetWeightStore(tmp_path), run_store=FileResearchRunStore(tmp_path),
        prediction_builder=PredictionBuilder(tmp_path),
    )
    with pytest.raises(ContractError, match="portfolio quality decision binding mismatch"):
        adapter.run(
            plan, prediction_stage_result=PredictionStageResult(
                manifest=prediction_manifest,
                frame=pd.DataFrame({"instrument": INSTRUMENTS, "score": [0.9, 0.5, 0.1]}),
                published_state=result.published_state,
            ),
            decision_dates=["2026-01-05"],
            definition_quality_decision=_wrong_binding_type_decision(result.definition_manifest["generation_id"]),
            weights_quality_decision=_weights_decision(), runner_identity=RUNNER,
        )


def test_backtest_stage_publishes_ordered_weight_lineage(tmp_path: Path) -> None:
    plan, portfolio, portfolio_result, config = _prepare_portfolio(tmp_path)
    backtest = BacktestStageAdapter(
        backtest_engine=BacktestEngine(tmp_path),
        backtest_result_store=BacktestResultStore(tmp_path),
        backtest_config_store=BacktestConfigStore(tmp_path), run_store=portfolio.run_store,
        adjusted_price_store=dataset_price_store(tmp_path),
        market_dataset_store=BacktestMarketDatasetStore(tmp_path),
    )
    result = backtest.run(
        plan, portfolio_stage_result=portfolio_result,
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


def dataset_price_store(tmp_path: Path):
    from uq.research_chain.owning_contracts import AdjustedPriceDatasetStore

    return AdjustedPriceDatasetStore(tmp_path)


def test_backtest_rejects_universe_binding_mismatch(tmp_path: Path) -> None:
    plan, portfolio, portfolio_result, wrong_config = _prepare_portfolio(tmp_path)
    adjusted_binding = next(
        binding for binding in plan.stage_bindings
        if binding.output_family == "adjusted_price_dataset"
    )
    adjusted_manifest = dataset_price_store(tmp_path).read_manifest(adjusted_binding.generation_id)
    config = _publish_market_inputs(
        tmp_path, "a" * 64, adjusted_binding.generation_id,
        adjusted_manifest["data_checksum_sha256"], universe_override="a" * 64,
    )
    plan = dataclasses.replace(plan, stage_bindings=tuple(
        ResolvedStageBinding(
            stage="backtest_execution", output_family="backtest_config",
            generation_id=config["generation_id"],
            manifest_digest_sha256=config["manifest_digest_sha256"],
            data_checksum_sha256=config["price_source_binding"]["data_checksum_sha256"],
        ) if binding.output_family == "backtest_config" else binding
        for binding in plan.stage_bindings
    ))
    backtest = BacktestStageAdapter(
        backtest_engine=BacktestEngine(tmp_path),
        backtest_result_store=BacktestResultStore(tmp_path),
        backtest_config_store=BacktestConfigStore(tmp_path), run_store=portfolio.run_store,
        adjusted_price_store=dataset_price_store(tmp_path),
        market_dataset_store=BacktestMarketDatasetStore(tmp_path),
    )
    with pytest.raises(ContractError, match="backtest universe binding mismatch"):
        backtest.run(
            plan, portfolio_stage_result=portfolio_result,
            quality_decision=_result_decision(), runner_identity=RUNNER,
        )


def test_backtest_rejects_forged_portfolio_state(tmp_path: Path) -> None:
    plan, portfolio, portfolio_result, _ = _prepare_portfolio(tmp_path)
    forged = dataclasses.replace(
        portfolio_result,
        published_state=PublishedState(
            portfolio_result.published_state.manifest_path, "f" * 64,
        ),
    )
    backtest = BacktestStageAdapter(
        backtest_engine=BacktestEngine(tmp_path),
        backtest_result_store=BacktestResultStore(tmp_path),
        backtest_config_store=BacktestConfigStore(tmp_path), run_store=portfolio.run_store,
        adjusted_price_store=dataset_price_store(tmp_path),
        market_dataset_store=BacktestMarketDatasetStore(tmp_path),
    )
    with pytest.raises(ContractError, match="research state manifest digest mismatch"):
        backtest.run(
            plan, portfolio_stage_result=forged,
            quality_decision=_result_decision(), runner_identity=RUNNER,
        )


def _replace_backtest_config(plan, config: dict):
    return dataclasses.replace(plan, stage_bindings=tuple(
        ResolvedStageBinding(
            stage="backtest_execution", output_family="backtest_config",
            generation_id=config["generation_id"],
            manifest_digest_sha256=config["manifest_digest_sha256"],
            data_checksum_sha256=config["price_source_binding"]["data_checksum_sha256"],
        ) if binding.output_family == "backtest_config" else binding
        for binding in plan.stage_bindings
    ))


def _backtest_adapter(tmp_path: Path, run_store):
    return BacktestStageAdapter(
        backtest_engine=BacktestEngine(tmp_path),
        backtest_result_store=BacktestResultStore(tmp_path),
        backtest_config_store=BacktestConfigStore(tmp_path), run_store=run_store,
        adjusted_price_store=dataset_price_store(tmp_path),
        market_dataset_store=BacktestMarketDatasetStore(tmp_path),
    )


def test_backtest_records_suspension_skip(tmp_path: Path) -> None:
    plan, portfolio, portfolio_result, config = _prepare_portfolio(
        tmp_path, ["2026-01-05", "2026-01-07", "2026-01-08"]
    )
    config = _publish_market_inputs(
        tmp_path, config["universe_binding"]["snapshot_generation_id"],
        config["price_source_binding"]["dataset_generation_id"],
        config["price_source_binding"]["data_checksum_sha256"],
        suspension=[
            {"date": DATES[0], "instrument": "INST1", "suspended": True},
            {"date": DATES[1], "instrument": "INST0", "suspended": True},
            {"date": DATES[1], "instrument": "INST1", "suspended": True},
        ],
    )
    plan = _replace_backtest_config(plan, config)
    backtest = _backtest_adapter(tmp_path, portfolio.run_store)
    result = backtest.run(
        plan, portfolio_stage_result=portfolio_result,
        quality_decision=_result_decision(), runner_identity=RUNNER,
    )
    statuses = result.result_artifacts["fills"]["status"].unique().tolist()
    assert "skipped_suspended" in statuses


def test_backtest_rejects_corporate_action_overlap(tmp_path: Path) -> None:
    plan, portfolio, portfolio_result, config = _prepare_portfolio(tmp_path)
    config = _publish_market_inputs(
        tmp_path, config["universe_binding"]["snapshot_generation_id"],
        config["price_source_binding"]["dataset_generation_id"],
        config["price_source_binding"]["data_checksum_sha256"],
        corporate=[{"date": DATES[1], "instrument": "INST0"}],
    )
    plan = _replace_backtest_config(plan, config)
    backtest = _backtest_adapter(tmp_path, portfolio.run_store)
    with pytest.raises(ContractError, match="corporate-action instruments"):
        backtest.run(
            plan, portfolio_stage_result=portfolio_result,
            quality_decision=_result_decision(), runner_identity=RUNNER,
        )


def test_backtest_rejects_tampered_price_data(tmp_path: Path) -> None:
    plan, portfolio, portfolio_result, config = _prepare_portfolio(tmp_path)
    price_path = next((tmp_path / "canonical").glob("**/date=*/data.parquet"))
    price_path.write_bytes(price_path.read_bytes() + b"tampered")
    backtest = BacktestStageAdapter(
        backtest_engine=BacktestEngine(tmp_path),
        backtest_result_store=BacktestResultStore(tmp_path),
        backtest_config_store=BacktestConfigStore(tmp_path), run_store=portfolio.run_store,
        adjusted_price_store=dataset_price_store(tmp_path),
        market_dataset_store=BacktestMarketDatasetStore(tmp_path),
    )
    with pytest.raises(ContractError, match="tampered adjusted price data checksum"):
        backtest.run(
            plan, portfolio_stage_result=portfolio_result,
            quality_decision=_result_decision(), runner_identity=RUNNER,
        )
