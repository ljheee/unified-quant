from __future__ import annotations

import json
from pathlib import Path

import pytest

from uq.contracts.model_layer import research_contract_identities
from uq.errors import ContractError
import dataclasses
import pandas as pd

from types import SimpleNamespace

from tests.review_key import REVIEWER_PRIVATE_KEY
from uq.contracts.model_layer import bind_reviewed_quality_decision, create_reviewed_quality_decision, research_contract_identities
from uq.models.qlib_export import QlibDatasetExporter
from uq.models.predictions import PredictionBuilder
from uq.models.trainer import ArtifactStore
from uq.models.dataset_writer import DatasetWriter
from uq.portfolio.builder import PortfolioBuilder, TargetWeightStore
from uq.research_chain import (
    BacktestStageAdapter,
    DatasetStageAdapter,
    FactorStageAdapter,
    FileResearchRunStore,
    ModelStageAdapter,
    PortfolioStageAdapter,
    PredictionStageAdapter,
    QlibExportStageAdapter,
    ResearchChainRunner,
    ResolvedStageBinding,
)
from uq.backtest.engine import BacktestEngine, BacktestResultStore
from uq.factors.store import FactorStore
from uq.contracts.factor_governance import FactorRegistry
from uq.research_chain.owning_contracts import BacktestConfigStore, BacktestMarketDatasetStore

ROOT = Path(__file__).resolve().parents[1]
requires_qlib = pytest.mark.skipif(
    __import__("importlib.util", fromlist=["find_spec"]).find_spec("qlib") is None,
    reason="Qlib runtime extras are unavailable",
)
REQUEST = ROOT / "evidence/research-chain/phase-0/fixtures/research_run_request-valid.json"
DIGEST = "0" * 64
RUNNER = {"code_fingerprint": DIGEST, "environment_profile": "locked-test", "lock_digest_sha256": DIGEST}
STAGES = [
    "resolve_request",
    "factor_computation",
    "dataset_preparation",
    "qlib_export",
    "model_training",
    "prediction_publication",
    "portfolio_construction",
    "backtest_execution",
    "result_reconciliation",
]


def _binding(stage: str, family: str, seed: str = "1") -> ResolvedStageBinding:
    return ResolvedStageBinding(
        stage=stage,
        output_family=family,
        generation_id=seed.ljust(64, "0")[:64],
        manifest_digest_sha256=("2" + seed).ljust(64, "0")[:64],
        data_checksum_sha256=("3" + seed).ljust(64, "0")[:64],
    )


def _request():
    return json.loads(REQUEST.read_text())


def _result(request: dict, generation_id: str = DIGEST) -> dict:
    stage_records = []
    for stage in STAGES:
        stage_bindings = [] if stage in {"resolve_request", "result_reconciliation"} else [{
            **{key: value for key, value in _binding(stage, "factor_partition").__dict__.items() if key != "stage"},
            "physical_path": f"test/{stage}/manifest.json",
            "quality_decision_checksum_sha256": "0" * 64,
            "failure_reason": None,
        }]
        if stage == "result_reconciliation":
            stage_bindings = [{
                "output_family": "research_run_result",
                "generation_id": "0" * 64,
                "manifest_digest_sha256": "0" * 64,
                "data_checksum_sha256": "0" * 64,
                "physical_path": f"research_runs/results/request={request['request_content_generation_id']}/run={request['run_id']}/result={generation_id}/manifest.json",
                "quality_decision_checksum_sha256": "0" * 64,
                "failure_reason": None,
            }]
        stage_records.append({
            "stage": stage,
            "status": "passed",
            "output_bindings": stage_bindings,
            "failure_reason": None,
        })
    result = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "request_content_generation_id": request["request_content_generation_id"],
        "request_manifest_digest_sha256": request["manifest_digest_sha256"],
        "run_id": request["run_id"],
        "created_at": request["created_at"],
        "runner_identity": dict(RUNNER),
        "stage_records": stage_records,
        "readback_status": {"factor_partition": "passed"},
        "overall_logical_fingerprint": DIGEST,
        "final_status": "passed",
        "result_content_generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    generation, digest = research_contract_identities(result, schema_name="research_run_result")
    result["result_content_generation_id"] = generation
    result["manifest_digest_sha256"] = digest
    final_binding = {
        **result["stage_records"][-1]["output_bindings"][0],
        "generation_id": generation,
        "manifest_digest_sha256": digest,
        "physical_path": f"research_runs/results/request={request['request_content_generation_id']}/run={request['run_id']}/result={generation}/manifest.json",
    }
    result["stage_records"][-1]["output_bindings"] = [final_binding]
    return result


