from __future__ import annotations

import json
import copy
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from ..contracts.model_layer import (
    ModelContractLoader,
    bind_reviewed_quality_decision,
    reviewed_allowed_statuses,
    research_contract_identities,
    research_stage_plan_sha256,
    research_stage_plan_v2_sha256,
    research_stage_plan_v3_sha256,
    research_stage_plan_v4_sha256,
    research_stage_plan_v5_sha256,
    research_stage_plan_v6_sha256,
    resolve_subject_quality_decision,
    sha256_json,
)
from ..contracts.gate_contracts import adjustment_snapshot_generation
from ..errors import ContractError
from ..risk.contracts import (
    validate_risk_event_sequence,
    validate_risk_manifest,
    validate_risk_policy_governance,
)


def _prediction_binding_date(binding: Any) -> str | None:
    """The decision date one prediction_set binding was published for, if it carries one.

    The date is a physical-path segment (`.../date=YYYY-MM-DD`) rather than a
    manifest field, so it is recoverable from the plan alone: a resumed run has
    no in-memory prediction results to read it from. A resolver-built binding has
    no physical path (only stage output bindings do), so a missing path yields
    `None` rather than failing -- the caller then has no date to order by and
    must take the dates from the prediction stage itself.
    """
    physical_path = binding.physical_path if not isinstance(binding, Mapping) else binding.get("physical_path")
    if not physical_path:
        return None
    return Path(physical_path).name.removeprefix("date=")


def _binding_key(stage: str, binding: Any) -> tuple[str, str, str]:
    """Identity of one output binding, per date where the family repeats.

    cr.md 8.62 C: `prediction_set` and `target_weights` publish one binding per
    decision date under the *same* output family, so the family alone cannot
    stand in for a binding. Folding a stage record back into the plan on family
    alone dropped every date but the last one, which the single-date fixtures
    could not see.

    The key deliberately excludes `physical_path`: the resolver builds plan
    bindings without one, while the stage records always carry it, so a
    path-based key would never match and every replayed stage would leave a
    duplicate binding behind for its own family.
    """
    if isinstance(binding, Mapping):
        family, generation_id = binding.get("output_family"), binding.get("generation_id")
    else:
        family, generation_id = binding.output_family, binding.generation_id
    return (stage, str(family), str(generation_id))


