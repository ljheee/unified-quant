from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..contracts.model_layer import ModelContractLoader, research_contract_identities
from ..errors import ContractError
from .contracts import PublishedState
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