def _runner(tmp_path: Path):
    run_store = FileResearchRunStore(tmp_path)
    return ResearchChainRunner(
        factor_adapter=SimpleNamespace(run_store=run_store),
        dataset_adapter=SimpleNamespace(run_store=run_store),
        export_adapter=SimpleNamespace(run_store=run_store),
        model_adapter=SimpleNamespace(run_store=run_store),
        prediction_adapter=SimpleNamespace(run_store=run_store),
        portfolio_adapter=SimpleNamespace(run_store=run_store),
        backtest_adapter=SimpleNamespace(run_store=run_store),
        run_store=run_store,
    )


def test_result_store_publishes_and_reads_verified_manifest(tmp_path: Path) -> None:
    request = _request()
    result = _result(request)
    store = FileResearchRunStore(tmp_path)
    published = store.publish_result(result, path_policy="strict_v1")
    assert published.manifest_path.is_file()
    assert store.read_result(
        result["result_content_generation_id"], result["manifest_digest_sha256"]
    )["request_content_generation_id"] == request["request_content_generation_id"]
    with pytest.raises(ContractError, match="immutable research manifest already exists"):
        store.publish_result(result, path_policy="strict_v1")


def test_result_store_rejects_tamper_and_wrong_digest(tmp_path: Path) -> None:
    request = _request()
    result = _result(request)
    store = FileResearchRunStore(tmp_path)
    store.publish_result(result, path_policy="strict_v1")
    with pytest.raises(ContractError, match="manifest digest mismatch"):
        store.read_result(result["result_content_generation_id"], "f" * 64)
    manifest_path = store.read_result(
        result["result_content_generation_id"], result["manifest_digest_sha256"]
    )["stage_records"][-1]["output_bindings"][0]["physical_path"]
    target = tmp_path / manifest_path
    document = json.loads(target.read_text())
    document["overall_logical_fingerprint"] = "f" * 64
    target.write_text(json.dumps(document))
    with pytest.raises(ContractError, match="stable content identity mismatch"):
        store.read_result(result["result_content_generation_id"], result["manifest_digest_sha256"])


def test_dry_run_publishes_request_and_resolution_state_only(tmp_path: Path) -> None:
    request = _request()
    runner = _runner(tmp_path)
    plan = SimpleNamespace(
        request=request,
        request_manifest_digest_sha256=request["manifest_digest_sha256"],
    )
    outcome = runner.dry_run(plan, runner_identity=RUNNER, created_at=request["created_at"])
    assert outcome["manifest_path"].is_file()
    assert not (tmp_path / "research_runs" / "results").exists()
    snapshots = FileResearchRunStore(tmp_path).list_state_snapshots(
        request["request_content_generation_id"], request["run_id"]
    )
    assert [item.stage for item in snapshots] == ["resolve_request"]


def test_failed_stage_stops_run_and_publishes_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request()
    runner = _runner(tmp_path)
    plan = SimpleNamespace(
        request=request,
        request_manifest_digest_sha256=request["manifest_digest_sha256"],
        stage_plan_sha256=request["stage_plan_sha256"],
        stage_bindings=(),
    )
    def fail(*args, **kwargs):
        raise ContractError("factor stage forced failure")

    monkeypatch.setattr(runner, "_run_factor", fail)
    with pytest.raises(ContractError, match="factor stage forced failure"):
        runner.execute(plan, runner_identity=RUNNER, quality_decisions={}, scores_by_decision_date={})
    snapshots = FileResearchRunStore(tmp_path).list_state_snapshots(
        request["request_content_generation_id"], request["run_id"]
    )
    assert [item.stage for item in snapshots] == ["resolve_request", "factor_computation"]
    assert snapshots[-1].status == "failed"
    assert not (tmp_path / "research_runs" / "results").exists()


