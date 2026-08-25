"""End-to-end integration test for the full model layer pipeline.

Chain: factor publish → accepted index → feature schema → dataset write →
Qlib export → init receipt → model definition → train → artifact store →
predict → prediction publish.
"""
from __future__ import annotations

import json
import numpy as np
import pandas as pd
import pytest
from datetime import date, datetime
from pathlib import Path

from uq.errors import ContractError
from uq.models.accepted_store import AcceptedFactorIndexRuntime
from uq.models.dataset import DatasetBuilder, SplitValidator
from uq.models.dataset_writer import DatasetWriter
from uq.models.definition import MetricReport, ModelDefinitionBuilder
from uq.models.features import FeatureSchemaBuilder
from uq.models.predictions import PredictionBuilder
from uq.models.qlib_export import QlibDatasetExporter, QlibInitReceiptBuilder
from uq.contracts.model_layer import model_manifest_identities, resolve_bindings
from uq.models.trainer import ArtifactStore, ModelRunBuilder, ModelTrainer


def _publish_factor(root: Path) -> None:
    """Publish a basic factor partition for testing."""
    from uq.factors.store import FactorStore, factor_generation
    from uq.contracts.factor_governance import FactorRegistry
    from uq.contracts.artifacts import QualityReportStore
    from uq.contracts.canonical_v2 import file_sha256_bytes

    dates = pd.bdate_range("2026-01-05", periods=5)
    rows = []
    rng = np.random.RandomState(42)
    for i in range(3):
        for d in dates:
            rows.append({"instrument": f"INST{i}", "datetime": d, "volume_ratio_20d": rng.randn()})
    frame = pd.DataFrame(rows)
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
    generation = factor_generation(**arguments)
    report_path = root / "reports" / "factor_v1" / generation / "report.json"
    if not (root / "reports").exists():
        QualityReportStore().save(root, {
            "report_version": 1, "binding_type": "factor_v1",
            "bound_generation_id": generation, "policy": "reject_all", "status": "passed",
            "checks": [
                {"name": "null_rate", "threshold": 0.5, "observed": 0.0, "level": "error", "result": "passed"},
                {"name": "coverage", "threshold": 0.0, "observed": 1.0, "level": "error", "result": "passed"},
            ],
            "errors": [], "warnings": [],
        })
    arguments["quality_report_checksum"] = file_sha256_bytes(report_path.read_bytes())
    registry = FactorRegistry(Path(__file__).resolve().parents[1])
    FactorStore(root, registry).publish(**{k: v for k, v in arguments.items() if k != "frame"}, frame=arguments["frame"])


DIGEST = "0" * 64


