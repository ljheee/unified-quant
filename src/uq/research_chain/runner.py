from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from ..contracts.model_layer import (
    ModelContractLoader,
    bind_reviewed_quality_decision,
    research_contract_identities,
    sha256_json,
)
from ..contracts.gate_contracts import adjustment_snapshot_generation
from ..errors import ContractError


def _result_output_binding(binding: ResolvedStageBinding) -> dict[str, Any]:
    physical_path = binding.physical_path
    if not physical_path:
        physical_path = {
            "universe_snapshot": f"universes/generation={binding.generation_id}/manifest.json",
            "adjusted_price_dataset": f"adjusted_prices/generation={binding.generation_id}/manifest.json",
            "label_set": f"label_sets/generation={binding.generation_id}/manifest.json",
            "feature_preprocessing": f"feature_preprocessing/generation={binding.generation_id}/manifest.json",
            "backtest_config": f"backtest_configs/generation={binding.generation_id}/manifest.json",
        }[binding.output_family]
    quality_checksum = binding.quality_decision_checksum_sha256
    if not quality_checksum:
        quality_checksum = "0" * 64
    return {
        "output_family": binding.output_family,
        "generation_id": binding.generation_id,
        "manifest_digest_sha256": binding.manifest_digest_sha256,
        "data_checksum_sha256": binding.data_checksum_sha256,
        "physical_path": physical_path,
        "quality_decision_checksum_sha256": quality_checksum,
        "failure_reason": None,
    }
from .adapters import (
    BacktestStageAdapter,
    DatasetStageAdapter,
    FactorStageAdapter,
    ModelStageAdapter,
    PortfolioStageAdapter,
    PredictionStageAdapter,
    QlibExportStageAdapter,
    _STAGE_PLAN,
    _synthesized_portfolio_definition,
    build_stage_state,
)
from ..models.definition import ModelDefinitionBuilder
from .resolver import FileResearchRunStore, ResolvedExecutionPlan, ResolvedStageBinding, build_dry_run_state


@dataclass(frozen=True)
class ResearchRunOutcome:
    result_manifest: dict[str, Any]
    result_path: Path
    state_summaries: list[dict[str, Any]]