def _research_result_decision(generation_id: str) -> dict:
    unsigned = create_reviewed_quality_decision(
        binding_type="research_run_result_v1",
        policy="reject_all",
        status="passed",
        checks=[
            {"name": "result_stage_completeness_valid", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "readback_status_valid", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
        ],
        errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    report, _ = bind_reviewed_quality_decision(
        unsigned, binding_type="research_run_result_v1",
        subject_generation_id=generation_id, subject_content_sha256=generation_id,
    )
    return {
        "contract_version": 1, "schema_version": "1.0.0",
        "binding_type": "research_run_result_v1",
        "subject_generation_id": generation_id,
        "subject_manifest_digest_sha256": generation_id,
        "owning_report": report,
        "decision_checksum_sha256": report["report_checksum_sha256"],
        "provider_id": "external-model-quality-reviewer-v1",
        "trust_anchor_id": report["key_id"],
    }


def _build_runner(tmp_path: Path, dataset_adapter, factor_store, run_store: FileResearchRunStore):
    from uq.models.qlib_runtime import QlibRuntimeTrainer
    writer = DatasetWriter(tmp_path)
    exporter = QlibDatasetExporter(tmp_path / "qlib_exports")
    portfolio = PortfolioStageAdapter(
        universe_store=dataset_adapter.universe_store,
        portfolio_builder=PortfolioBuilder(tmp_path),
        target_weight_store=TargetWeightStore(tmp_path),
        run_store=run_store,
        prediction_builder=PredictionBuilder(tmp_path),
    )
    return ResearchChainRunner(
        factor_adapter=FactorStageAdapter(factor_store, run_store),
        dataset_adapter=dataset_adapter,
        export_adapter=QlibExportStageAdapter(
            exporter=exporter,
            receipt_builder=__import__("uq.models.qlib_export", fromlist=["QlibInitReceiptBuilder"]).QlibInitReceiptBuilder(),
            dataset_writer=writer,
            run_store=run_store,
        ),
        model_adapter=ModelStageAdapter(
            trainer=QlibRuntimeTrainer(), exporter=exporter, artifact_store=ArtifactStore(tmp_path),
            dataset_writer=writer, universe_store=dataset_adapter.universe_store, run_store=run_store,
        ),
        prediction_adapter=PredictionStageAdapter(PredictionBuilder(tmp_path), run_store),
        portfolio_adapter=portfolio,
        backtest_adapter=BacktestStageAdapter(
            backtest_engine=BacktestEngine(tmp_path),
            backtest_result_store=BacktestResultStore(tmp_path),
            backtest_config_store=BacktestConfigStore(tmp_path),
            run_store=run_store,
            adjusted_price_store=dataset_adapter.adjusted_price_store,
            market_dataset_store=BacktestMarketDatasetStore(tmp_path),
        ),
        run_store=run_store,
    )


def _run_full_chain(tmp_path: Path):
    from tests.test_research_chain_phase4 import _extended_plan
    from tests.test_research_chain_phase2 import (
        _dataset_quality_decision,
        _factor_quality_decision,
        _preprocessing_quality_decision,
        _prepare_dataset_store,
    )
    from tests.test_research_chain_phase3 import (
        artifact_decision,
        export_decision,
        prediction_decision,
        receipt_decision,
        run_decision,
    )
    from tests.test_research_chain_phase4 import (
        _definition_decision,
        _publish_market_inputs,
        _replace_backtest_config,
        _result_decision,
        _weights_decision,
    )

    decision_date = "2026-01-05"
    dataset_adapter, plan, universe_binding = _extended_plan(tmp_path, "9" * 64, "c" * 64, "c" * 64)
    run_store = FileResearchRunStore(tmp_path)
    plan.request.pop("universe_id", None)
    plan.request["window_start_date"] = "2026-01-05"
    plan.request["window_end_date"] = "2026-01-30"
    plan.request["request_content_generation_id"], plan.request["manifest_digest_sha256"] = research_contract_identities(
        plan.request, schema_name="research_run_request"
    )
    plan = dataclasses.replace(plan, request_manifest_digest_sha256=plan.request["manifest_digest_sha256"])
    factor_store = FactorStore(tmp_path, FactorRegistry(ROOT))
    factor_binding = next(binding for binding in plan.stage_bindings if binding.output_family == "factor_partition")
    factor_manifest = factor_store.read_manifest(factor_binding.generation_id)
    adjusted_binding = next(binding for binding in plan.stage_bindings if binding.output_family == "adjusted_price_dataset")
    adjusted_manifest = dataset_adapter.adjusted_price_store.read_manifest(adjusted_binding.generation_id)
    config = _publish_market_inputs(
        tmp_path,
        universe_binding.generation_id,
        adjusted_binding.generation_id,
        adjusted_manifest["data_checksum_sha256"],
    )
    plan = _replace_backtest_config(plan, config)
    runner = _build_runner(tmp_path, dataset_adapter, factor_store, run_store)
    created_at = "2026-01-30T07:00:00+00:00"
    outcome = runner.execute(
        plan,
        runner_identity=RUNNER,
        quality_decisions={
            "factor_partition": _factor_quality_decision(factor_manifest),
            "feature_preprocessing": _preprocessing_quality_decision(),
            "model_dataset": _dataset_quality_decision(),
            "qlib_dataset_export": export_decision(),
            "qlib_init_receipt": receipt_decision(),
            "model_run": run_decision(),
            "model_artifact": artifact_decision,
            "prediction_set": prediction_decision(),
            "portfolio_definition": _definition_decision,
            "target_weights": _weights_decision(),
            "backtest_result": _result_decision(),
            "research_run_result": _research_result_decision,
        },
        scores_by_decision_date={
            decision_date: pd.DataFrame({"instrument": ["INST0", "INST1", "INST2"], "score": [0.9, 0.5, 0.1]})
        },
        decision_dates=[decision_date],
        created_at=created_at,
    )
    assert outcome.result_manifest["final_status"] == "passed"
    stored = run_store.read_result(
        outcome.result_manifest["result_content_generation_id"],
        outcome.result_manifest["manifest_digest_sha256"],
    )
    assert stored["overall_logical_fingerprint"]
    assert [item.stage for item in run_store.list_state_snapshots(
        plan.request["request_content_generation_id"], plan.request["run_id"]
    )] == STAGES[:-1]
    result_binding = outcome.result_manifest["stage_records"][-1]["output_bindings"][0]
    assert result_binding["output_family"] == "research_run_result"
    assert result_binding["physical_path"] == str(outcome.result_path.relative_to(run_store.root))
    assert all(outcome.result_manifest["readback_status"][family] == "passed" for family in {
        "factor_partition", "universe_snapshot", "adjusted_price_dataset", "label_set",
        "feature_preprocessing", "model_dataset", "qlib_dataset_export", "qlib_init_receipt",
        "model_run", "model_artifact", "prediction_set", "target_weights", "backtest_result",
        "backtest_config", "research_run_request",
    })
    return outcome, run_store, plan


@requires_qlib
def test_full_research_chain_end_to_end(tmp_path: Path) -> None:
    outcome, _, _ = _run_full_chain(tmp_path)
    assert outcome.result_manifest["final_status"] == "passed"


def test_cli_dry_run_publishes_only_request_and_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from uq.research_chain.cli import uq_research_run

    request = _request()
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    data_root = tmp_path / "data"
    exit_code = uq_research_run([
        "--project-root", str(ROOT),
        "--data-root", str(data_root),
        "--mode", "dry-run",
        "--request-json", str(request_path),
    ])
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "dry_run_published"
    assert data_root.joinpath("research_runs", "requests").exists()
    assert data_root.joinpath("research_runs", "states").exists()
    assert not data_root.joinpath("factors").exists()
    assert not data_root.joinpath("models").exists()
    assert not data_root.joinpath("research_runs", "results").exists()


def test_cli_execute_requires_external_decisions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from uq.research_chain.cli import uq_research_run

    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_request()))
    exit_code = uq_research_run([
        "--project-root", str(ROOT),
        "--data-root", str(tmp_path / "data"),
        "--mode", "execute",
        "--request-json", str(request_path),
    ])
    assert exit_code == 3
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_failure"


