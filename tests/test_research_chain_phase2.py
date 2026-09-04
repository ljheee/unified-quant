from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from uq.contracts.factor_governance import FactorRegistry
from uq.errors import ContractError
from uq.factors.store import FactorStore
from uq.research_chain import FactorStageAdapter, FileResearchRunStore
from uq.research_chain.resolver import ResolvedExecutionPlan, ResolvedStageBinding

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "00000000-0000-4000-8000-000000000002"


def _publish_factor(root: Path) -> FactorStore:
    from tests.test_model_end_to_end import _publish_factor

    _publish_factor(root)
    return FactorStore(root, FactorRegistry(ROOT))


def _plan(manifest: dict) -> ResolvedExecutionPlan:
    request = {
        "request_content_generation_id": manifest["generation_id"],
        "manifest_digest_sha256": manifest["manifest_digest_sha256"],
        "run_id": RUN_ID,
    }
    binding = ResolvedStageBinding(
        stage="factor_computation",
        output_family="factor_partition",
        generation_id=manifest["generation_id"],
        manifest_digest_sha256=manifest["manifest_digest_sha256"],
        data_checksum_sha256=manifest["data_checksum_sha256"],
    )
    return ResolvedExecutionPlan(
        request=request,
        request_manifest_digest_sha256=manifest["manifest_digest_sha256"],
        stage_plan_sha256="0" * 64,
        stage_bindings=(binding,),
        resolved_execution_plan_sha256="0" * 64,
    )


def test_factor_stage_binds_verified_partition(tmp_path: Path) -> None:
    store = _publish_factor(tmp_path)
    run_store = FileResearchRunStore(tmp_path)
    from tests.test_model_end_to_end import _load_factor_document

    manifest_path = next((tmp_path / "factors").rglob("manifest.json"))
    manifest = _load_factor_document(tmp_path, __import__("json").loads(manifest_path.read_text())["generation_id"])
    adapter = FactorStageAdapter(store, run_store)
    result = adapter.run(
        _plan(manifest),
        runner_identity={
            "code_fingerprint": "0" * 64,
            "environment_profile": "locked-test",
            "lock_digest_sha256": "0" * 64,
        },
        quality_decision_checksum_sha256="a" * 64,
        created_at="2026-01-30T07:00:00+00:00",
    )

    assert result.published_state.manifest_path.is_file()
    state = run_store.read_state(
        manifest["generation_id"], RUN_ID, "factor_computation",
        result.published_state.manifest_digest_sha256,
    )
    binding = state["stage_records"][-1]["output_bindings"][0]
    assert binding["output_family"] == "factor_partition"
    assert binding["generation_id"] == manifest["generation_id"]
    assert binding["physical_path"].startswith("factors/")
    assert run_store.list_state_snapshots(manifest["generation_id"], RUN_ID)[-1].stage == "factor_computation"


def test_factor_stage_rejects_tampered_partition(tmp_path: Path) -> None:
    store = _publish_factor(tmp_path)
    manifest_path = next((tmp_path / "factors").rglob("manifest.json"))
    manifest = store.read_manifest(__import__("json").loads(manifest_path.read_text())["generation_id"])
    data_path = manifest_path.parent / "data.parquet"
    data_path.write_bytes(data_path.read_bytes() + b"tampered")

    adapter = FactorStageAdapter(store, FileResearchRunStore(tmp_path))
    with pytest.raises(ContractError, match="tampered factor data"):
        adapter.run(
            _plan(manifest),
            runner_identity={
                "code_fingerprint": "0" * 64,
                "environment_profile": "locked-test",
                "lock_digest_sha256": "0" * 64,
            },
            quality_decision_checksum_sha256="a" * 64,
        )


def test_factor_stage_rejects_binding_mismatch(tmp_path: Path) -> None:
    store = _publish_factor(tmp_path)
    manifest_path = next((tmp_path / "factors").rglob("manifest.json"))
    manifest = store.read_manifest(__import__("json").loads(manifest_path.read_text())["generation_id"])
    plan = _plan(manifest)
    assert plan.stage_bindings[0].manifest_digest_sha256 == manifest["manifest_digest_sha256"]
    assert plan.stage_bindings[0].data_checksum_sha256 == manifest["data_checksum_sha256"]