def _result_output_binding(binding: ResolvedStageBinding) -> dict[str, Any]:
    physical_path = binding.physical_path
    if not physical_path:
        # cr.md 8.62 C: `prediction_set` and `target_weights` publish one binding
        # per decision date, and their read-back needs the date segment. A
        # resolver-built binding has no path at all, so it cannot stand in for a
        # published one; the published binding for the same (stage, family,
        # generation) is the one this result must describe.
        physical_path = {
            "universe_snapshot": f"universes/generation={binding.generation_id}/manifest.json",
            "adjusted_price_dataset": f"adjusted_prices/generation={binding.generation_id}/manifest.json",
            "label_set": f"label_sets/generation={binding.generation_id}/manifest.json",
            "feature_preprocessing": f"feature_preprocessing/generation={binding.generation_id}/manifest.json",
            "sample_construction": f"sample_construction/generation={binding.generation_id}/manifest.json",
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
    DatasetStageResult,
    FactorStageAdapter,
    ModelStageAdapter,
    ModelStageResult,
    PortfolioStageAdapter,
    PortfolioStageResult,
    PredictionStageAdapter,
    PredictionStageResult,
    QlibExportStageAdapter,
    _synthesized_portfolio_definition,
    build_stage_state,
)
from ..models.definition import ModelDefinitionBuilder
from .contracts import research_request_schema_name, verify_stage_plan_review
from .resolver import FileResearchRunStore, ResolvedExecutionPlan, ResolvedStageBinding, build_dry_run_state, stage_plan_for_request


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
        paper_adapter: Any | None = None,
    ) -> None:
        self.factor_adapter = factor_adapter
        self.dataset_adapter = dataset_adapter
        self.export_adapter = export_adapter
        self.model_adapter = model_adapter
        self.prediction_adapter = prediction_adapter
        self.portfolio_adapter = portfolio_adapter
        self.backtest_adapter = backtest_adapter
        self.paper_adapter = paper_adapter
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
        scores_by_decision_date: Mapping[str, pd.DataFrame] | None,
        decision_dates: Sequence[str] | None = None,
        provider_uri: str = "file:///research-exports",
        qlib_import_path: str = "qlib",
        qlib_version: str | None = None,
        cache_root: Path | str | None = None,
        export_layout_root: Path | str | None = None,
        artifact_decision_provider: Callable[[str], Mapping[str, Any]] | None = None,
        definition_decision_provider: Callable[..., Mapping[str, Any]] | None = None,
        risk_documents: Mapping[str, Any] | None = None,
        paper_risk_decision_source: Callable[[str, str], Mapping[str, Any]] | None = None,
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

            # Every writer is immutable, so re-running a completed stage raises
            # "already exists". Fold the stages this run already passed into the plan
            # and skip them, or a failure anywhere makes the run unrepeatable.
            plan, completed = self._resume_from_passed_stages(plan)

            if "factor_computation" in completed:
                factor_result = None
            else:
                factor_result = self._run_factor(
                    plan, runner_identity=runner_identity,
                    quality_decisions=quality_decisions, created_at=created_at,
                )
                plan = self._plan_with_stage_outputs(plan, factor_result.published_state.manifest_path)
            if "dataset_preparation" in completed:
                dataset_result = None
            else:
                dataset_result = self._run_dataset(
                    plan, runner_identity=runner_identity,
                    quality_decisions=quality_decisions, created_at=created_at,
                )
                plan = self._plan_with_stage_outputs(plan, dataset_result.published_state.manifest_path)
            if "qlib_export" in completed:
                export_result = None
            else:
                export_result = self._run_export(
                    plan, runner_identity=runner_identity,
                    quality_decisions=quality_decisions, created_at=created_at,
                    provider_uri=provider_uri, qlib_import_path=qlib_import_path,
                    qlib_version=qlib_version, cache_root=cache_root,
                    export_layout_root=export_layout_root,
                )
                plan = self._plan_with_stage_outputs(plan, export_result.published_state.manifest_path)
            if "model_training" in completed:
                model_result = None
            else:
                model_result = self._run_model(
                    plan, runner_identity=runner_identity,
                    quality_decisions=quality_decisions, created_at=created_at,
                    artifact_decision_provider=artifact_decision_provider,
                )
                plan = self._plan_with_stage_outputs(plan, model_result.published_state.manifest_path)
            if "prediction_publication" in completed:
                prediction_results = []
            else:
                prediction_results = self._run_predictions(
                    plan, runner_identity=runner_identity,
                    quality_decisions=quality_decisions, created_at=created_at,
                    decision_dates=decision_dates,
                    scores_by_decision_date=scores_by_decision_date,
                )
                for prediction_result in prediction_results:
                    plan = self._plan_with_stage_outputs(plan, prediction_result.published_state.manifest_path)
            self._enforce_risk_gate(plan, risk_documents=risk_documents)
            if "portfolio_construction" in completed:
                portfolio_result = None
            else:
                portfolio_result = self._run_portfolio(
                    plan, runner_identity=runner_identity,
                    quality_decisions=quality_decisions, created_at=created_at,
                    definition_decision_provider=definition_decision_provider,
                )
                plan = self._plan_with_stage_outputs(plan, portfolio_result.published_state.manifest_path)
            if "backtest_execution" in completed:
                backtest_result = None
            else:
                backtest_result = self._run_backtest(
                    plan, runner_identity=runner_identity,
                    quality_decisions=quality_decisions, created_at=created_at,
                )
                plan = self._plan_with_stage_outputs(plan, backtest_result.published_state.manifest_path)
            if stage_plan_for_request(plan.request)[-2] == "paper_execution" and "paper_execution" not in completed:
                paper_result = self._run_paper(
                    plan, runner_identity=runner_identity,
                    quality_decisions=quality_decisions, created_at=created_at,
                    risk_decision_source=paper_risk_decision_source,
                )
                plan = self._plan_with_stage_outputs(plan, paper_result.published_state.manifest_path)
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

    def _validate_risk_binding(
        self,
        plan: ResolvedExecutionPlan,
        risk_documents: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if plan.request.get("contract_version") != 2:
            if risk_documents is not None:
                raise ContractError("risk documents are only accepted by research request v2")
            return {}
        if not isinstance(risk_documents, Mapping):
            raise ContractError("research request v2 requires risk documents")
        binding = plan.request["risk_binding"]
        policy = risk_documents.get("policy")
        policy_review = risk_documents.get("policy_review")
        state = risk_documents.get("state")
        run = risk_documents.get("run")
        decision = risk_documents.get("decision")
        events = risk_documents.get("events")
        if (
            any(document is None for document in (policy, policy_review, state, run, decision))
            or not isinstance(events, list)
        ):
            raise ContractError("research request v2 risk evidence is incomplete")
        validate_risk_manifest("risk_policy", dict(policy))
        validate_risk_manifest("risk_review_decision", dict(policy_review))
        validate_risk_policy_governance(dict(policy), policy_review)
        if binding["policy_review_binding"] != {
            "family": "risk_review_decision_v1",
            "generation_id": policy_review["generation_id"],
            "manifest_digest_sha256": policy_review["manifest_digest_sha256"],
        }:
            raise ContractError("research request v2 risk policy approval review lineage mismatch")
        validate_risk_manifest("risk_state", dict(state))
        validate_risk_manifest("risk_run", dict(run))
        validate_risk_manifest("risk_decision", dict(decision))
        validate_risk_event_sequence([dict(event) for event in events])
        expected = {
            "policy": binding["policy_binding"],
            "state": binding["state_binding"],
            "run": binding["run_binding"],
        }
        documents = {"policy": policy, "state": state, "run": run}
        for key, expected_binding in expected.items():
            actual = {
                "family": "risk_policy_v1" if key == "policy" else (
                    "risk_state_v1" if key == "state" else "risk_run_v1"
                ),
                "generation_id": documents[key]["generation_id"],
                "manifest_digest_sha256": documents[key]["manifest_digest_sha256"],
            }
            if actual != expected_binding:
                raise ContractError(f"research request v2 risk {key} lineage mismatch")
        if decision["risk_policy_binding"] != binding["policy_binding"]:
            raise ContractError("research request v2 risk decision policy lineage mismatch")
        if decision["risk_state_binding"] != binding["state_binding"]:
            raise ContractError("research request v2 risk decision state lineage mismatch")
        if binding["decision_binding"]["generation_id"] != decision["generation_id"] or binding["decision_binding"]["manifest_digest_sha256"] != decision["manifest_digest_sha256"]:
            raise ContractError("research request v2 risk decision lineage mismatch")
        if binding["decision_binding"]["action"] != decision["action"] or binding["decision_binding"]["decision_scope"] != decision["decision_scope"] or binding["decision_binding"]["decision_digest"] != decision["decision_digest"]:
            raise ContractError("research request v2 risk decision semantic mismatch")
        if run["run_scope"] != "portfolio_publication" or run["risk_policy_binding"] != binding["policy_binding"] or run["risk_state_binding"] != binding["state_binding"]:
            raise ContractError("research request v2 risk run lineage mismatch")
        decision_index = {
            (item["generation_id"], item["manifest_digest_sha256"])
            for item in run["decision_bindings"]
        }
        if (decision["generation_id"], decision["manifest_digest_sha256"]) not in decision_index:
            raise ContractError("research request v2 risk decision is not indexed by risk run")
        event_index = {
            (item["generation_id"], item["manifest_digest_sha256"])
            for item in run["event_bindings"]
        }
        if any(
            (event_binding["generation_id"], event_binding["manifest_digest_sha256"]) not in event_index
            for event_binding in binding["event_bindings"]
        ):
            raise ContractError("research request v2 risk events are not indexed by risk run")
        return {
            "policy": policy,
            "policy_review": policy_review,
            "state": state,
            "run": run,
            "decision": decision,
            "events": events,
        }

    def _enforce_risk_gate(
        self,
        plan: ResolvedExecutionPlan,
        *,
        risk_documents: Mapping[str, Any] | None,
    ) -> None:
        documents = self._validate_risk_binding(plan, risk_documents)
        if plan.request.get("contract_version") != 2:
            return
        if documents["decision"]["action"] != "allow":
            raise ContractError("research portfolio stage stopped by rejected risk decision")

    def _validate_plan(self, plan: ResolvedExecutionPlan) -> None:
        if plan.request.get("contract_version") in {3, 4}:
            raise ContractError("research request v3/v4 paper-execution runtime is not enabled")
        request_schema = research_request_schema_name(plan.request)
        if request_schema in {"research_run_request_v2", "research_run_request_v3", "research_run_request_v4", "research_run_request_v5", "research_run_request_v6"}:
            verify_stage_plan_review(
                plan.request["stage_plan_review"],
                stage_plan_sha256=plan.request["stage_plan_sha256"],
            )
        expected_stage_plan_sha256 = (
            research_stage_plan_sha256()
            if request_schema == "research_run_request"
            else research_stage_plan_v2_sha256()
            if request_schema == "research_run_request_v2"
            else research_stage_plan_v3_sha256()
            if request_schema == "research_run_request_v3"
            else research_stage_plan_v4_sha256()
            if request_schema == "research_run_request_v4"
            else research_stage_plan_v5_sha256()
            if request_schema == "research_run_request_v5"
            else research_stage_plan_v6_sha256()
        )
        if plan.request["stage_plan_sha256"] != expected_stage_plan_sha256:
            raise ContractError("research request stage plan digest mismatch")
        expected_generation, expected_digest = research_contract_identities(
            plan.request, schema_name=research_request_schema_name(plan.request)
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
            state["request_content_generation_id"], state["run_id"], "resolve_request",
            stage_plan_for_request(plan.request),
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
                request["request_content_generation_id"],
                request["manifest_digest_sha256"],
                request["run_id"],
            )
            return
        self.run_store.publish_request(request, path_policy="strict_v1")

    def _decision(
        self,
        quality_decisions: Mapping[str, Any],
        key: str,
        subject_generation_id: str | None = None,
        subject_content_sha256: str | None = None,
    ) -> Mapping[str, Any]:
        decision = quality_decisions.get(key)
        if decision is None:
            raise ContractError(f"missing research chain quality decision: {key}")
        if callable(decision):
            if subject_generation_id is None:
                raise ContractError(f"quality decision provider requires a subject: {key}")
            decision = resolve_subject_quality_decision(
                decision,
                subject_generation_id=subject_generation_id,
                subject_content_sha256=subject_content_sha256 or subject_generation_id,
            )
        if not isinstance(decision, Mapping):
            raise ContractError(f"invalid research chain quality decision: {key}")
        return decision

    def _subject_decision(
        self,
        quality_decisions: Mapping[str, Any],
        key: str,
    ) -> Any:
        """Pass a per-subject decision through to the stage flow unresolved.

        cr.md U14: stage-owned outputs whose subject is derived inside the
        flow itself (model_dataset, model_run, prediction_set,
        backtest_config/result) and flows whose subject content digest differs
        from the generation id (qlib export/receipt, target_weights) cannot be
        resolved eagerly by the runner — the flow derives the subject and
        resolves provider callables / verifies resolver envelopes itself.
        (portfolio_definition is the exception: the runner synthesizes it, so
        _run_portfolio resolves eagerly with the manifest digest.)
        """
        decision = quality_decisions.get(key)
        if decision is None:
            raise ContractError(f"missing research chain quality decision: {key}")
        if callable(decision):
            return decision
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
    ) -> DatasetStageResult:
        # cr.md H5: label_set/backtest_config decisions are fetched by resolved
        # subject at execute time so provider mode attests per subject and the
        # stage adapters verify them (aligned with the factor stage).
        # cr.md U14: the dataset is this stage's own output — the plan carries
        # no model_dataset binding yet, so the decision must pass through to
        # the writer, which resolves providers against the derived generation.
        label_binding = self._binding(plan, "label_set")
        preprocessing_binding = self._binding(plan, "feature_preprocessing")
        sample_binding = (
            self._binding(plan, "sample_construction")
            if plan.request.get("contract_version") == 6
            else None
        )
        result = self.dataset_adapter.run(
            plan,
            runner_identity=dict(runner_identity),
            quality_decision=self._subject_decision(quality_decisions, "model_dataset"),
            preprocessing_quality_decision=self._decision(
                quality_decisions, "feature_preprocessing",
                preprocessing_binding.generation_id,
            ),
            sample_construction_quality_decision=(
                self._decision(
                    quality_decisions, "sample_construction",
                    sample_binding.generation_id,
                )
                if sample_binding is not None
                else None
            ),
            label_quality_decision=self._decision(
                quality_decisions, "label_set", label_binding.generation_id
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
            export_quality_decision=self._subject_decision(
                quality_decisions, "qlib_dataset_export"
            ),
            receipt_quality_decision=self._subject_decision(
                quality_decisions, "qlib_init_receipt"
            ),
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
            model_quality_decision=self._subject_decision(quality_decisions, "model_run"),
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
        scores_by_decision_date: Mapping[str, pd.DataFrame] | None,
    ) -> None:
        dates = sorted(set(
            decision_dates or self._default_decision_dates(plan)
        ))
        dataset_binding = self._binding(plan, "model_dataset")
        if scores_by_decision_date is None:
            # No external scores were supplied: derive them from the artifact this
            # run just trained. Before this, `--scores-json` was mandatory, which
            # made the chain circular -- the prediction stage needed scores that
            # only the model in the same run could produce, so any real training
            # had to be split into two executes with a hand-written file in
            # between. Loading the artifact here keeps the governed surface
            # intact: the scores come from a published, reviewed model, not from
            # a file outside the chain.
            scores_by_decision_date = self._infer_scores_for_dates(
                plan, dates, runner_identity=runner_identity
            )
        if not scores_by_decision_date:
            raise ContractError("prediction stage requires governed scores")
        for decision_date in dates:
            if decision_date not in scores_by_decision_date:
                raise ContractError(f"missing governed scores for decision date {decision_date}")
        if self.model_adapter.last_result is None:
            # A resumed run skips model_training, so the artifact hand-off must be
            # rebuilt before the prediction stage. Recompute it here as well rather
            # than relying on the resume path's ordering.
            self.model_adapter.last_result = self._restored_model_result(plan)
        results = self.prediction_adapter.run_for_dates(
            plan,
            dataset_generation_id=dataset_binding.generation_id,
            model_stage_result=self.model_adapter.last_result,
            scores_by_decision_date={
                date: scores_by_decision_date[date]
                for date in dates
            },
            quality_decision=self._subject_decision(quality_decisions, "prediction_set"),
            eligibility_policy="reviewed-v1",
            eligibility_status="passed",
            runner_identity=dict(runner_identity),
            created_at=created_at,
        )
        self.prediction_adapter.last_result = results[-1]
        return results

    def _default_decision_dates(self, plan: ResolvedExecutionPlan) -> list[str]:
        """Score every exported session in the governed test split.

        Training and validation dates are not out-of-sample evidence, so the
        no-scores default must not backtest them. An explicit
        `--scores-json`/decision-date list still wins.
        """
        from ..models.dataset import resolve_test_decision_dates

        export_binding = self._binding(plan, "qlib_dataset_export")
        dataset_binding = self._binding(plan, "model_dataset")
        template = plan.request["dataset_policy_template"]
        dataset_manifest, _ = self.dataset_adapter.dataset_writer.read(
            template["dataset_name"],
            template["semantic_version"],
            dataset_binding.generation_id,
        )
        snapshot = (
            self.export_adapter.exporter.export_root
            / f"dataset={template['dataset_name']}"
            / f"generation={export_binding.generation_id}"
        )
        calendar_path = snapshot / "calendars" / "day.txt"
        try:
            calendar = [
                line for line in calendar_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
        except OSError as exc:
            raise ContractError(f"unpublished Qlib export calendar: {calendar_path}") from exc
        return resolve_test_decision_dates(dataset_manifest, calendar)

    def _infer_scores_for_dates(
        self,
        plan: ResolvedExecutionPlan,
        dates: Sequence[str],
        *,
        runner_identity: Mapping[str, str],
    ) -> dict[str, pd.DataFrame]:
        """Score `dates` by loading the model artifact this run published."""
        from ..models.inference import score_decision_dates

        if self.model_adapter.last_result is None:
            raise ContractError(
                "prediction stage needs scores, but no model artifact is available "
                "in this run; pass --scores-json or run the model stage first"
            )
        export_binding = self._binding(plan, "qlib_dataset_export")
        artifact_binding = self._binding(plan, "model_artifact")
        run_binding = self._binding(plan, "model_run")
        template = plan.request["dataset_policy_template"]
        artifact_manifest = self.model_adapter.last_result.artifact_manifest
        result = score_decision_dates(
            export_root=self.export_adapter.exporter.export_root,
            dataset_name=template["dataset_name"],
            export_generation_id=export_binding.generation_id,
            run_generation_id=run_binding.generation_id,
            artifact_generation_id=artifact_binding.generation_id,
            decision_dates=dates,
            # `qlib_version` belongs to the model artifact's runtime, not to
            # the export contract. Reading it from the export manifest made the
            # guard permanently receive `None`, so a training/inference runtime
            # mismatch was never checked on the research-chain path.
            expected_qlib_version=artifact_manifest["runtime_version"],
        )
        return result.scores

    def _run_portfolio(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decisions: Mapping[str, Any],
        created_at: str,
        definition_decision_provider: Callable[..., Mapping[str, Any]] | None,
    ) -> None:
        # cr.md 8.62 C: prediction_set is a per-date family, so it is selected
        # with the date-qualified helper, not the single-binding `_binding()`
        # that refuses a repeated family. The first decision date's binding is
        # what the definition names; the map below carries all of them.
        prediction_bindings = [
            binding for binding in self._stage_bindings(plan, "prediction_publication")
            if binding.output_family == "prediction_set"
        ]
        if not prediction_bindings:
            raise ContractError("research plan has no prediction_set binding")
        # cr.md 8.62 C: the hand-off is the last prediction built
        # (`self.prediction_adapter.last_result` is `results[-1]`), so the
        # definition must name that same generation. Selecting the earliest date
        # instead made the portfolio stage refuse a consistent run.
        from .adapters import _prediction_binding_for

        first_prediction = _prediction_binding_for(
            prediction_bindings, self.prediction_adapter.last_result.manifest["generation_id"]
        )
        universe_binding = self._binding(plan, "universe_snapshot")
        definition = _synthesized_portfolio_definition(
            plan,
            template=plan.request["portfolio_definition_template"],
            prediction_generation_id=first_prediction.generation_id,
            universe_generation_id=universe_binding.generation_id,
            created_at=created_at,
        )
        # Dates only, and only the bindings that actually carry one: a
        # resolver-built stand-in has no date to contribute.
        decision_dates = [
            date for date in
            (_prediction_binding_date(binding) for binding in prediction_bindings)
            if date
        ]
        result = self.portfolio_adapter.run(
            plan,
            prediction_stage_result=self.prediction_adapter.last_result,
            decision_dates=decision_dates,
            # cr.md U15: the runner synthesizes the definition, so it can hand
            # providers the true subject content (the manifest digest) —
            # PortfolioDefinitionBinding.bind requires raw signed decisions to
            # commit to it, and resolving with content=generation made that
            # documented shape unreachable. One-argument providers keep the
            # legacy generation-only resolution and remain envelope-only.
            definition_quality_decision=(
                resolve_subject_quality_decision(
                    definition_decision_provider,
                    subject_generation_id=definition["generation_id"],
                    subject_content_sha256=definition["manifest_digest_sha256"],
                )
                if definition_decision_provider else self._decision(
                    quality_decisions, "portfolio_definition", definition["generation_id"],
                    subject_content_sha256=definition["manifest_digest_sha256"],
                )
            ),
            weights_quality_decision=self._subject_decision(
                quality_decisions, "target_weights"
            ),
            runner_identity=dict(runner_identity),
            created_at=created_at,
            prediction_generation_by_date={
                _prediction_binding_date(binding): binding.generation_id
                for binding in prediction_bindings
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
        # cr.md U14: the backtest result is this stage's own output and the
        # config review attests the provisional generation (the config identity
        # covers the recorded checksum) — both decisions must pass through to
        # the adapter, which resolves providers against the derived subjects.
        result = self.backtest_adapter.run(
            plan,
            portfolio_stage_result=self.portfolio_adapter.last_result,
            quality_decision=self._subject_decision(
                quality_decisions, "backtest_result"
            ),
            config_quality_decision=self._subject_decision(
                quality_decisions, "backtest_config"
            ),
            runner_identity=dict(runner_identity),
            created_at=created_at,
        )
        self.backtest_adapter.last_result = result
        return result

    def _run_paper(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decisions: Mapping[str, Any],
        created_at: str,
        risk_decision_source: Callable[[str, str], Mapping[str, Any]] | None,
    ) -> Any:
        if self.paper_adapter is None:
            raise ContractError(
                f"research request v{plan.request.get('contract_version')} requires "
                "a wired paper execution adapter"
            )
        if risk_decision_source is None:
            raise ContractError(
                f"research request v{plan.request.get('contract_version')} requires "
                "a paper risk decision source"
            )
        portfolio_result = self.portfolio_adapter.last_result
        decision_dates = sorted(portfolio_result.target_weight_manifests)
        if not decision_dates:
            raise ContractError("paper stage requires ordered target-weight decision dates")
        universe_binding = self._binding(plan, "universe_snapshot")
        universe_manifest = self.dataset_adapter.universe_store.read_manifest(
            universe_binding.generation_id
        )

        def source(decision_date, config_generation_id, context):
            return risk_decision_source(
                decision_date,
                config_generation_id,
                {**dict(context), "universe": dict(universe_manifest)},
            )

        result = self.paper_adapter.run(
            plan,
            portfolio_stage_result=portfolio_result,
            decision_dates=decision_dates,
            market=self.paper_adapter.market,
            quality_decisions={
                family: self._subject_decision(quality_decisions, family)
                for family in (
                    "execution_config",
                    "order_plan",
                    "execution_result",
                    "paper_portfolio_state",
                )
            },
            run_store=self.run_store,
            risk_decision_source=source,
            runner_identity=dict(runner_identity),
            created_at=created_at,
        )
        self.paper_adapter.last_result = result
        return result

    def _reconcile_paper_bindings(self, bindings: Sequence[Any]) -> None:
        """Reject missing, extra, unordered or cross-date paper bindings."""
        from ..execution.result_reconciliation import (
            reconcile_paper_stage_bindings,
            verify_paper_binding_tampering,
        )

        if self.paper_adapter is None:
            raise ContractError("paper stage result is unavailable for reconciliation")
        # A resumed run skips `paper_execution` (already `passed`), so
        # `paper_adapter.last_result` is empty exactly on the normal retry path.
        # Recover the decision dates from the published execution configs: each
        # one records its own `decision_date`, and the bindings are ordered 4N.
        in_memory = getattr(self.paper_adapter, "last_result", None)
        if in_memory is not None:
            decision_dates = list(in_memory.decision_dates)
        else:
            decision_dates = [
                str(self.paper_adapter.config_store.read(binding.generation_id)["decision_date"])
                for binding in bindings
                if binding.output_family == "execution_config"
            ]
        if not decision_dates:
            raise ContractError("paper stage result is unavailable for reconciliation")
        derived: list[dict[str, Any]] = []
        for index, binding in enumerate(bindings):
            decision_date = decision_dates[index // 4] if index // 4 < len(decision_dates) else None
            parts = Path(binding.physical_path or "").parts
            execution_id = parts[1] if len(parts) > 1 else ""
            derived.append({
                "output_family": binding.output_family,
                "execution_id": execution_id,
                "decision_date": decision_date,
                "generation_id": binding.generation_id,
                "manifest_digest_sha256": binding.manifest_digest_sha256,
                "data_checksum_sha256": binding.data_checksum_sha256,
                "physical_path": binding.physical_path,
            })
        reconcile_paper_stage_bindings(derived, decision_dates=decision_dates)
        # The reconciled bindings are read back from the published stage state,
        # so they must still match the authoritative in-memory stage result;
        # a tampered state file fails closed here.
        published = (
            list(in_memory.bindings)
            if in_memory is not None
            else [
                {
                    "output_family": binding.output_family,
                    "generation_id": binding.generation_id,
                    "manifest_digest_sha256": binding.manifest_digest_sha256,
                    "data_checksum_sha256": binding.data_checksum_sha256,
                    "physical_path": binding.physical_path,
                }
                for binding in bindings
            ]
        )
        if len(published) != len(derived):
            raise ContractError("paper stage result bindings do not match the reconciled set")
        for item, expected in zip(derived, published):
            if item["output_family"] != expected.get("output_family"):
                raise ContractError("paper stage result binding order does not match the reconciled set")
            verify_paper_binding_tampering(
                item,
                expected_generation_id=expected.get("generation_id"),
                expected_digest=expected.get("manifest_digest_sha256"),
            )
        for offset in range(0, len(derived), 4):
            group = derived[offset:offset + 4]
            if len({item["execution_id"] for item in group}) != 1:
                raise ContractError("paper bindings mix execution ids within one decision date")

    def _plan_with_stage_outputs(
        self, plan: ResolvedExecutionPlan, state_path: Path
    ) -> ResolvedExecutionPlan:
        # The state lives in this run's own store, so scope the link rule to that
        # store: without a root the loader refuses the OS's ``/tmp`` and ``/var``
        # links and every state readback fails (external review B9-B).
        state = ModelContractLoader(self.run_store.root).load("research_run_state", state_path)
        output_bindings = state["stage_records"][-1]["output_bindings"]
        stage = state["stage_records"][-1]["stage"]
        # Per-date families (prediction_set, target_weights) legitimately publish
        # many bindings under one family in a single stage record, so "the family
        # this stage just wrote" does not identify a single binding. Dedupe on the
        # reproduction key (stage, family, generation id, physical path) instead of
        # the family alone, or every date but the last one is silently dropped.
        existing_keys = {
            _binding_key(stage, binding) for binding in output_bindings
        }
        retained = [
            binding for binding in plan.stage_bindings
            if _binding_key(binding.stage, binding) not in existing_keys
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
        if plan.request.get("contract_version") == 2:
            readback_status.update({
                "risk_policy": "passed",
                "risk_state": "passed",
                "risk_run": "passed",
                "risk_decision": "passed",
            })
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
        stage_plan = stage_plan_for_request(plan.request)
        self._reverify_input_sets(plan)
        for stage in stage_plan[1:-1]:
            bindings = self._stage_bindings(plan, stage)
            # cr.md 8.62 C: `prediction_set` and `target_weights` publish one
            # binding per decision date, so a family-level dedupe (or a
            # path-less resolver stand-in for the same family) must not survive
            # next to the published ones. Drop a stand-in only when the stage
            # actually published that family -- resolver-only input families
            # (universe snapshot, adjusted prices, labels, preprocessing, config)
            # keep their path-less binding, which `_result_output_binding`
            # reconstructs a path for.
            published_families = {
                binding.output_family for binding in bindings if binding.physical_path
            }
            bindings = [
                binding for binding in bindings
                if binding.physical_path
                or binding.output_family not in published_families
            ]
            if not bindings:
                raise ContractError(f"research stage output is missing: {stage}")
            if stage == "paper_execution":
                self._reconcile_paper_bindings(bindings)
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
            "contract_version": plan.request.get("contract_version", 1),
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
        if "owning_report" not in decision:
            # Provider mode hands the runner a *raw* signed decision (the stage
            # flows bind it themselves), but this is the one call site that
            # needs the `quality_decision` envelope: it re-reads the published
            # report and compares its checksum to the result identity. Wrap it
            # here rather than changing the shared `_decision` contract, which
            # every stage flow depends on.
            from ..contracts.model_layer import bind_reviewed_quality_decision

            report, checksum = bind_reviewed_quality_decision(
                dict(decision),
                binding_type="research_run_result_v1",
                subject_generation_id=generation,
                subject_content_sha256=generation,
            )
            decision = {
                "contract_version": 1, "schema_version": "1.0.0",
                "binding_type": "research_run_result_v1",
                "subject_generation_id": generation,
                "subject_manifest_digest_sha256": generation,
                "owning_report": report,
                "decision_checksum_sha256": checksum,
                "provider_id": "external-model-quality-reviewer-v1",
                "trust_anchor_id": report["key_id"],
            }
        ModelContractLoader.validate("quality_decision", dict(decision))
        owning_report = dict(decision["owning_report"])
        if (
            decision["binding_type"] != "research_run_result_v1"
            or owning_report["binding_type"] != "research_run_result_v1"
            or decision["subject_generation_id"] != generation
            or owning_report["bound_generation_id"] != generation
            or decision["decision_checksum_sha256"] != owning_report["report_checksum_sha256"]
            or owning_report["status"] not in reviewed_allowed_statuses("research_run_result_v1")
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

    def _resume_from_passed_stages(
        self, plan: ResolvedExecutionPlan
    ) -> tuple[ResolvedExecutionPlan, set[str]]:
        """Replay the stages this run already completed, so a failure is retryable.

        Every writer in this repo is immutable, so re-running a completed stage raises
        "already exists" instead of producing the same artifact again. Without this,
        any failure -- even in the last stage -- makes the whole run unrepeatable and
        the operator has to move directories aside by hand.

        The states are already durable (`_publish_failed_state` writes them on the way
        out), so resuming is a matter of reading back what passed and folding each
        stage's output bindings into the plan, which is exactly what the inline
        `_plan_with_stage_outputs` calls do on the success path.

        A stage is replayed only when its recorded state is `passed`. Anything else
        (failed, blocked, running) is left for the normal path to redo, so this cannot
        mask a half-finished stage.
        """
        request_generation_id = plan.request["request_content_generation_id"]
        run_id = plan.request["run_id"]
        completed: set[str] = set()
        resumed = plan
        for summary in self.run_store.list_state_snapshots(request_generation_id, run_id):
            if summary.status != "passed":
                continue
            state = self.run_store.read_state(
                request_generation_id, run_id, summary.stage, summary.manifest_digest_sha256,
            )
            stage_record = state["stage_records"][-1]
            if not stage_record["output_bindings"]:
                # `resolve_request` has no output family; the later stages all do.
                continue
            resumed = self._apply_stage_bindings(resumed, stage_record)
            completed.add(summary.stage)
        # A resumed run never executes the stages it skipped, so the in-process
        # hand-off between stages (`adapter.last_result`) is empty for them. The next
        # stage that runs therefore has to get its predecessor's result from the
        # published artifacts rather than from memory -- otherwise the first stage
        # after the resume point dies on an attribute that nothing ever set.
        # `resumed`, not `plan`: the bindings for the replayed stages only exist on
        # the folded plan, and the hand-off needs them to find the published ids.
        self._restore_cross_stage_state(resumed, completed)
        return resumed, completed

    def _restore_cross_stage_state(self, plan: ResolvedExecutionPlan, completed: set[str]) -> None:
        """Re-hydrate the stage-to-stage hand-offs a resume skips.

        Only `model_training` needs restoring for now: it is the sole hand-off with no
        read-back path (`_read_published_document` refuses `model_artifact` rather than
        reconstructing it). Every other pair is either served by a store read inside the
        consuming stage, or by `_read_receipt`. Restoring from the published manifests
        keeps the invariant that a resumed run consumes the same bytes it published.
        """
        if "model_training" not in completed:
            return
        self.model_adapter.last_result = self._restored_model_result(plan)
        # cr.md 8.62 D: the portfolio stage consumes the prediction hand-off, not
        # just the plan's bindings, so a resume that replays prediction_publication
        # has to re-hydrate it too. It is a per-date family, so the earliest
        # published prediction is the one the hand-off carries.
        if "prediction_publication" in completed:
            self.prediction_adapter.last_result = self._restored_prediction_result(plan)
        # cr.md 8.62 D: the backtest stage reads the portfolio hand-off the same
        # way, so a resume that replays portfolio_construction has to rebuild it
        # from the published per-date target weights.
        if "portfolio_construction" in completed:
            self.portfolio_adapter.last_result = self._restored_portfolio_result(plan)

    def _restored_model_result(self, plan: ResolvedExecutionPlan) -> ModelStageResult:
        """Rebuild the model hand-off from the published run and artifact.

        The model stage's in-process result is the only hand-off with no read-back
        path (`_read_published_document` refuses `model_artifact`), so it is rebuilt
        from the two published artifacts through the same store read the stage
        itself performs after training.
        """
        run_binding = self._binding(plan, "model_run")
        artifact_binding = self._binding(plan, "model_artifact")
        run_path = (
            self.run_store.root / "model_runs" / f"{run_binding.generation_id}.json"
        )
        if not run_path.is_file():
            raise ContractError("published model run is unavailable for readback")
        try:
            run_manifest = json.loads(run_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ContractError("malformed model run manifest") from exc
        ModelContractLoader.validate("model_run", run_manifest)
        if run_manifest["run_content_generation_id"] != run_binding.generation_id:
            raise ContractError("model run readback identity mismatch")
        artifact_manifest, _ = self.model_adapter.artifact_store.read(
            run_manifest["run_content_generation_id"], artifact_binding.generation_id,
        )
        if artifact_manifest["generation_id"] != artifact_binding.generation_id:
            raise ContractError("model artifact readback identity mismatch")
        return ModelStageResult(
            run_manifest=run_manifest,
            artifact_manifest=artifact_manifest,
        )

    def _restored_portfolio_result(self, plan: ResolvedExecutionPlan) -> Any:
        """Rebuild the portfolio hand-off from the published target weights.

        The definition is *synthesized* by the runner (never stored), so it is
        re-derived the same way `_run_portfolio` derives it, from the same plan
        bindings. Only the per-date target weights have to be read back.
        """
        from .adapters import _latest_prediction_binding
        from .contracts import PublishedState

        bindings = [
            binding for binding in plan.stage_bindings
            if binding.stage == "portfolio_construction"
            and binding.output_family == "target_weights"
        ]
        if not bindings:
            raise ContractError("resumed run has no published target_weights binding")
        manifests: dict[str, dict[str, Any]] = {}
        frames: dict[str, pd.DataFrame] = {}
        for binding in sorted(bindings, key=lambda item: _binding_key(item.stage, item)):
            # `target_weights/date=YYYY-MM-DD/generation=...`
            decision_date = Path(binding.physical_path).parent.name.removeprefix("date=")
            manifest, frame = self.portfolio_adapter.target_weight_store.read(
                binding.generation_id, decision_date,
            )
            if manifest["generation_id"] != binding.generation_id:
                raise ContractError("target-weight readback identity mismatch on resume")
            manifests[decision_date] = manifest
            frames[decision_date] = frame
        # The backtest stage re-reads the portfolio stage's own state snapshot, so
        # the restored hand-off has to carry that snapshot's real digest. Deriving
        # it from a binding (or guessing a stage number) cannot satisfy
        # `read_state`, which refuses a digest it cannot reproduce.
        state_path = self._state_path_for(plan, "portfolio_construction")
        state = ModelContractLoader(self.run_store.root).load("research_run_state", state_path)
        published_state = PublishedState(
            manifest_path=state_path,
            manifest_digest_sha256=state["manifest_digest_sha256"],
        )
        prediction_bindings = [
            binding for binding in plan.stage_bindings
            if binding.stage == "prediction_publication"
            and binding.output_family == "prediction_set"
        ]
        definition = _synthesized_portfolio_definition(
            plan,
            template=plan.request["portfolio_definition_template"],
            prediction_generation_id=_latest_prediction_binding(prediction_bindings).generation_id,
            universe_generation_id=self._binding(plan, "universe_snapshot").generation_id,
            created_at=state["created_at"],
        )
        return PortfolioStageResult(
            definition_manifest=definition,
            target_weight_manifests=manifests,
            target_weight_frames=frames,
            published_state=published_state,
        )

    def _state_path_for(self, plan: ResolvedExecutionPlan, stage: str) -> Path:
        """The published state snapshot path for one stage of this run."""
        number = stage_plan_for_request(plan.request).index(stage)
        return (
            self.run_store.root / "research_runs" / "states"
            / f"request={plan.request['request_content_generation_id']}"
            / f"run={plan.request['run_id']}" / f"stage={number:02d}" / "manifest.json"
        )

    def _restored_prediction_result(self, plan: ResolvedExecutionPlan) -> Any:
        """Rebuild the prediction hand-off from the published prediction sets."""
        from .adapters import _latest_prediction_binding

        bindings = [
            binding for binding in plan.stage_bindings
            if binding.stage == "prediction_publication"
            and binding.output_family == "prediction_set"
        ]
        if not bindings:
            raise ContractError("resumed run has no published prediction_set binding")
        binding = _latest_prediction_binding(bindings)
        manifest, frame = self.prediction_adapter.prediction_builder.read(
            binding.generation_id,
            Path(binding.physical_path).name.removeprefix("date=") if binding.physical_path else plan.request["window_start_date"],
        )
        if manifest["generation_id"] != binding.generation_id:
            raise ContractError("prediction readback identity mismatch on resume")
        return PredictionStageResult(
            manifest=manifest, frame=frame, published_state=None,
        )

    def _apply_stage_bindings(
        self, plan: ResolvedExecutionPlan, stage_record: Mapping[str, Any]
    ) -> ResolvedExecutionPlan:
        """Fold one recorded stage's output bindings into the plan."""
        stage = stage_record["stage"]
        output_bindings = stage_record["output_bindings"]
        existing_keys = {_binding_key(stage, binding) for binding in output_bindings}
        retained = [
            binding for binding in plan.stage_bindings
            if _binding_key(binding.stage, binding) not in existing_keys
        ]
        bindings = tuple([
            *retained,
            *(ResolvedStageBinding(
                stage=stage,
                output_family=binding["output_family"],
                generation_id=binding["generation_id"],
                manifest_digest_sha256=binding["manifest_digest_sha256"],
                data_checksum_sha256=binding["data_checksum_sha256"],
                physical_path=binding.get("physical_path"),
                quality_decision_checksum_sha256=binding.get("quality_decision_checksum_sha256"),
            ) for binding in output_bindings),
        ])
        # Tests drive this runner with a lightweight stand-in plan, so fall back to a
        # shallow copy rather than assuming `dataclasses.replace` applies.
        if is_dataclass(plan) and not isinstance(plan, type):
            return replace(plan, stage_bindings=bindings)
        resumed = copy.copy(plan)
        resumed.stage_bindings = bindings
        return resumed

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
        stage_plan = stage_plan_for_request(plan.request)
        last_passed_index = max(
            (stage_plan.index(record["stage"]) for record in stage_records),
            default=-1,
        )
        failed_stage = stage_plan[min(last_passed_index + 1, len(stage_plan) - 1)]
        stage_records.append({
            "stage": failed_stage,
            "status": "failed",
            "output_bindings": [],
            "failure_reason": "stage_failed",
        })
        state = {
            "contract_version": plan.request.get("contract_version", 1),
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
            request_generation_id, run_id, failed_stage, stage_plan
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
    def _state_relative_path(
        request_generation_id: str, run_id: str, stage: str, stage_order: Sequence[str]
    ) -> str:
        # The caller passes the request's own stage plan (V3 inserts
        # paper_execution); never number stages from a module default.
        stage_number = f"{tuple(stage_order).index(stage):02d}"
        return (
            f"research_runs/states/request={request_generation_id}"
            f"/run={run_id}/stage={stage_number}/manifest.json"
        )

    def _read_receipt(self, binding) -> dict[str, Any]:
        last_result = getattr(self.export_adapter, "last_result", None)
        if last_result is not None and last_result.manifest["generation_id"] == binding.generation_id:
            return dict(last_result.manifest)
        # The published receipt lives under `research_exports/`; the
        # `external_quality_reviews` directory holds the review report, not the
        # receipt. Only the resumed path reaches this branch, because a fresh run
        # is served by the adapter's in-memory result -- which is why the wrong
        # directory had never been read.
        path = self.run_store.root / "research_exports" / f"{binding.generation_id}.json"
        if not path.is_file():
            raise ContractError("unpublished Qlib init receipt")
        # Same scoping as the state readback: the receipt is written under this
        # run's store, so that store is the governed root (external review B9-B).
        document = ModelContractLoader(self.run_store.root).load("qlib_init_receipt", path)
        if document["generation_id"] != binding.generation_id:
            raise ContractError("Qlib init receipt generation mismatch")
        if document["manifest_digest_sha256"] != binding.manifest_digest_sha256:
            raise ContractError("Qlib init receipt manifest digest mismatch")
        return dict(document)

    def _reverify_input_sets(self, plan: ResolvedExecutionPlan) -> None:
        """Re-bind v4/v5/v6 set bindings to their owning stores at reconciliation.

        The membership and factor-partition sets are resolved once at resolve time,
        but the raw artifacts stay on disk for the whole run.  Re-deriving both sets
        here (before any stage output is read back) means a store mutated after
        resolution fails the run instead of silently publishing a result whose input
        lineage no longer matches the request.  The inputs are immutable, so one
        re-verification per reconciliation covers every stage readback.
        """
        if plan.request.get("contract_version") not in {4, 5, 6}:
            return
        membership_store = getattr(self.dataset_adapter, "membership_store", None)
        trading_calendar = getattr(self.dataset_adapter, "trading_calendar", None)
        if membership_store is None or trading_calendar is None:
            raise ContractError(
                "v4/v5/v6 reconciliation requires a governed membership store and trading calendar"
            )
        from .membership_input import verify_request_input_sets

        try:
            verify_request_input_sets(
                plan.request,
                membership_store=membership_store,
                factor_store=self.factor_adapter.store,
                trading_days=trading_calendar,
            )
        except ContractError as exc:
            raise ContractError(f"input set readback verification failed: {exc}") from exc

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
        elif family == "sample_construction":
            manifest = self.dataset_adapter.sample_construction_store.read_manifest(generation_id)
        elif family == "model_dataset":
            template = self.run_store.last_plan.request["dataset_policy_template"]
            manifest = self.dataset_adapter.dataset_writer.read(
                template["dataset_name"],
                template["semantic_version"],
                generation_id,
            )[0]
        elif family == "qlib_dataset_export":
            # The dataset name is a property of the request's template, not of
            # whatever this process happened to publish. Reading it off
            # `dataset_writer.last_published_manifest` made reconciliation work
            # only in the same process that ran the dataset stage: a resumed run
            # (the normal retry path after any later stage fails) has an empty
            # writer and died here with "no dataset has been published by this
            # writer". `model_dataset` above already resolves the same name from
            # the template; this branch now does the same.
            template = self.run_store.last_plan.request["dataset_policy_template"]
            manifest = self.export_adapter.exporter.read(
                template["dataset_name"],
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
        elif family in {"execution_config", "order_plan", "execution_result", "paper_portfolio_state"}:
            if self.paper_adapter is None:
                raise ContractError("paper stage adapter is unavailable for readback")
            reader = {
                "execution_config": self.paper_adapter.config_store.read,
                "order_plan": self.paper_adapter.plan_store.read,
                "execution_result": self.paper_adapter.result_store.read,
                "paper_portfolio_state": self.paper_adapter.state_store.read,
            }[family]
            readback = reader(generation_id)
            manifest = readback[0] if isinstance(readback, tuple) else readback
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
            price_binding = manifest.get("price_source_binding", {})
            # cr.md 8.63: v1 names one partition, v2 names the window's set
            expected_checksum = (
                price_binding.get("data_checksum_sha256")
                or price_binding.get("set_digest_sha256")
            )
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
        elif family == "sample_construction":
            expected_checksum = manifest.get("data_checksum_sha256")
        elif family == "execution_config":
            expected_checksum = manifest.get("manifest_digest_sha256")
        else:
            expected_checksum = manifest.get("data_checksum_sha256")
        if binding.data_checksum_sha256 and expected_checksum != binding.data_checksum_sha256:
            raise ContractError(f"{family} readback data checksum mismatch")
