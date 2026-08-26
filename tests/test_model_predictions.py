from __future__ import annotations

import json

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.models.definition import ModelDefinitionBuilder
from uq.contracts.model_layer import create_reviewed_quality_decision
from uq.models.predictions import PredictionBuilder
from uq.models.trainer import ArtifactStore, ModelTrainer

DIGEST = "0" * 64


def prediction_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="prediction_set_v1", policy="reject_all", status="passed",
        checks=[{"name": "finite_scores", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
    )


def _scores(n: int = 10) -> pd.DataFrame:
    import numpy as np
    rng = np.random.RandomState(42)
    return pd.DataFrame({
        "instrument": [f"I{i}" for i in range(n)],
        "datetime": pd.bdate_range("2026-02-01", periods=n),
        "score": rng.randn(n),
    })


class TestPredictionBuilder:
    def _publish_artifact(self, root):
        definition = ModelDefinitionBuilder(run_content_generation_id=DIGEST, reviewed=True).build(
            algorithm="regularized_linear", hyperparameters={"alpha": 1.0},
            seed_policy={"base_seed": 42, "derivation": "fixed"}, model_set="baseline",
            model_version="1.0.0", feature_schema_generation_id=DIGEST,
            compatible_dataset_versions=["1.0.0"], metrics=[{"name": "ic", "direction": "maximize"}],
            selection_rule="max ic",
        )
        rng = __import__("numpy").random.RandomState(42)
        frame = pd.DataFrame({
            "instrument": [f"I{i}" for i in range(10)],
            "datetime": pd.bdate_range("2026-01-01", periods=10),
            "feature": rng.randn(10), "label": rng.randn(10) * 0.01,
        })
        artifact_manifest, artifact_bytes = ModelTrainer(root).train(
            definition=definition, dataset_frame=frame,
            feature_columns=["feature"], label_column="label",
        )
        from uq.contracts.model_layer import model_manifest_identities, sha256_json
        candidate = {**artifact_manifest, "generation_id": DIGEST, "manifest_digest_sha256": DIGEST}
        generation, _ = model_manifest_identities(candidate, schema_name="model_artifact", exclude_fields={"quality_report_checksum_sha256"})
        from uq.contracts.model_layer import bind_reviewed_quality_decision, create_reviewed_quality_decision
        decision = create_reviewed_quality_decision(
            binding_type="model_artifact_v1", policy="reject_all", status="passed",
            checks=[{"name": "artifact_checksum", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
            errors=[], warnings=[], producer_code_fingerprint=DIGEST,
        )
        report, _ = bind_reviewed_quality_decision(
            decision, binding_type="model_artifact_v1",
            subject_generation_id=generation, subject_content_sha256=generation,
        )
        partition = ArtifactStore(root).publish(artifact_manifest, artifact_bytes, quality_report=report)
        return (
            partition.name.removeprefix("artifact_generation="),
            artifact_manifest["artifact_checksum_sha256"],
            artifact_manifest["model_run_content_generation_id"],
        )

    def _prepare_dataset_marker(self, tmp_path) -> None:
        path = tmp_path / "datasets" / f"dataset={'c' * 64}" / "generation=placeholder" / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}")

    def _build_valid(self, tmp_path, name: str = "x"):
        builder = PredictionBuilder(tmp_path)
        artifact_generation_id, checksum, run_generation_id = self._publish_artifact(tmp_path)
        self._prepare_dataset_marker(tmp_path)
        manifest, artifact = builder.build(
            prediction_set_name=name, artifact_store=None,
            model_artifact_generation_id=artifact_generation_id,
            model_artifact_checksum=checksum, input_dataset_generation_id="c" * 64,
            run_generation_id=run_generation_id, eligibility_status="passed",
            decision_date="2026-02-15", scores=_scores(), quality_decision=prediction_decision(),
        )
        return builder, manifest, artifact

    def test_build_and_publish(self, tmp_path) -> None:
        builder = PredictionBuilder(tmp_path)
        artifact_generation_id, checksum, run_generation_id = self._publish_artifact(tmp_path)
        self._prepare_dataset_marker(tmp_path)
        manifest, artifact = builder.build(
            prediction_set_name="daily", artifact_store=None,
            model_artifact_generation_id=artifact_generation_id,
            model_artifact_checksum=checksum, input_dataset_generation_id="c" * 64,
            run_generation_id=run_generation_id, eligibility_status="passed",
            decision_date="2026-02-15", scores=_scores(), quality_decision=prediction_decision(),
        )
        assert len(manifest["generation_id"]) == 64
        assert builder.publish(manifest, artifact).is_dir()

    def test_read_published(self, tmp_path) -> None:
        builder, manifest, artifact = self._build_valid(tmp_path, "daily")
        builder.publish(manifest, artifact)
        loaded_manifest, loaded_frame = builder.read(manifest["generation_id"], "2026-02-15")
        assert len(loaded_frame) == 10

    def test_non_finite_score_rejected(self, tmp_path) -> None:
        builder = PredictionBuilder(tmp_path)
        scores = _scores(5)
        scores.loc[2, "score"] = float("nan")
        with pytest.raises(ContractError, match="non-finite"):
            builder.build(
                prediction_set_name="x", artifact_store=ArtifactStore(tmp_path),
                model_artifact_generation_id="a" * 64, model_artifact_checksum="b" * 64,
                input_dataset_generation_id="c" * 64, run_generation_id=DIGEST,
                eligibility_status="passed", decision_date="2026-02-15", scores=scores,
                quality_decision=prediction_decision(),
            )

    def test_immutable_overwrite_rejected(self, tmp_path) -> None:
        builder, manifest, artifact = self._build_valid(tmp_path)
        builder.publish(manifest, artifact)
        with pytest.raises(ContractError, match="immutable"):
            builder.publish(manifest, artifact)

    def test_tampered_data_rejected_on_read(self, tmp_path) -> None:
        builder, manifest, artifact = self._build_valid(tmp_path)
        partition = builder.publish(manifest, artifact)
        (partition / "data.parquet").write_bytes(b"tampered")
        with pytest.raises(ContractError, match="tampered"):
            builder.read(manifest["generation_id"], "2026-02-15")

    def test_tampered_artifact_before_build_rejected(self, tmp_path) -> None:
        builder = PredictionBuilder(tmp_path)
        artifact_generation_id, checksum, run_generation_id = self._publish_artifact(tmp_path)
        partition = tmp_path / "models" / f"run_generation={run_generation_id}" / f"artifact_generation={artifact_generation_id}"
        (partition / "artifact.bin").write_bytes(b"tampered")
        with pytest.raises(ContractError, match="accepted model artifact|tampered"):
            builder.build(
                prediction_set_name="blocked", artifact_store=None,
                model_artifact_generation_id=artifact_generation_id,
                model_artifact_checksum=checksum, input_dataset_generation_id="c" * 64,
                run_generation_id=run_generation_id, eligibility_status="passed",
                decision_date="2026-02-15", scores=_scores(), quality_decision=prediction_decision(),
            )

    def test_empty_scores_rejected(self, tmp_path) -> None:
        with pytest.raises(ContractError, match="empty"):
            PredictionBuilder(tmp_path).build(
                prediction_set_name="x", artifact_store=None,
                model_artifact_generation_id="a" * 64, model_artifact_checksum="b" * 64,
                input_dataset_generation_id="c" * 64, run_generation_id=DIGEST,
                eligibility_status="passed", decision_date="2026-02-15",
                scores=pd.DataFrame(columns=["instrument", "datetime", "score"]),
                quality_decision=None,
            )
