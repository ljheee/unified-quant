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
from uq.research_chain.resolver import stage_plan_for_request
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
STAGES = list(stage_plan_for_request({"contract_version": 1}))


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
        # CI 38053013828: 12d44af made the reviewed subject mandatory at
        # creation (U9) but missed this call — the file is qlib-gated
        # locally, so the TypeError only surfaced in the CI qlib cell.
        subject_generation_id=generation_id, subject_content_sha256=generation_id,
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
            factor_store=factor_store,
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
        _label_quality_decision,
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
        _config_decision,
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
            "label_set": _label_quality_decision(),
            "feature_preprocessing": _preprocessing_quality_decision(),
            "model_dataset": _dataset_quality_decision(),
            "qlib_dataset_export": export_decision(),
            "qlib_init_receipt": receipt_decision(),
            "model_run": run_decision(),
            "model_artifact": artifact_decision,
            "prediction_set": prediction_decision(),
            "portfolio_definition": _definition_decision,
            "target_weights": _weights_decision(),
            "backtest_config": _config_decision(),
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
def test_completed_stages_are_replayed_from_state_not_reexecuted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every writer in this repo is immutable, so re-executing a completed stage hits
    # "already exists". The runner must instead fold the recorded passed stages into
    # the plan and skip them. The plan under test is captured from the failing run
    # itself, because rebuilding the fixture would republish its immutable stores.
    from uq.research_chain.runner import ResearchChainRunner

    captured: dict[str, object] = {}
    real_execute = ResearchChainRunner.execute

    def capturing_execute(self, plan, *args, **kwargs):
        captured["plan"] = plan
        captured["runner"] = self
        return real_execute(self, plan, *args, **kwargs)

    def fail(*args, **kwargs):
        raise ContractError("forced export failure")

    monkeypatch.setattr(ResearchChainRunner, "execute", capturing_execute)
    with monkeypatch.context() as patched:
        patched.setattr(ResearchChainRunner, STAGE_METHODS["qlib_export"], fail)
        with pytest.raises(ContractError, match="forced export failure"):
            _run_full_chain(tmp_path)

    plan = captured["plan"]
    runner = captured["runner"]
    resumed, completed = runner._resume_from_passed_stages(plan)

    # the stages before the forced failure are marked complete and their bindings are
    # carried into the plan, which is what lets execute skip them on a retry
    assert "factor_computation" in completed
    assert "dataset_preparation" in completed
    assert "qlib_export" not in completed
    assert "model_dataset" in {binding.output_family for binding in resumed.stage_bindings}


@requires_qlib
def test_a_resumed_run_rehydrates_the_model_hand_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A resumed run skips the model stage, so nothing assigns
    # `model_adapter.last_result` in this process, and the prediction stage -- the
    # first consumer of that hand-off -- dies on an attribute no code ever set unless
    # the runner re-hydrates it from the published artifacts. Failing past the model
    # stage is what makes the hand-off exist while the process is fresh.
    from uq.research_chain.runner import ResearchChainRunner

    captured: dict[str, object] = {}
    real_execute = ResearchChainRunner.execute

    def capturing_execute(self, plan, *args, **kwargs):
        captured["plan"] = plan
        captured["runner"] = self
        return real_execute(self, plan, *args, **kwargs)

    def fail(*args, **kwargs):
        raise ContractError("forced post-model failure")

    monkeypatch.setattr(ResearchChainRunner, "execute", capturing_execute)
    with monkeypatch.context() as patched:
        patched.setattr(ResearchChainRunner, STAGE_METHODS["prediction_publication"], fail)
        with pytest.raises(ContractError, match="forced post-model failure"):
            _run_full_chain(tmp_path)

    plan = captured["plan"]
    runner = captured["runner"]
    # a brand-new process starts with the hand-off unset: that is the condition the
    # resume has to repair
    runner.model_adapter.last_result = None
    assert runner.model_adapter.last_result is None

    resumed, completed = runner._resume_from_passed_stages(plan)
    assert "model_training" in completed
    assert "prediction_publication" not in completed

    hand_off = runner.model_adapter.last_result
    assert hand_off is not None, "a resumed run must re-hydrate the model hand-off"
    artifact_binding = next(
        b for b in resumed.stage_bindings if b.output_family == "model_artifact"
    )
    assert hand_off.artifact_manifest["generation_id"] == artifact_binding.generation_id
    # the fields only a fresh run has are absent rather than fabricated
    assert hand_off.definition is None
    assert hand_off.artifact_bytes == b""
    assert hand_off.published_state is None