class ResearchChainRunner:
    def __init__(
        self,
        *,
        factor_adapter: FactorStageAdapter,
        dataset_adapter: DatasetStageAdapter,
        export_adapter: QlibExportStageAdapter,
        model_adapter: ModelStageAdapter,
        prediction_adapter: PredictionStageAdapter,
        portfolio_adapter: PortfolioStageAdapter,
        backtest_adapter: BacktestStageAdapter,
        run_store: FileResearchRunStore,
    ) -> None:
        self.factor_adapter = factor_adapter
        self.dataset_adapter = dataset_adapter
        self.export_adapter = export_adapter
        self.model_adapter = model_adapter
        self.prediction_adapter = prediction_adapter
        self.portfolio_adapter = portfolio_adapter
        self.backtest_adapter = backtest_adapter
        self.run_store = run_store

    def dry_run(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        created_at: str | None = None,
    ) -> dict[str, Any]:
        self._validate_plan(plan)
        self.run_store.publish_request(plan.request, path_policy="strict_v1")
        state = build_dry_run_state(
            plan, runner_identity=dict(runner_identity), created_at=created_at
        )
        return dict(self.run_store.publish_state(state, stage="resolve_request").__dict__)

    def execute(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decisions: Mapping[str, Any],
        scores_by_decision_date: Mapping[str, pd.DataFrame],
        decision_dates: Sequence[str] | None = None,
        provider_uri: str = "file:///research-exports",
        qlib_import_path: str = "qlib",
        qlib_version: str | None = None,
        cache_root: Path | str | None = None,
        export_layout_root: Path | str | None = None,
        artifact_decision_provider: Callable[[str], Mapping[str, Any]] | None = None,
        definition_decision_provider: Callable[[str], Mapping[str, Any]] | None = None,
        created_at: str | None = None,
    ) -> ResearchRunOutcome:
        if plan.request.get("execution_mode") != "full_research_run":
            raise ContractError("research runner only supports full_research_run")
        self._validate_plan(plan)
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        self.run_store.last_plan = plan
        try:
            self._ensure_request(plan.request)
            self._publish_resolution_state(plan, runner_identity=runner_identity, created_at=created_at)

            factor_result = self._run_factor(
                plan, runner_identity=runner_identity,
                quality_decisions=quality_decisions, created_at=created_at,
            )
            plan = self._plan_with_stage_outputs(plan, factor_result.published_state.manifest_path)
            dataset_result = self._run_dataset(
                plan, runner_identity=runner_identity,
                quality_decisions=quality_decisions, created_at=created_at,
            )
            plan = self._plan_with_stage_outputs(plan, dataset_result.published_state.manifest_path)
            export_result = self._run_export(
                plan, runner_identity=runner_identity,
                quality_decisions=quality_decisions, created_at=created_at,
                provider_uri=provider_uri, qlib_import_path=qlib_import_path,
                qlib_version=qlib_version, cache_root=cache_root,
                export_layout_root=export_layout_root,
            )
            plan = self._plan_with_stage_outputs(plan, export_result.published_state.manifest_path)
            model_result = self._run_model(
                plan, runner_identity=runner_identity,
                quality_decisions=quality_decisions, created_at=created_at,
                artifact_decision_provider=artifact_decision_provider,
            )
            plan = self._plan_with_stage_outputs(plan, model_result.published_state.manifest_path)
            prediction_results = self._run_predictions(
                plan, runner_identity=runner_identity,
                quality_decisions=quality_decisions, created_at=created_at,
                decision_dates=decision_dates,
                scores_by_decision_date=scores_by_decision_date,
            )
            for prediction_result in prediction_results:
                plan = self._plan_with_stage_outputs(plan, prediction_result.published_state.manifest_path)
            portfolio_result = self._run_portfolio(
                plan, runner_identity=runner_identity,
                quality_decisions=quality_decisions, created_at=created_at,
                definition_decision_provider=definition_decision_provider,
            )
            plan = self._plan_with_stage_outputs(plan, portfolio_result.published_state.manifest_path)
            backtest_result = self._run_backtest(
                plan, runner_identity=runner_identity,
                quality_decisions=quality_decisions, created_at=created_at,
            )
            plan = self._plan_with_stage_outputs(plan, backtest_result.published_state.manifest_path)
            result = self._reconcile(
                plan, runner_identity=runner_identity,
                quality_decisions=quality_decisions, created_at=created_at,
            )
        except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
            self._publish_failed_state(
                plan, runner_identity=runner_identity, created_at=created_at,
                completed_bindings=[], error=exc,
            )
            raise
        return result

    def _validate_plan(self, plan: ResolvedExecutionPlan) -> None:
        if plan.request["stage_plan_sha256"] != sha256_json({
            "schema_version": "v1",
            "stage_plan": _STAGE_PLAN,
        }):
            raise ContractError("research request stage plan digest mismatch")
        expected_generation, expected_digest = research_contract_identities(
            plan.request, schema_name="research_run_request"
        )
        if (
            plan.request["request_content_generation_id"] != expected_generation
            or plan.request["manifest_digest_sha256"] != expected_digest
        ):
            raise ContractError("research request identity mismatch")

    def _publish_resolution_state(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        created_at: str,
    ) -> None:
        state = build_dry_run_state(
            plan, runner_identity=dict(runner_identity), created_at=created_at
        )
        state["intent"] = "execute"
        state["state_content_generation_id"] = "0" * 64
        state["manifest_digest_sha256"] = "0" * 64
        generation, digest = research_contract_identities(state, schema_name="research_run_state")
        state["state_content_generation_id"] = generation
        state["manifest_digest_sha256"] = digest
        path = self.run_store.root / self._state_relative_path(
            state["request_content_generation_id"], state["run_id"], "resolve_request"
        )
        if path.exists() or path.is_symlink():
            existing = self.run_store.list_state_snapshots(
                state["request_content_generation_id"], state["run_id"]
            )
            if not existing or existing[0].stage != "resolve_request":
                raise ContractError("research state overwrite conflict")
            return
        self.run_store.publish_state(state, stage="resolve_request")

    def _ensure_request(self, request: Mapping[str, Any]) -> None:
        path = self.run_store.root / self._request_relative_path(request)
        if path.exists() or path.is_symlink():
            self.run_store.read_request(
                request["request_content_generation_id"], request["manifest_digest_sha256"]
            )
            return
        self.run_store.publish_request(request, path_policy="strict_v1")

    def _decision(
        self,
        quality_decisions: Mapping[str, Any],
        key: str,
        subject_generation_id: str | None = None,
    ) -> Mapping[str, Any]:
        decision = quality_decisions.get(key)
        if decision is None:
            raise ContractError(f"missing research chain quality decision: {key}")
        if callable(decision):
            if subject_generation_id is None:
                raise ContractError(f"quality decision provider requires a subject: {key}")
            decision = decision(subject_generation_id)
        if not isinstance(decision, Mapping):
            raise ContractError(f"invalid research chain quality decision: {key}")
        return decision

    def _run_factor(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decisions: Mapping[str, Any],
        created_at: str,
    ) -> None:
        factor_binding = self._binding(plan, "factor_partition")
        result = self.factor_adapter.run(
            plan,
            runner_identity=dict(runner_identity),
            quality_decision=self._decision(
                quality_decisions, "factor_partition", factor_binding.generation_id
            ),
            created_at=created_at,
        )
        return result

    def _run_dataset(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decisions: Mapping[str, Any],
        created_at: str,
    ) -> None:
        result = self.dataset_adapter.run(
            plan,
            runner_identity=dict(runner_identity),
            quality_decision=self._decision(quality_decisions, "model_dataset"),
            preprocessing_quality_decision=self._decision(
                quality_decisions, "feature_preprocessing"
            ),
            created_at=created_at,
        )
        return result

    def _run_export(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decisions: Mapping[str, Any],
        created_at: str,
        provider_uri: str,
        qlib_import_path: str,
        qlib_version: str | None,
        cache_root: Path | str | None,
        export_layout_root: Path | str | None,
    ) -> None:
        dataset_binding = self._binding(plan, "model_dataset")
        dataset_name = plan.request["dataset_policy_template"]["dataset_name"]
        dataset_manifest, _ = self.dataset_adapter.dataset_writer.read(
            dataset_name,
            plan.request["dataset_policy_template"]["semantic_version"],
            dataset_binding.generation_id,
        )
        if dataset_manifest["manifest_digest_sha256"] != dataset_binding.manifest_digest_sha256:
            raise ContractError("export dataset manifest digest mismatch")
        cache_root = Path(cache_root or (self.run_store.root / "qlib_cache"))
        cache_root.mkdir(parents=True, exist_ok=True)
        provider_uri = f"file://{Path(export_layout_root or (self.run_store.root / 'qlib_exports')).absolute().as_posix()}"
        cache_before = self._cache_files(cache_root)
        result = self.export_adapter.run(
            plan,
            dataset_generation_id=dataset_binding.generation_id,
            dataset_manifest_digest_sha256=dataset_binding.manifest_digest_sha256,
            feature_columns=list(dataset_manifest["ordered_features"]),
            label_column="label",
            provider_uri=provider_uri,
            qlib_import_path=qlib_import_path,
            qlib_version=qlib_version or "0.9.7",
            cache_root=str(cache_root),
            cache_files_before=cache_before,
            cache_files_after=None,
            export_quality_decision=self._decision(quality_decisions, "qlib_dataset_export"),
            receipt_quality_decision=self._decision(quality_decisions, "qlib_init_receipt"),
            runner_identity=dict(runner_identity),
            created_at=created_at,
        )
        self.export_adapter.last_result = result
        return result

    def _run_model(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decisions: Mapping[str, Any],
        created_at: str,
        artifact_decision_provider: Callable[[str], Mapping[str, Any]] | None,
    ) -> None:
        dataset_binding = self._binding(plan, "model_dataset")
        label_binding = self._binding(plan, "label_set")
        universe_binding = self._binding(plan, "universe_snapshot")
        factor_binding = self._binding(plan, "factor_partition")
        preprocessing_binding = self._binding(plan, "feature_preprocessing")
        export_binding = self._binding(plan, "qlib_dataset_export")
        receipt_binding = self._binding(plan, "qlib_init_receipt")
        preprocessing = self.dataset_adapter.preprocessing_store.read_manifest(
            preprocessing_binding.generation_id
        )
        template = plan.request["model_definition_template"]
        definition = ModelDefinitionBuilder(
            run_content_generation_id="0" * 64, reviewed=True,
            code_fingerprint=template["code_fingerprint"],
        ).build(
            model_set=template["model_set"],
            model_version=template["model_version"],
            algorithm=template["algorithm"],
            hyperparameters=dict(template["hyperparameters"]),
            seed_policy=dict(template["seed_policy"]),
            feature_schema_generation_id=preprocessing["output_feature_schema_generation_id"],
            compatible_dataset_versions=list(template["compatible_dataset_versions"]),
            metrics=[dict(item) for item in template["metrics"]],
            selection_rule=template["selection_rule"],
            quality_policy=template["quality_policy"],
            serializer_version="joblib-v1" if template["algorithm"] == "qlib_linear" else template["serializer_version"],
        )
        result = self.model_adapter.run(
            plan,
            dataset_generation_id=dataset_binding.generation_id,
            label_generation_id=label_binding.generation_id,
            universe_generation_id=universe_binding.generation_id,
            factor_generation_id=factor_binding.generation_id,
            export_manifest=self.export_adapter.exporter.read(
                plan.request["dataset_policy_template"]["dataset_name"],
                export_binding.generation_id,
            )[0],
            receipt_manifest=self._read_receipt(receipt_binding),
            definition=definition,
            environment_lock_sha256=plan.request["environment"]["environment_lock_digest_sha256"],
            determinism_controls={
                "random_seed": plan.request["environment"]["seed"],
                "threads": plan.request["environment"]["thread_count"],
            },
            model_quality_decision=self._decision(quality_decisions, "model_run"),
            artifact_quality_decision_provider=artifact_decision_provider
                or (lambda generation_id: self._decision(
                    quality_decisions, "model_artifact", generation_id
                )),
            feature_columns=list(preprocessing["ordered_features"]),
            label_column="label",
            runner_identity=dict(runner_identity),
            created_at=created_at,
        )
        self.model_adapter.last_result = result
        return result

    def _run_predictions(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decisions: Mapping[str, Any],
        created_at: str,
        decision_dates: Sequence[str] | None,
        scores_by_decision_date: Mapping[str, pd.DataFrame],
    ) -> None:
        dates = sorted(set(decision_dates or [plan.request["window_end_date"]]))
        dataset_binding = self._binding(plan, "model_dataset")
        if not scores_by_decision_date:
            raise ContractError("prediction stage requires governed scores")
        for decision_date in dates:
            if decision_date not in scores_by_decision_date:
                raise ContractError(f"missing governed scores for decision date {decision_date}")
        results = self.prediction_adapter.run_for_dates(
            plan,
            dataset_generation_id=dataset_binding.generation_id,
            model_stage_result=self.model_adapter.last_result,
            scores_by_decision_date={
                date: scores_by_decision_date[date]
                for date in dates
            },
            quality_decision=self._decision(quality_decisions, "prediction_set"),
            eligibility_policy="reviewed-v1",
            eligibility_status="passed",
            runner_identity=dict(runner_identity),
            created_at=created_at,
        )
        self.prediction_adapter.last_result = results[-1]
        return results

    def _run_portfolio(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decisions: Mapping[str, Any],
        created_at: str,
        definition_decision_provider: Callable[[str], Mapping[str, Any]] | None,
    ) -> None:
        first_prediction = self._binding(plan, "prediction_set")
        universe_binding = self._binding(plan, "universe_snapshot")
        definition = _synthesized_portfolio_definition(
            plan,
            template=plan.request["portfolio_definition_template"],
            prediction_generation_id=first_prediction.generation_id,
            universe_generation_id=universe_binding.generation_id,
            created_at=created_at,
        )
        decision_dates = sorted({
            Path(binding.physical_path).name.removeprefix("date=")
            for binding in self._stage_bindings(plan, "prediction_publication")
        })
        result = self.portfolio_adapter.run(
            plan,
            prediction_stage_result=self.prediction_adapter.last_result,
            decision_dates=decision_dates,
            definition_quality_decision=(
                definition_decision_provider(definition["generation_id"])
                if definition_decision_provider else self._decision(
                    quality_decisions, "portfolio_definition", definition["generation_id"]
                )
            ),
            weights_quality_decision=self._decision(quality_decisions, "target_weights"),
            runner_identity=dict(runner_identity),
            created_at=created_at,
            prediction_generation_by_date={
                Path(binding.physical_path).name.removeprefix("date="): binding.generation_id
                for binding in self._stage_bindings(plan, "prediction_publication")
            },
        )
        self.portfolio_adapter.last_result = result
        return result

    def _run_backtest(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decisions: Mapping[str, Any],
        created_at: str,
    ) -> None:
        result = self.backtest_adapter.run(
            plan,
            portfolio_stage_result=self.portfolio_adapter.last_result,
            quality_decision=self._decision(quality_decisions, "backtest_result"),
            runner_identity=dict(runner_identity),
            created_at=created_at,
        )
        self.backtest_adapter.last_result = result
        return result

    def _plan_with_stage_outputs(
        self, plan: ResolvedExecutionPlan, state_path: Path
    ) -> ResolvedExecutionPlan:
        state = ModelContractLoader().load("research_run_state", state_path)
        output_bindings = state["stage_records"][-1]["output_bindings"]
        stage = state["stage_records"][-1]["stage"]
        existing_families = {binding["output_family"] for binding in output_bindings}
        retained = [
            binding for binding in plan.stage_bindings
            if not (binding.stage == stage and binding.output_family in existing_families)
        ]
        return replace(plan, stage_bindings=tuple([
            *retained,
            *(ResolvedStageBinding(
                stage=stage,
                output_family=binding["output_family"],
                generation_id=binding["generation_id"],
                manifest_digest_sha256=binding["manifest_digest_sha256"],
                data_checksum_sha256=binding["data_checksum_sha256"],
                physical_path=binding["physical_path"],
                quality_decision_checksum_sha256=binding["quality_decision_checksum_sha256"],
            ) for binding in output_bindings),
        ]))

    def _reconcile(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decisions: Mapping[str, Any],
        created_at: str,
    ) -> ResearchRunOutcome:
        stage_records: list[dict[str, Any]] = []
        readback_status: dict[str, str] = {
            "research_run_request": "passed",
        }
        resolution_state = self.run_store.read_state(
            plan.request["request_content_generation_id"],
            plan.request["run_id"],
            "resolve_request",
            next(
                summary.manifest_digest_sha256
                for summary in self.run_store.list_state_snapshots(
                    plan.request["request_content_generation_id"], plan.request["run_id"]
                )
                if summary.stage == "resolve_request"
            ),
        )
        stage_records.append(resolution_state["stage_records"][-1])
        for stage in _STAGE_PLAN[1:-1]:
            bindings = self._stage_bindings(plan, stage)
            if not bindings:
                raise ContractError(f"research stage output is missing: {stage}")
            for binding in bindings:
                self._readback(binding)
                readback_status[binding.output_family] = "passed"
            stage_records.append({
                "stage": stage,
                "status": "passed",
                "output_bindings": [_result_output_binding(binding) for binding in bindings],
                "failure_reason": None,
            })
        overall_logical_fingerprint = sha256_json({
            "schema_version": "v1",
            "bindings": [
                {
                    "output_family": binding.output_family,
                    "generation_id": binding.generation_id,
                    "data_checksum_sha256": binding.data_checksum_sha256,
                }
                for binding in plan.stage_bindings
            ],
        })
        provisional_binding = {
            "output_family": "research_run_result",
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
            "data_checksum_sha256": sha256_json(readback_status),
            "physical_path": self._result_relative_path(plan, "0" * 64),
            "quality_decision_checksum_sha256": "0" * 64,
            "failure_reason": None,
        }
        stage_records.append({
            "stage": "result_reconciliation",
            "status": "passed",
            "output_bindings": [provisional_binding],
            "failure_reason": None,
        })
        result = {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "request_content_generation_id": plan.request["request_content_generation_id"],
            "request_manifest_digest_sha256": plan.request_manifest_digest_sha256,
            "run_id": plan.request["run_id"],
            "created_at": created_at,
            "runner_identity": dict(runner_identity),
            "stage_records": stage_records,
            "readback_status": readback_status,
            "overall_logical_fingerprint": overall_logical_fingerprint,
            "final_status": "passed",
            "result_content_generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation, digest = research_contract_identities(result, schema_name="research_run_result")
        result["result_content_generation_id"] = generation
        result["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("research_run_result", result)

        decision = self._decision(
            quality_decisions, "research_run_result", generation
        )
        ModelContractLoader.validate("quality_decision", dict(decision))
        owning_report = dict(decision["owning_report"])
        if (
            decision["binding_type"] != "research_run_result_v1"
            or owning_report["binding_type"] != "research_run_result_v1"
            or decision["subject_generation_id"] != generation
            or owning_report["bound_generation_id"] != generation
            or decision["decision_checksum_sha256"] != owning_report["report_checksum_sha256"]
            or owning_report["status"] not in {"passed", "warning"}
        ):
            raise ContractError("research result quality decision subject or checksum mismatch")
        final_binding = {
            **provisional_binding,
            "generation_id": generation,
            "manifest_digest_sha256": digest,
            "quality_decision_checksum_sha256": owning_report["report_checksum_sha256"],
            "physical_path": self._result_relative_path(plan, generation),
        }
        result["stage_records"][-1]["output_bindings"] = [final_binding]
        final_generation, final_digest = research_contract_identities(
            result, schema_name="research_run_result"
        )
        if final_generation != generation or final_digest != digest:
            raise ContractError("research result identity is not stable under reconciliation")
        ModelContractLoader.validate("research_run_result", result)
        published = self.run_store.publish_result(result, path_policy="strict_v1")
        self.run_store.read_result(generation, digest)
        summaries = [
            {
                "stage": summary.stage,
                "status": summary.status,
                "manifest_digest_sha256": summary.manifest_digest_sha256,
            }
            for summary in self.run_store.list_state_snapshots(
                plan.request["request_content_generation_id"], plan.request["run_id"]
            )
        ]
        return ResearchRunOutcome(result, published.manifest_path, summaries)

    def _publish_failed_state(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        created_at: str,
        completed_bindings: list[dict[str, Any]],
        error: Exception,
    ) -> None:
        request_generation_id = plan.request["request_content_generation_id"]
        run_id = plan.request["run_id"]
        passed_summaries = [
            summary for summary in self.run_store.list_state_snapshots(request_generation_id, run_id)
            if summary.status == "passed"
        ]
        stage_records: list[dict[str, Any]] = []
        for summary in passed_summaries:
            state = self.run_store.read_state(
                request_generation_id, run_id, summary.stage,
                summary.manifest_digest_sha256,
            )
            stage_records.append(state["stage_records"][-1])
        completed: dict[str, list[dict[str, Any]]] = {}
        for binding in completed_bindings:
            completed.setdefault(binding["stage"], []).append(binding)
        for stage, bindings in completed.items():
            if any(record["stage"] == stage for record in stage_records):
                continue
            stage_records.append({
                "stage": stage,
                "status": "passed",
                "output_bindings": bindings,
                "failure_reason": None,
            })
        last_passed_index = max(
            (_STAGE_PLAN.index(record["stage"]) for record in stage_records),
            default=-1,
        )
        failed_stage = _STAGE_PLAN[min(last_passed_index + 1, len(_STAGE_PLAN) - 1)]
        stage_records.append({
            "stage": failed_stage,
            "status": "failed",
            "output_bindings": [],
            "failure_reason": "stage_failed",
        })
        state = {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "request_content_generation_id": request_generation_id,
            "request_manifest_digest_sha256": plan.request_manifest_digest_sha256,
            "run_id": run_id,
            "created_at": created_at,
            "runner_identity": dict(runner_identity),
            "intent": "execute",
            "stage_records": stage_records,
            "final_status": "failed",
            "state_content_generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation, digest = research_contract_identities(state, schema_name="research_run_state")
        state["state_content_generation_id"] = generation
        state["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("research_run_state", state)
        path = self.run_store.root / self._state_relative_path(
            request_generation_id, run_id, failed_stage
        )
        if path.exists() or path.is_symlink():
            return
        self.run_store.publish_state(state, stage=failed_stage)

    @staticmethod
    def _binding(plan: ResolvedExecutionPlan, output_family: str):
        matches = [
            binding for binding in plan.stage_bindings
            if binding.output_family == output_family
        ]
        if not matches:
            raise ContractError(f"research plan has no {output_family} binding")
        if len(matches) > 1:
            raise ContractError(f"ambiguous {output_family} binding in research plan")
        return matches[0]

    @staticmethod
    def _stage_bindings(plan: ResolvedExecutionPlan, stage: str):
        return [
            binding for binding in plan.stage_bindings
            if binding.stage == stage
        ]

    @staticmethod
    def _cache_files(root: Path | str) -> set[str]:
        base = Path(root)
        if not base.exists():
            return set()
        return {
            path.absolute().as_posix()
            for path in base.rglob("*")
            if path.is_file() and not any(part.startswith(".") for part in path.parts[len(base.resolve().parts):])
        }

    @staticmethod
    def _result_relative_path(plan: ResolvedExecutionPlan, generation_id: str) -> str:
        return (
            f"research_runs/results/request={plan.request['request_content_generation_id']}"
            f"/run={plan.request['run_id']}/result={generation_id}/manifest.json"
        )

    @staticmethod
    def _request_relative_path(request: Mapping[str, Any]) -> str:
        return (
            f"research_runs/requests/request={request['request_content_generation_id']}"
            f"/run={request['run_id']}/manifest.json"
        )

    @staticmethod
    def _state_relative_path(request_generation_id: str, run_id: str, stage: str) -> str:
        stage_number = f"{_STAGE_PLAN.index(stage):02d}"
        return (
            f"research_runs/states/request={request_generation_id}"
            f"/run={run_id}/stage={stage_number}/manifest.json"
        )

    def _read_receipt(self, binding) -> dict[str, Any]:
        last_result = getattr(self.export_adapter, "last_result", None)
        if last_result is not None and last_result.manifest["generation_id"] == binding.generation_id:
            return dict(last_result.manifest)
        path = self.run_store.root / "external_quality_reviews" / f"{binding.generation_id}.json"
        if not path.is_file():
            raise ContractError("unpublished Qlib init receipt")
        document = ModelContractLoader().load("qlib_init_receipt", path)
        if document["generation_id"] != binding.generation_id:
            raise ContractError("Qlib init receipt generation mismatch")
        if document["manifest_digest_sha256"] != binding.manifest_digest_sha256:
            raise ContractError("Qlib init receipt manifest digest mismatch")
        return dict(document)

    def _readback(self, binding) -> None:
        family = binding.output_family
        generation_id = binding.generation_id
        if family == "factor_partition":
            manifest = self.factor_adapter.store.read_manifest(generation_id)
        elif family == "universe_snapshot":
            manifest = self.dataset_adapter.universe_store.read_manifest(generation_id)
        elif family == "adjusted_price_dataset":
            manifest = self.dataset_adapter.adjusted_price_store.read_manifest(generation_id)
        elif family == "label_set":
            manifest = self.dataset_adapter.label_store.read_frame(generation_id)[0]
        elif family == "feature_preprocessing":
            manifest = self.dataset_adapter.preprocessing_store.read_manifest(generation_id)
        elif family == "model_dataset":
            template = self.run_store.last_plan.request["dataset_policy_template"]
            manifest = self.dataset_adapter.dataset_writer.read(
                template["dataset_name"],
                template["semantic_version"],
                generation_id,
            )[0]
        elif family == "qlib_dataset_export":
            manifest = self.export_adapter.exporter.read(
                self.dataset_adapter.dataset_writer.last_published_manifest["dataset_name"],
                generation_id,
            )[0]
        elif family == "qlib_init_receipt":
            manifest = self._read_receipt(binding)
        elif family == "model_run":
            run_path = self.run_store.root / "model_runs" / f"{generation_id}.json"
            if not run_path.is_file():
                raise ContractError("published model run is unavailable for readback")
            try:
                manifest = json.loads(run_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ContractError("malformed model run manifest") from exc
            ModelContractLoader.validate("model_run", manifest)
        elif family == "model_artifact":
            if self.model_adapter.last_result is None:
                raise ContractError("model stage result is unavailable for readback")
            manifest = self.model_adapter.last_result.artifact_manifest
        elif family == "prediction_set":
            manifest = self.prediction_adapter.prediction_builder.read(
                generation_id,
                Path(binding.physical_path).name.removeprefix("date="),
            )[0]
        elif family == "target_weights":
            manifest = self.portfolio_adapter.target_weight_store.read(
                generation_id,
                Path(binding.physical_path).parent.name.removeprefix("date="),
            )[0]
        elif family == "backtest_result":
            manifest = self.backtest_adapter.backtest_result_store.read(generation_id)[0]
        elif family == "backtest_config":
            manifest = self.backtest_adapter.backtest_config_store.read_manifest(generation_id)
        else:
            raise ContractError(f"unsupported research output family: {family}")
        manifest_generation = manifest.get("run_content_generation_id", manifest.get("generation_id"))
        if manifest_generation != generation_id:
            raise ContractError(f"{family} readback generation mismatch")
        if family == "qlib_init_receipt":
            if manifest.get("file_list_checksum_sha256") != binding.data_checksum_sha256:
                raise ContractError("receipt file list checksum mismatch")
            return
        if family == "universe_snapshot":
            expected_digest = adjustment_snapshot_generation(manifest)
        elif family == "qlib_dataset_export":
            expected_digest = manifest.get("manifest_digest_sha256")
        else:
            expected_digest = manifest.get("manifest_digest_sha256")
        if binding.manifest_digest_sha256 and expected_digest != binding.manifest_digest_sha256:
            raise ContractError(f"{family} readback manifest digest mismatch")
        if family == "universe_snapshot":
            expected_checksum = manifest.get("members_artifact", {}).get("checksum_sha256")
        elif family == "backtest_result":
            expected_checksum = manifest.get("equity_curve_artifact", {}).get("checksum_sha256")
        elif family == "backtest_config":
            expected_checksum = manifest.get("price_source_binding", {}).get("data_checksum_sha256")
        elif family == "target_weights":
            expected_checksum = manifest.get("weights_checksum_sha256")
        elif family == "model_artifact":
            expected_checksum = manifest.get("artifact_checksum_sha256")
        elif family == "model_run":
            expected_checksum = manifest.get("model_definition_generation_id")
        elif family == "qlib_dataset_export":
            expected_checksum = manifest.get("manifest_digest_sha256")
        elif family == "feature_preprocessing":
            expected_checksum = manifest.get("output_frame_sha256")
        else:
            expected_checksum = manifest.get("data_checksum_sha256")
        if binding.data_checksum_sha256 and expected_checksum != binding.data_checksum_sha256:
            raise ContractError(f"{family} readback data checksum mismatch")
