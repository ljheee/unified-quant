from __future__ import annotations

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.models.predictions import PredictionBuilder


def _scores(n: int = 10) -> pd.DataFrame:
    import numpy as np
    rng = np.random.RandomState(42)
    return pd.DataFrame({
        "instrument": [f"I{i}" for i in range(n)],
        "datetime": pd.bdate_range("2026-02-01", periods=n),
        "score": rng.randn(n),
    })


class TestPredictionBuilder:
    def test_build_and_publish(self, tmp_path) -> None:
        builder = PredictionBuilder(tmp_path)
        manifest, artifact = builder.build(
            prediction_set_name="daily",
            model_artifact_generation_id="a" * 64,
            model_artifact_checksum="b" * 64,
            input_dataset_generation_id="c" * 64,
            eligibility_status="passed",
            decision_date="2026-02-15",
            scores=_scores(),
        )
        assert len(manifest["generation_id"]) == 64
        partition = builder.publish(manifest, artifact)
        assert partition.is_dir()

    def test_read_published(self, tmp_path) -> None:
        builder = PredictionBuilder(tmp_path)
        manifest, artifact = builder.build(
            prediction_set_name="daily",
            model_artifact_generation_id="a" * 64,
            model_artifact_checksum="b" * 64,
            input_dataset_generation_id="c" * 64,
            eligibility_status="passed",
            decision_date="2026-02-15",
            scores=_scores(),
        )
        builder.publish(manifest, artifact)
        loaded_manifest, loaded_frame = builder.read(manifest["generation_id"], "2026-02-15")
        assert len(loaded_frame) == 10

    def test_non_finite_score_rejected(self, tmp_path) -> None:
        builder = PredictionBuilder(tmp_path)
        scores = _scores(5)
        scores.loc[2, "score"] = float("nan")
        with pytest.raises(ContractError, match="non-finite"):
            builder.build(
                prediction_set_name="x", model_artifact_generation_id="a" * 64,
                model_artifact_checksum="b" * 64, input_dataset_generation_id="c" * 64,
                eligibility_status="passed", decision_date="2026-02-15", scores=scores,
            )

    def test_immutable_overwrite_rejected(self, tmp_path) -> None:
        builder = PredictionBuilder(tmp_path)
        manifest, artifact = builder.build(
            prediction_set_name="x", model_artifact_generation_id="a" * 64,
            model_artifact_checksum="b" * 64, input_dataset_generation_id="c" * 64,
            eligibility_status="passed", decision_date="2026-02-15", scores=_scores(),
        )
        builder.publish(manifest, artifact)
        with pytest.raises(ContractError, match="immutable"):
            builder.publish(manifest, artifact)

    def test_tampered_data_rejected_on_read(self, tmp_path) -> None:
        builder = PredictionBuilder(tmp_path)
        manifest, artifact = builder.build(
            prediction_set_name="x", model_artifact_generation_id="a" * 64,
            model_artifact_checksum="b" * 64, input_dataset_generation_id="c" * 64,
            eligibility_status="passed", decision_date="2026-02-15", scores=_scores(),
        )
        partition = builder.publish(manifest, artifact)
        (partition / "data.parquet").write_bytes(b"tampered")
        with pytest.raises(ContractError, match="tampered"):
            builder.read(manifest["generation_id"], "2026-02-15")

    def test_empty_scores_rejected(self, tmp_path) -> None:
        builder = PredictionBuilder(tmp_path)
        with pytest.raises(ContractError, match="empty"):
            builder.build(
                prediction_set_name="x", model_artifact_generation_id="a" * 64,
                model_artifact_checksum="b" * 64, input_dataset_generation_id="c" * 64,
                eligibility_status="passed", decision_date="2026-02-15", scores=pd.DataFrame(columns=["instrument", "datetime", "score"]),
            )