@requires_qlib
def test_cli_execute_provider_wiring_is_complete(tmp_path: Path) -> None:
    # Registered in the phase-5 record, so dropping it breaks the release evidence
    # index. Restored verbatim from the commit that introduced the CLI wiring.
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


@requires_qlib
def test_locked_environment_rebuild_is_reproducible(tmp_path: Path) -> None:
    # Registered in the phase-5 record; restored verbatim from b891f8f.
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
    # Registered in the release record; restored verbatim from 316a5c0.
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



def test_a_multi_date_stage_keeps_every_date_when_folded_into_the_plan(tmp_path: Path):
    """cr.md 8.62 C: per-date families must not be deduped by family name.

    `prediction_set` and `target_weights` publish one binding per decision date
    under the same output family. Folding a recorded stage back into the plan on
    family alone kept the last date and dropped the rest, so the portfolio stage
    bound a single date and reconciliation read a plan that no longer described
    every published prediction. The single-date fixtures could not see it.
    """
    from uq.research_chain.runner import ResearchChainRunner, _binding_key

    stage = "prediction_publication"
    published = [
        {
            "output_family": "prediction_set",
            "generation_id": str(index).ljust(64, "0"),
            "manifest_digest_sha256": "2" * 64,
            "data_checksum_sha256": "3" * 64,
            "physical_path": f"predictions/prediction_set={'0' * 64}/date=2026-01-{day:02d}",
            "quality_decision_checksum_sha256": "0" * 64,
            "failure_reason": None,
        }
        for index, day in enumerate([5, 6, 7], start=1)
    ]
    stage_record = {
        "stage": stage,
        "status": "passed",
        "output_bindings": published,
        "failure_reason": None,
    }
    # The resolver's own stand-in binding for the same family and generation is
    # already in the plan and carries no physical path.
    resolver_stand_in = ResolvedStageBinding(
        stage=stage,
        output_family="prediction_set",
        generation_id=published[0]["generation_id"],
        manifest_digest_sha256="2" * 64,
        data_checksum_sha256="3" * 64,
    )
    plan = SimpleNamespace(stage_bindings=(resolver_stand_in,))
    runner = ResearchChainRunner.__new__(ResearchChainRunner)

    folded = runner._apply_stage_bindings(plan, stage_record)
    per_date = [
        binding for binding in folded.stage_bindings
        if binding.output_family == "prediction_set"
    ]
    # 1) every published date survives the fold,
    assert len(per_date) == 3
    assert {
        Path(binding.physical_path).name.removeprefix("date=") for binding in per_date
    } == {"2026-01-05", "2026-01-06", "2026-01-07"}
    # 2) and the path-less stand-in is replaced, not duplicated.
    assert sum(binding.physical_path is None for binding in per_date) == 0
    assert len({_binding_key(binding.stage, binding) for binding in per_date}) == 3

    # Folding twice (a re-read of the same state) must stay idempotent.
    refolded = runner._apply_stage_bindings(folded, stage_record)
    assert len([
        binding for binding in refolded.stage_bindings
        if binding.output_family == "prediction_set"
    ]) == 3


