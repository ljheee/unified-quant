from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from ..backtest.engine import BacktestEngine, BacktestResultStore
from ..contracts.canonical_v2 import file_sha256_bytes
from ..contracts.gate_contracts import adjustment_snapshot_generation
from ..contracts.model_layer import ModelContractLoader, bind_reviewed_quality_decision, canonical_json, model_manifest_identities, research_contract_identities, sha256_bytes
from ..contracts.artifacts import UniverseSnapshotStore
from ..errors import ContractError
from ..models.dataset import DatasetBuilder
from ..models.qlib_export import QlibDatasetExporter, QlibInitReceiptBuilder
from ..models.predictions import PredictionBuilder
from ..models.trainer import ModelRunBuilder
from ..models.dataset_writer import DatasetWriter
from ..models.feature_preprocessing import FeaturePreprocessorBuilder
from ..models.features import FeatureSchemaBuilder, FeatureSchemaValidator
from ..portfolio.builder import PortfolioBuilder, PortfolioDefinitionBinding, TargetWeightStore
from .contracts import PublishedState
from .owning_contracts import (
    AdjustedPriceDatasetStore,
    BacktestConfigStore,
    BacktestMarketDatasetStore,
    FeaturePreprocessingStore,
    FeatureSchemaStore,
    LabelStore,
)
from .resolver import FileResearchRunStore, ResolvedExecutionPlan, _STAGE_PLAN


_STAGE_BINDING_KEYS = {
    "generation_id", "manifest_digest_sha256", "data_checksum_sha256"
}


def _stage_binding(plan: ResolvedExecutionPlan, *, stage: str, output_family: str):
    matches = [
        binding for binding in plan.stage_bindings
        if binding.stage == stage and binding.output_family == output_family
    ]
    if not matches:
        raise ContractError(f"resolved plan has no {output_family} binding")
    if len(matches) != 1:
        raise ContractError(f"ambiguous {output_family} binding in resolved plan")
    return matches[0]


def build_stage_state(
    plan: ResolvedExecutionPlan,
    *,
    stage: str,
    output_bindings: Sequence[Mapping[str, Any]],
    runner_identity: Mapping[str, str],
    created_at: str | None = None,
) -> dict[str, Any]:
    if stage not in _STAGE_PLAN or stage == "resolve_request":
        raise ContractError("invalid research runtime stage")
    if any(
        set(binding) != _STAGE_BINDING_KEYS | {
            "output_family", "physical_path", "quality_decision_checksum_sha256", "failure_reason"
        }
        for binding in output_bindings
    ):
        raise ContractError("invalid stage output binding fields")
    current_index = _STAGE_PLAN.index(stage)
    stage_records: list[dict[str, Any]] = [
        {
            "stage": current_stage,
            "status": "passed",
            "output_bindings": [],
            "failure_reason": None,
        }
        for current_stage in _STAGE_PLAN[:current_index]
    ]
    stage_records.append({
        "stage": stage,
        "status": "passed",
        "output_bindings": [dict(binding) for binding in output_bindings],
        "failure_reason": None,
    })
    state: dict[str, Any] = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "request_content_generation_id": plan.request["request_content_generation_id"],
        "request_manifest_digest_sha256": plan.request_manifest_digest_sha256,
        "run_id": plan.request["run_id"],
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "runner_identity": dict(runner_identity),
        "intent": "execute",
        "stage_records": stage_records,
        "final_status": "passed",
    }
    state["state_content_generation_id"] = "0" * 64
    state["manifest_digest_sha256"] = "0" * 64
    generation, digest = research_contract_identities(state, schema_name="research_run_state")
    state["state_content_generation_id"] = generation
    state["manifest_digest_sha256"] = digest
    ModelContractLoader.validate("research_run_state", state)
    return state


@dataclass(frozen=True)
class FactorStageResult:
    manifest: dict[str, Any]
    published_state: PublishedState


@dataclass(frozen=True)
class DatasetStageResult:
    manifest: dict[str, Any]
    frame: pd.DataFrame
    published_state: PublishedState


def _merge_labels(
    factor_frame: pd.DataFrame,
    label_frame: pd.DataFrame,
    *,
    label_generation_id: str,
) -> pd.DataFrame:
    if label_frame.empty:
        raise ContractError("label artifact is empty")
    if list(label_frame.columns) != ["instrument", "decision_date", "label"]:
        raise ContractError("label artifact columns do not match the dataset contract")
    label_keys = label_frame[["instrument", "decision_date"]].astype({"instrument": "string"})
    if label_keys.duplicated().any():
        raise ContractError("duplicate label keys prevent dataset preparation")
    factor_keys = factor_frame[["instrument", "datetime"]].copy()
    factor_keys["decision_date"] = pd.to_datetime(factor_keys["datetime"]).dt.tz_localize(None)
    factor_frame = factor_frame.copy()
    factor_frame["decision_date"] = factor_keys["decision_date"]
    merged = factor_frame.merge(
        label_frame,
        on=["instrument", "decision_date"],
        how="left",
        validate="one_to_one",
        suffixes=("", "__label"),
    )
    if merged["label"].isna().all():
        raise ContractError(f"label artifact has no values for dataset generation {label_generation_id}")
    feature_columns = [column for column in merged.columns if column not in {"instrument", "datetime", "decision_date", "label"}]
    return merged[["instrument", "datetime", *feature_columns, "label"]]


