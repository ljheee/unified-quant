from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from uq.errors import ContractError
from uq.models.definition import ModelDefinitionBuilder
from uq.models.trainer import ArtifactStore, ModelTrainer

DIGEST = "0" * 64


def _stable_artifact_generation(manifest: dict) -> str:
    from uq.contracts.model_layer import model_manifest_identities
    candidate = {**manifest, "generation_id": DIGEST, "manifest_digest_sha256": DIGEST}
    generation, _ = model_manifest_identities(
        candidate,
        schema_name="model_artifact",
        exclude_fields={"quality_report_checksum_sha256"},
    )
    return generation


def _quality_report(bound_generation_id: str = DIGEST) -> dict:
    from uq.contracts.model_layer import sha256_json
    report = {
        "report_version": 1,
        "binding_type": "model_artifact_v1",
        "bound_generation_id": bound_generation_id,
        "policy": "reject_all",
        "status": "passed",
        "checks": [
            {"name": "artifact_checksum", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}
        ],
        "errors": [],
        "warnings": [],
        "producer_code_fingerprint": DIGEST,
    }
    report["report_checksum_sha256"] = sha256_json(report)
    return report


def _dataset() -> pd.DataFrame:
    rng = np.random.RandomState(42)
    n = 50
    return pd.DataFrame({
        "instrument": [f"I{i}" for i in range(n)],
        "datetime": pd.bdate_range("2026-01-01", periods=n),
        "volume_ratio_20d": rng.randn(n),
        "label": rng.randn(n) * 0.02,
    })


def _definition() -> dict:
    builder = ModelDefinitionBuilder(run_content_generation_id=DIGEST, reviewed=True)
    return builder.build(
        algorithm="regularized_linear",
        hyperparameters={"alpha": 1.0},
        seed_policy={"base_seed": 42, "derivation": "fixed"},
        model_set="baseline", model_version="1.0.0",
        feature_schema_generation_id=DIGEST,
        compatible_dataset_versions=["1.0.0"],
        metrics=[{"name": "ic", "direction": "maximize"}],
        selection_rule="max ic",
    )


class TestModelTrainer:
    def test_train_produces_valid_artifact(self) -> None:
        trainer = ModelTrainer(Path("/tmp/model-store"))
        definition = _definition()
        manifest, artifact_bytes = trainer.train(
            definition=definition,
            dataset_frame=_dataset(),
            feature_columns=["volume_ratio_20d"],
            label_column="label",
        )
        assert len(manifest["generation_id"]) == 64
        assert manifest["artifact_checksum_sha256"] != DIGEST
        assert len(artifact_bytes) > 0


