from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from ..contracts.factor_governance import FactorRegistry
from ..contracts.gate_contracts import validate_contract
from ..contracts.model_layer import ModelContractLoader
from ..backtest.engine import BacktestEngine, BacktestResultStore
from ..contracts.artifacts import UniverseSnapshotStore
from ..factors.store import FactorStore
from ..errors import ContractError
from ..models.dataset_writer import DatasetWriter
from ..models.predictions import PredictionBuilder
from ..models.trainer import ArtifactStore
from ..portfolio.builder import PortfolioBuilder, TargetWeightStore
from .owning_contracts import (
    AdjustedPriceDatasetStore,
    BacktestConfigStore,
    BacktestMarketDatasetStore,
    FeaturePreprocessingStore,
    FeatureSchemaStore,
    LabelStore,
)
from .adapters import (
    BacktestStageAdapter,
    DatasetStageAdapter,
    FactorStageAdapter,
    ModelStageAdapter,
    PortfolioStageAdapter,
    PredictionStageAdapter,
    QlibExportStageAdapter,
)
from .resolver import FileResearchRunStore, ResearchChainRequestResolver
from .runner import ResearchChainRunner

_EXIT_CODES = {
    "dry_run_published": 0,
    "published": 0,
    "rejected": 3,
    "configuration_failure": 3,
    "quality_decision_missing": 3,
    "quality_decision_rejected": 3,
    "lineage_mismatch": 3,
    "input_unresolved": 3,
    "input_tampered": 3,
    "request_invalid": 3,
    "store_read_failed": 4,
    "publication_conflict": 5,
    "stage_failed": 4,
}


