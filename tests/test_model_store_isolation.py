from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
from tests.review_key import REVIEWER_PRIVATE_KEY

from uq.contracts.model_layer import (
    bind_reviewed_quality_decision,
    create_reviewed_quality_decision,
    model_manifest_identities,
)
from uq.errors import ContractError
from uq.models.accepted_store import AcceptedFactorIndexRuntime
from uq.models.dataset import DatasetBuilder
from uq.models.dataset_writer import DatasetWriter
from uq.models.definition import ModelDefinitionBuilder
from uq.models.features import FeatureSchemaBuilder
from uq.models.predictions import PredictionBuilder
from uq.models.trainer import ArtifactStore, ModelTrainer

DIGEST = "0" * 64


def _factor_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=20)
    return pd.DataFrame([
        {
            "instrument": f"INST{instrument}",
            "datetime": day,
            "volume_ratio_20d": 1.0 + instrument * 0.1,
        }
        for instrument in range(2)
        for day in dates
    ])


def _publish_factor(root: Path) -> Path:
    from uq.contracts.artifacts import QualityReportStore
    from uq.contracts.factor_governance import FactorRegistry
    from uq.contracts.canonical_v2 import file_sha256_bytes
    from uq.factors.store import FactorStore, factor_generation, _validate_factor_frame

    frame = _factor_frame()
    arguments = {
        "frame": frame,
        "partition_date": date(2026, 1, 5),
        "input_dataset": "bars_daily",
        "input_schema_version": "research-v1",
        "upstream_generation_id": "a" * 64,
        "upstream_data_checksum": "b" * 64,
        "quality_report_checksum": "",
        "upstream_created_at": datetime.fromisoformat("2026-01-04T15:00:00+08:00"),
    }
    registry = FactorRegistry(Path(__file__).resolve().parents[1])
    generation = factor_generation(**arguments)
    report_path = root / "reports" / "factor_v1" / generation / "report.json"
    if not (root / "reports").exists():
        QualityReportStore().save(root, {
            "report_version": 1,
            "binding_type": "factor_v1",
            "bound_generation_id": generation,
            "policy": "reject_all",
            "status": "passed",
            "checks": _validate_factor_frame(frame, registry.get("basic", "1.0.0"))["checks"],
            "errors": [],
            "warnings": [],
        })
    arguments["quality_report_checksum"] = file_sha256_bytes(report_path.read_bytes())
    return FactorStore(root, registry).publish(**{key: value for key, value in arguments.items() if key != "frame"}, frame=frame)