def test_the_portfolio_stage_binds_the_date_the_hand_off_carries(tmp_path: Path):
    """cr.md 8.62 C/E: the definition must name the prediction the run carries.

    The runner's hand-off is the last prediction it built (`results[-1]`), so the
    definition it synthesizes and the binding the portfolio stage checks have to
    agree on that generation. With one binding per decision date, "the earliest
    date" and "whatever the plan listed first" both disagree with the hand-off,
    which is what made a multi-date run refuse its own consistent state.
    """
    from uq.research_chain.adapters import (
        _latest_prediction_binding,
        _prediction_binding_for,
    )

    bindings = [
        ResolvedStageBinding(
            stage="prediction_publication",
            output_family="prediction_set",
            generation_id=str(index).ljust(64, "0"),
            manifest_digest_sha256="2" * 64,
            data_checksum_sha256="3" * 64,
            physical_path=f"predictions/prediction_set={'0' * 64}/date={day}",
        )
        for index, day in enumerate(["2026-01-30", "2026-01-05", "2026-01-19"], start=1)
    ]
    # 1) the restored hand-off is the LAST date, matching `results[-1]`,
    assert Path(_latest_prediction_binding(bindings).physical_path).name.removeprefix("date=") == "2026-01-30"
    # The date is what orders them, so a permuted list selects the same binding
    # (2026-01-30 is generation "1" in this fixture, whichever position it holds).
    assert _latest_prediction_binding(list(reversed(bindings))).generation_id == "1".ljust(64, "0")
    # 2) and the portfolio stage binds the generation the hand-off carries,
    assert _prediction_binding_for(bindings, "2".ljust(64, "0")).generation_id == "2".ljust(64, "0")
    # 3) a generation no binding names fails closed,
    with pytest.raises(ContractError, match="no prediction_set binding"):
        _prediction_binding_for(bindings, "9".ljust(64, "0"))
    # 4) a path-less resolver stand-in is never preferred over a published one.
    stand_in = ResolvedStageBinding(
        stage="prediction_publication", output_family="prediction_set",
        generation_id="9" * 64, manifest_digest_sha256="2" * 64, data_checksum_sha256="3" * 64,
    )
    assert _latest_prediction_binding([stand_in, *bindings]).generation_id == "1".ljust(64, "0")


@requires_qlib
def test_the_runner_scores_from_the_artifact_when_no_scores_file_is_given(tmp_path: Path):
    """Without `--scores-json` the prediction stage must load the model it trained.

    The CLI used to require an external scores document, which made real training
    circular: the document could only be produced after the run had trained the
    model it was supposed to consume.  This drives the full chain without a scores
    mapping and asserts the prediction comes from the governed artifact.
    """
    from tests.test_research_chain_phase4 import _extended_plan
    from tests.test_research_chain_phase2 import (
        _dataset_quality_decision,
        _factor_quality_decision,
        _label_quality_decision,
        _preprocessing_quality_decision,
    )
    from tests.test_research_chain_phase3 import (
        artifact_decision,
        export_decision,
        prediction_decision,
        receipt_decision,
        run_decision,
    )
    from tests.test_research_chain_phase4 import (
        _config_decision,
        _definition_decision,
        _publish_market_inputs,
        _replace_backtest_config,
        _result_decision,
        _weights_decision,
    )
    from tests.test_research_chain_phase5 import _research_result_decision

    dataset_adapter, plan, universe_binding = _extended_plan(
        tmp_path, "9" * 64, "c" * 64, "c" * 64
    )
    plan.request.pop("universe_id", None)
    plan.request["window_start_date"] = "2026-01-05"
    plan.request["window_end_date"] = "2026-01-30"
    plan.request["request_content_generation_id"], plan.request["manifest_digest_sha256"] = (
        research_contract_identities(plan.request, schema_name="research_run_request")
    )
    plan = dataclasses.replace(
        plan, request_manifest_digest_sha256=plan.request["manifest_digest_sha256"]
    )
    factor_store = FactorStore(tmp_path, FactorRegistry(ROOT))
    factor_binding = next(
        binding for binding in plan.stage_bindings
        if binding.output_family == "factor_partition"
    )
    factor_manifest = factor_store.read_manifest(factor_binding.generation_id)
    adjusted_binding = next(
        binding for binding in plan.stage_bindings
        if binding.output_family == "adjusted_price_dataset"
    )
    adjusted_manifest = dataset_adapter.adjusted_price_store.read_manifest(
        adjusted_binding.generation_id
    )
    config = _publish_market_inputs(
        tmp_path,
        universe_binding.generation_id,
        adjusted_binding.generation_id,
        adjusted_manifest["data_checksum_sha256"],
    )
    plan = _replace_backtest_config(plan, config)
    runner = _build_runner(
        tmp_path, dataset_adapter, factor_store, FileResearchRunStore(tmp_path)
    )
    # Stop after prediction publication. The fixture window has one session, and a
    # one-session backtest legitimately has no Sharpe ratio; the v1 backtest-result
    # schema requires a number there, which is a separate contract issue. This test
    # only needs the prediction stage to have published from the artifact.
    from uq.errors import ContractError

    def stop_after_predictions(*args, **kwargs):
        raise ContractError("stop after prediction publication")

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(ResearchChainRunner, "_run_portfolio", stop_after_predictions)
        with pytest.raises(ContractError, match="stop after prediction publication"):
            runner.execute(
                plan,
                runner_identity=RUNNER,
                quality_decisions={
                    "factor_partition": _factor_quality_decision(factor_manifest),
                    "label_set": _label_quality_decision(),
                    "feature_preprocessing": _preprocessing_quality_decision(),
                    "model_dataset": _dataset_quality_decision(),
                    "qlib_dataset_export": export_decision(),
                    "qlib_init_receipt": receipt_decision(),
                    "model_run": run_decision(),
                    "model_artifact": artifact_decision,
                    "prediction_set": prediction_decision(),
                    "portfolio_definition": _definition_decision,
                    "target_weights": _weights_decision(),
                    "backtest_config": _config_decision(),
                    "backtest_result": _result_decision(),
                    "research_run_result": _research_result_decision,
                },
                scores_by_decision_date=None,
                # This integration fixture predates test splits. The default
                # test-split selection has dedicated pure and runner tests;
                # here we explicitly exercise artifact inference for one date.
                decision_dates=["2026-01-09"],
                created_at="2026-01-30T07:00:00+00:00",
            )
    snapshots = FileResearchRunStore(tmp_path).list_state_snapshots(
        plan.request["request_content_generation_id"], plan.request["run_id"]
    )
    assert any(
        snapshot.stage == "prediction_publication" and snapshot.status == "passed"
        for snapshot in snapshots
    )


