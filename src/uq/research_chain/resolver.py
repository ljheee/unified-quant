from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from ..contracts.model_layer import (
    ModelContractLoader,
    research_contract_identities,
    research_stage_plan_sha256,
    sha256_json,
)
from ..errors import ContractError
from .contracts import PublishedRequest, PublishedResult, PublishedState, StateSummary, validate_research_layout
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
_STAGE_PLAN = [
    "resolve_request", "factor_computation", "dataset_preparation", "qlib_export",
    "model_training", "prediction_publication", "portfolio_construction",
    "backtest_execution", "result_reconciliation",
]
_EXPECTED_STAGE_PLAN_SHA256 = research_stage_plan_sha256()


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

    def __init__(self, stores_by_output_family: Mapping[str, Any]) -> None:
        self.stores_by_output_family = dict(stores_by_output_family)

    def resolve(
        self,
        request: Mapping[str, Any],
        *,
        quality_provider: QualityDecisionProvider | None = None,
        provider_config_ref: str | None = None,
    ) -> ResolvedExecutionPlan:
        try:
            candidate = dict(request)
            ModelContractLoader.validate("research_run_request", candidate)
            if candidate["stage_plan_sha256"] != _EXPECTED_STAGE_PLAN_SHA256:
                raise ResearchResolutionError(
                    "request_invalid",
                    "request stage plan digest does not match the normative v1 plan",
                )
            expected_generation, expected_digest = research_contract_identities(
                candidate, schema_name="research_run_request"
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


class FileResearchRunStore:
    """Atomic, immutable Research Run ledger for request and dry-run states."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def publish_request(self, manifest: Mapping[str, Any], *, path_policy: str) -> PublishedRequest:
        if path_policy != "strict_v1":
            raise ContractError("unsupported research path policy")
        request = dict(manifest)
        ModelContractLoader.validate("research_run_request", request)
        expected_generation, expected_digest = research_contract_identities(
            request, schema_name="research_run_request"
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
        ModelContractLoader.validate("research_run_request", request)
        expected_generation, expected_digest = research_contract_identities(
            request, schema_name="research_run_request"
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
        relative_path = _state_relative_path(
            state["request_content_generation_id"], state["run_id"], stage
        )
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
        relative_path = _state_relative_path(request_content_generation_id, run_id, stage)
        state = _read_json(self.root / relative_path)
        ModelContractLoader.validate("research_run_state", state)
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
        order = {stage: index for index, stage in enumerate(_STAGE_PLAN)}
        return sorted(summaries, key=lambda item: order[item.stage])

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
        if not any(value == "passed" for value in result["readback_status"].values()):
            raise ContractError("research result has no passed readback evidence")
        return result

    def _atomic_write(self, relative_path: Path, document: Mapping[str, Any]) -> Path:
        absolute_path = self.root / relative_path
        if absolute_path.parent.exists() and absolute_path.parent.is_symlink():
            raise ContractError("research ledger parent is symlinked")
        if absolute_path.exists() or absolute_path.is_symlink():
            raise ContractError(f"immutable research manifest already exists: {relative_path}")
        stage = document.get("stage_records", [{}])[-1].get("stage") if "stage_records" in document else None
        validate_research_layout(
            absolute_path,
            data_root=self.root,
            kind=(
                "request" if relative_path.parts[1] == "requests"
                else "result" if relative_path.parts[1] == "results"
                else "state"
            ),
            request_generation_id=document["request_content_generation_id"],
            run_id=document["run_id"],
            stage=stage,
            result_generation_id=document.get("result_content_generation_id"),
            require_parent=False,
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
        "contract_version": 1,
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
    stage_number = f"{_STAGE_PLAN.index(stage):02d}"
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
