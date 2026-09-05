from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tests.review_key import REVIEWER_PRIVATE_KEY
from uq.contracts.model_layer import (
    bind_reviewed_quality_decision,
    canonical_json,
    create_reviewed_quality_decision,
    model_manifest_identities,
    sha256_bytes,
)
from uq.errors import ContractError
from uq.models.qlib_export import QlibDatasetExporter, QlibInitReceiptBuilder
from uq.models.predictions import PredictionBuilder
from uq.research_chain import DatasetStageAdapter, FactorStageAdapter, FileResearchRunStore
from uq.research_chain.adapters import ModelStageAdapter, PredictionStageAdapter, QlibExportStageAdapter
from uq.research_chain.owning_contracts import FeaturePreprocessingStore, FeatureSchemaStore, LabelStore
from uq.research_chain import ResolvedStageBinding
from uq.models.dataset_writer import DatasetWriter
from uq.models.definition import ModelDefinitionBuilder
from uq.models.predictions import PredictionBuilder
from io import BytesIO

import joblib
from uq.models.qlib_runtime import QlibRuntimeTrainer
from uq.models.trainer import ModelRunBuilder
from uq.models.trainer import ArtifactStore
from tests.test_research_chain_phase2 import (
    RUN_ID,
    _dataset_quality_decision,
    _preprocessing_quality_decision,
    _prepare_dataset_store,
)


DIGEST = "0" * 64