def _prepare_preprocessed_frame(
    factor_frame: pd.DataFrame,
    preprocessing: Mapping[str, Any],
    input_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any],
    *,
    label_generation_id: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not isinstance(factor_frame, pd.DataFrame) or factor_frame.empty:
        raise ContractError("factor artifact is empty")
    ordered_features = list(preprocessing["ordered_features"])
    expected_input_columns = ["instrument", "datetime", *ordered_features]
    if factor_frame.columns.tolist() != expected_input_columns:
        raise ContractError("factor frame columns do not match preprocessing input schema")
    builder = FeaturePreprocessorBuilder(
        preprocess_name=preprocessing["preprocess_name"],
        semantic_version=preprocessing["semantic_version"],
        transform=preprocessing["transform"],
        min_group_observations=preprocessing["policy"]["min_group_observations"],
        code_fingerprint=preprocessing["code_fingerprint"],
    )
    input_frame = factor_frame.sort_values(
        ["instrument", "datetime"], kind="mergesort"
    ).reset_index(drop=True)
    if builder.frame_fingerprint(input_frame) != preprocessing["input_frame_sha256"]:
        raise ContractError("preprocessing input frame fingerprint mismatch")
    FeatureSchemaValidator.validate_against_frame(dict(input_schema), input_frame)
    if input_schema["generation_id"] != preprocessing["input_feature_schema_generation_id"]:
        raise ContractError("preprocessing input feature schema mismatch")
    output_frame = builder.transform(input_frame, ordered_features)
    output_frame_fingerprint = FeaturePreprocessorBuilder.frame_fingerprint(output_frame)
    if output_frame_fingerprint != preprocessing["output_frame_sha256"]:
        raise ContractError("preprocessing output frame fingerprint mismatch")
    FeatureSchemaValidator.validate_against_frame(dict(output_schema), output_frame)
    if output_schema["generation_id"] != preprocessing["output_feature_schema_generation_id"]:
        raise ContractError("preprocessing output feature schema mismatch")
    if output_frame.empty:
        raise ContractError(f"preprocessed dataset has no rows for label generation {label_generation_id}")
    return input_frame, output_frame


class DatasetStageAdapter:
    """Prepare a dataset from resolved owning artifacts and reviewed policy."""

    def __init__(
        self,
        *,
        factor_store: Any,
        adjusted_price_store: AdjustedPriceDatasetStore,
        label_store: LabelStore,
        universe_store: UniverseSnapshotStore,
        feature_schema_store: FeatureSchemaStore,
        preprocessing_store: FeaturePreprocessingStore,
        dataset_writer: DatasetWriter,
        run_store: FileResearchRunStore,
    ) -> None:
        self.factor_store = factor_store
        self.adjusted_price_store = adjusted_price_store
        self.label_store = label_store
        self.universe_store = universe_store
        self.feature_schema_store = feature_schema_store
        self.preprocessing_store = preprocessing_store
        self.dataset_writer = dataset_writer
        self.run_store = run_store

    def run(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decision: Mapping[str, Any],
        preprocessing_quality_decision: Mapping[str, Any],
        created_at: str | None = None,
    ) -> DatasetStageResult:
        template = plan.request.get("dataset_policy_template")
        if not isinstance(template, dict) or template.get("status") != "reviewed":
            raise ContractError("research request has no reviewed dataset policy")
        factor_binding = _stage_binding(plan, stage="factor_computation", output_family="factor_partition")
        universe_binding = _stage_binding(plan, stage="factor_computation", output_family="universe_snapshot")
        adjusted_binding = _stage_binding(plan, stage="dataset_preparation", output_family="adjusted_price_dataset")
        label_binding = _stage_binding(plan, stage="dataset_preparation", output_family="label_set")
        preprocessing_binding = _stage_binding(plan, stage="dataset_preparation", output_family="feature_preprocessing")

        factor_manifest = self.factor_store.read_manifest(factor_binding.generation_id)
        if factor_manifest["manifest_digest_sha256"] != factor_binding.manifest_digest_sha256:
            raise ContractError("factor binding manifest digest mismatch")
        if factor_manifest["data_checksum_sha256"] != factor_binding.data_checksum_sha256:
            raise ContractError("factor binding data checksum mismatch")
        _, factor_frame = self.factor_store.read_partition(factor_binding.generation_id)

        universe_manifest = self.universe_store.read_manifest(universe_binding.generation_id)
        if universe_manifest["generation_id"] != universe_binding.generation_id:
            raise ContractError("universe binding generation mismatch")
        universe_manifest_digest = adjustment_snapshot_generation(universe_manifest)
        if universe_manifest_digest != universe_binding.manifest_digest_sha256:
            raise ContractError("universe binding manifest digest mismatch")
        members = self.universe_store.read_members(
            universe_manifest["generation_id"],
            universe_id=universe_manifest["universe_id"],
            requested_valid_from=date.fromisoformat(plan.request["window_start_date"]),
            requested_valid_to=date.fromisoformat(plan.request["window_end_date"]),
        )
        member_ids = set(members["instrument"].astype(str))
        factor_frame = factor_frame[factor_frame["instrument"].astype(str).isin(member_ids)].copy()
        if factor_frame.empty:
            raise ContractError("factor artifact has no universe members")

        adjusted_manifest = self.adjusted_price_store.read_manifest(adjusted_binding.generation_id)
        if adjusted_manifest["manifest_digest_sha256"] != adjusted_binding.manifest_digest_sha256:
            raise ContractError("adjusted price binding manifest digest mismatch")
        if adjusted_manifest["data_checksum_sha256"] != adjusted_binding.data_checksum_sha256:
            raise ContractError("adjusted price binding data checksum mismatch")
        label_manifest, label_frame = self.label_store.read_frame(label_binding.generation_id)
        if label_manifest["manifest_digest_sha256"] != label_binding.manifest_digest_sha256:
            raise ContractError("label binding manifest digest mismatch")
        if label_manifest["data_checksum_sha256"] != label_binding.data_checksum_sha256:
            raise ContractError("label binding data checksum mismatch")
        preprocessing = self.preprocessing_store.read_manifest(preprocessing_binding.generation_id)
        if preprocessing["manifest_digest_sha256"] != preprocessing_binding.manifest_digest_sha256:
            raise ContractError("preprocessing binding manifest digest mismatch")
        _, expected_preprocessing_checksum = bind_reviewed_quality_decision(
            dict(preprocessing_quality_decision),
            binding_type="feature_preprocessing_v1",
            subject_generation_id=preprocessing["generation_id"],
            subject_content_sha256=preprocessing["generation_id"],
        )
        if expected_preprocessing_checksum != preprocessing["quality_report_checksum_sha256"]:
            raise ContractError("preprocessing quality decision checksum mismatch")
        if preprocessing["input_factor_set"] != factor_manifest["factor_set"]:
            raise ContractError("preprocessing input factor set mismatch")
        if preprocessing["input_factor_version"] != factor_manifest["factor_version"]:
            raise ContractError("preprocessing input factor version mismatch")
        if preprocessing["input_factor_generation_ids"] != [factor_binding.generation_id]:
            raise ContractError("preprocessing input factor generation mismatch")
        input_schema = self.feature_schema_store.read_schema(preprocessing["input_feature_schema_generation_id"])
        output_schema = self.feature_schema_store.read_schema(preprocessing["output_feature_schema_generation_id"])
        _, preprocessed_frame = _prepare_preprocessed_frame(
            factor_frame,
            preprocessing,
            input_schema,
            output_schema,
            label_generation_id=label_binding.generation_id,
        )
        dataset_frame = _merge_labels(
            preprocessed_frame,
            label_frame,
            label_generation_id=label_binding.generation_id,
        )
        if template["missing_policy"] == "fail_closed":
            null_columns = [*preprocessing["ordered_features"], "label"]
            null_counts = dataset_frame[null_columns].isna().sum()
            if (null_counts > 0).any():
                raise ContractError(f"fail_closed dataset contains null values: {null_counts[null_counts > 0].to_dict()}")

        manifest = DatasetBuilder(
            dataset_name=template["dataset_name"],
            semantic_version=template["semantic_version"],
            code_fingerprint=template["code_fingerprint"],
        ).build(
            ordered_features=list(preprocessing["ordered_features"]),
            factor_set=factor_manifest["factor_set"],
            factor_version=factor_manifest["factor_version"],
            factor_generation_ids=[factor_binding.generation_id],
            label_set_name=label_manifest["name"],
            label_generation_id=label_binding.generation_id,
            universe_snapshot_generation_id=universe_binding.generation_id,
            split_policy=template["split_policy"],
            missing_policy=template["missing_policy"],
            feature_preprocessing_generation_id=preprocessing_binding.generation_id,
            input_feature_schema=input_schema,
            row_count=len(dataset_frame),
        )
        self.dataset_writer.write(
            manifest,
            dataset_frame,
            feature_schema=output_schema,
            quality_report=dict(quality_decision),
            preprocessing_manifest=dict(preprocessing),
            preprocessing_input_frame=factor_frame,
            preprocessing_frame=preprocessed_frame,
            preprocessing_input_feature_schema=input_schema,
            preprocessing_quality_report=dict(preprocessing_quality_decision),
        )
        published_manifest = self.dataset_writer.last_published_manifest
        _, readback_frame = self.dataset_writer.read(
            published_manifest["dataset_name"],
            published_manifest["semantic_version"],
            published_manifest["generation_id"],
        )
        relative_path = (
            Path("datasets")
            / f"dataset={published_manifest['dataset_name']}"
            / f"version={published_manifest['semantic_version']}"
            / f"generation={published_manifest['generation_id']}"
            / "manifest.json"
        )
        output_binding = {
            "output_family": "model_dataset",
            "generation_id": published_manifest["generation_id"],
            "manifest_digest_sha256": published_manifest["manifest_digest_sha256"],
            "data_checksum_sha256": published_manifest["data_checksum_sha256"],
            "physical_path": str(relative_path),
            "quality_decision_checksum_sha256": published_manifest["quality_report_checksum_sha256"],
            "failure_reason": None,
        }
        state = build_stage_state(
            plan,
            stage="dataset_preparation",
            output_bindings=[output_binding],
            runner_identity=runner_identity,
            created_at=created_at,
        )
        return DatasetStageResult(
            manifest=published_manifest,
            frame=readback_frame,
            published_state=self.run_store.publish_state(
                state, stage="dataset_preparation"
            ),
        )


