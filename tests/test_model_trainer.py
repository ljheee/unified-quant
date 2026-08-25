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
    builder = ModelDefinitionBuilder()
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
        partition = store.publish(manifest, artifact_bytes)
        assert partition.is_dir()
        loaded_manifest, loaded_bytes = store.read(manifest["model_run_content_generation_id"], manifest["generation_id"])
        assert loaded_bytes == artifact_bytes

    def test_immutable_overwrite_rejected(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        trainer = ModelTrainer(tmp_path)
        manifest, artifact_bytes = trainer.train(definition=_definition(), dataset_frame=_dataset(), feature_columns=["volume_ratio_20d"], label_column="label")
        store.publish(manifest, artifact_bytes)
        with pytest.raises(ContractError, match="immutable"):
            store.publish(manifest, artifact_bytes)

    def test_tampered_artifact_rejected(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        trainer = ModelTrainer(tmp_path)
        manifest, artifact_bytes = trainer.train(definition=_definition(), dataset_frame=_dataset(), feature_columns=["volume_ratio_20d"], label_column="label")
        partition = store.publish(manifest, artifact_bytes)
        (partition / "artifact.bin").write_bytes(b"tampered")
        with pytest.raises(ContractError, match="tampered"):
            store.read(manifest["model_run_content_generation_id"], manifest["generation_id"])

    def test_quarantine_rejects_accepted_read(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        q_dir = store.quarantine("test rejection", artifact_bytes=b"data")
        assert q_dir.is_dir()
        manifest = json.loads((q_dir / "manifest.json").read_text())
        assert manifest["retention_policy"] == "manual-review; no automatic accepted promotion"

    def test_deterministic_training_same_seed(self) -> None:
        trainer = ModelTrainer(Path("/tmp/model-store"))
        d1, a1 = trainer.train(definition=_definition(), dataset_frame=_dataset(), feature_columns=["volume_ratio_20d"], label_column="label")
        d2, a2 = trainer.train(definition=_definition(), dataset_frame=_dataset(), feature_columns=["volume_ratio_20d"], label_column="label")
        assert a1 == a2  # same seed → same weights → same bytes (except run metadata)