def export_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="qlib_dataset_export_v1", policy="reject_all", status="passed",
        checks=[{"name": "export_files_verified", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def receipt_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="qlib_init_receipt_v1", policy="reject_all", status="passed",
        checks=[{"name": "runtime_cache_boundary", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def run_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="model_run_v1", policy="reject_all", status="passed",
        checks=[{"name": "upstream_lineage_resolved", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def artifact_decision(artifact_generation: str) -> dict:
    unsigned = create_reviewed_quality_decision(
        binding_type="model_artifact_v1", policy="reject_all", status="passed",
        checks=[{"name": "artifact_checksum", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    bound, _ = bind_reviewed_quality_decision(
        unsigned, binding_type="model_artifact_v1",
        subject_generation_id=artifact_generation,
        subject_content_sha256=artifact_generation,
    )
    return bound


def prediction_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="prediction_set_v1", policy="reject_all", status="passed",
        checks=[
            {"name": "finite_scores", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "eligibility_coverage", "threshold": 1, "observed": 1, "level": "error", "result": "passed"},
        ],
        errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def _prepare_model_stage(tmp_path: Path):
    adapter, plan, factor_store = _prepare_dataset_store(tmp_path)
    dataset_result = adapter.run(
        plan,
        runner_identity={"code_fingerprint": DIGEST, "environment_profile": "locked-test", "lock_digest_sha256": DIGEST},
        quality_decision=_dataset_quality_decision(),
        preprocessing_quality_decision=_preprocessing_quality_decision(),
        created_at="2026-01-30T07:00:00+00:00",
    )
    dataset_generation_id = dataset_result.manifest["generation_id"]
    dataset_binding = ResolvedStageBinding(
        stage="dataset_preparation", output_family="model_dataset",
        generation_id=dataset_generation_id,
        manifest_digest_sha256=dataset_result.manifest["manifest_digest_sha256"],
        data_checksum_sha256=dataset_result.manifest["data_checksum_sha256"],
    )
    plan = dataclasses.replace(plan, stage_bindings=(*plan.stage_bindings, dataset_binding))
    binding = plan.stage_bindings[0]
    _, factor_frame = factor_store.read_partition(binding.generation_id)
    return plan, dataset_result, dataset_generation_id, factor_frame, adapter


def test_model_chain_exports_trains_and_predicts(tmp_path: Path):
    plan, dataset_result, dataset_generation_id, factor_frame, dataset_adapter = _prepare_model_stage(tmp_path)
    writer = DatasetWriter(tmp_path)
    _, dataset_frame = writer.read(
        plan.request["dataset_policy_template"]["dataset_name"],
        plan.request["dataset_policy_template"]["semantic_version"],
        dataset_generation_id,
    )
    exporter = QlibDatasetExporter(tmp_path / "qlib_exports")
    receipt_builder = QlibInitReceiptBuilder()
    run_store = FileResearchRunStore(tmp_path)
    export_adapter = QlibExportStageAdapter(
        exporter=exporter, receipt_builder=receipt_builder, dataset_writer=writer, run_store=run_store,
    )
    cache_file = tmp_path / ".cache" / "qlib" / "calendar.pkl"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"governed-cache")
    export_result = export_adapter.run(
        plan,
        dataset_generation_id=dataset_generation_id,
        dataset_manifest_digest_sha256=dataset_result.manifest["manifest_digest_sha256"],
        feature_columns=list(dataset_result.manifest["ordered_features"]),
        label_column="label",
        provider_uri="file:///research-exports",
        qlib_import_path="qlib",
        qlib_version="0.9.7",
        cache_root=str(tmp_path / ".cache"),
        cache_files_before=set(),
        cache_files_after={str(cache_file)},
        export_quality_decision=export_decision(),
        receipt_quality_decision=receipt_decision(),
        runner_identity={"code_fingerprint": DIGEST, "environment_profile": "locked-test", "lock_digest_sha256": DIGEST},
    )
    assert export_result.manifest["export_generation_id"] == export_result.export_manifest["generation_id"]
    assert export_result.manifest["export_manifest_digest_sha256"] == export_result.export_manifest["manifest_digest_sha256"]

    preprocessing = dataset_adapter.preprocessing_store.read_manifest(plan.stage_bindings[4].generation_id)
    definition = ModelDefinitionBuilder(run_content_generation_id=DIGEST, reviewed=True).build(
        algorithm="qlib_linear", hyperparameters={"alpha": 0.5, "fit_intercept": False},
        seed_policy={"base_seed": 7, "derivation": "fixed"}, model_set="research-baseline",
        model_version="1.0.0", feature_schema_generation_id=preprocessing["output_feature_schema_generation_id"],
        compatible_dataset_versions=[plan.request["dataset_policy_template"]["semantic_version"]],
        metrics=[{"name": "ic", "direction": "maximize"}], selection_rule="maximum validation ic",
        serializer_version="joblib-v1",
    )
    model_adapter = ModelStageAdapter(
        trainer=QlibRuntimeTrainer(), exporter=exporter, artifact_store=ArtifactStore(tmp_path),
        dataset_writer=writer, universe_store=dataset_adapter.universe_store, run_store=run_store,
    )
    model_result = model_adapter.run(
        plan,
        dataset_generation_id=dataset_generation_id,
        label_generation_id=plan.stage_bindings[3].generation_id,
        universe_generation_id=plan.stage_bindings[1].generation_id,
        factor_generation_id=plan.stage_bindings[0].generation_id,
        export_manifest=export_result.export_manifest,
        receipt_manifest=export_result.manifest,
        definition=definition,
        environment_lock_sha256=DIGEST,
        determinism_controls={"random_seed": 7, "threads": 1},
        model_quality_decision=run_decision(),
        artifact_quality_decision_provider=artifact_decision,
        feature_columns=list(dataset_result.manifest["ordered_features"]),
        label_column="label",
        runner_identity={"code_fingerprint": DIGEST, "environment_profile": "locked-test", "lock_digest_sha256": DIGEST},
    )
    assert model_result.published_state.manifest_path.is_file()
    model = joblib.load(BytesIO(model_result.artifact_bytes))
    assert model.__class__.__name__ == "LinearModel"

    repeated_definition_input = ModelDefinitionBuilder(run_content_generation_id=DIGEST, reviewed=True).build(
        algorithm="qlib_linear", hyperparameters={"alpha": 0.5, "fit_intercept": False},
        seed_policy={"base_seed": 7, "derivation": "fixed"}, model_set="research-baseline",
        model_version="1.0.0", feature_schema_generation_id=preprocessing["output_feature_schema_generation_id"],
        compatible_dataset_versions=[plan.request["dataset_policy_template"]["semantic_version"]],
        metrics=[{"name": "ic", "direction": "maximize"}], selection_rule="maximum validation ic",
        serializer_version="joblib-v1",
    )
    _, repeated_definition = ModelRunBuilder.build(
        definition=repeated_definition_input, dataset_manifest=dataset_result.manifest,
        export_manifest=export_result.export_manifest, receipt_manifest=export_result.manifest,
        environment_lock_sha256=DIGEST, determinism_controls={"random_seed": 7, "threads": 1},
        label_manifest=dataset_adapter.label_store.read_manifest(plan.stage_bindings[3].generation_id),
        universe_snapshot=dataset_adapter.universe_store.read_manifest(plan.stage_bindings[1].generation_id),
        factor_manifests={plan.stage_bindings[0].generation_id: dataset_adapter.factor_store.read_manifest(plan.stage_bindings[0].generation_id)},
        quality_decision=run_decision(), store_root=run_store.root,
    )
    _, repeated_bytes = QlibRuntimeTrainer().train(
        definition=repeated_definition, dataset_manifest=dataset_result.manifest,
        export_manifest=export_result.export_manifest, receipt_manifest=export_result.manifest,
        verified_export=exporter.read(plan.request["dataset_policy_template"]["dataset_name"], export_result.export_manifest["generation_id"]),
        feature_columns=list(dataset_result.manifest["ordered_features"]), label_column="label",
    )
    assert repeated_bytes == model_result.artifact_bytes

    scores = dataset_frame[["instrument", "datetime"]].copy()
    scores["score"] = dataset_frame[dataset_result.manifest["ordered_features"][0]].to_numpy()
    prediction_adapter = PredictionStageAdapter(
        PredictionBuilder(tmp_path), run_store,
    )
    prediction_result = prediction_adapter.run(
        plan,
        dataset_generation_id=dataset_generation_id,
        model_stage_result=model_result,
        scores=scores,
        decision_date="2026-01-09",
        quality_decision=prediction_decision(),
        eligibility_policy="reviewed-v1",
        eligibility_status="passed",
        runner_identity={"code_fingerprint": DIGEST, "environment_profile": "locked-test", "lock_digest_sha256": DIGEST},
    )
    assert prediction_result.published_state.manifest_path.is_file()
    assert len(prediction_result.frame) == len(scores)


def test_wrong_artifact_quality_report_rejects_publication(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    manifest = {
        "contract_version": 1,
        "artifact_filename": "artifact.bin",
        "artifact_checksum_sha256": sha256_bytes(b"artifact"),
        "byte_size": 8,
        "runtime_name": "numpy_ridge",
        "runtime_version": "test",
        "runtime_import_path": "test",
        "model_run_content_generation_id": DIGEST,
        "serialization_profile_id": "json-numpy-v1",
        "run_id": "00000000-0000-4000-8000-000000000003",
        "created_at": "2026-01-30T07:00:00+00:00",
        "generation_id": "a" * 64,
        "manifest_digest_sha256": "b" * 64,
    }
    wrong = artifact_decision("b" * 64)
    with pytest.raises(ContractError, match="quality report does not bind to the artifact generation"):
        store.publish(manifest, b"artifact", quality_report=wrong)


def test_tampered_export_rejects_before_model_stage(tmp_path: Path):
    plan, dataset_result, dataset_generation_id, _, dataset_adapter = _prepare_model_stage(tmp_path)
    writer = DatasetWriter(tmp_path)
    exporter = QlibDatasetExporter(tmp_path / "qlib_exports")
    export_adapter = QlibExportStageAdapter(
        exporter=exporter, receipt_builder=QlibInitReceiptBuilder(), dataset_writer=writer,
        run_store=FileResearchRunStore(tmp_path),
    )
    cache_file = tmp_path / ".cache" / "qlib" / "calendar.pkl"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"governed-cache")
    export_adapter.run(
        plan,
        dataset_generation_id=dataset_generation_id,
        dataset_manifest_digest_sha256=dataset_result.manifest["manifest_digest_sha256"],
        feature_columns=list(dataset_result.manifest["ordered_features"]),
        label_column="label", provider_uri="file:///research-exports",
        qlib_import_path="qlib", qlib_version="0.9.6",
        cache_root=str(tmp_path / ".cache"), cache_files_before=set(), cache_files_after={str(cache_file)},
        export_quality_decision=export_decision(), receipt_quality_decision=receipt_decision(),
        runner_identity={"code_fingerprint": DIGEST, "environment_profile": "locked-test", "lock_digest_sha256": DIGEST},
    )
    export_manifest = next((tmp_path / "qlib_exports" / f"dataset={plan.request['dataset_policy_template']['dataset_name']}").rglob("manifest.json"))
    manifest = json.loads(export_manifest.read_text())
    bin_path = export_manifest.parent / "features" / "inst0" / "volume_ratio_20d.day.bin"
    bin_path.write_bytes(bin_path.read_bytes() + b"tampered")
    with pytest.raises(ContractError, match="tampered Qlib export file"):
        exporter.read(plan.request["dataset_policy_template"]["dataset_name"], manifest["generation_id"])


def test_tampered_artifact_rejects_read(tmp_path: Path):
    plan, dataset_result, dataset_generation_id, _, dataset_adapter = _prepare_model_stage(tmp_path)
    store = ArtifactStore(tmp_path)
    partition = store.models_dir / "run_generation=0" / ("artifact_generation=" + "a" * 64)
    partition.mkdir(parents=True)
    (partition / "artifact.bin").write_bytes(b"tampered")
    with pytest.raises(ContractError):
        store.read("0" * 64, "a" * 64)