class FactorStageAdapter:
    """Bind a reviewed factor partition through its owning read APIs only."""

    def __init__(self, store: Any, run_store: FileResearchRunStore) -> None:
        self.store = store
        self.run_store = run_store

    def run(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decision: Mapping[str, Any],
        created_at: str | None = None,
    ) -> FactorStageResult:
        binding = _stage_binding(
            plan, stage="factor_computation", output_family="factor_partition"
        )
        manifest = self.store.read_manifest(binding.generation_id)
        if manifest["manifest_digest_sha256"] != binding.manifest_digest_sha256:
            raise ContractError("factor binding manifest digest mismatch")
        if manifest["data_checksum_sha256"] != binding.data_checksum_sha256:
            raise ContractError("factor binding data checksum mismatch")
        try:
            quality_document = dict(quality_decision)
            owning_report = dict(quality_document["owning_report"])
            ModelContractLoader.validate("quality_decision", quality_document)
        except (KeyError, TypeError) as exc:
            raise ContractError("invalid factor quality decision envelope") from exc
        if (
            quality_document.get("binding_type") != "factor_v1"
            or owning_report.get("binding_type") != "factor_v1"
            or quality_document.get("subject_generation_id") != manifest["generation_id"]
            or owning_report.get("bound_generation_id") != manifest["generation_id"]
            or quality_document.get("subject_manifest_digest_sha256") is not None
            or quality_document.get("trust_anchor_id") != "factor-review-key-v1"
            or quality_document.get("decision_checksum_sha256") != sha256_bytes(canonical_json(owning_report))
            or owning_report.get("status") not in {"passed", "warning"}
        ):
            raise ContractError("factor quality decision subject or checksum mismatch")
        quality_decision_checksum_sha256 = quality_document["decision_checksum_sha256"]
        if not isinstance(quality_decision_checksum_sha256, str) or len(quality_decision_checksum_sha256) != 64:
            raise ContractError("invalid factor quality decision checksum")
        _, frame = self.store.read_partition(binding.generation_id)
        if frame.empty or manifest["row_count"] != len(frame):
            raise ContractError("factor readback row count mismatch")
        relative_path = self.store.manifest_path(binding.generation_id)
        output_binding = {
            "output_family": "factor_partition",
            "generation_id": binding.generation_id,
            "manifest_digest_sha256": binding.manifest_digest_sha256,
            "data_checksum_sha256": binding.data_checksum_sha256,
            "physical_path": str(relative_path),
            "quality_decision_checksum_sha256": quality_decision_checksum_sha256,
            "failure_reason": None,
        }
        state = build_stage_state(
            plan,
            stage="factor_computation",
            output_bindings=[output_binding],
            runner_identity=runner_identity,
            created_at=created_at,
        )
        return FactorStageResult(
            manifest=manifest,
            published_state=self.run_store.publish_state(
                state, stage="factor_computation"
            ),
        )


@dataclass(frozen=True)
class QlibExportStageResult:
    manifest: dict[str, Any]
    export_manifest: dict[str, Any]
    snapshot_path: Path
    published_state: PublishedState


