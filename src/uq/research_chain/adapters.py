from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

import re

import pandas as pd

if TYPE_CHECKING:
    # `qlib_runtime` imports qlib at module load, and qlib is an optional
    # dependency, so the trainer type must never be imported at runtime -- only
    # named here for the annotation.
    from ..models.qlib_runtime import QlibRuntimeTrainer

from ..backtest.engine import BacktestEngine, BacktestResultStore
from ..contracts.canonical_v2 import file_sha256_bytes
from ..contracts.gate_contracts import adjustment_snapshot_generation
from ..contracts.model_layer import ModelContractLoader, bind_reviewed_quality_decision, canonical_json, model_manifest_identities, research_contract_identities, resolve_subject_quality_decision, sha256_bytes
from ..contracts.artifacts import UniverseSnapshotStore
from ..errors import ContractError
from ..models.dataset import DatasetBuilder
from ..models.qlib_export import QlibDatasetExporter, QlibInitReceiptBuilder
from ..models.predictions import PredictionBuilder
from ..models.trainer import ModelRunBuilder
from ..models.dataset_writer import DatasetWriter
from ..models.feature_preprocessing import FeaturePreprocessorBuilder, complete_rows
from ..models.features import FeatureSchemaBuilder, FeatureSchemaValidator
from ..portfolio.builder import PortfolioBuilder, PortfolioDefinitionBinding, TargetWeightStore
from .contracts import PublishedState
from .membership_input import filtered_factor_frame, verify_request_input_sets
from .owning_contracts import (
    AdjustedPriceDatasetStore,
    BacktestConfigStore,
    BacktestMarketDatasetStore,
    FeaturePreprocessingStore,
    FeatureSchemaStore,
    LabelStore,
)
from .resolver import FileResearchRunStore, ResolvedExecutionPlan, stage_plan_for_request


_STAGE_BINDING_KEYS = {
    "generation_id", "manifest_digest_sha256", "data_checksum_sha256"
}


def _prediction_binding_for(bindings: Sequence[Any], generation_id: str) -> Any:
    """The binding for the prediction generation a hand-off carries.

    cr.md 8.62 C: the runner's hand-off is the *last* prediction it built
    (`results[-1]`), and the runner's synthesized definition names that same
    generation. Selecting "the earliest date" instead made the adapter refuse a
    perfectly consistent run, because the hand-off and the definition disagree
    with which date they mean. Keying on the generation that is actually being
    carried removes the ambiguity without changing the selection rule.
    """
    matches = [binding for binding in bindings if binding.generation_id == generation_id]
    if not matches:
        raise ContractError("no prediction_set binding for the prediction the run carries")
    return matches[0]


def _latest_prediction_binding(bindings: Sequence[Any]) -> Any:
    """The prediction binding for the latest decision date.

    cr.md 8.62 C: `prediction_set` publishes one binding per decision date. The
    runner's cross-stage hand-off is the *last* prediction it built, so the
    restored one has to be the same date, or the portfolio stage binds a
    generation the run never carried. Bindings whose physical path carries no
    date sort last: those are resolver-built stand-ins with no date to order by.
    """
    def sort_key(binding: Any) -> tuple[bool, str]:
        date_part = Path(binding.physical_path).name.removeprefix("date=") if binding.physical_path else None
        # A path-less binding has no date to order by, so it sorts *first*: this
        # helper selects `[-1]`, and a stand-in must never be what it returns.
        return (date_part is not None, date_part or "")

    return sorted(bindings, key=sort_key)[-1]


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


def _decision_payload(decision: Any) -> Any:
    """Pass provider callables through; copy concrete decisions (cr.md U14).

    Stages whose subject is derived inside the build flow (per-date manifests,
    content digests) must let the flow resolve the provider per subject —
    materializing a callable with dict() would crash provider mode here.
    """
    return decision if callable(decision) else dict(decision)


