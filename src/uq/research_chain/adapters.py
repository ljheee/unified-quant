from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from ..contracts.gate_contracts import adjustment_snapshot_generation
from ..contracts.model_layer import ModelContractLoader, bind_reviewed_quality_decision, research_contract_identities
from ..contracts.artifacts import UniverseSnapshotStore
from ..errors import ContractError
from ..models.dataset import DatasetBuilder
from ..models.dataset_writer import DatasetWriter
from ..models.feature_preprocessing import FeaturePreprocessorBuilder
from ..models.features import FeatureSchemaBuilder, FeatureSchemaValidator
from .contracts import PublishedState
from .owning_contracts import AdjustedPriceDatasetStore, FeaturePreprocessingStore, FeatureSchemaStore, LabelStore
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
    if builder.frame_fingerprint(output_frame) != preprocessing["output_frame_sha256"]:
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
        quality_decision_checksum_sha256: str,
        created_at: str | None = None,
    ) -> FactorStageResult:
        if not isinstance(quality_decision_checksum_sha256, str) or len(quality_decision_checksum_sha256) != 64:
            raise ContractError("invalid factor quality decision checksum")
        binding = _stage_binding(
            plan, stage="factor_computation", output_family="factor_partition"
        )
        manifest = self.store.read_manifest(binding.generation_id)
        if manifest["manifest_digest_sha256"] != binding.manifest_digest_sha256:
            raise ContractError("factor binding manifest digest mismatch")
        if manifest["data_checksum_sha256"] != binding.data_checksum_sha256:
            raise ContractError("factor binding data checksum mismatch")
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