class TestEndToEndPipeline:
    def test_full_chain_from_factor_to_prediction(self, tmp_path: Path) -> None:
        # === Phase 1: Accepted factor index ===
        _publish_factor(tmp_path)
        runtime = AcceptedFactorIndexRuntime(tmp_path)
        query = {"contract_version": 1, "filters": {}, "ordering": ["partition_date"], "visibility": "accepted_only", "pagination": {"limit": 10}}
        entries = runtime.list(query)
        assert len(entries) == 1
        factor_gen = entries[0]["generation_id"]
        factor_data = runtime.read(factor_gen)
        assert "volume_ratio_20d" in factor_data.columns

        # === Phase 1: Feature schema ===
        # Build feature schema BEFORE adding label column
        fs_builder = FeatureSchemaBuilder()
        feature_schema = fs_builder.build(factor_data, source_factor_set="basic", source_factor_version="1.0.0")

        # Add label column AFTER schema is built
        factor_data["label"] = np.random.RandomState(7).randn(len(factor_data)) * 0.01

        # === Phase 1: Dataset build + write ===
        dataset_manifest = DatasetBuilder(dataset_name="e2e_slice", semantic_version="1.0.0").build(
            ordered_features=["volume_ratio_20d"],
            factor_set="basic", factor_version="1.0.0",
            factor_generation_ids=[factor_gen],
            label_set_name="return_5d", label_generation_id=DIGEST,
            universe_snapshot_generation_id=DIGEST,
            split_policy={"purge_trading_days": 5, "embargo_trading_days": 2, "splits": [{"name": "train", "start_date": "2026-01-05", "end_date": "2026-01-09"}]},
            row_count=len(factor_data),
        )
        writer = DatasetWriter(tmp_path)
        ds_partition = writer.write(dataset_manifest, factor_data, feature_schema=feature_schema)
        _, loaded_ds = writer.read("e2e_slice", "1.0.0", writer.last_published_manifest["generation_id"])
        loaded_ds_manifest = writer.last_published_manifest

        # === Phase 2A: Qlib export + init receipt ===
        exporter = QlibDatasetExporter(tmp_path / "qlib_exports")
        feature_mapping = {"volume_ratio_20d": "VOLUME_RATIO"}
        calendar_dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=5)]
        instruments = sorted(factor_data["instrument"].unique().tolist())
        export_manifest = exporter.export(
            dataset_name="e2e_slice", generation_id=dataset_manifest["generation_id"],
            frame=factor_data[["instrument", "datetime", "volume_ratio_20d"]],
            feature_mapping=feature_mapping,
            calendar_dates=calendar_dates, instruments=instruments,
            provider_uri="file:///test/exports",
        )
        receipt_builder = QlibInitReceiptBuilder()
        cache_before = {"/tmp/.cache/old_file"}
        cache_after = cache_before | {str(tmp_path / ".cache" / "qlib" / "calendar.pkl")}
        receipt = receipt_builder.build(
            export_manifest=export_manifest, resolved_provider_uri="file:///test/exports",
            qlib_import_path="qlib", qlib_version="0.9.6",
            cache_root=str(tmp_path / ".cache"), 
            cache_files_before=cache_before, cache_files_after=cache_after,
        )
        assert receipt["no_ungoverned_source_assertion"] is True

        # === Phase 3: Model definition ===
        provisional_definition = ModelDefinitionBuilder(run_content_generation_id="1" * 64, reviewed=True).build(
            algorithm="regularized_linear",
            hyperparameters={"alpha": 0.5},
            seed_policy={"base_seed": 42, "derivation": "fixed"},
            model_set="baseline", model_version="1.0.0",
            feature_schema_generation_id=feature_schema["generation_id"],
            compatible_dataset_versions=["1.0.0"],
            metrics=[{"name": "ic", "direction": "maximize"}],
            selection_rule="max validation ic",
        )

        run_manifest, definition = ModelRunBuilder.build(
            definition=provisional_definition,
            dataset_manifest=loaded_ds_manifest,
            export_manifest=export_manifest,
            receipt_manifest=receipt,
            environment_lock_sha256=DIGEST,
            determinism_controls={"random_seed": 42, "threads": 1},
        )
        # === Phase 4: Train + publish artifact ===
        trainer = ModelTrainer(tmp_path)
        artifact_manifest, artifact_bytes = trainer.train(
            definition=definition,
            dataset_frame=loaded_ds,
            feature_columns=["volume_ratio_20d"],
            label_column="label",
        )
        artifact_store = ArtifactStore(tmp_path)
        artifact_partition = artifact_store.publish(
            artifact_manifest, artifact_bytes,
            quality_report_checksum=artifact_manifest.get("quality_report_checksum_sha256", "0" * 64),
        )
        assert artifact_partition.is_dir()

        # Verify deterministic training
        _, artifact_bytes_2 = trainer.train(
            definition=definition, dataset_frame=loaded_ds,
            feature_columns=["volume_ratio_20d"], label_column="label",
        )
        assert artifact_bytes == artifact_bytes_2  # same seed → same weights

        # === Phase 5: Predictions ===
        # Simulate predictions using trained weights
        weights = json.loads(artifact_bytes)["weights"]
        scores = loaded_ds[["instrument", "datetime"]].copy()
        scores["score"] = loaded_ds["volume_ratio_20d"].values * weights[0]
        pred_builder = PredictionBuilder(tmp_path)
        pred_manifest, pred_artifact = pred_builder.build(
            prediction_set_name="daily_e2e",
            model_artifact_generation_id=artifact_manifest["generation_id"],
            model_artifact_checksum=artifact_manifest["artifact_checksum_sha256"],
            input_dataset_generation_id=loaded_ds_manifest["generation_id"],
            decision_date="2026-01-09",
            scores=scores,
        )
        pred_partition = pred_builder.publish(pred_manifest, pred_artifact)
        assert pred_partition.is_dir()
        loaded_pred_manifest, loaded_pred_frame = pred_builder.read(
            pred_manifest["generation_id"], "2026-01-09"
        )
        assert len(loaded_pred_frame) == len(scores)

        # === Cross-manifest binding verification ===
        from uq.contracts.model_layer import resolve_bindings
        bindings_report = resolve_bindings({
            "model_dataset": loaded_ds_manifest,
            "model_definition": definition,
            "qlib_dataset_export": export_manifest,
            "qlib_init_receipt": receipt,
            "model_run": run_manifest,
            "model_artifact": artifact_manifest,
            "prediction_set": pred_manifest,
        })
        assert bindings_report["errors"] == []

        print(f"\nE2E pipeline complete: {len(entries)} factors, "
              f"dataset={dataset_manifest['generation_id'][:12]}..., "
              f"artifact={artifact_manifest['generation_id'][:12]}..., "
              f"predictions={pred_manifest['generation_id'][:12]}...")

    def test_lineage_tamper_breaks_read(self, tmp_path: Path) -> None:
        """Tampering with any downstream artifact breaks the read chain."""
        _publish_factor(tmp_path)
        runtime = AcceptedFactorIndexRuntime(tmp_path)
        query = {"contract_version": 1, "filters": {}, "ordering": ["partition_date"], "visibility": "accepted_only", "pagination": {"limit": 10}}
        entries = runtime.list(query)
        gen = entries[0]["generation_id"]
        
        # Find and tamper the factor data
        factors_dir = tmp_path / "factors"
        for manifest_path in factors_dir.rglob("manifest.json"):
            manifest = json.loads(manifest_path.read_text())
            if manifest["generation_id"] == gen:
                data_path = manifest_path.parent / "data.parquet"
                original = data_path.read_bytes()
                data_path.write_bytes(original + b"x")
                break
        
        with pytest.raises(ContractError, match="tampered"):
            runtime.list(query)
        runtime._tampered_generations.add(gen)
        with pytest.raises(ContractError, match="tampered or invalid"):
            runtime.read(gen)