def build_stage_state(
    plan: ResolvedExecutionPlan,
    *,
    stage: str,
    output_bindings: Sequence[Mapping[str, Any]],
    runner_identity: Mapping[str, str],
    created_at: str | None = None,
) -> dict[str, Any]:
    stage_plan = stage_plan_for_request(plan.request)
    if stage not in stage_plan or stage == "resolve_request":
        raise ContractError("invalid research runtime stage")
    if any(
        set(binding) != _STAGE_BINDING_KEYS | {
            "output_family", "physical_path", "quality_decision_checksum_sha256", "failure_reason"
        }
        for binding in output_bindings
    ):
        raise ContractError("invalid stage output binding fields")
    current_index = stage_plan.index(stage)
    stage_records: list[dict[str, Any]] = [
        {
            "stage": current_stage,
            "status": "passed",
            "output_bindings": [],
            "failure_reason": None,
        }
        for current_stage in stage_plan[:current_index]
    ]
    stage_records.append({
        "stage": stage,
        "status": "passed",
        "output_bindings": [dict(binding) for binding in output_bindings],
        "failure_reason": None,
    })
    state: dict[str, Any] = {
        "contract_version": plan.request.get("contract_version", 1),
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
    # Audit trail for `missing_policy: drop_incomplete_rows` (feature nulls) and
    # `missing_policy: drop_unlabeled_rows` (label nulls). Empty for the other
    # policies. Both are carried on the result rather than inside the manifest or the
    # run state because those are closed contracts (`model_dataset.v1` and
    # `research_run_state.v1` are `additionalProperties: false`), and widening either
    # for a diagnostic would change what those artifacts mean.
    dropped_rows: dict[str, Any] = field(default_factory=dict)
    dropped_labels: dict[str, Any] = field(default_factory=dict)


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


def unlabeled_keys(label_frame: pd.DataFrame) -> pd.DataFrame:
    """The `(instrument, decision_date)` pairs whose label is null.

    Extracted so the publisher and the stage cannot disagree about which rows are
    unlabelled -- the publisher must review the preprocessing artifact over the
    frame that survives this filter, and the stage must drop exactly those rows.
    Two copies of the rule would drift the way `label_row_checksum` once did
    (cr.md OBS-F).
    """
    keys = label_frame[["instrument", "decision_date"]].copy()
    return keys[label_frame["label"].isna()]


def drop_unlabelled_factor_rows(
    factor_frame: pd.DataFrame, label_frame: pd.DataFrame
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Remove the factor rows this stage cannot train on, before the transform.

    A null label is only knowable after the label merge, which is downstream of
    preprocessing, so this cannot filter the merged frame: `DatasetWriter` requires
    the published frame and the preprocessing output to carry the same rows. The
    filter is applied to the preprocessing *input* instead and the transform re-run.

    Two shapes are removed, and both must be, because only one of them is visible to
    a value test. A label that is *present but null* leaves a row in ``label_frame``
    that ``unlabeled_keys`` can name. A label whose key is *absent* leaves no row at
    all, so no value test can see it -- yet a later left join turns it into a NaN that
    reaches the published dataset. That asymmetry is exactly why the two sibling
    policies (which assert on the merged frame) caught both shapes while this one
    caught only the first: the check was frame-local. The key-set comparison below is
    total instead. pandas documents ``indicator`` specifically for "the merge key only
    appears in one frame"; no ``validate=`` value detects it, because those guard key
    *uniqueness* (``merge.py``'s ``_ValidateOperation``).
    """
    dropped_keys = unlabeled_keys(label_frame)
    present_keys = label_frame[["instrument", "decision_date"]].drop_duplicates()
    null_label = factor_frame.merge(
        dropped_keys.drop_duplicates(),
        how="left", left_on=["instrument", "datetime"],
        right_on=["instrument", "decision_date"], indicator="null_label",
    )
    key_present = factor_frame.merge(
        present_keys,
        how="left", left_on=["instrument", "datetime"],
        right_on=["instrument", "decision_date"], indicator="key_present",
    )
    keep_mask = (
        (null_label["null_label"] == "left_only").to_numpy()
        & (key_present["key_present"] == "both").to_numpy()
    )
    if not keep_mask.any():
        raise ContractError("drop_unlabeled_rows left no labeled rows")
    kept = factor_frame.loc[keep_mask].reset_index(drop=True)
    audit = {
        "rows": int((~keep_mask).sum()),
        "instruments": int(factor_frame.loc[~keep_mask, "instrument"].nunique()),
        "null_labels": int((null_label["null_label"] == "both").sum()),
        "absent_keys": int((key_present["key_present"] == "left_only").sum()),
        "note": (
            "label is the target: these rows cannot be trained on but may still be "
            "scored at inference; see missing_policy drop_unlabeled_rows"
        ),
    }
    return kept, audit


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
        membership_store: Any | None = None,
        trading_calendar: Sequence[date] | None = None,
    ) -> None:
        self.factor_store = factor_store
        self.adjusted_price_store = adjusted_price_store
        self.label_store = label_store
        self.universe_store = universe_store
        self.feature_schema_store = feature_schema_store
        self.preprocessing_store = preprocessing_store
        self.dataset_writer = dataset_writer
        self.run_store = run_store
        self.membership_store = membership_store
        self.trading_calendar = list(trading_calendar) if trading_calendar is not None else None

    def _read_factor_frame(self, plan: ResolvedExecutionPlan, factor_binding) -> pd.DataFrame:
        """Select the factor frame the dataset stage is allowed to consume.

        v4/v5 requests declare an `index_membership_binding`, so their dataset must be
        built from the per-day visible-member intersection; a missing governed
        membership store is a hard failure rather than a silent fallback to the raw
        partition, which would admit members that were not in the index on that day.
        """
        if plan.request.get("contract_version") in {4, 5}:
            if self.membership_store is None or self.trading_calendar is None:
                raise ContractError(
                    "v4/v5 dataset stage requires a governed membership store and trading calendar"
                )
            return filtered_factor_frame(
                plan.request,
                membership_store=self.membership_store,
                factor_store=self.factor_store,
                trading_days=self.trading_calendar,
            )
        _, factor_frame = self.factor_store.read_partition(factor_binding.generation_id)
        return factor_frame

    def _factor_partition_set(self, plan: ResolvedExecutionPlan) -> dict[str, Any]:
        """Rebuild the request's factor partition set from its owning store.

        The request carries only the set's digest, so the partitions have to be
        re-derived. v4/v5 already do this inside `filtered_factor_frame`; the
        preprocessing binding check needs the same list, and re-deriving it is what
        keeps that check anchored to the store rather than to the request's claim.
        """
        if plan.request.get("contract_version") not in {4, 5}:
            raise ContractError(
                "factor partition set resolution requires a v4/v5 request; earlier "
                "contracts bind a single factor partition and carry no set"
            )
        if self.membership_store is None or self.trading_calendar is None:
            raise ContractError(
                "v4/v5 dataset stage requires a governed membership store and trading calendar"
            )
        return verify_request_input_sets(
            plan.request,
            membership_store=self.membership_store,
            factor_store=self.factor_store,
            trading_days=self.trading_calendar,
        ).factor_partition_set

    def run(
        self,
        plan: ResolvedExecutionPlan,
        *,
        runner_identity: Mapping[str, str],
        quality_decision: Mapping[str, Any],
        preprocessing_quality_decision: Mapping[str, Any],
        label_quality_decision: Mapping[str, Any],
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
        factor_frame = self._read_factor_frame(plan, factor_binding)

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
        # cr.md H5: the label-set review decision must be retrieved and verified
        # at execute time (aligned with the factor stage), not trusted from
        # resolve-time envelope checks alone.
        bind_reviewed_quality_decision(
            dict(label_quality_decision),
            binding_type="label_set_v1",
            subject_generation_id=label_manifest["generation_id"],
            subject_content_sha256=label_manifest["generation_id"],
        )
        # cr.md U22: no status re-check here — the review registry enforces
        # allowed_statuses inside bind; a second hardcoded copy could only
        # diverge from the registry's policy.
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
        # The preprocessing artifact attests the exact factor partitions its
        # cross-sectional statistics were computed over. Comparing it to the single
        # `factor_binding` was wrong for any window longer than one session: a
        # per-date transform needs the whole window's partitions, and the contract
        # has always declared this field as an array (`minItems: 1`). Compare against
        # the resolved partition set instead, which is stricter than the old
        # equality -- a set built for a different window or ordering is refused.
        if plan.request.get("contract_version") in {4, 5}:
            expected_factor_generations = [
                entry["generation_id"]
                for entry in self._factor_partition_set(plan)["partitions"]
            ]
        else:
            # v1-v3 bind exactly one factor partition and carry no set, so the
            # historical single-generation equality is the correct check there.
            expected_factor_generations = [factor_binding.generation_id]
        if preprocessing["input_factor_generation_ids"] != expected_factor_generations:
            raise ContractError(
                "preprocessing input factor generation mismatch: the artifact was "
                f"built over {len(preprocessing['input_factor_generation_ids'])} "
                f"partition(s) but this request resolves {len(expected_factor_generations)}"
            )
        input_schema = self.feature_schema_store.read_schema(preprocessing["input_feature_schema_generation_id"])
        output_schema = self.feature_schema_store.read_schema(preprocessing["output_feature_schema_generation_id"])

        # `drop_incomplete_rows` has to act *here*, before preprocessing, not after
        # the label merge. `DatasetWriter` requires the published frame and the
        # preprocessing output to have the same rows, and re-derives
        # `output_frame_sha256` from the dataset frame on both publish and read
        # (`dataset_writer.py:381-393`, `:296-300`). Dropping rows after
        # preprocessing would break both, and would also standardize over rows that
        # never enter training.
        #
        # Both policies drop feature-null rows here. A null *label* is handled
        # separately below, because it is a different defect with a different remedy.
        # Only *feature* incompleteness means "this row cannot be computed".
        dropped_rows: dict[str, Any] = {}
        dropped_labels: dict[str, Any] = {}
        if template["missing_policy"] in ("drop_incomplete_rows", "drop_unlabeled_rows"):
            feature_columns = list(preprocessing["ordered_features"])
            incomplete = factor_frame[feature_columns].isna().any(axis=1)
            if incomplete.any():
                kept = complete_rows(factor_frame, feature_columns)
                dropped_rows = {
                    "rows": int(len(factor_frame) - len(kept)),
                    "instruments_fully_dropped": int(
                        len(set(factor_frame["instrument"]) - set(kept["instrument"]))
                    ),
                    "per_feature": {
                        name: int(count)
                        for name, count in factor_frame[feature_columns].isna().sum().items()
                        if count
                    },
                }
                factor_frame = kept
                if factor_frame.empty:
                    raise ContractError(
                        "drop_incomplete_rows left no usable factor rows: "
                        f"{dropped_rows}"
                    )
        if template["missing_policy"] == "drop_unlabeled_rows":
            # A null label is a different defect from a null feature, so it gets its
            # own policy rather than being folded into `drop_incomplete_rows`.
            # Qlib makes the same split: `DropnaLabel` is a separate processor from
            # `DropnaProcessor`, and `is_for_infer()` is False for labels and True
            # for features -- a row can be unscoreable *and* untrainable at once and
            # still be perfectly scoreable at inference. sklearn draws the same line
            # inside one call: `ensure_all_finite` governs X and its docstring says
            # it "does not influence whether y can have np.inf, np.nan, pd.NA values".
            #
            # This is deliberately a *learning-time* row exclusion. Dropping the
            # whole instrument instead would reintroduce the hindsight bias that
            # `drop_incomplete_rows` exists to avoid.
            #
            # A null label must also drop the row, but unlike a null feature it is
            # only knowable *after* the label merge -- which is downstream of
            # preprocessing. `DatasetWriter` requires the published frame and the
            # preprocessing output to have the same rows, so this cannot simply
            # drop from the merged frame: the row has to be removed from the
            # preprocessing *input* too, or the writer refuses with "dataset and
            # preprocessing frame row counts disagree".
            factor_frame, dropped_labels = drop_unlabelled_factor_rows(
                factor_frame, label_frame
            )
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
        if template["missing_policy"] == "drop_incomplete_rows":
            # Features were filtered above; a surviving null can only be a label.
            # That is a target-definition gap, not feature noise, so it stays
            # fail-closed rather than being silently dropped.
            label_nulls = int(dataset_frame["label"].isna().sum())
            if label_nulls:
                missing_instruments = sorted(
                    dataset_frame.loc[dataset_frame["label"].isna(), "instrument"]
                    .astype(str).unique()
                )
                raise ContractError(
                    "drop_incomplete_rows cannot drop null labels: the label is the "
                    f"target, so {label_nulls} rows across {len(missing_instruments)} "
                    f"instruments need a universe-level exclusion instead "
                    f"(first: {missing_instruments[:5]})"
                )

        manifest = DatasetBuilder(
            dataset_name=template["dataset_name"],
            semantic_version=template["semantic_version"],
            code_fingerprint=template["code_fingerprint"],
        ).build(
            ordered_features=list(preprocessing["ordered_features"]),
            factor_set=factor_manifest["factor_set"],
            factor_version=factor_manifest["factor_version"],
            # The dataset must declare the same factor partitions its preprocessing
            # was computed over. A per-date transform spans the whole window, so this
            # is the resolved set rather than the single `factor_binding`.
            factor_generation_ids=expected_factor_generations,
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
            # cr.md U14: the dataset generation is derived inside the writer,
            # so a per-subject provider must pass through untouched; static
            # decisions keep the historical defensive copy.
            quality_report=quality_decision if callable(quality_decision) else dict(quality_decision),
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
            dropped_rows=dropped_rows,
            dropped_labels=dropped_labels,
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
    """Hand-off between the model stage and its consumers.

    ``definition``, ``artifact_bytes`` and ``published_state`` describe work done in
    this process. A resumed run rebuilds the hand-off from the published artifacts,
    where those three do not exist, so they are optional: everything downstream
    actually reads is the two manifests.
    """
    run_manifest: dict[str, Any]
    artifact_manifest: dict[str, Any]
    definition: dict[str, Any] | None = None
    artifact_bytes: bytes = b""
    published_state: PublishedState | None = None


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
        # A resumed run skips this stage, so nothing assigns this attribute
        # in-process. Declaring it here turns a silent AttributeError into an
        # explicit "unavailable" that the runner can recover from by reading the
        # published artifact back.
        self.last_result: Any = None

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
        # cr.md 8.62 C: this family has one binding per decision date, so
        # `bindings[0]` was an arbitrary one. Bind the date the prediction
        # hand-off actually carries, and check the rest through the per-date map
        # below.
        prediction_binding = _prediction_binding_for(
            prediction_bindings, prediction_stage_result.manifest["generation_id"]
        )
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
            # cr.md N1: the request window must be proven inside the snapshot's PIT
            # validity here too, exactly as the dataset stage does; otherwise this
            # read reuses a snapshot the request never covered.
            requested_valid_from=date.fromisoformat(plan.request["window_start_date"]),
            requested_valid_to=date.fromisoformat(plan.request["window_end_date"]),
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
                # Nothing precedes the first decision date of a run, and that is the
                # only state allowed to exempt itself from the turnover cap.
                initial_state=previous_frame is None,
                quality_decision=None,
            )
            self.target_weight_store.publish(
                manifest,
                frame,
                quality_decision=_decision_payload(weights_quality_decision),
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
        # A resumed run skips this stage, so nothing assigns this attribute
        # in-process. Declaring it here turns a silent AttributeError into an
        # explicit "unavailable" that the runner can recover from by reading the
        # published artifact back.
        self.last_result: Any = None

    def run(
        self,
        plan: ResolvedExecutionPlan,
        *,
        portfolio_stage_result: PortfolioStageResult,
        quality_decision: Mapping[str, Any],
        config_quality_decision: Mapping[str, Any],
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
        # cr.md H5: the backtest-config review decision must be retrieved and
        # verified at execute time (aligned with the factor stage), not trusted
        # from resolve-time envelope checks alone.
        # cr.md U9: compare the bound report checksum with the config manifest's
        # recorded binding — the checksum used to be discarded here. The
        # backtest_config identity covers that recorded checksum, so the review
        # attests the provisional generation (checksum zeroed): re-derive it
        # before binding, aligned with TargetWeightStore.read.
        provisional_config_generation, _ = model_manifest_identities(
            {**config, "quality_report_checksum_sha256": "0" * 64,
             "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
            schema_name="backtest_config",
        )
        resolved_config_decision = (
            resolve_subject_quality_decision(
                config_quality_decision,
                subject_generation_id=provisional_config_generation,
                subject_content_sha256=provisional_config_generation,
            )
            if callable(config_quality_decision)
            else dict(config_quality_decision)
        )
        _, expected_config_checksum = bind_reviewed_quality_decision(
            resolved_config_decision,
            binding_type="backtest_config_v1",
            subject_generation_id=provisional_config_generation,
        )
        if expected_config_checksum != config["quality_report_checksum_sha256"]:
            raise ContractError("backtest config quality decision checksum mismatch")
        # cr.md U22: status policy is enforced by the review registry inside
        # bind (allowed_statuses); duplicating it here risks divergence.
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
        # cr.md 8.62 F: `bars_daily/v2` renames the session key `datetime` to
        # `session_date` (config/schemas/bars_daily.v2.yaml migration), and the
        # published adjusted-price partitions key on whichever the source schema
        # declares -- the factor side stays `datetime`-keyed by
        # `_validate_factor_frame`. Requiring only `datetime` made a v2 backtest
        # refuse its own published price panel, so the panel's own key is used.
        session_column = "session_date" if "session_date" in price_frame.columns else "datetime"
        required_price_columns = {
            session_column, "instrument", "open", "high", "low", "close", "volume", "amount",
        }
        if not required_price_columns.issubset(price_frame.columns):
            raise ContractError("backtest price dataset is missing required columns")
        if file_sha256_bytes(
            self.adjusted_price_store.data_path(price_binding["dataset_generation_id"]).read_bytes()
        ) != price_binding["data_checksum_sha256"]:
            raise ContractError("backtest price data checksum mismatch")
        price_panel = price_frame.copy()
        price_panel["date"] = pd.to_datetime(price_panel[session_column]).dt.strftime("%Y-%m-%d")
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
        # cr.md U14: the result generation is derived inside the result store
        # at publish time, so a per-subject provider must pass through
        # untouched; static decisions keep the historical defensive copy.
        manifest, artifacts = self.backtest_engine.run(
            config=config,
            portfolio_definition=portfolio_stage_result.definition_manifest,
            weight_partitions=weight_partitions,
            price_panel=price_panel,
            suspension_dates=suspension_dates,
            corporate_action_instruments=corporate_action_instruments,
            quality_decision=_decision_payload(quality_decision),
        )
        self.backtest_result_store.publish(
            manifest,
            artifacts,
            quality_decision=_decision_payload(quality_decision),
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

    @staticmethod
    def _cache_files(root: str | Path) -> set[str]:
        base = Path(root)
        if not base.exists():
            return set()
        base_parts = base.resolve().parts
        return {
            path.resolve().as_posix()
            for path in base.rglob("*")
            if path.is_file()
            and not any(part.startswith(".") for part in path.resolve().parts[len(base_parts):])
        }

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
        cache_files_after: set[str] | None,
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
            quality_decision=_decision_payload(export_quality_decision),
        )
        verified_export = self.exporter.read(dataset_manifest["dataset_name"], export_manifest["generation_id"])
        if verified_export[0]["generation_id"] != export_manifest["generation_id"]:
            raise ContractError("Qlib export readback identity mismatch")
        cache_before = cache_files_before
        cache_after = self._cache_files(cache_root) if cache_files_after is None else cache_files_after
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
            quality_decision=_decision_payload(receipt_quality_decision),
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
        factor_store: Any,
    ) -> None:
        self.trainer = trainer
        self.exporter = exporter
        self.artifact_store = artifact_store
        self.dataset_writer = dataset_writer
        self.universe_store = universe_store
        self.run_store = run_store
        self.factor_store = factor_store
        # A resumed run skips this stage, so nothing assigns this attribute
        # in-process. Declaring it here turns a silent AttributeError into an
        # explicit "unavailable" that the runner can recover from by reading the
        # published artifact back.
        self.last_result: Any = None

    def _factor_lineage(
        self,
        dataset_manifest: Mapping[str, Any],
        *,
        factor_binding,
        factor_generation_id: str,
    ) -> dict[str, Any]:
        """Read every factor partition the dataset's preprocessing spanned.

        The bound partition is read through the run store, because the run
        published it and the request carries its binding; every other partition
        comes from the factor store that owns it. Reading only the bound
        partition was correct while a dataset carried one, but a per-date
        transform spans the whole window, so the model manifest's lineage has
        to cover all of them.
        """
        manifests: dict[str, Any] = {}
        for generation_id in dataset_manifest["factor_generation_ids"]:
            if generation_id == factor_generation_id:
                manifests[generation_id] = self.run_store.read_published_document(
                    "factor_partition", generation_id,
                    manifest_digest_sha256=factor_binding.manifest_digest_sha256,
                    data_checksum_sha256=factor_binding.data_checksum_sha256,
                )
            else:
                manifests[generation_id] = self.factor_store.read_manifest(generation_id)
        return manifests

    @staticmethod
    def _bound_artifact_report(
        decision: Mapping[str, Any],
        artifact_generation: str,
    ) -> dict[str, Any]:
        """Bind a reviewer's artifact decision to the derived artifact generation.

        cr.md 8.62 B: the caller's provider returns the reviewer's *signed
        decision* (`create_reviewed_quality_decision`), whose field set is the
        13 signed fields and carries `subject_generation_id`.
        `ArtifactStore.publish` validates against `model_quality_report.v2`
        and requires `bound_generation_id` plus `report_checksum_sha256`.
        Handing it the raw decision fails schema validation before any
        cryptographic check runs, so every provider-form caller reached this
        exception. A pre-bound decision (the field set of the tests' fixtures)
        also reaches `ArtifactStore.publish` and must stay acceptable, because
        a bound report already carries `bound_generation_id` and a verified
        `report_checksum_sha256`; re-binding it is not possible either --
        `bind_reviewed_quality_decision` accepts either the resolver envelope
        or the raw signed decision, and a bound report is neither.

        So the two accepted shapes are distinguished by `bound_generation_id`
        rather than by field count, and the raw one goes through the owning
        bind that `predictions.py:137`, `trainer.py:365` and
        `qlib_export.py:184` use before publishing.
        """
        if "bound_generation_id" in decision:
            if decision["bound_generation_id"] != artifact_generation:
                raise ContractError("artifact decision is bound to a different artifact generation")
            return dict(decision)
        report, _ = bind_reviewed_quality_decision(
            decision,
            binding_type="model_artifact_v1",
            subject_generation_id=artifact_generation,
            subject_content_sha256=artifact_generation,
        )
        return report


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
        # The dataset declares every factor partition its preprocessing covered, so
        # the model stage has to read all of them. Reading only the single bound
        # partition was correct when the dataset carried one; a per-date transform
        # spans the whole window and the run manifest now lists each partition.
        factor_manifests = self._factor_lineage(
            dataset_manifest,
            factor_binding=factor_binding,
            factor_generation_id=factor_generation_id,
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
            factor_manifests=factor_manifests,
            quality_decision=_decision_payload(model_quality_decision),
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
            quality_report=self._bound_artifact_report(
                artifact_quality_decision_provider(artifact_generation),
                artifact_generation,
            ),
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
        # A resumed run skips this stage, so nothing assigns this attribute
        # in-process. Declaring it here turns a silent AttributeError into an
        # explicit "unavailable" that the runner can recover from by reading the
        # published artifact back.
        self.last_result: Any = None

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
        results = self.run_for_dates(
            plan,
            dataset_generation_id=dataset_generation_id,
            model_stage_result=model_stage_result,
            scores_by_decision_date={decision_date: scores},
            quality_decision=quality_decision,
            eligibility_policy=eligibility_policy,
            eligibility_status=eligibility_status,
            runner_identity=runner_identity,
            created_at=created_at,
        )
        if len(results) != 1:
            raise ContractError("single-date prediction run produced more than one result")
        return results[0]

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
        """Build one prediction per decision date and publish the stage once.

        A stage state is a stage-level publication record whose immutable path is
        keyed by stage number, so publishing inside the per-date loop made every run
        with two or more decision dates collide on ``immutable research manifest
        already exists`` (external review B6).  The publishing stage now matches the
        portfolio stage: one publication carrying one binding per decision date,
        which is also where the runner reads its decision dates back from.
        """
        dataset_binding = _stage_binding(plan, stage="dataset_preparation", output_family="model_dataset")
        if dataset_generation_id != dataset_binding.generation_id:
            raise ContractError("prediction dataset binding mismatch")
        if not scores_by_decision_date:
            raise ContractError("prediction stage requires governed scores")

        built: list[tuple[dict[str, Any], pd.DataFrame]] = []
        output_bindings: list[dict[str, Any]] = []
        for decision_date, scores in sorted(scores_by_decision_date.items()):
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
                quality_decision=_decision_payload(quality_decision),
            )
            self.prediction_builder.publish(manifest, artifact)
            readback_manifest, frame = self.prediction_builder.read(
                manifest["generation_id"], decision_date,
            )
            if readback_manifest["generation_id"] != manifest["generation_id"]:
                raise ContractError("prediction readback identity mismatch")
            built.append((readback_manifest, frame))
            output_bindings.append({
                "output_family": "prediction_set",
                "generation_id": readback_manifest["generation_id"],
                "manifest_digest_sha256": readback_manifest["manifest_digest_sha256"],
                "data_checksum_sha256": readback_manifest["data_checksum_sha256"],
                "physical_path": f"predictions/prediction_set={readback_manifest['generation_id']}/date={decision_date}",
                "quality_decision_checksum_sha256": readback_manifest["quality_report_checksum_sha256"],
                "failure_reason": None,
            })

        state = build_stage_state(
            plan,
            stage="prediction_publication",
            output_bindings=output_bindings,
            runner_identity=runner_identity,
            created_at=created_at,
        )
        published = self.run_store.publish_state(state, stage="prediction_publication")
        return [
            PredictionStageResult(manifest=manifest, frame=frame, published_state=published)
            for manifest, frame in built
        ]