@dataclass(frozen=True)
class ReceiptStageResult:
    manifest: dict[str, Any]
    published_state: PublishedState


@dataclass(frozen=True)
class ModelStageResult:
    run_manifest: dict[str, Any]
    definition: dict[str, Any]
    artifact_manifest: dict[str, Any]
    artifact_bytes: bytes
    published_state: PublishedState


@dataclass(frozen=True)
class PredictionStageResult:
    manifest: dict[str, Any]
    frame: pd.DataFrame
    published_state: PublishedState


@dataclass(frozen=True)
class PortfolioStageResult:
    definition_manifest: dict[str, Any]
    target_weight_manifests: dict[str, dict[str, Any]]
    target_weight_frames: dict[str, pd.DataFrame]
    published_state: PublishedState


@dataclass(frozen=True)
class BacktestStageResult:
    config_manifest: dict[str, Any]
    result_manifest: dict[str, Any]
    result_artifacts: dict[str, pd.DataFrame]
    published_state: PublishedState


def _synthesized_portfolio_definition(
    plan: ResolvedExecutionPlan,
    *,
    template: Mapping[str, Any],
    prediction_generation_id: str,
    universe_generation_id: str,
    created_at: str,
) -> dict[str, Any]:
    template_keys = {
        "contract_version", "portfolio_name", "weight_scheme", "scheme_parameters",
        "score_policy", "constraints", "rebalance_schedule", "industry_source_binding",
    }
    definition = {
        **{key: value for key, value in template.items() if key in template_keys},
        "schema_version": "1.0.0",
        "universe_snapshot_generation_id": universe_generation_id,
        "prediction_set_generation_id": prediction_generation_id,
        "run_id": plan.request["run_id"],
        "created_at": created_at,
        "quality_report_checksum_sha256": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
        "generation_id": "0" * 64,
    }
    definition["generation_id"], definition["manifest_digest_sha256"] = model_manifest_identities(
        definition, schema_name="portfolio_definition"
    )
    return definition


class PortfolioStageAdapter:
    """Construct daily target weights from the reviewed prediction stage."""

    def __init__(
        self,
        *,
        universe_store: UniverseSnapshotStore,
        portfolio_builder: PortfolioBuilder,
        target_weight_store: TargetWeightStore,
        run_store: FileResearchRunStore,
        prediction_builder: PredictionBuilder,
    ) -> None:
        self.universe_store = universe_store
        self.portfolio_builder = portfolio_builder
        self.target_weight_store = target_weight_store
        self.run_store = run_store
        self.prediction_builder = prediction_builder

    def run(
        self,
        plan: ResolvedExecutionPlan,
        *,
        prediction_stage_result: PredictionStageResult,
        decision_dates: Sequence[str],
        definition_quality_decision: Mapping[str, Any],
        weights_quality_decision: Mapping[str, Any],
        runner_identity: Mapping[str, str],
        created_at: str | None = None,
        prediction_generation_by_date: Mapping[str, str] | None = None,
    ) -> PortfolioStageResult:
        if not decision_dates:
            raise ContractError("portfolio construction requires decision dates")
        prediction_bindings = [
            binding for binding in plan.stage_bindings
            if binding.stage == "prediction_publication"
            and binding.output_family == "prediction_set"
        ]
        if not prediction_bindings:
            raise ContractError("resolved plan has no prediction_set binding")
        prediction_binding = prediction_bindings[0]
        if prediction_stage_result.manifest["generation_id"] != prediction_binding.generation_id:
            raise ContractError("portfolio prediction binding mismatch")
        if (
            prediction_stage_result.manifest["manifest_digest_sha256"]
            != prediction_binding.manifest_digest_sha256
        ):
            raise ContractError("portfolio prediction manifest digest mismatch")
        universe_binding = _stage_binding(
            plan, stage="factor_computation", output_family="universe_snapshot"
        )
        definition = _synthesized_portfolio_definition(
            plan,
            template=plan.request["portfolio_definition_template"],
            prediction_generation_id=prediction_binding.generation_id,
            universe_generation_id=universe_binding.generation_id,
            created_at=created_at or prediction_stage_result.manifest["created_at"],
        )
        report = definition_quality_decision.get("owning_report")
        if not isinstance(report, dict):
            raise ContractError("portfolio stage requires a bound owning quality report")
        prediction_generation_map = dict(prediction_generation_by_date or {})
        prediction_generation_digests = {
            binding.generation_id: binding.manifest_digest_sha256
            for binding in plan.stage_bindings
            if binding.output_family == "prediction_set"
        }
        for decision_date in sorted(set(decision_dates)):
            if decision_date not in prediction_generation_map:
                prediction_generation_map[decision_date] = prediction_binding.generation_id
        bound_definition, _ = PortfolioDefinitionBinding.bind(
            definition, quality_decision=dict(definition_quality_decision)
        )
        universe_manifest = self.universe_store.read_manifest(universe_binding.generation_id)
        members = self.universe_store.read_members(
            universe_binding.generation_id,
            universe_id=plan.request.get("universe_id", universe_manifest["universe_id"]),
        )
        universe_instruments = members["instrument"].astype(str).tolist()

        target_manifests: dict[str, dict[str, Any]] = {}
        target_frames: dict[str, pd.DataFrame] = {}
        previous_generation: str | None = None
        previous_frame: pd.DataFrame | None = None
        output_bindings: list[dict[str, Any]] = []
        for decision_date in sorted(set(decision_dates)):
            current_prediction_generation = prediction_generation_map[decision_date]
            prediction_manifest, prediction_frame = self.prediction_builder.read(
                current_prediction_generation, decision_date
            )
            if current_prediction_generation not in prediction_generation_digests:
                raise ContractError("portfolio prediction binding mismatch")
            if prediction_manifest["manifest_digest_sha256"] != prediction_generation_digests[current_prediction_generation]:
                raise ContractError("portfolio prediction manifest digest mismatch")
            if prediction_manifest["decision_date"] != decision_date:
                raise ContractError("portfolio prediction decision date mismatch")
            scores = prediction_frame.set_index("instrument")["score"]
            previous_target_weights = (
                dict(zip(previous_frame["instrument"], previous_frame["weight"]))
                if previous_frame is not None else None
            )
            manifest, frame = self.portfolio_builder.build(
                definition=bound_definition,
                prediction_generation_id=current_prediction_generation,
                decision_date=decision_date,
                scores=scores,
                universe_instruments=universe_instruments,
                previous_target_weights=previous_target_weights,
                quality_decision=None,
            )
            self.target_weight_store.publish(
                manifest,
                frame,
                quality_decision=dict(weights_quality_decision),
                previous_target_weights_generation_id=previous_generation,
            )
            readback_manifest, readback_frame = self.target_weight_store.read(
                manifest["generation_id"], decision_date
            )
            if readback_manifest["generation_id"] != manifest["generation_id"]:
                raise ContractError("target-weight readback identity mismatch")
            target_manifests[decision_date] = readback_manifest
            target_frames[decision_date] = readback_frame
            previous_generation = readback_manifest["generation_id"]
            previous_frame = readback_frame
            output_bindings.append({
                "output_family": "target_weights",
                "generation_id": readback_manifest["generation_id"],
                "manifest_digest_sha256": readback_manifest["manifest_digest_sha256"],
                "data_checksum_sha256": readback_manifest["weights_checksum_sha256"],
                "physical_path": f"target_weights/date={decision_date}/generation={readback_manifest['generation_id']}",
                "quality_decision_checksum_sha256": readback_manifest["quality_report_checksum_sha256"],
                "failure_reason": None,
            })
        state = build_stage_state(
            plan,
            stage="portfolio_construction",
            output_bindings=output_bindings,
            runner_identity=runner_identity,
            created_at=created_at,
        )
        return PortfolioStageResult(
            definition_manifest=bound_definition,
            target_weight_manifests=target_manifests,
            target_weight_frames=target_frames,
            published_state=self.run_store.publish_state(state, stage="portfolio_construction"),
        )