def _dataset_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="model_dataset_v1", policy="reject_all", status="passed",
        checks=[{"name": "row_count", "threshold": 6, "observed": 40, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def _artifact_report(generation: str) -> dict:
    decision = create_reviewed_quality_decision(
        binding_type="model_artifact_v1", policy="reject_all", status="passed",
        checks=[{"name": "artifact_checksum", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    report, _ = bind_reviewed_quality_decision(
        decision, binding_type="model_artifact_v1",
        subject_generation_id=generation, subject_content_sha256=generation,
    )
    return report


def _train_and_publish_artifact(root: Path) -> tuple[ArtifactStore, str, str]:
    definition = ModelDefinitionBuilder(run_content_generation_id=DIGEST, reviewed=True).build(
        algorithm="regularized_linear", hyperparameters={"alpha": 1.0},
        seed_policy={"base_seed": 42, "derivation": "fixed"}, model_set="baseline",
        model_version="1.0.0", feature_schema_generation_id=DIGEST,
        compatible_dataset_versions=["1.0.0"], metrics=[{"name": "ic", "direction": "maximize"}],
        selection_rule="max ic",
    )
    frame = pd.DataFrame({
        "instrument": [f"I{index}" for index in range(20)],
        "datetime": pd.bdate_range("2026-01-01", periods=20),
        "feature": range(20),
        "label": [value * 0.01 for value in range(20)],
    })
    manifest, artifact_bytes = ModelTrainer(root).train(
        definition=definition, dataset_frame=frame,
        feature_columns=["feature"], label_column="label",
    )
    candidate = {**manifest, "generation_id": DIGEST, "manifest_digest_sha256": DIGEST}
    generation, _ = model_manifest_identities(
        candidate, schema_name="model_artifact", exclude_fields={"quality_report_checksum_sha256"}
    )
    store = ArtifactStore(root)
    store.publish(manifest, artifact_bytes, quality_report=_artifact_report(generation))
    return store, generation, manifest["model_run_content_generation_id"]


def _build_dataset(root: Path) -> tuple[DatasetWriter, str]:
    _publish_factor(root)
    runtime = AcceptedFactorIndexRuntime(root)
    query = {"contract_version": 1, "filters": {}, "ordering": ["partition_date"], "visibility": "accepted_only", "pagination": {"limit": 10}}
    factor_data = runtime.read(runtime.list(query)[0]["generation_id"])
    manifest = DatasetBuilder(dataset_name="isolation_slice", semantic_version="1.0.0").build(
        ordered_features=["volume_ratio_20d"], factor_set="basic", factor_version="1.0.0",
        factor_generation_ids=[DIGEST], label_set_name="return_5d", label_generation_id=DIGEST,
        universe_snapshot_generation_id=DIGEST,
        split_policy={
            "purge_trading_days": 5, "embargo_trading_days": 2,
            "splits": [
                {"name": "train", "start_date": "2026-01-05", "end_date": "2026-01-12"},
                {"name": "validation", "start_date": "2026-01-23", "end_date": "2026-01-26"},
            ],
        },
        row_count=len(factor_data),
    )
    schema = FeatureSchemaBuilder().build(factor_data, source_factor_set="basic", source_factor_version="1.0.0")
    writer = DatasetWriter(root)
    writer.write(manifest, factor_data, feature_schema=schema, quality_report=_dataset_decision())
    return writer, writer.last_published_manifest["generation_id"]


def _copy_partition(source: Path, target: Path) -> None:
    target.mkdir(parents=True)
    for path in source.iterdir():
        if path.is_file():
            (target / path.name).write_bytes(path.read_bytes())


@pytest.mark.parametrize("store_family", ["accepted_factor", "dataset", "artifact", "prediction"])
def test_staging_directories_are_invisible_to_accepted_readers(tmp_path: Path, store_family: str) -> None:
    if store_family == "accepted_factor":
        partition = _publish_factor(tmp_path)
        runtime = AcceptedFactorIndexRuntime(tmp_path)
        query = {"contract_version": 1, "filters": {}, "ordering": ["partition_date"], "visibility": "accepted_only", "pagination": {"limit": 10}}
        generation = runtime.list(query)[0]["generation_id"]
        staging = partition.with_name(partition.name + ".staging.x")
        _copy_partition(partition, staging)
        assert [entry["generation_id"] for entry in runtime.list(query)] == [generation]
        assert runtime.read(generation).equals(runtime._frames[generation])
        return

    if store_family == "dataset":
        writer, generation = _build_dataset(tmp_path)
        partition = tmp_path / "datasets" / "dataset=isolation_slice" / "version=1.0.0" / f"generation={generation}"
        staging = partition.with_name(partition.name + ".staging.x")
        _copy_partition(partition, staging)
        loaded_manifest, _ = writer.read("isolation_slice", "1.0.0", generation)
        assert loaded_manifest["generation_id"] == generation
        return

    if store_family == "artifact":
        store, generation, run_generation = _train_and_publish_artifact(tmp_path)
        partition = tmp_path / "models" / f"run_generation={run_generation}" / f"artifact_generation={generation}"
        staging = partition.with_name(partition.name + ".staging.x")
        _copy_partition(partition, staging)
        loaded_manifest, _ = store.read(run_generation, generation)
        assert loaded_manifest["generation_id"] == generation
        return

    store, generation, run_generation = _train_and_publish_artifact(tmp_path)
    manifest, _ = store.read(run_generation, generation)
    builder = PredictionBuilder(tmp_path)
    frame = pd.DataFrame({
        "instrument": [f"I{index}" for index in range(5)],
        "datetime": pd.bdate_range("2026-02-01", periods=5),
        "score": range(5),
    })
    prediction_decision = create_reviewed_quality_decision(
        binding_type="prediction_set_v1", policy="reject_all", status="passed",
        checks=[
            {"name": "finite_scores", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "eligibility_coverage", "threshold": 1, "observed": 1, "level": "error", "result": "passed"},
        ],
        errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    prediction_manifest, prediction_bytes = builder.build(
        prediction_set_name="isolation", artifact_store=store,
        model_artifact_generation_id=generation,
        model_artifact_checksum=manifest["artifact_checksum_sha256"],
        input_dataset_generation_id=DIGEST, run_generation_id=run_generation,
        decision_date="2026-02-05", scores=frame,
        eligibility_policy="reviewed-v1", eligibility_status="passed",
        quality_decision=prediction_decision,
    )
    partition = builder.publish(prediction_manifest, prediction_bytes)
    staging = partition.with_name(partition.name + ".staging.x")
    _copy_partition(partition, staging)
    loaded_manifest, _ = builder.read(prediction_manifest["generation_id"], prediction_manifest["decision_date"])
    assert loaded_manifest["generation_id"] == prediction_manifest["generation_id"]
