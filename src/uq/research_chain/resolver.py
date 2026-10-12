from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from ..contracts.model_layer import (
    ModelContractLoader,
    RESEARCH_STAGE_ORDER_V1,
    RESEARCH_STAGE_ORDER_V3,
    research_contract_identities,
    research_stage_plan_sha256,
    research_stage_plan_v2_sha256,
    research_stage_plan_v3_sha256,
    research_stage_plan_v4_sha256,
    sha256_json,
)
from ..errors import ContractError
from .membership_input import verify_request_input_sets
from .contracts import (
    PublishedRequest,
    PublishedResult,
    PublishedState,
    StateSummary,
    research_request_schema_name,
    validate_research_layout,
    verify_stage_plan_review,
)
from .owning_contracts import (
    AdjustedPriceDatasetStore,
    BacktestConfigStore,
    FeatureSchemaStore,
    LabelStore,
)


_FAILURE_REASONS = frozenset({
    "request_invalid", "input_unresolved", "input_tampered", "lineage_mismatch",
    "quality_decision_missing", "quality_decision_rejected", "stage_failed",
    "store_read_failed", "overwrite_conflict", "reproducibility_failed",
    "result_reconciliation_failed",
})
# Stage orders live in uq.contracts.model_layer (single source, shared with
# schema validation and the physical layout validator); aliased here so the
# runner-facing names keep working.
_STAGE_PLAN = RESEARCH_STAGE_ORDER_V1
_STAGE_PLAN_V3 = RESEARCH_STAGE_ORDER_V3
_EXPECTED_STAGE_PLAN_SHA256 = research_stage_plan_sha256()