class BacktestStageAdapter:
    """Execute one governed backtest from ordered portfolio stage outputs."""

    def __init__(
        self,
        *,
        backtest_engine: BacktestEngine,
        backtest_result_store: BacktestResultStore,
        backtest_config_store: BacktestConfigStore,
        run_store: FileResearchRunStore,
        adjusted_price_store: AdjustedPriceDatasetStore,
        market_dataset_store: BacktestMarketDatasetStore,
    ) -> None:
        self.adjusted_price_store = adjusted_price_store
        self.market_dataset_store = market_dataset_store
        self.backtest_engine = backtest_engine
        self.backtest_result_store = backtest_result_store
        self.backtest_config_store = backtest_config_store
        self.run_store = run_store

    def run(
        self,
        plan: ResolvedExecutionPlan,
        *,
        portfolio_stage_result: PortfolioStageResult,
        quality_decision: Mapping[str, Any],
        runner_identity: Mapping[str, str],
        created_at: str | None = None,
    ) -> BacktestStageResult:
        config_binding = _stage_binding(
            plan, stage="backtest_execution", output_family="backtest_config"
        )
        config = self.backtest_config_store.read_manifest(
            config_binding.generation_id
        )
        if config["manifest_digest_sha256"] != config_binding.manifest_digest_sha256:
            raise ContractError("backtest config manifest digest mismatch")
        state = self.run_store.read_state(
            plan.request["request_content_generation_id"],
            plan.request["run_id"],
            "portfolio_construction",
            portfolio_stage_result.published_state.manifest_digest_sha256,
        )
        expected_state_keys = {
            "request_content_generation_id": plan.request["request_content_generation_id"],
            "run_id": plan.request["run_id"],
        }
        if (
            state["manifest_digest_sha256"] != portfolio_stage_result.published_state.manifest_digest_sha256
            or any(state.get(key) != value for key, value in expected_state_keys.items())
        ):
            raise ContractError("portfolio stage published state binding mismatch")
        portfolio_stage = next(
            record for record in state["stage_records"] if record["stage"] == "portfolio_construction"
        )
        if portfolio_stage["status"] != "passed" or len(portfolio_stage["output_bindings"]) != len(portfolio_stage_result.target_weight_manifests):
            raise ContractError("portfolio stage published state is incomplete")
        if (
            config["universe_binding"]["snapshot_generation_id"]
            != portfolio_stage_result.definition_manifest["universe_snapshot_generation_id"]
        ):
            raise ContractError("backtest universe binding mismatch")
        ordered_dates = sorted(portfolio_stage_result.target_weight_manifests)
        for decision_date in ordered_dates:
            manifest = portfolio_stage_result.target_weight_manifests[decision_date]
            output_binding = next(
                binding for binding in portfolio_stage["output_bindings"]
                if binding["output_family"] == "target_weights"
                and binding.get("physical_path") == f"target_weights/date={decision_date}/generation={manifest['generation_id']}"
            )
            if (
                output_binding["manifest_digest_sha256"] != manifest["manifest_digest_sha256"]
                or output_binding["data_checksum_sha256"] != manifest["weights_checksum_sha256"]
            ):
                raise ContractError("portfolio target-weight published binding mismatch")

        price_binding = config["price_source_binding"]
        adjusted_binding = _stage_binding(
            plan, stage="dataset_preparation", output_family="adjusted_price_dataset"
        )
        if price_binding["dataset_generation_id"] != adjusted_binding.generation_id:
            raise ContractError("backtest price binding mismatch")
        adjusted_manifest = self.adjusted_price_store.read_manifest(price_binding["dataset_generation_id"])
        price_frame = self.adjusted_price_store.read_frame(price_binding["dataset_generation_id"])
        required_price_columns = {"datetime", "instrument", "open", "high", "low", "close", "volume", "amount"}
        if not required_price_columns.issubset(price_frame.columns):
            raise ContractError("backtest price dataset is missing required columns")
        if file_sha256_bytes(
            self.adjusted_price_store.data_path(price_binding["dataset_generation_id"]).read_bytes()
        ) != price_binding["data_checksum_sha256"]:
            raise ContractError("backtest price data checksum mismatch")
        price_panel = price_frame.copy()
        price_panel["date"] = pd.to_datetime(price_panel["datetime"]).dt.strftime("%Y-%m-%d")
        price_panel = price_panel.set_index(["date", "instrument"])[["open", "high", "low", "close", "volume", "amount"]]

        calendar_frame = self.market_dataset_store.read_frame(
            config["calendar_binding"]["generation_id"],
            config["calendar_binding"]["checksum_sha256"],
            dataset_type="calendar",
            required_columns={"date"},
        )
        calendar_dates = sorted(calendar_frame["date"].astype(str).unique().tolist())
        price_dates = sorted({str(value) for value in price_panel.index.get_level_values("date").unique()})
        if not set(ordered_dates).issubset(set(calendar_dates)):
            raise ContractError("portfolio decision dates are not in the governed calendar")
        if set(price_dates) != set(calendar_dates):
            raise ContractError("backtest price dates do not match governed calendar")

        suspension_frame = self.market_dataset_store.read_frame(
            config["suspension_binding"]["dataset_generation_id"],
            config["suspension_binding"]["data_checksum_sha256"],
            dataset_type="suspension",
            required_columns={"date", "instrument", "suspended"},
        )
        suspension_dates = {
            (str(row.date), str(row.instrument))
            for row in suspension_frame.loc[suspension_frame["suspended"].astype(bool)].itertuples(index=False)
        }
        corporate_action_frame = self.market_dataset_store.read_frame(
            config["corporate_action_binding"]["dataset_generation_id"],
            config["corporate_action_binding"]["data_checksum_sha256"],
            dataset_type="corporate_action",
            required_columns={"date", "instrument"},
        )
        corporate_action_instruments = set(
            corporate_action_frame.loc[
                corporate_action_frame["date"].astype(str).isin(
                    {
                        str(price_dates[price_dates.index(date) + 1])
                        for date in ordered_dates
                        if price_dates.index(date) + 1 < len(price_dates)
                    }
                ), "instrument"
            ].astype(str).unique().tolist()
        )
        weight_partitions = {
            decision_date: portfolio_stage_result.target_weight_frames[decision_date]
            for decision_date in ordered_dates
        }
        target_weight_generations = [
            {
                "decision_date": decision_date,
                "generation_id": portfolio_stage_result.target_weight_manifests[decision_date]["generation_id"],
                "manifest_digest_sha256": portfolio_stage_result.target_weight_manifests[decision_date]["manifest_digest_sha256"],
            }
            for decision_date in ordered_dates
        ]
        manifest, artifacts = self.backtest_engine.run(
            config=config,
            portfolio_definition=portfolio_stage_result.definition_manifest,
            weight_partitions=weight_partitions,
            price_panel=price_panel,
            suspension_dates=suspension_dates,
            corporate_action_instruments=corporate_action_instruments,
            quality_decision=dict(quality_decision),
        )
        self.backtest_result_store.publish(
            manifest,
            artifacts,
            quality_decision=dict(quality_decision),
            target_weight_generations=target_weight_generations,
        )
        readback_manifest, readback_artifacts = self.backtest_result_store.read(
            manifest["generation_id"]
        )
        if readback_manifest["generation_id"] != manifest["generation_id"]:
            raise ContractError("backtest result readback identity mismatch")
        final_state = build_stage_state(
            plan,
            stage="backtest_execution",
            output_bindings=[{
                "output_family": "backtest_result",
                "generation_id": readback_manifest["generation_id"],
                "manifest_digest_sha256": readback_manifest["manifest_digest_sha256"],
                "data_checksum_sha256": readback_manifest["equity_curve_artifact"]["checksum_sha256"],
                "physical_path": f"backtest_results/result={readback_manifest['generation_id']}",
                "quality_decision_checksum_sha256": readback_manifest["quality_report_checksum_sha256"],
                "failure_reason": None,
            }],
            runner_identity=runner_identity,
            created_at=created_at,
        )
        return BacktestStageResult(
            config_manifest=config,
            result_manifest=readback_manifest,
            result_artifacts=readback_artifacts,
            published_state=self.run_store.publish_state(final_state, stage="backtest_execution"),
        )