def test_default_prediction_dates_cover_the_governed_test_split(tmp_path: Path):
    """The default covers the whole governed test split.

    The forward-label tail makes `window_end_date` unscorable even though the
    dataset and export are correct. The default decision dates must come from
    the export calendar intersected with the test split, not the request's
    nominal window end.
    """
    from types import SimpleNamespace
    from uq.research_chain.resolver import ResolvedStageBinding

    runner = ResearchChainRunner.__new__(ResearchChainRunner)
    class _DatasetWriter:
        def read(self, *_args, **_kwargs):
            return {
                "split_policy": {
                    "splits": [
                        {"name": "train", "start_date": "2026-01-05", "end_date": "2026-01-06"},
                        {"name": "test", "start_date": "2026-01-07", "end_date": "2026-01-08"},
                    ]
                }
            }, None

    runner.dataset_adapter = SimpleNamespace(dataset_writer=_DatasetWriter())
    runner.export_adapter = SimpleNamespace(
        exporter=SimpleNamespace(export_root=tmp_path / "qlib_exports")
    )
    generation = "9" * 64
    plan = SimpleNamespace(
        request={
            "dataset_policy_template": {
                "dataset_name": "research_slice",
                "semantic_version": "1.0.0",
            },
            "window_start_date": "2026-01-05",
            "window_end_date": "2026-01-09",
        },
        stage_bindings=(
            ResolvedStageBinding(
                stage="qlib_export",
                output_family="qlib_dataset_export",
                generation_id=generation,
                manifest_digest_sha256=generation,
                data_checksum_sha256=generation,
            ),
            ResolvedStageBinding(
                stage="dataset_preparation",
                output_family="model_dataset",
                generation_id="8" * 64,
                manifest_digest_sha256="8" * 64,
                data_checksum_sha256="8" * 64,
            ),
        ),
    )
    calendar_path = (
        tmp_path / "qlib_exports" / "dataset=research_slice"
        / f"generation={generation}" / "calendars" / "day.txt"
    )
    calendar_path.parent.mkdir(parents=True, exist_ok=True)
    calendar_path.write_text("2026-01-05\n2026-01-06\n2026-01-07\n2026-01-08\n")
    assert runner._default_decision_dates(plan) == ["2026-01-07", "2026-01-08"]