STAGE_METHODS = {
    "factor_computation": "_run_factor",
    "dataset_preparation": "_run_dataset",
    "qlib_export": "_run_export",
    "model_training": "_run_model",
    "prediction_publication": "_run_predictions",
    "portfolio_construction": "_run_portfolio",
    "backtest_execution": "_run_backtest",
}


@pytest.mark.parametrize("stage", [
    "factor_computation",
    "dataset_preparation",
    "qlib_export",
    "model_training",
    "prediction_publication",
    "portfolio_construction",
    "backtest_execution",
])
@requires_qlib
def test_every_stage_failure_stops_later_stages(tmp_path: Path, stage: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise ContractError(f"{stage} forced failure")

    with monkeypatch.context() as patched:
        patched.setattr(ResearchChainRunner, STAGE_METHODS[stage], fail)
        with pytest.raises(ContractError, match=f"{stage} forced failure"):
            _run_full_chain(tmp_path)
    run_store = FileResearchRunStore(tmp_path)
    state_root = run_store.root / "research_runs" / "states"
    request_directory = next(state_root.glob("request=*"))
    run_directory = next(request_directory.glob("run=*"))
    snapshots = run_store.list_state_snapshots(
        request_directory.name.removeprefix("request="),
        run_directory.name.removeprefix("run="),
    )
    assert snapshots[-1].status == "failed"
    assert not (run_store.root / "research_runs" / "results").exists()



@requires_qlib
def test_locked_environment_rebuild_is_reproducible(tmp_path: Path) -> None:
    first, first_store, _ = _run_full_chain(tmp_path / "first")
    second, second_store, _ = _run_full_chain(tmp_path / "second")
    assert first.result_manifest["result_content_generation_id"] == second.result_manifest["result_content_generation_id"]
    assert first.result_manifest["manifest_digest_sha256"] == second.result_manifest["manifest_digest_sha256"]
    assert first.result_manifest["overall_logical_fingerprint"] == second.result_manifest["overall_logical_fingerprint"]
    first_stored = first_store.read_result(
        first.result_manifest["result_content_generation_id"],
        first.result_manifest["manifest_digest_sha256"],
    )
    second_stored = second_store.read_result(
        second.result_manifest["result_content_generation_id"],
        second.result_manifest["manifest_digest_sha256"],
    )

    def stable_bindings(document: dict) -> list[dict]:
        return [
            {key: value for key, value in binding.items() if key != "manifest_digest_sha256"}
            for record in document["stage_records"]
            for binding in record["output_bindings"]
        ]

    assert stable_bindings(first_stored) == stable_bindings(second_stored)
    assert first_stored["readback_status"] == second_stored["readback_status"]
    assert all(
        left["generation_id"] == right["generation_id"]
        and left["data_checksum_sha256"] == right["data_checksum_sha256"]
        for left, right in zip(stable_bindings(first_stored), stable_bindings(second_stored))
    )


def test_remote_evidence_aggregation_is_mechanically_verified() -> None:
    import importlib.util

    script = ROOT / "scripts/verify_research_evidence.py"
    spec = importlib.util.spec_from_file_location("verify_research_evidence", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    summary = module.verify(
        ROOT / "evidence/research-chain/phase-5/final-head-ci/33960583993"
    )
    assert summary["cell_count"] == 10
    assert summary["git_commit"] == "7ee7a8f60a5da61b686cb34550597df149a50f69"


@requires_qlib
def test_cli_execute_provider_wiring_is_complete(tmp_path: Path) -> None:
    from uq.research_chain.cli import _build_runner

    runner = _build_runner(
        project_root=ROOT,
        data_root=tmp_path,
        run_store=FileResearchRunStore(tmp_path),
    )
    assert isinstance(runner.factor_adapter, FactorStageAdapter)
    assert isinstance(runner.dataset_adapter, DatasetStageAdapter)
    assert isinstance(runner.export_adapter, QlibExportStageAdapter)
    assert isinstance(runner.model_adapter, ModelStageAdapter)
    assert isinstance(runner.prediction_adapter, PredictionStageAdapter)
    assert isinstance(runner.portfolio_adapter, PortfolioStageAdapter)
    assert isinstance(runner.backtest_adapter, BacktestStageAdapter)
    assert runner.run_store.root == tmp_path