class QlibExportStageAdapter:
    """Export and verify the governed model dataset through its owning APIs."""

    def __init__(
        self,
        *,
        exporter: QlibDatasetExporter,
        receipt_builder: QlibInitReceiptBuilder,
        dataset_writer: DatasetWriter,
        run_store: FileResearchRunStore,
    ) -> None:
        self.exporter = exporter
        self.receipt_builder = receipt_builder
        self.dataset_writer = dataset_writer
        self.run_store = run_store

    def run(
        self,
        plan: ResolvedExecutionPlan,
        *,
        dataset_generation_id: str,
        dataset_manifest_digest_sha256: str,
        feature_columns: Sequence[str],
        label_column: str,
        provider_uri: str,
        qlib_import_path: str,
        qlib_version: str,
        cache_root: str | Path,
        cache_files_before: set[str],
        cache_files_after: set[str],
        export_quality_decision: Mapping[str, Any],
        receipt_quality_decision: Mapping[str, Any],
        runner_identity: Mapping[str, str],
        created_at: str | None = None,
    ) -> QlibExportStageResult:
        dataset_binding = _stage_binding(plan, stage="dataset_preparation", output_family="model_dataset")
        if dataset_generation_id != dataset_binding.generation_id or dataset_manifest_digest_sha256 != dataset_binding.manifest_digest_sha256:
            raise ContractError("dataset stage binding mismatch")
        dataset_manifest, dataset_frame = self.dataset_writer.read(
            plan.request["dataset_policy_template"]["dataset_name"],
            plan.request["dataset_policy_template"]["semantic_version"],
            dataset_generation_id,
        )
        if dataset_manifest["manifest_digest_sha256"] != dataset_manifest_digest_sha256:
            raise ContractError("verified dataset manifest digest mismatch")
        ordered_features = list(dataset_manifest["ordered_features"])
        if list(feature_columns) != ordered_features:
            raise ContractError("export feature columns do not match dataset")
        feature_mapping = {column: column.upper() for column in feature_columns}
        calendar_dates = sorted(dataset_frame["datetime"].dt.strftime("%Y-%m-%d").unique().tolist())
        instruments = sorted(dataset_frame["instrument"].astype(str).unique().tolist())
        export_frame = dataset_frame[["instrument", "datetime", *feature_columns, label_column]].copy()
        numeric_columns = [*feature_columns, label_column]
        if export_frame[numeric_columns].isna().any().any():
            raise ContractError("Qlib export cannot contain missing governed dataset values")
        export_manifest = self.exporter.export(
            dataset_name=dataset_manifest["dataset_name"],
            generation_id=dataset_generation_id,
            frame=export_frame,
            feature_mapping=feature_mapping,
            label_column=label_column,
            label_mapping=label_column.upper(),
            calendar_dates=calendar_dates,
            instruments=instruments,
            provider_uri=provider_uri,
            quality_decision=dict(export_quality_decision),
        )
        verified_export = self.exporter.read(dataset_manifest["dataset_name"], export_manifest["generation_id"])
        if verified_export[0]["generation_id"] != export_manifest["generation_id"]:
            raise ContractError("Qlib export readback identity mismatch")
        cache_before = cache_files_before
        cache_after = cache_files_after
        cache_root_prefix = str(cache_root).rstrip("/") + "/"
        new_cache_files = cache_after - cache_before
        if new_cache_files and not any(path.startswith(cache_root_prefix) for path in new_cache_files):
            raise ContractError("Qlib cache evidence escapes governed cache root")
        receipt = self.receipt_builder.build(
            export_manifest=export_manifest,
            resolved_provider_uri=provider_uri,
            qlib_import_path=qlib_import_path,
            qlib_version=qlib_version,
            cache_root=cache_root,
            cache_files_before=cache_before,
            cache_files_after=cache_after,
            verified_export=verified_export,
            governance_root=self.run_store.root,
            quality_decision=dict(receipt_quality_decision),
        )
        state = build_stage_state(
            plan,
            stage="qlib_export",
            output_bindings=[{
                "output_family": "qlib_dataset_export",
                "generation_id": export_manifest["generation_id"],
                "manifest_digest_sha256": export_manifest["manifest_digest_sha256"],
                "data_checksum_sha256": verified_export[0]["manifest_digest_sha256"],
                "physical_path": verified_export[1].relative_to(self.run_store.root).as_posix(),
                "quality_decision_checksum_sha256": export_manifest["quality_report_checksum_sha256"],
                "failure_reason": None,
            }, {
                "output_family": "qlib_init_receipt",
                "generation_id": receipt["generation_id"],
                "manifest_digest_sha256": receipt["manifest_digest_sha256"],
                "data_checksum_sha256": receipt["file_list_checksum_sha256"],
                "physical_path": f"research_exports/{receipt['generation_id']}.json",
                "quality_decision_checksum_sha256": receipt["quality_report_checksum_sha256"],
                "failure_reason": None,
            }],
            runner_identity=runner_identity,
            created_at=created_at,
        )
        return QlibExportStageResult(
            manifest=receipt,
            export_manifest=export_manifest,
            snapshot_path=verified_export[1],
            published_state=self.run_store.publish_state(state, stage="qlib_export"),
        )