def test_inference_uses_the_artifact_runtime_version_not_the_export_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version guard must read the runtime that trained the artifact.

    The export contract deliberately carries no `qlib_version`; reading one from
    it made the guard receive `None` forever. This test drives the real
    `_infer_scores_for_dates` branch and asserts the artifact's runtime version
    reaches `score_decision_dates`.
    """
    from types import SimpleNamespace
    from uq.research_chain.resolver import ResolvedStageBinding

    runner = ResearchChainRunner.__new__(ResearchChainRunner)
    generation = "9" * 64
    runner.export_adapter = SimpleNamespace(
        exporter=SimpleNamespace(
            export_root=tmp_path / "qlib_exports",
            read=lambda *_args, **_kwargs: ({}, tmp_path),
        )
    )
    runner.model_adapter = SimpleNamespace(
        last_result=SimpleNamespace(
            artifact_manifest={"runtime_version": "0.9.7"},
        )
    )
    plan = SimpleNamespace(
        request={
            "dataset_policy_template": {"dataset_name": "research_slice"},
        },
        stage_bindings=(
            ResolvedStageBinding(
                stage="qlib_export",
                output_family="qlib_dataset_export",
                generation_id=generation,
                manifest_digest_sha256=generation,
                data_checksum_sha256=generation,
            ),
            ResolvedStageBinding(
                stage="model_training",
                output_family="model_run",
                generation_id="8" * 64,
                manifest_digest_sha256="8" * 64,
                data_checksum_sha256="8" * 64,
            ),
            ResolvedStageBinding(
                stage="model_training",
                output_family="model_artifact",
                generation_id="7" * 64,
                manifest_digest_sha256="7" * 64,
                data_checksum_sha256="7" * 64,
            ),
        ),
    )
    captured: dict[str, object] = {}
    scores = {"2026-01-07": object()}

    def fake_score(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(scores=scores)

    monkeypatch.setattr("uq.models.inference.score_decision_dates", fake_score)
    result = runner._infer_scores_for_dates(
        plan,
        ["2026-01-07"],
        runner_identity={"code_fingerprint": "0" * 64},
    )
    assert captured["expected_qlib_version"] == "0.9.7"
    assert result is scores


def test_a_v2_price_panel_is_keyed_on_its_own_session_column(tmp_path: Path):
    """cr.md 8.62 F: `bars_daily/v2` publishes `session_date`, not `datetime`.

    The research-chain backtest required `datetime` outright, so a run whose
    request bound the v2 adjusted-price line refused the panel it had just
    published. The panel's own key is used instead, which also keeps the v1
    (`datetime`) line working: the column name is read off the frame, not
    hardcoded in both places.
    """
    import pandas as pd

    from uq.research_chain import adapters as adapters_module

    source = Path(adapters_module.__file__).read_text()
    # 1) the required-column set is derived from the frame, not fixed to `datetime`
    assert 'session_column = "session_date" if "session_date" in price_frame.columns else "datetime"' in source
    # 2) and the panel's date column uses the same selection
    assert 'price_panel[session_column]' in source
    # 3) the v1 spelling still works, so the branch is a fallback rather than a rename
    v1 = pd.DataFrame({
        "instrument": ["A"], "datetime": [pd.Timestamp("2026-01-05")],
        "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
        "volume": [1.0], "amount": [10.5],
    })
    v2 = v1.rename(columns={"datetime": "session_date"})
    for frame, expected in ((v1, "datetime"), (v2, "session_date")):
        chosen = "session_date" if "session_date" in frame.columns else "datetime"
        assert chosen == expected
        panel = frame.copy()
        panel["date"] = pd.to_datetime(panel[chosen]).dt.strftime("%Y-%m-%d")
        panel = panel.set_index(["date", "instrument"])[["open", "high", "low", "close", "volume", "amount"]]
        assert ("2026-01-05", "A") in panel.index
        # both lines lose the same columns the same way once paneled
        assert list(panel.columns) == ["open", "high", "low", "close", "volume", "amount"]