def uq_research_run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uq-research-run")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("dry-run", "execute"), required=True)
    parser.add_argument("--request-json", type=Path, required=True)
    parser.add_argument("--quality-decisions-json", type=Path)
    parser.add_argument("--scores-json", type=Path)
    parser.add_argument("--decision-provider-json", type=Path)
    args = parser.parse_args(argv)

    try:
        args.data_root.mkdir(parents=True, exist_ok=True)
        request = json.loads(args.request_json.read_text(encoding="utf-8"))
        if args.mode == "dry-run":
            result = _dry_run(request, data_root=args.data_root)
        else:
            decisions_document = None
            if args.quality_decisions_json is None:
                raise ContractError("execute mode requires --quality-decisions-json")
            if args.scores_json is None:
                raise ContractError("execute mode requires --scores-json")
            decisions_document = json.loads(args.quality_decisions_json.read_text(encoding="utf-8"))
            scores_document = json.loads(args.scores_json.read_text(encoding="utf-8"))
            provider_document = None
            if args.decision_provider_json is not None:
                provider_document = json.loads(args.decision_provider_json.read_text(encoding="utf-8"))
            result = _execute(
                request,
                decisions_document=decisions_document,
                scores_document=scores_document,
                provider_document=provider_document,
                project_root=args.project_root,
                data_root=args.data_root,
            )
    except (ContractError, OSError, json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        status = getattr(exc, "reason", "configuration_failure")
        if status not in _EXIT_CODES:
            status = "configuration_failure"
        print(json.dumps({"status": status, "errors": [str(exc)]}, sort_keys=True))
        return _EXIT_CODES[status]
    print(json.dumps(result, sort_keys=True))
    return _EXIT_CODES[result["status"]]


def _dry_run(request: dict[str, Any], *, data_root: Path) -> dict[str, Any]:
    ModelContractLoader.validate("research_run_request", request)
    runner = ResearchChainRunner.__new__(ResearchChainRunner)
    runner.factor_adapter = None
    runner.dataset_adapter = None
    runner.export_adapter = None
    runner.model_adapter = None
    runner.prediction_adapter = None
    runner.portfolio_adapter = None
    runner.backtest_adapter = None
    runner.run_store = FileResearchRunStore(data_root)
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Plan:
        request: dict[str, Any]
        request_manifest_digest_sha256: str

    plan = _Plan(
        request=request,
        request_manifest_digest_sha256=request["manifest_digest_sha256"],
    )
    environment = request["environment"]
    state = runner.dry_run(
        plan,
        runner_identity={
            "code_fingerprint": environment["code_fingerprint"],
            "environment_profile": "locked",
            "lock_digest_sha256": environment["environment_lock_digest_sha256"],
        },
    )
    return {
        "status": "dry_run_published",
        "request_content_generation_id": request["request_content_generation_id"],
        "run_id": request["run_id"],
        "manifest_path": str(state["manifest_path"]),
        "manifest_digest_sha256": state["manifest_digest_sha256"],
    }


class _MappingDecisionProvider:
    def __init__(self, decisions: Mapping[str, Mapping[str, Any]]) -> None:
        self.decisions = decisions

    def resolve(self, *, output_family: str, subject_generation_id: str, **_: Any) -> Mapping[str, Any]:
        decision = self.decisions.get(output_family)
        if decision is None:
            raise ContractError(f"missing research chain quality decision: {output_family}")
        return decision


class _CommandDecisionProvider:
    def __init__(self, *, command: list[str], provider_config_ref: str) -> None:
        self.command = command
        self.provider_config_ref = provider_config_ref

    def resolve(
        self,
        *,
        binding_type: str,
        subject_generation_id: str,
        subject_manifest_digest_sha256: str | None,
        output_family: str,
        provider_config_ref: str,
    ) -> Mapping[str, Any]:
        completed = subprocess.run(
            [
                *self.command,
                "--binding-type", binding_type,
                "--subject-generation-id", subject_generation_id,
                "--subject-manifest-digest-sha256", subject_manifest_digest_sha256 or "",
                "--output-family", output_family,
                "--provider-config-ref", provider_config_ref,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "external decision provider failed"
            raise ContractError(detail)
        try:
            decision = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ContractError("external decision provider returned malformed JSON") from exc
        if not isinstance(decision, dict):
            raise ContractError("external decision provider must return an object")
        return decision


def _decision_provider_for_family(provider: Any, output_family: str):
    binding_type = _BINDING_TYPE_BY_FAMILY[output_family]

    def resolve(subject_generation_id: str) -> Mapping[str, Any]:
        return provider.resolve(
            binding_type=binding_type,
            subject_generation_id=subject_generation_id,
            subject_manifest_digest_sha256=None,
            output_family=output_family,
            provider_config_ref=provider.provider_config_ref,
        )

    return resolve


_BINDING_TYPE_BY_FAMILY = {
    "factor_partition": "factor_v1",
    "label_set": "label_set_v1",
    "feature_preprocessing": "feature_preprocessing_v1",
    "model_dataset": "model_dataset_v1",
    "qlib_dataset_export": "qlib_dataset_export_v1",
    "qlib_init_receipt": "qlib_init_receipt_v1",
    "model_run": "model_run_v1",
    "model_artifact": "model_artifact_v1",
    "prediction_set": "prediction_set_v1",
    "portfolio_definition": "portfolio_definition_v1",
    "target_weights": "target_weights_v1",
    "backtest_config": "backtest_config_v1",
    "backtest_result": "backtest_result_v1",
    "research_run_result": "research_run_result_v1",
}


def _load_scores(document: Any) -> dict[str, pd.DataFrame]:
    validate_contract("research_scores.v1.json", document)
    scores: dict[str, pd.DataFrame] = {}
    for decision_date, rows in document["scores_by_decision_date"].items():
        frame = pd.DataFrame(rows)
        frame["score"] = pd.to_numeric(frame["score"], errors="raise")
        if not frame["score"].map(math.isfinite).all():
            raise ContractError(f"non-finite scores for decision date {decision_date}")
        if frame["instrument"].map(lambda value: not isinstance(value, str) or not value.strip()).any():
            raise ContractError(f"invalid instruments for decision date {decision_date}")
        scores[decision_date] = frame[["instrument", "score"]]
    return scores


def _build_runner(*, project_root: Path, data_root: Path, run_store: FileResearchRunStore):
    factor_store = FactorStore(data_root, FactorRegistry(project_root))
    feature_store_root = data_root / "governed"
    universe_store = UniverseSnapshotStore(data_root)
    dataset_adapter = DatasetStageAdapter(
        factor_store=factor_store,
        adjusted_price_store=AdjustedPriceDatasetStore(data_root),
        label_store=LabelStore(data_root),
        universe_store=universe_store,
        feature_schema_store=FeatureSchemaStore(feature_store_root),
        preprocessing_store=FeaturePreprocessingStore(feature_store_root),
        dataset_writer=DatasetWriter(data_root),
        run_store=run_store,
    )
    from uq.models.qlib_export import QlibDatasetExporter, QlibInitReceiptBuilder
    from uq.models.qlib_runtime import QlibRuntimeTrainer

    exporter = QlibDatasetExporter(data_root / "qlib_exports")
    dataset_writer = DatasetWriter(data_root)
    prediction_builder = PredictionBuilder(data_root)
    portfolio_builder = PortfolioBuilder(data_root)
    return ResearchChainRunner(
        factor_adapter=FactorStageAdapter(factor_store, run_store),
        dataset_adapter=dataset_adapter,
        export_adapter=QlibExportStageAdapter(
            exporter=exporter,
            receipt_builder=QlibInitReceiptBuilder(),
            dataset_writer=dataset_writer,
            run_store=run_store,
        ),
        model_adapter=ModelStageAdapter(
            trainer=QlibRuntimeTrainer(),
            exporter=exporter,
            artifact_store=ArtifactStore(data_root),
            dataset_writer=dataset_writer,
            universe_store=universe_store,
            run_store=run_store,
        ),
        prediction_adapter=PredictionStageAdapter(prediction_builder, run_store),
        portfolio_adapter=PortfolioStageAdapter(
            universe_store=universe_store,
            portfolio_builder=portfolio_builder,
            target_weight_store=TargetWeightStore(data_root),
            run_store=run_store,
            prediction_builder=prediction_builder,
        ),
        backtest_adapter=BacktestStageAdapter(
            backtest_engine=BacktestEngine(data_root),
            backtest_result_store=BacktestResultStore(data_root),
            backtest_config_store=BacktestConfigStore(data_root),
            run_store=run_store,
            adjusted_price_store=dataset_adapter.adjusted_price_store,
            market_dataset_store=BacktestMarketDatasetStore(data_root),
        ),
        run_store=run_store,
    )


def _execute(
    request: dict[str, Any],
    *,
    decisions_document: dict[str, Any],
    scores_document: dict[str, Any],
    project_root: Path,
    data_root: Path,
    provider_document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(decisions_document.get("quality_decisions"), dict):
        raise ContractError("quality decisions document requires quality_decisions")
    decisions = decisions_document["quality_decisions"]
    if provider_document is not None:
        command = provider_document.get("provider_command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ContractError("decision provider document requires provider_command")
        if provider_document.get("provider_id") != "external-model-quality-reviewer-v1":
            raise ContractError("unsupported decision provider identity")
        provider = _CommandDecisionProvider(
            command=command,
            provider_config_ref=provider_document.get("provider_config_ref", "research-cli"),
        )
        decisions = {
            family: _decision_provider_for_family(provider, family)
            for family in _BINDING_TYPE_BY_FAMILY
        }
    scores = _load_scores(scores_document)
    run_store = FileResearchRunStore(data_root)
    runner = _build_runner(project_root=project_root, data_root=data_root, run_store=run_store)
    resolver = ResearchChainRequestResolver({
        "factor_partition": runner.factor_adapter.store,
        "universe_snapshot": runner.dataset_adapter.universe_store,
        "adjusted_price_dataset": runner.dataset_adapter.adjusted_price_store,
        "label_set": runner.dataset_adapter.label_store,
        "feature_preprocessing": runner.dataset_adapter.preprocessing_store,
        "backtest_config": runner.backtest_adapter.backtest_config_store,
    })
    try:
        plan = resolver.resolve(
            request,
            quality_provider=_MappingDecisionProvider(decisions),
            provider_config_ref="research-cli",
        )
    except Exception as exc:
        status = getattr(exc, "reason", None)
        if status not in {"request_invalid", "input_unresolved", "input_tampered", "lineage_mismatch", "quality_decision_missing", "quality_decision_rejected", "store_read_failed"}:
            status = "configuration_failure"
        error = ContractError(status, str(exc))
        error.reason = status
        raise error from exc
    environment = request["environment"]
    outcome = runner.execute(
        plan,
        runner_identity={
            "code_fingerprint": environment["code_fingerprint"],
            "environment_profile": "locked",
            "lock_digest_sha256": environment["environment_lock_digest_sha256"],
        },
        quality_decisions=decisions,
        scores_by_decision_date=scores,
        decision_dates=sorted(scores),
    )
    return {
        "status": "published",
        "request_content_generation_id": request["request_content_generation_id"],
        "run_id": request["run_id"],
        "result_content_generation_id": outcome.result_manifest["result_content_generation_id"],
        "manifest_path": str(outcome.result_path),
        "manifest_digest_sha256": outcome.result_manifest["manifest_digest_sha256"],
        "final_status": outcome.result_manifest["final_status"],
    }


if __name__ == "__main__":
    raise SystemExit(uq_research_run())