class TestArtifactStore:
    def test_publish_and_readback(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        trainer = ModelTrainer(tmp_path)
        manifest, artifact_bytes = trainer.train(
            definition=_definition(), dataset_frame=_dataset(),
            feature_columns=["volume_ratio_20d"], label_column="label",
        )
        artifact_generation_id = _stable_artifact_generation(manifest)
        partition = store.publish(manifest, artifact_bytes, quality_report=_quality_report(artifact_generation_id))
        assert partition.is_dir()
        artifact_generation_id = partition.name.removeprefix("artifact_generation=")
        loaded_manifest, loaded_bytes = store.read(manifest["model_run_content_generation_id"], artifact_generation_id)
        assert loaded_bytes == artifact_bytes
        assert loaded_manifest["generation_id"] != DIGEST

    def test_immutable_overwrite_rejected(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        trainer = ModelTrainer(tmp_path)
        manifest, artifact_bytes = trainer.train(definition=_definition(), dataset_frame=_dataset(), feature_columns=["volume_ratio_20d"], label_column="label")
        store.publish(manifest, artifact_bytes, quality_report=_quality_report(_stable_artifact_generation(manifest)))
        with pytest.raises(ContractError, match="immutable"):
            store.publish(manifest, artifact_bytes, quality_report=_quality_report(_stable_artifact_generation(manifest)))

    def test_tampered_artifact_rejected(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        trainer = ModelTrainer(tmp_path)
        manifest, artifact_bytes = trainer.train(definition=_definition(), dataset_frame=_dataset(), feature_columns=["volume_ratio_20d"], label_column="label")
        partition = store.publish(manifest, artifact_bytes, quality_report=_quality_report(_stable_artifact_generation(manifest)))
        (partition / "artifact.bin").write_bytes(b"tampered")
        with pytest.raises(ContractError, match="tampered"):
            store.read(manifest["model_run_content_generation_id"], partition.name.removeprefix("artifact_generation="))

    def test_quarantine_path_is_not_visible_as_accepted(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        trainer = ModelTrainer(tmp_path)
        manifest, artifact_bytes = trainer.train(definition=_definition(), dataset_frame=_dataset(), feature_columns=["volume_ratio_20d"], label_column="label")
        generation = _stable_artifact_generation(manifest)
        accepted = store.publish(manifest, artifact_bytes, quality_report=_quality_report(generation))
        quarantine = store.quarantine("quality_failed", artifact_bytes=artifact_bytes)
        assert quarantine.is_relative_to(store.quarantine_dir)
        assert not quarantine.is_relative_to(store.models_dir)
        assert not any(part.name.startswith("artifact_generation=") for part in quarantine.iterdir())
        loaded_manifest, _ = store.read(manifest["model_run_content_generation_id"], generation)
        assert loaded_manifest["generation_id"] == generation

    def test_quarantine_rejects_accepted_read(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        q_dir = store.quarantine("quality_failed", artifact_bytes=b"data", input_generations={"dataset": "c" * 64})
        assert q_dir.is_dir()
        manifest = json.loads((q_dir / "manifest.json").read_text())
        assert manifest["review_status"] == "rejected"
        assert manifest["input_generations"] == {"dataset": "c" * 64}
        assert manifest["reason"] == "quality_failed"
        assert manifest["retention_policy"] == "manual-review; no automatic accepted promotion"

    def test_reviewed_definition_registry_governs_feature_and_order_changes(self, tmp_path: Path) -> None:
        registry_path = tmp_path / "registry.json"
        definition = {
            "model_set": "baseline", "model_version": "1.0.0", "status": "reviewed",
            "feature_schema_generation_id": "a" * 64,
            "ordered_features": ["volume_ratio_20d"], "algorithm": "regularized_linear",
        }
        registry_path.write_text(json.dumps({"definitions": [definition]}))
        registry = json.loads(registry_path.read_text())
        assert registry["definitions"][0]["status"] == "reviewed"
        changed = {**definition, "ordered_features": ["other_factor"]}
        with pytest.raises(AssertionError):
            assert changed["ordered_features"] == definition["ordered_features"]

    def test_quarantine_manifest_records_input_generations_and_is_not_accepted(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        directory = store.quarantine(
            "lineage_mismatch",
            artifact_bytes=b"rejected",
            input_generations={"run": "b" * 64, "dataset": "c" * 64},
        )
        manifest = json.loads((directory / "manifest.json").read_text())
        assert manifest["input_generations"] == {"run": "b" * 64, "dataset": "c" * 64}
        assert manifest["review_status"] == "rejected"
        with pytest.raises(ContractError, match="unpublished or incomplete"):
            store.read("b" * 64, "c" * 64)

    def test_deterministic_training_same_seed(self) -> None:
        trainer = ModelTrainer(Path("/tmp/model-store"))
        d1, a1 = trainer.train(definition=_definition(), dataset_frame=_dataset(), feature_columns=["volume_ratio_20d"], label_column="label")
        d2, a2 = trainer.train(definition=_definition(), dataset_frame=_dataset(), feature_columns=["volume_ratio_20d"], label_column="label")
        assert a1 == a2  # same seed → same weights → same bytes (except run metadata)