class ModelStageAdapter:
    """Bind a verified export through ModelRunBuilder, trainer, and ArtifactStore."""

    def __init__(
        self,
        *,
        trainer: QlibRuntimeTrainer,
        exporter: QlibDatasetExporter,
        artifact_store: Any,
        dataset_writer: DatasetWriter,
        universe_store: UniverseSnapshotStore,
        run_store: FileResearchRunStore,
    ) -> None:
        self.trainer = trainer
        self.exporter = exporter
        self.artifact_store = artifact_store
        self.dataset_writer = dataset_writer
        self.universe_store = universe_store
        self.run_store = run_store

    def run(
        self,
        plan: ResolvedExecutionPlan,
        *,
        dataset_generation_id: str,
        label_generation_id: str,
        universe_generation_id: str,
        factor_generation_id: str,
        export_manifest: dict[str, Any],
        receipt_manifest: dict[str, Any],
        definition: dict[str, Any],
        environment_lock_sha256: str,
        determinism_controls: Mapping[str, Any],
        model_quality_decision: Mapping[str, Any],
        artifact_quality_decision_provider: Callable[[str], Mapping[str, Any]],
        feature_columns: Sequence[str],
        label_column: str,
        runner_identity: Mapping[str, str],
        created_at: str | None = None,
    ) -> ModelStageResult:
        factor_binding = _stage_binding(plan, stage="factor_computation", output_family="factor_partition")
        dataset_binding = _stage_binding(plan, stage="dataset_preparation", output_family="model_dataset")
        universe_binding = _stage_binding(plan, stage="factor_computation", output_family="universe_snapshot")
        label_binding = _stage_binding(plan, stage="dataset_preparation", output_family="label_set")
        if dataset_generation_id != dataset_binding.generation_id:
            raise ContractError("model stage dataset binding mismatch")
        if label_generation_id != label_binding.generation_id:
            raise ContractError("model stage label binding mismatch")
        if universe_generation_id != universe_binding.generation_id:
            raise ContractError("model stage universe binding mismatch")
        if factor_generation_id != factor_binding.generation_id:
            raise ContractError("model stage factor binding mismatch")
        dataset_manifest, _ = self.dataset_writer.read(
            plan.request["dataset_policy_template"]["dataset_name"],
            plan.request["dataset_policy_template"]["semantic_version"],
            dataset_generation_id,
        )
        if dataset_generation_id != dataset_binding.generation_id or dataset_manifest["manifest_digest_sha256"] != dataset_binding.manifest_digest_sha256:
            raise ContractError("model stage dataset binding mismatch")
        label_manifest = self.run_store.read_published_document(
            "label_set", label_generation_id,
            manifest_digest_sha256=label_binding.manifest_digest_sha256,
            data_checksum_sha256=label_binding.data_checksum_sha256,
        )
        universe_manifest = self.universe_store.read_manifest(universe_generation_id)
        factor_manifest = self.run_store.read_published_document(
            "factor_partition", factor_generation_id,
            manifest_digest_sha256=factor_binding.manifest_digest_sha256,
            data_checksum_sha256=factor_binding.data_checksum_sha256,
        )
        run_manifest, bound_definition = ModelRunBuilder.build(
            definition=definition,
            dataset_manifest=dataset_manifest,
            export_manifest=export_manifest,
            receipt_manifest=receipt_manifest,
            environment_lock_sha256=environment_lock_sha256,
            determinism_controls=dict(determinism_controls),
            label_manifest=label_manifest,
            universe_snapshot=universe_manifest,
            factor_manifests={factor_generation_id: factor_manifest},
            quality_decision=dict(model_quality_decision),
            store_root=self.run_store.root,
        )
        if bound_definition["model_run_content_generation_id"] != run_manifest["run_content_generation_id"]:
            raise ContractError("model run content binding mismatch")
        verified_export = self.exporter.read(
            dataset_manifest["dataset_name"], export_manifest["generation_id"],
        )
        if verified_export[0] != export_manifest or verified_export[0]["manifest_digest_sha256"] != export_manifest["manifest_digest_sha256"]:
            raise ContractError("verified Qlib export binding mismatch")
        artifact_manifest, artifact_bytes = self.trainer.train(
            definition=bound_definition,
            dataset_manifest=dataset_manifest,
            export_manifest=export_manifest,
            receipt_manifest=receipt_manifest,
            verified_export=verified_export,
            feature_columns=list(feature_columns),
            label_column=label_column,
        )
        artifact_manifest = dict(artifact_manifest)
        artifact_manifest["generation_id"] = "0" * 64
        artifact_manifest["manifest_digest_sha256"] = "0" * 64
        artifact_generation = model_manifest_identities(
            artifact_manifest,
            schema_name="model_artifact",
            exclude_fields={"quality_report_checksum_sha256"},
        )[0]
        artifact_manifest["generation_id"] = artifact_generation
        self.artifact_store.publish(
            artifact_manifest,
            artifact_bytes,
            quality_report=dict(artifact_quality_decision_provider(artifact_generation)),
        )
        artifact_readback, _ = self.artifact_store.read(
            run_manifest["run_content_generation_id"], artifact_generation,
        )
        if artifact_readback["generation_id"] != artifact_generation:
            raise ContractError("artifact readback identity mismatch")
        state = build_stage_state(
            plan,
            stage="model_training",
            output_bindings=[
                {
                    "output_family": "model_run",
                    "generation_id": run_manifest["run_content_generation_id"],
                    "manifest_digest_sha256": run_manifest["manifest_digest_sha256"],
                    "data_checksum_sha256": bound_definition["generation_id"],
                    "physical_path": f"model_runs/{run_manifest['run_content_generation_id']}.json",
                    "quality_decision_checksum_sha256": run_manifest["quality_report_checksum_sha256"],
                    "failure_reason": None,
                },
                {
                    "output_family": "model_artifact",
                    "generation_id": artifact_generation,
                    "manifest_digest_sha256": artifact_readback["manifest_digest_sha256"],
                    "data_checksum_sha256": artifact_manifest["artifact_checksum_sha256"],
                    "physical_path": f"models/run_generation={run_manifest['run_content_generation_id']}/artifact_generation={artifact_generation}",
                    "quality_decision_checksum_sha256": artifact_readback["quality_report_checksum_sha256"],
                    "failure_reason": None,
                },
            ],
            runner_identity=runner_identity,
            created_at=created_at,
        )
        return ModelStageResult(
            run_manifest=run_manifest,
            definition=bound_definition,
            artifact_manifest=artifact_readback,
            artifact_bytes=artifact_bytes,
            published_state=self.run_store.publish_state(state, stage="model_training"),
        )


