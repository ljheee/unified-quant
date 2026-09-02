from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from tests.review_key import REVIEWER_PRIVATE_KEY
import pytest

from uq.errors import ContractError
from uq.models.definition import ModelDefinitionBuilder
from uq.contracts.model_layer import bind_reviewed_quality_decision, create_reviewed_quality_decision, sha256_json
from uq.models.predictions import PredictionBuilder
from uq.models.trainer import ArtifactStore, ModelTrainer

DIGEST = "0" * 64


def prediction_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="prediction_set_v1", policy="reject_all", status="passed",
        checks=[
            {"name": "finite_scores", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "eligibility_coverage", "threshold": 1, "observed": 1, "level": "error", "result": "passed"},
        ],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
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
            private_key_pem=REVIEWER_PRIVATE_KEY,
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
            run_generation_id=run_generation_id, eligibility_policy="reviewed-v1", eligibility_status="passed",
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
            run_generation_id=run_generation_id, eligibility_policy="reviewed-v1", eligibility_status="passed",
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
                eligibility_policy="reviewed-v1", eligibility_status="passed", decision_date="2026-02-15", scores=scores,
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
                run_generation_id=run_generation_id, eligibility_policy="reviewed-v1", eligibility_status="passed",
                decision_date="2026-02-15", scores=_scores(), quality_decision=prediction_decision(),
            )

    def test_empty_scores_rejected(self, tmp_path) -> None:
        with pytest.raises(ContractError, match="empty"):
            PredictionBuilder(tmp_path).build(
                prediction_set_name="x", artifact_store=None,
                model_artifact_generation_id="a" * 64, model_artifact_checksum="b" * 64,
                input_dataset_generation_id="c" * 64, run_generation_id=DIGEST,
                eligibility_policy="reviewed-v1", eligibility_status="passed", decision_date="2026-02-15",
                scores=pd.DataFrame(columns=["instrument", "datetime", "score"]),
                quality_decision=None,
            )

    def test_prediction_report_missing_wrong_generation_and_failed_reject_publication_read(self, tmp_path: Path) -> None:
        builder, manifest, artifact = self._build_valid(tmp_path)
        partition = builder.publish(manifest, artifact)
        report_path = tmp_path / "external_quality_reviews" / f"{manifest['quality_report_checksum_sha256']}.json"
        original_report = json.loads(report_path.read_text())

        report_path.unlink()
        with pytest.raises(ContractError, match="prediction quality report unavailable"):
            builder.read(manifest["generation_id"], manifest["decision_date"])

        wrong_decision = create_reviewed_quality_decision(
            binding_type="prediction_set_v1", policy="reject_all", status="passed",
            checks=[
                {"name": "finite_scores", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
                {"name": "eligibility_coverage", "threshold": 1, "observed": 1, "level": "error", "result": "passed"},
            ],
            errors=[], warnings=[], producer_code_fingerprint=DIGEST,
            private_key_pem=REVIEWER_PRIVATE_KEY,
        )
        wrong_generation, _ = bind_reviewed_quality_decision(
            wrong_decision, binding_type="prediction_set_v1",
            subject_generation_id="f" * 64, subject_content_sha256="f" * 64,
        )
        report_path.write_text(json.dumps(wrong_generation, sort_keys=True))
        with pytest.raises(ContractError, match="prediction quality report rejects read|model quality review signature mismatch"):
            builder.read(manifest["generation_id"], manifest["decision_date"])

        data_path = partition / "data.parquet"
        data_bytes = data_path.read_bytes()
        report_path.write_text(json.dumps(original_report, sort_keys=True))
        data_path.write_bytes(b"tampered")
        with pytest.raises(ContractError, match="tampered prediction data"):
            builder.read(manifest["generation_id"], manifest["decision_date"])
        data_path.write_bytes(data_bytes)

        failed_decision = create_reviewed_quality_decision(
            binding_type="prediction_set_v1", policy="reject_all", status="passed",
            checks=[
                {"name": "finite_scores", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
                {"name": "eligibility_coverage", "threshold": 1, "observed": 1, "level": "error", "result": "passed"},
            ],
            errors=[], warnings=[], producer_code_fingerprint=DIGEST,
            private_key_pem=REVIEWER_PRIVATE_KEY,
        )
        failed_report = {
            **failed_decision,
            "bound_generation_id": manifest["generation_id"],
            "subject_content_sha256": manifest["generation_id"],
            "status": "rejected",
        }
        failed_report["report_checksum_sha256"] = sha256_json(failed_report)
        report_path.write_text(json.dumps(failed_report, sort_keys=True))
        tampered = {**manifest, "quality_report_checksum_sha256": failed_report["report_checksum_sha256"]}
        tampered["manifest_digest_sha256"] = sha256_json(tampered)
        with pytest.raises(ContractError, match="prediction quality report rejects read|model quality review signature mismatch"):
            builder.read(tampered["generation_id"], tampered["decision_date"])

    def test_prediction_eligibility_policy_and_status_fail_closed(self, tmp_path: Path) -> None:
        builder = PredictionBuilder(tmp_path)
        artifact_generation_id, checksum, run_generation_id = self._publish_artifact(tmp_path)
        for status in ("rejected", "unknown"):
            with pytest.raises(ContractError, match="eligibility policy and passed status"):
                builder.build(
                    prediction_set_name="blocked", artifact_store=None,
                    model_artifact_generation_id=artifact_generation_id,
                    model_artifact_checksum=checksum, input_dataset_generation_id="c" * 64,
                    run_generation_id=run_generation_id, decision_date="2026-02-15",
                    scores=_scores(), eligibility_policy="reviewed-v1", eligibility_status=status, quality_decision=prediction_decision(),
                )
        missing_eligibility_decision = create_reviewed_quality_decision(
            binding_type="prediction_set_v1", policy="reject_all", status="passed",
            checks=[{"name": "finite_scores", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
            errors=[], warnings=[], producer_code_fingerprint=DIGEST,
            private_key_pem=REVIEWER_PRIVATE_KEY,
        )
        with pytest.raises(ContractError, match="passed eligibility_coverage"):
            builder.build(
                prediction_set_name="blocked", artifact_store=None,
                model_artifact_generation_id=artifact_generation_id,
                model_artifact_checksum=checksum, input_dataset_generation_id="c" * 64,
                run_generation_id=run_generation_id, decision_date="2026-02-15",
                scores=_scores(), eligibility_policy="reviewed-v1", eligibility_status="passed",
                quality_decision=missing_eligibility_decision,
            )
        with pytest.raises(ContractError, match="unsupported reviewed prediction"):
            builder.build(
                prediction_set_name="blocked", artifact_store=None,
                model_artifact_generation_id=artifact_generation_id,
                model_artifact_checksum=checksum, input_dataset_generation_id="c" * 64,
                run_generation_id=run_generation_id, decision_date="2026-02-15",
                scores=_scores(), eligibility_policy="reviewed-v1", eligibility_status="passed",
                score_semantics={"unit": "raw_score", "direction": "higher_better", "ranking_scope": "global", "tie_policy": "instrument_order", "normalization": "none"},
                quality_decision=prediction_decision(),
            )