def stage_plan_for_request(request: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the reviewed stage order for the request contract version.

    V3, V4 and V5 all insert ``paper_execution`` before ``result_reconciliation``
    (their normative plans are identical), so both must map to the V3 order:
    the previous ``== 3`` test silently gave V4 the released V1 order, which
    would have numbered the paper stage as ``result_reconciliation`` the moment
    the V4 runtime is enabled. Unknown versions fail closed rather than being
    guessed as V1.
    """
    # V3/V4 runtime execution is refused in runner._validate_plan; the mapping
    # still has to be correct for state numbering, listing and the result path.
    version = request.get("contract_version", 1)
    if version in {1, 2}:
        return tuple(_STAGE_PLAN)
    if version in {3, 4, 5}:
        return tuple(_STAGE_PLAN_V3)
    raise ContractError(f"unsupported research request contract version: {version!r}")
_DEFAULT_STAGE_PLAN = tuple(_STAGE_PLAN)


class ResearchResolutionError(Exception):
    """A typed, fail-closed research request resolution failure."""

    def __init__(self, reason: str, detail: str) -> None:
        if reason not in _FAILURE_REASONS:
            raise ValueError(f"unknown research failure reason: {reason}")
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


class OwningManifestStore(Protocol):
    def read_manifest(self, generation_id: str) -> dict[str, Any]:
        """Return the owning layer's verified manifest for a generation."""


class QualityDecisionProvider(Protocol):
    def resolve(
        self,
        *,
        binding_type: str,
        subject_generation_id: str,
        subject_manifest_digest_sha256: str | None,
        output_family: str,
        provider_config_ref: str,
    ) -> Mapping[str, Any]:
        """Return the owning layer's immutable quality decision."""


@dataclass(frozen=True)
class ResolvedStageBinding:
    stage: str
    output_family: str
    generation_id: str
    manifest_digest_sha256: str
    data_checksum_sha256: str
    physical_path: str | None = None
    quality_decision_checksum_sha256: str | None = None


@dataclass(frozen=True)
class ResolvedExecutionPlan:
    request: dict[str, Any]
    request_manifest_digest_sha256: str
    stage_plan_sha256: str
    stage_bindings: tuple[ResolvedStageBinding, ...]
    resolved_execution_plan_sha256: str


class ResearchChainRequestResolver:
    """Resolve immutable request references through owning-layer stores."""

    def __init__(
        self,
        stores_by_output_family: Mapping[str, Any],
        *,
        membership_store: Any | None = None,
        trading_calendar: Sequence[date] | None = None,
        market_dataset_store: Any | None = None,
    ) -> None:
        if (membership_store is None) != (trading_calendar is None):
            raise ContractError(
                "index membership governance requires both a membership store and a trading calendar"
            )
        self.stores_by_output_family = dict(stores_by_output_family)
        self.membership_store = membership_store
        self.trading_calendar = list(trading_calendar) if trading_calendar is not None else None
        self.market_dataset_store = market_dataset_store

    def resolve(
        self,
        request: Mapping[str, Any],
        *,
        quality_provider: QualityDecisionProvider | None = None,
        provider_config_ref: str | None = None,
    ) -> ResolvedExecutionPlan:
        try:
            candidate = dict(request)
            request_schema = research_request_schema_name(candidate)
            ModelContractLoader.validate(request_schema, candidate)
            if request_schema in {"research_run_request_v2", "research_run_request_v3", "research_run_request_v4", "research_run_request_v5"}:
                verify_stage_plan_review(
                    candidate["stage_plan_review"],
                    stage_plan_sha256=candidate["stage_plan_sha256"],
                )
            expected_stage_plan_sha256 = (
                _EXPECTED_STAGE_PLAN_SHA256
                if request_schema == "research_run_request"
                else research_stage_plan_v2_sha256()
                if request_schema == "research_run_request_v2"
                else research_stage_plan_v3_sha256()
                if request_schema == "research_run_request_v3"
                else research_stage_plan_v4_sha256()
                if request_schema == "research_run_request_v4"
                else research_stage_plan_v5_sha256()
            )
            if candidate["stage_plan_sha256"] != expected_stage_plan_sha256:
                raise ResearchResolutionError(
                    "request_invalid",
                    "request stage plan digest does not match the normative v1 plan",
                )
            expected_generation, expected_digest = research_contract_identities(
                candidate, schema_name=request_schema
            )
            if candidate["request_content_generation_id"] != expected_generation:
                raise ResearchResolutionError("request_invalid", "request stable identity mismatch")
            if candidate["manifest_digest_sha256"] != expected_digest:
                raise ResearchResolutionError("request_invalid", "request manifest digest mismatch")
        except ResearchResolutionError:
            raise
        except (ContractError, KeyError, TypeError) as exc:
            raise ResearchResolutionError(
                "request_invalid", f"invalid research request: {exc}"
            ) from exc

        binding_specs = (
            ("factor_binding", "factor_partition", "factor_computation", "factor_v1"),
            ("universe_snapshot_binding", "universe_snapshot", "factor_computation", None),
            ("adjusted_price_binding", "adjusted_price_dataset", "dataset_preparation", None),
            ("label_binding", "label_set", "dataset_preparation", "label_set_v1"),
            (
                "feature_preprocessing_binding", "feature_preprocessing",
                "dataset_preparation", "feature_preprocessing_v1",
            ),
            (
                "backtest_config_binding", "backtest_config",
                "backtest_execution", "backtest_config_v1",
            ),
        )
        stage_bindings: list[ResolvedStageBinding] = []
        for request_field, output_family, stage, quality_binding_type in binding_specs:
            store = self.stores_by_output_family.get(output_family)
            if store is None:
                raise ResearchResolutionError(
                    "request_invalid",
                    f"no owning store is registered for output family {output_family}",
                )
            binding = candidate[request_field]
            if binding.get("family") not in {None, output_family}:
                raise ResearchResolutionError(
                    "request_invalid",
                    f"binding family {binding.get('family')} is not supported by output family {output_family}",
                )
            generation_id = binding["generation_id"]
            try:
                manifest = store.read_manifest(generation_id)
            except ContractError as exc:
                detail = str(exc)
                if "tampered" in detail or "identity mismatch" in detail:
                    raise ResearchResolutionError("input_tampered", detail) from exc
                if "malformed" in detail or "ambiguous" in detail:
                    raise ResearchResolutionError("store_read_failed", detail) from exc
                raise ResearchResolutionError("input_unresolved", detail) from exc
            except (OSError, ValueError, TypeError) as exc:
                raise ResearchResolutionError(
                    "store_read_failed", f"owning store read failed: {exc}"
                ) from exc

            requested_digest = binding.get("manifest_digest_sha256")
            if requested_digest is not None and manifest.get("manifest_digest_sha256") != requested_digest:
                raise ResearchResolutionError(
                    "input_tampered",
                    f"manifest digest mismatch for {output_family} generation {generation_id}",
                )
            data_checksum = _data_checksum_for(output_family, manifest)
            if data_checksum is None:
                raise ResearchResolutionError(
                    "lineage_mismatch",
                    f"owning manifest has no data checksum: {output_family}",
                )
            if quality_binding_type is not None:
                _resolve_quality_decision(
                    quality_provider=quality_provider,
                    provider_config_ref=provider_config_ref,
                    binding_type=quality_binding_type,
                    subject_generation_id=generation_id,
                    subject_manifest_digest_sha256=(
                        None if quality_binding_type == "factor_v1" else requested_digest
                    ),
                    output_family=output_family,
                )
            stage_bindings.append(ResolvedStageBinding(
                stage=stage,
                output_family=output_family,
                generation_id=generation_id,
                manifest_digest_sha256=manifest["manifest_digest_sha256"],
                data_checksum_sha256=data_checksum,
            ))

        self._verify_contract_version_inputs(request_schema, candidate)

        execution_plan = {
            "schema_version": "v1",
            "request_content_generation_id": candidate["request_content_generation_id"],
            "stage_plan_sha256": candidate["stage_plan_sha256"],
            "stage_bindings": [
                {
                    "stage": binding.stage,
                    "output_family": binding.output_family,
                    "generation_id": binding.generation_id,
                    "manifest_digest_sha256": binding.manifest_digest_sha256,
                    "data_checksum_sha256": binding.data_checksum_sha256,
                }
                for binding in stage_bindings
            ],
        }
        return ResolvedExecutionPlan(
            request=candidate,
            request_manifest_digest_sha256=candidate["manifest_digest_sha256"],
            stage_plan_sha256=candidate["stage_plan_sha256"],
            stage_bindings=tuple(stage_bindings),
            resolved_execution_plan_sha256=sha256_json(execution_plan),
        )

    def _verify_contract_version_inputs(
        self, request_schema: str, candidate: dict[str, Any]
    ) -> None:
        """Verify the extra input sets a contract version declares.

        v5 is v4 plus the paper-execution template, so it carries the same required
        `index_membership_binding` / `factor_partition_set_binding` and must bind them
        to their owning stores exactly as v4 does; a v5 run must not skip the PIT
        membership check merely because its paper bindings resolve first.
        """
        if request_schema in {"research_run_request_v4", "research_run_request_v5"}:
            self._verify_v4_input_sets(candidate)
        if request_schema == "research_run_request_v5":
            self._verify_v5_paper_bindings(candidate)

    def _verify_v5_paper_bindings(self, candidate: dict[str, Any]) -> None:
        """Resolve template-v2 paper bindings against the published market-data store."""
        template = candidate.get("paper_execution_template", {})
        if not self.market_dataset_store:
            raise ResearchResolutionError(
                "request_invalid",
                "v5 request requires a market_dataset_store for paper input resolution",
            )
        _PAPER_BINDING_FAMILIES = {
            "market_data_binding": "bars_daily_v2",
            "calendar_binding": "trading_calendar_snapshot_v1",
            "suspension_binding": "suspension_snapshot_v1",
            "corporate_action_binding": "dividend_event_v1",
        }
        from datetime import date as _date
        for field, expected_family in _PAPER_BINDING_FAMILIES.items():
            binding = template.get(field)
            if not binding:
                raise ResearchResolutionError("input_unresolved", f"missing paper binding: {field}")
            try:
                self.market_dataset_store.verify_set_binding(binding)
            except ResearchResolutionError:
                raise
            except ContractError as exc:
                detail = str(exc)
                if "tampered" in detail or "mismatch" in detail:
                    raise ResearchResolutionError("input_tampered", detail) from exc
                raise ResearchResolutionError("input_unresolved", detail) from exc


    def _verify_v4_input_sets(self, request: dict[str, Any]) -> None:
        """Bind a v4/v5 request to the point-in-time membership and factor partition sets."""
        if self.membership_store is None or self.trading_calendar is None:
            raise ResearchResolutionError(
                "request_invalid",
                "v4/v5 requests require an index membership store and a governed trading calendar",
            )
        factor_store = self.stores_by_output_family.get("factor_partition")
        if factor_store is None:
            raise ResearchResolutionError(
                "request_invalid",
                "no owning store is registered for output family factor_partition",
            )
        try:
            verify_request_input_sets(
                request,
                membership_store=self.membership_store,
                factor_store=factor_store,
                trading_days=self.trading_calendar,
            )
        except ContractError as exc:
            detail = str(exc)
            code = (
                "input_tampered"
                if "tampered" in detail or "digest mismatch" in detail
                else "input_unresolved"
            )
            raise ResearchResolutionError(code, detail) from exc


class FileResearchRunStore:
    """Atomic, immutable Research Run ledger for request and dry-run states."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def publish_request(self, manifest: Mapping[str, Any], *, path_policy: str) -> PublishedRequest:
        if path_policy != "strict_v1":
            raise ContractError("unsupported research path policy")
        request = dict(manifest)
        request_schema = research_request_schema_name(request)
        ModelContractLoader.validate(request_schema, request)
        expected_generation, expected_digest = research_contract_identities(
            request, schema_name=request_schema
        )
        if request["request_content_generation_id"] != expected_generation:
            raise ContractError("research request stable content identity mismatch")
        if request["manifest_digest_sha256"] != expected_digest:
            raise ContractError("research request manifest digest mismatch")
        request_generation = request["request_content_generation_id"]
        relative_path = _request_relative_path(request_generation, request["run_id"])
        path = self._atomic_write(relative_path, request)
        return PublishedRequest(
            manifest_path=path,
            manifest_digest_sha256=request["manifest_digest_sha256"],
        )

    def read_request(
        self, request_content_generation_id: str, manifest_digest_sha256: str
    ) -> Mapping[str, Any]:
        base = self.root / "research_runs" / "requests"
        matches = [
            path.parent
            for path in base.glob(f"request={request_content_generation_id}/run=*/manifest.json")
            if base.exists()
        ]
        if not matches:
            raise ContractError(f"unpublished research request: {request_content_generation_id}")
        if len(matches) > 1:
            raise ContractError(f"ambiguous research request: {request_content_generation_id}")
        request = _read_json(matches[0] / "manifest.json")
        request_schema = research_request_schema_name(request)
        ModelContractLoader.validate(request_schema, request)
        expected_generation, expected_digest = research_contract_identities(
            request, schema_name=request_schema
        )
        if request["request_content_generation_id"] != request_content_generation_id:
            raise ContractError("research request generation mismatch")
        if request["request_content_generation_id"] != expected_generation:
            raise ContractError("research request stable content identity mismatch")
        if request["manifest_digest_sha256"] != manifest_digest_sha256:
            raise ContractError("research request manifest digest mismatch")
        if request["manifest_digest_sha256"] != expected_digest:
            raise ContractError("research request manifest digest mismatch")
        if request["manifest_digest_sha256"] != manifest_digest_sha256:
            raise ContractError("research request manifest digest mismatch")
        return request

    def publish_state(self, manifest: Mapping[str, Any], *, stage: str) -> PublishedState:
        state = dict(manifest)
        ModelContractLoader.validate("research_run_state", state)
        stage_record = state["stage_records"][-1]
        if stage_record["stage"] != stage:
            raise ContractError("state stage does not match publication stage")
        stage_number = stage_plan_for_request(state).index(stage)
        relative_path = Path("research_runs") / "states" / f"request={state['request_content_generation_id']}" / f"run={state['run_id']}" / f"stage={stage_number:02d}" / "manifest.json"
        path = self._atomic_write(relative_path, state)
        return PublishedState(
            manifest_path=path,
            manifest_digest_sha256=state["manifest_digest_sha256"],
        )

    def read_published_document(
        self,
        output_family: str,
        generation_id: str,
        *,
        manifest_digest_sha256: str,
        data_checksum_sha256: str,
    ) -> dict[str, Any]:
        """Read an owning manifest by its Research Chain output family."""
        import json as _json

        family_roots = {
            "factor_partition": self.root / "factors",
            "label_set": self.root / "label_sets",
        }
        base = family_roots.get(output_family)
        if base is None:
            raise ContractError(f"unsupported published document family: {output_family}")
        matches: list[Path] = []
        if base.exists():
            for path in base.rglob("manifest.json"):
                try:
                    manifest = _json.loads(path.read_text(encoding="utf-8"))
                except (OSError, _json.JSONDecodeError):
                    continue
                if manifest.get("generation_id") == generation_id:
                    matches.append(manifest)
        if not matches:
            raise ContractError(f"unpublished {output_family}: {generation_id}")
        if len(matches) != 1:
            raise ContractError(f"ambiguous {output_family}: {generation_id}")
        manifest = matches[0]
        if manifest.get("manifest_digest_sha256") != manifest_digest_sha256:
            raise ContractError(f"{output_family} manifest digest mismatch")
        if manifest.get("data_checksum_sha256") != data_checksum_sha256:
            raise ContractError(f"{output_family} data checksum mismatch")
        return manifest

    def read_state(
        self,
        request_content_generation_id: str,
        run_id: str,
        stage: str,
        manifest_digest_sha256: str,
    ) -> Mapping[str, Any]:
        # Locate the snapshot by its own stage record rather than by the module
        # default stage number: V3/V4/V5 number paper_execution at 08 while the
        # default V1 order has no such stage, so a hardcoded lookup could never
        # read a published paper_execution state back.
        base = (
            self.root / "research_runs" / "states"
            / f"request={request_content_generation_id}" / f"run={run_id}"
        )
        if not base.exists():
            raise ContractError(f"research state is unavailable: {stage}")
        matches: list[Mapping[str, Any]] = []
        for manifest_path in sorted(base.glob("stage=*/manifest.json")):
            candidate = _read_json(manifest_path)
            ModelContractLoader.validate("research_run_state", candidate)
            if candidate["stage_records"][-1]["stage"] == stage:
                matches.append(candidate)
        if len(matches) != 1:
            raise ContractError(f"research state for stage {stage} is missing or ambiguous")
        state = matches[0]
        if state["manifest_digest_sha256"] != manifest_digest_sha256:
            raise ContractError("research state manifest digest mismatch")
        return state

    def list_state_snapshots(
        self, request_content_generation_id: str, run_id: str
    ) -> list[StateSummary]:
        base = (
            self.root / "research_runs" / "states"
            / f"request={request_content_generation_id}" / f"run={run_id}"
        )
        if not base.exists():
            return []
        summaries: list[StateSummary] = []
        for manifest_path in base.glob("stage=*/manifest.json"):
            state = _read_json(manifest_path)
            ModelContractLoader.validate("research_run_state", state)
            stage_record = state["stage_records"][-1]
            summaries.append(StateSummary(
                stage=stage_record["stage"],
                manifest_digest_sha256=state["manifest_digest_sha256"],
                status=state["final_status"],
            ))
        if not summaries:
            return []
        # The states of a run share the request's contract version — derive
        # the listing order from the documents themselves (any of them); a
        # hardcoded V1 order would silently drop V3 paper_execution
        # snapshots, which publish_state now numbers correctly.
        order = stage_plan_for_request(state)
        summaries_by_stage = {item.stage: item for item in summaries}
        return [summaries_by_stage[stage] for stage in order if stage in summaries_by_stage]

    def publish_result(self, manifest: Mapping[str, Any], *, path_policy: str) -> PublishedResult:
        if path_policy != "strict_v1":
            raise ContractError("unsupported research path policy")
        result = dict(manifest)
        ModelContractLoader.validate("research_run_result", result)
        expected_generation, expected_digest = research_contract_identities(
            result, schema_name="research_run_result"
        )
        if result["result_content_generation_id"] != expected_generation:
            raise ContractError("research result stable content identity mismatch")
        if result["manifest_digest_sha256"] != expected_digest:
            raise ContractError("research result manifest digest mismatch")
        relative_path = _result_relative_path(
            result["request_content_generation_id"],
            result["run_id"],
            result["result_content_generation_id"],
        )
        path = self._atomic_write(relative_path, result)
        return PublishedResult(
            manifest_path=path,
            manifest_digest_sha256=result["manifest_digest_sha256"],
        )

    def read_result(self, result_generation_id: str, manifest_digest_sha256: str) -> Mapping[str, Any]:
        base = self.root / "research_runs" / "results"
        matches = [
            path.parent
            for path in base.glob(f"request=*/run=*/result={result_generation_id}/manifest.json")
            if base.exists()
        ]
        if not matches:
            raise ContractError(f"unpublished research result: {result_generation_id}")
        if len(matches) > 1:
            raise ContractError(f"ambiguous research result: {result_generation_id}")
        result = _read_json(matches[0] / "manifest.json")
        ModelContractLoader.validate("research_run_result", result)
        expected_generation, expected_digest = research_contract_identities(
            result, schema_name="research_run_result"
        )
        if result["result_content_generation_id"] != result_generation_id:
            raise ContractError("research result generation mismatch")
        if result["result_content_generation_id"] != expected_generation:
            raise ContractError("research result stable content identity mismatch")
        if result["manifest_digest_sha256"] != manifest_digest_sha256:
            raise ContractError("research result manifest digest mismatch")
        if result["manifest_digest_sha256"] != expected_digest:
            raise ContractError("research result manifest digest mismatch")
        result_binding = result["stage_records"][-1]["output_bindings"][-1]
        if result_binding["output_family"] != "research_run_result":
            raise ContractError("research result reconciliation binding is missing")
        if result_binding["generation_id"] != result_generation_id:
            raise ContractError("research result reconciliation generation mismatch")
        if result_binding["manifest_digest_sha256"] != manifest_digest_sha256:
            raise ContractError("research result reconciliation digest mismatch")
        runtime_stage_records = result["stage_records"][1:-1]
        # Deliberately the V1 order, not the payload's: the V3 paper-
        # execution runtime is disabled (runner._validate_plan), so no
        # legitimate V3 result can exist — deriving from the payload would
        # only loosen the closure check for forged documents.
        expected_runtime_stages = list(stage_plan_for_request({"contract_version": 1}))[1:-1]
        if [record.get("stage") for record in runtime_stage_records] != expected_runtime_stages:
            raise ContractError("research result stage closure is incomplete")
        if any(record.get("status") != "passed" for record in runtime_stage_records):
            raise ContractError("research result contains failed stages")
        for record in runtime_stage_records:
            bindings = record.get("output_bindings")
            if not isinstance(bindings, list) or not bindings:
                raise ContractError(f"research result stage output is missing: {record['stage']}")
            for binding in bindings:
                family = binding.get("output_family")
                if result["readback_status"].get(family) != "passed":
                    raise ContractError(f"research result has no passed readback evidence: {family}")
        return result

    def _atomic_write(self, relative_path: Path, document: Mapping[str, Any]) -> Path:
        absolute_path = self.root / relative_path
        if absolute_path.parent.exists() and absolute_path.parent.is_symlink():
            raise ContractError("research ledger parent is symlinked")
        if absolute_path.exists() or absolute_path.is_symlink():
            raise ContractError(f"immutable research manifest already exists: {relative_path}")
        stage = document.get("stage_records", [{}])[-1].get("stage") if "stage_records" in document else None
        kind = (
            "request" if relative_path.parts[1] == "requests"
            else "result" if relative_path.parts[1] == "results"
            else "state"
        )
        validate_research_layout(
            absolute_path,
            data_root=self.root,
            kind=kind,
            request_generation_id=document["request_content_generation_id"],
            run_id=document["run_id"],
            stage=stage,
            result_generation_id=document.get("result_content_generation_id"),
            require_parent=False,
            # The document carries its contract version, so its own stage
            # plan — not a validator default — decides the stage number.
            stage_order=stage_plan_for_request(document) if kind == "state" else None,
        )
        absolute_path.parent.mkdir(parents=True, exist_ok=False)
        staging = absolute_path.parent / f".{absolute_path.name}.staging.{uuid.uuid4().hex}"
        try:
            staging.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            os.replace(staging, absolute_path)
            descriptor = os.open(absolute_path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return absolute_path


def build_dry_run_state(
    plan: ResolvedExecutionPlan,
    *,
    runner_identity: Mapping[str, str],
    created_at: str | None = None,
) -> dict[str, Any]:
    request = plan.request
    state: dict[str, Any] = {
        "contract_version": request.get("contract_version", 1),
        "schema_version": "1.0.0",
        "request_content_generation_id": request["request_content_generation_id"],
        "request_manifest_digest_sha256": plan.request_manifest_digest_sha256,
        "run_id": request["run_id"],
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "runner_identity": dict(runner_identity),
        "intent": "dry_run",
        "stage_records": [{
            "stage": "resolve_request",
            "status": "passed",
            "output_bindings": [{
                "output_family": "research_run_request",
                "generation_id": request["request_content_generation_id"],
                "manifest_digest_sha256": plan.request_manifest_digest_sha256,
                "data_checksum_sha256": sha256_json(request),
                "physical_path": str(_request_relative_path(
                    request["request_content_generation_id"], request["run_id"]
                )),
                "quality_decision_checksum_sha256": "0" * 64,
                "failure_reason": None,
            }],
            "failure_reason": None,
        }],
        "final_status": "passed",
    }
    state["state_content_generation_id"] = "0" * 64
    state["manifest_digest_sha256"] = "0" * 64
    generation, digest = research_contract_identities(state, schema_name="research_run_state")
    state["state_content_generation_id"] = generation
    state["manifest_digest_sha256"] = digest
    ModelContractLoader.validate("research_run_state", state)
    return state


def _resolve_quality_decision(
    *,
    quality_provider: QualityDecisionProvider | None,
    provider_config_ref: str | None,
    binding_type: str,
    subject_generation_id: str,
    subject_manifest_digest_sha256: str | None,
    output_family: str,
) -> None:
    if quality_provider is None:
        raise ResearchResolutionError(
            "quality_decision_missing",
            f"quality provider is not configured for {output_family}",
        )
    if provider_config_ref is None:
        raise ResearchResolutionError(
            "quality_decision_missing",
            f"provider configuration is missing for {output_family}",
        )
    try:
        decision = quality_provider.resolve(
            binding_type=binding_type,
            subject_generation_id=subject_generation_id,
            subject_manifest_digest_sha256=subject_manifest_digest_sha256,
            output_family=output_family,
            provider_config_ref=provider_config_ref,
        )
    except ResearchResolutionError as exc:
        if exc.reason in {"quality_decision_missing", "quality_decision_rejected"}:
            raise
        raise ResearchResolutionError("quality_decision_missing", exc.detail) from exc
    except ContractError as exc:
        detail = str(exc)
        if "unregistered" in detail or "untrusted" in detail or "trust anchor" in detail:
            raise ResearchResolutionError("quality_decision_rejected", detail) from exc
        raise ResearchResolutionError("quality_decision_missing", detail) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise ResearchResolutionError(
            "quality_decision_missing", f"quality provider failed: {exc}"
        ) from exc

    try:
        candidate = dict(decision)
        ModelContractLoader.validate("quality_decision", candidate)
        if candidate["binding_type"] != binding_type:
            raise ContractError("quality decision binding mismatch")
        if candidate["subject_generation_id"] != subject_generation_id:
            raise ContractError("quality decision subject generation mismatch")
        if candidate.get("subject_manifest_digest_sha256") != subject_manifest_digest_sha256:
            raise ContractError("quality decision subject digest mismatch")
    except ContractError as exc:
        detail = str(exc)
        if "trust anchor" in detail:
            raise ResearchResolutionError("quality_decision_rejected", detail) from exc
        raise ResearchResolutionError("quality_decision_missing", detail) from exc


def _data_checksum_for(output_family: str, manifest: Mapping[str, Any]) -> str | None:
    if output_family == "factor_partition":
        return manifest.get("data_checksum_sha256")
    if output_family == "universe_snapshot":
        return manifest.get("members_artifact", {}).get("checksum_sha256")
    if output_family == "adjusted_price_dataset":
        return manifest.get("data_checksum_sha256")
    if output_family == "label_set":
        return manifest.get("data_checksum_sha256")
    if output_family == "feature_preprocessing":
        return manifest.get("output_frame_sha256")
    if output_family == "backtest_config":
        return manifest.get("price_source_binding", {}).get("data_checksum_sha256")
    return None


def _request_relative_path(request_generation_id: str, run_id: str) -> Path:
    return (
        Path("research_runs") / "requests" / f"request={request_generation_id}"
        / f"run={run_id}" / "manifest.json"
    )


def _state_relative_path(request_generation_id: str, run_id: str, stage: str) -> Path:
    stage_number = f"{_DEFAULT_STAGE_PLAN.index(stage):02d}"
    return (
        Path("research_runs") / "states" / f"request={request_generation_id}"
        / f"run={run_id}" / f"stage={stage_number}" / "manifest.json"
    )


def _result_relative_path(
    request_generation_id: str, run_id: str, result_generation_id: str
) -> Path:
    return (
        Path("research_runs") / "results" / f"request={request_generation_id}"
        / f"run={run_id}" / f"result={result_generation_id}" / "manifest.json"
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("malformed research ledger manifest") from exc
    if not isinstance(payload, dict):
        raise ContractError("research ledger manifest must be an object")
    return payload