class PredictionStageAdapter:
    """Publish and read predictions exclusively through PredictionBuilder."""

    def __init__(self, prediction_builder: PredictionBuilder, run_store: FileResearchRunStore) -> None:
        self.prediction_builder = prediction_builder
        self.run_store = run_store

    def run(
        self,
        plan: ResolvedExecutionPlan,
        *,
        dataset_generation_id: str,
        model_stage_result: ModelStageResult,
        scores: pd.DataFrame,
        decision_date: str,
        quality_decision: Mapping[str, Any],
        eligibility_policy: str,
        eligibility_status: str,
        runner_identity: Mapping[str, str],
        created_at: str | None = None,
    ) -> PredictionStageResult:
        dataset_binding = _stage_binding(plan, stage="dataset_preparation", output_family="model_dataset")
        if dataset_generation_id != dataset_binding.generation_id:
            raise ContractError("prediction dataset binding mismatch")
        manifest, artifact = self.prediction_builder.build(
            prediction_set_name=f"research_prediction_{decision_date}",
            model_artifact_generation_id=model_stage_result.artifact_manifest["generation_id"],
            model_artifact_checksum=model_stage_result.artifact_manifest["artifact_checksum_sha256"],
            input_dataset_generation_id=dataset_generation_id,
            run_generation_id=model_stage_result.run_manifest["run_content_generation_id"],
            artifact_store=self.prediction_builder.artifact_store,
            decision_date=decision_date,
            scores=scores,
            eligibility_policy=eligibility_policy,
            eligibility_status=eligibility_status,
            quality_decision=dict(quality_decision),
        )
        self.prediction_builder.publish(manifest, artifact)
        readback_manifest, frame = self.prediction_builder.read(
            manifest["generation_id"], decision_date,
        )
        if readback_manifest["generation_id"] != manifest["generation_id"]:
            raise ContractError("prediction readback identity mismatch")
        state = build_stage_state(
            plan,
            stage="prediction_publication",
            output_bindings=[{
                "output_family": "prediction_set",
                "generation_id": readback_manifest["generation_id"],
                "manifest_digest_sha256": readback_manifest["manifest_digest_sha256"],
                "data_checksum_sha256": readback_manifest["data_checksum_sha256"],
                "physical_path": f"predictions/prediction_set={readback_manifest['generation_id']}/date={decision_date}",
                "quality_decision_checksum_sha256": readback_manifest["quality_report_checksum_sha256"],
                "failure_reason": None,
            }],
            runner_identity=runner_identity,
            created_at=created_at,
        )
        return PredictionStageResult(
            manifest=readback_manifest,
            frame=frame,
            published_state=self.run_store.publish_state(state, stage="prediction_publication"),
        )

    def run_for_dates(
        self,
        plan: ResolvedExecutionPlan,
        *,
        dataset_generation_id: str,
        model_stage_result: ModelStageResult,
        scores_by_decision_date: Mapping[str, pd.DataFrame],
        quality_decision: Mapping[str, Any],
        eligibility_policy: str,
        eligibility_status: str,
        runner_identity: Mapping[str, str],
        created_at: str | None = None,
    ) -> list[PredictionStageResult]:
        return [
            self.run(
                plan,
                dataset_generation_id=dataset_generation_id,
                model_stage_result=model_stage_result,
                scores=scores,
                decision_date=decision_date,
                quality_decision=dict(quality_decision),
                eligibility_policy=eligibility_policy,
                eligibility_status=eligibility_status,
                runner_identity=runner_identity,
                created_at=created_at,
            )
            for decision_date, scores in sorted(scores_by_decision_date.items())
        ]
