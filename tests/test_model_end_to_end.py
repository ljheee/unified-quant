"""End-to-end integration test for the full model layer pipeline.

Chain: factor publish → accepted index → feature schema → dataset write →
Qlib export → init receipt → model definition → train → artifact store →
predict → prediction publish.
"""
from __future__ import annotations

import json
import numpy as np
import pandas as pd
from review_key import REVIEWER_PRIVATE_KEY
import pytest
from datetime import date, datetime
from pathlib import Path

from uq.errors import ContractError
from uq.models.accepted_store import AcceptedFactorIndexRuntime
from uq.models.dataset import DatasetBuilder, SplitValidator
from uq.models.dataset_writer import DatasetWriter
from uq.models.definition import MetricReport, ModelDefinitionBuilder
from uq.models.labels import LabelBuilder
from uq.models.features import FeatureSchemaBuilder
from uq.models.predictions import PredictionBuilder
from uq.models.qlib_export import QlibDatasetExporter, QlibInitReceiptBuilder
from uq.contracts.model_layer import bind_reviewed_quality_decision, create_reviewed_quality_decision, sha256_json, resolve_bindings
from uq.models.trainer import ArtifactStore, ModelRunBuilder, ModelTrainer

try:
    import qlib
except ImportError:
    qlib = None


def _publish_factor(root: Path) -> None:
    """Publish a basic factor partition for testing."""
    from uq.factors.store import FactorStore, factor_generation, _validate_factor_frame
    from uq.contracts.factor_governance import FactorRegistry
    from uq.contracts.artifacts import QualityReportStore
    from uq.contracts.canonical_v2 import file_sha256_bytes

    dates = pd.bdate_range("2026-01-05", periods=20)
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
    registry = FactorRegistry(Path(__file__).resolve().parents[1])
    generation = factor_generation(**arguments)
    report_path = root / "reports" / "factor_v1" / generation / "report.json"
    if not (root / "reports").exists():
        QualityReportStore().save(root, {
            "report_version": 1, "binding_type": "factor_v1",
            "bound_generation_id": generation, "policy": "reject_all", "status": "passed",
            "checks": _validate_factor_frame(frame, registry.get("basic", "1.0.0"))["checks"],
            "errors": [], "warnings": [],
        })
    arguments["quality_report_checksum"] = file_sha256_bytes(report_path.read_bytes())
    FactorStore(root, registry).publish(**{k: v for k, v in arguments.items() if k != "frame"}, frame=arguments["frame"])


def _load_factor_document(root: Path, generation_id: str) -> dict:
    for manifest_path in (root / "factors").rglob("manifest.json"):
        document = json.loads(manifest_path.read_text())
        if document.get("generation_id") == generation_id:
            return document
    raise AssertionError("factor manifest not found")


def _publish_universe(root: Path) -> dict:
    import hashlib

    members_artifact_bytes = b"INST0\nINST1\nINST2\n"
    members_artifact = {"path": "members.csv", "checksum_sha256": hashlib.sha256(members_artifact_bytes).hexdigest()}
    payload = {
        "universe_version": 1, "universe_id": "e2e-whitelist",
        "source": "test://e2e-whitelist",
        "snapshot_time": "2026-01-05T00:00:00Z",
        "visibility_time": "2026-01-05T00:00:00Z",
        "valid_from": "2026-01-05", "valid_to": None,
        "members_artifact": members_artifact,
        "membership_evidence": "deterministic E2E fixture; no live index membership",
    }
    generation_id = sha256_json(payload)
    members_path = root / "universes" / "e2e-whitelist" / generation_id / "members.csv"
    members_path.parent.mkdir(parents=True, exist_ok=True)
    members_path.write_bytes(members_artifact_bytes)
    return {**payload, "generation_id": generation_id}


DIGEST = "0" * 64


def dataset_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="model_dataset_v1", policy="reject_all", status="passed",
        checks=[{"name": "row_count", "threshold": 0, "observed": 60, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def export_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="qlib_dataset_export_v1", policy="reject_all", status="passed",
        checks=[{"name": "export_files_verified", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def receipt_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="qlib_init_receipt_v1", policy="reject_all", status="passed",
        checks=[{"name": "runtime_cache_boundary", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def run_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="model_run_v1", policy="reject_all", status="passed",
        checks=[{"name": "upstream_lineage_resolved", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )


def prediction_decision() -> dict:
    return create_reviewed_quality_decision(
        binding_type="prediction_set_v1", policy="reject_all", status="passed",
        checks=[{"name": "finite_scores", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
        errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )



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
        adjusted_frame = factor_data.copy()
        rng = np.random.RandomState(7)
        adjusted_frame["close"] = 10.0 + rng.randn(len(factor_data)) * 0.1
        adjusted_frame["adj_factor"] = 1.0
        adjusted_frame["limit_up"] = False
        adjusted_frame["limit_down"] = False
        adjusted_frame["delisted"] = False
        adjusted_frame["suspended"] = False
        adjusted_frame["listing_date"] = pd.Timestamp("2020-01-01", tz="UTC")
        adjusted_frame["label"] = 0.01
        label_manifest = LabelBuilder(name="return_5d", semantic_version="1.0.0").build(
            adjusted_frame,
            upstream_bindings=[{
                "binding": "adjusted_price",
                "dataset": "e2e_adjusted_bars",
                "schema_version": "adjusted-v1",
                "partition_date": "2026-01-09",
                "generation_id": DIGEST,
                "data_checksum_sha256": sha256_json({"rows": [
                    [str(row[0]), pd.Timestamp(row[1]).isoformat(), float(row[2]), float(row[3]), bool(row[4]), str(pd.Timestamp(row[5]).date())]
                    for row in adjusted_frame[[
                        "instrument", "datetime", "close", "adj_factor", "suspended", "listing_date"
                    ]].sort_values(["instrument", "datetime"], kind="mergesort").itertuples(index=False)
                ]}),
                "visible_cutoff": "2026-01-09T15:00:00+08:00",
            }],
        )
        label_frame = adjusted_frame.sort_values(["instrument", "datetime"], kind="mergesort").copy()
        label_frame["label"] = (
            (label_frame["close"] * label_frame["adj_factor"]).groupby(label_frame["instrument"], sort=False)
            .transform(lambda values: values.shift(-1) / values - 1)
        )
        factor_data["label"] = label_frame["label"].to_numpy()

        universe_manifest = _publish_universe(tmp_path)
        factor_document = _load_factor_document(tmp_path, factor_gen)

        # === Phase 1: Dataset build + write ===
        dataset_manifest = DatasetBuilder(dataset_name="e2e_slice", semantic_version="1.0.0").build(
            ordered_features=["volume_ratio_20d"],
            factor_set="basic", factor_version="1.0.0",
            factor_generation_ids=[factor_gen],
            label_set_name="return_5d", label_generation_id=label_manifest["generation_id"],
            universe_snapshot_generation_id=universe_manifest["generation_id"],
            split_policy={"purge_trading_days": 5, "embargo_trading_days": 2, "splits": [{"name": "train", "start_date": "2026-01-05", "end_date": "2026-01-12"}, {"name": "validation", "start_date": "2026-01-23", "end_date": "2026-01-26"}]},
            row_count=len(factor_data),
        )
        writer = DatasetWriter(tmp_path)
        ds_partition = writer.write(dataset_manifest, factor_data, feature_schema=feature_schema, quality_report=dataset_decision())
        _, loaded_ds = writer.read("e2e_slice", "1.0.0", writer.last_published_manifest["generation_id"])
        loaded_ds_manifest = writer.last_published_manifest

        # === Phase 2A: Qlib export + init receipt ===
        exporter = QlibDatasetExporter(tmp_path / "qlib_exports")
        feature_mapping = {"volume_ratio_20d": "VOLUME_RATIO"}
        calendar_dates = sorted(factor_data["datetime"].dt.strftime("%Y-%m-%d").unique().tolist())
        instruments = sorted(factor_data["instrument"].unique().tolist())
        export_manifest = exporter.export(
            dataset_name="e2e_slice", generation_id=dataset_manifest["generation_id"],
            frame=factor_data[["instrument", "datetime", "volume_ratio_20d", "label"]].fillna({"volume_ratio_20d": 0.0, "label": 0.0}),
            feature_mapping=feature_mapping,
            label_column="label", label_mapping="LABEL_5D",
            calendar_dates=calendar_dates, instruments=instruments,
            provider_uri="file:///test/exports", quality_decision=export_decision(),
        )
        receipt_builder = QlibInitReceiptBuilder()
        cache_before = {"/tmp/.cache/old_file"}
        cache_after = cache_before | {str(tmp_path / ".cache" / "qlib" / "calendar.pkl")}
        receipt = receipt_builder.build(
            export_manifest=export_manifest, resolved_provider_uri="file:///test/exports",
            qlib_import_path="qlib", qlib_version=(qlib.__version__ if qlib is not None else "0.9.6"),
            cache_root=str(tmp_path / ".cache"), 
            cache_files_before=cache_before, cache_files_after=cache_after,
            verified_export=exporter.read("e2e_slice", export_manifest["generation_id"]),
            governance_root=tmp_path, quality_decision=receipt_decision(),
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
            label_manifest=label_manifest,
            universe_snapshot=universe_manifest,
            factor_manifests={factor_gen: factor_document},
            quality_decision=run_decision(), store_root=tmp_path,
        )

        # === Phase 4A: Real Qlib runtime training ===
        if qlib is not None:
            from uq.models.qlib_runtime import QlibRuntimeTrainer

            qlib_definition_input = ModelDefinitionBuilder(
                run_content_generation_id="1" * 64, reviewed=True
            ).build(
                algorithm="qlib_linear", hyperparameters={"alpha": 0.5},
                seed_policy={"base_seed": 42, "derivation": "fixed"},
                model_set="qlib-baseline", model_version="1.0.0",
                feature_schema_generation_id=feature_schema["generation_id"],
                compatible_dataset_versions=["1.0.0"],
                metrics=[{"name": "ic", "direction": "maximize"}],
                selection_rule="max validation ic", serializer_version="joblib-v1",
            )
            qlib_run_manifest, qlib_definition = ModelRunBuilder.build(
                definition=qlib_definition_input,
                dataset_manifest=loaded_ds_manifest,
                export_manifest=export_manifest,
                receipt_manifest=receipt,
                environment_lock_sha256=DIGEST,
                determinism_controls={"random_seed": 42, "threads": 1},
                label_manifest=label_manifest,
                universe_snapshot=universe_manifest,
                factor_manifests={factor_gen: factor_document},
                quality_decision=run_decision(), store_root=tmp_path,
            )
            qlib_manifest, qlib_bytes = QlibRuntimeTrainer().train(
                definition=qlib_definition,
                dataset_manifest=loaded_ds_manifest,
                export_manifest=export_manifest,
                receipt_manifest=receipt,
                verified_export=exporter.read("e2e_slice", export_manifest["generation_id"]),
                feature_columns=["volume_ratio_20d"], label_column="label",
            )
            assert qlib_manifest["runtime_name"] == "qlib_linear"
            assert qlib_manifest["runtime_version"] == qlib.__version__
            assert "features/inst0/volume_ratio.day.bin" in {entry["path"] for entry in export_manifest["files"]}
            assert "features/inst0/label_5d.day.bin" in {entry["path"] for entry in export_manifest["files"]}

            from uq.contracts.model_layer import model_manifest_identities as _runtime_mmi
            runtime_generation = _runtime_mmi(
                {**qlib_manifest, "generation_id": DIGEST, "manifest_digest_sha256": DIGEST},
                schema_name="model_artifact",
                exclude_fields={"quality_report_checksum_sha256"},
            )[0]
            runtime_report, _ = bind_reviewed_quality_decision(
                create_reviewed_quality_decision(
                    binding_type="model_artifact_v1", policy="reject_all", status="passed",
                    checks=[{"name": "artifact_checksum", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
                    errors=[], warnings=[], producer_code_fingerprint=DIGEST,
                    private_key_pem=REVIEWER_PRIVATE_KEY,
                ),
                binding_type="model_artifact_v1", subject_generation_id=runtime_generation,
                subject_content_sha256=runtime_generation,
            )
            runtime_store = ArtifactStore(tmp_path)
            runtime_partition = runtime_store.publish(
                qlib_manifest, qlib_bytes, quality_report=runtime_report,
            )
            assert (runtime_partition / "model.joblib").is_file()
            loaded_runtime_manifest, loaded_runtime_bytes = runtime_store.read(
                qlib_manifest["model_run_content_generation_id"], runtime_generation,
            )
            assert loaded_runtime_manifest["artifact_filename"] == "model.joblib"
            assert loaded_runtime_bytes == qlib_bytes

        # === Phase 4: Train + publish artifact ===
        trainer = ModelTrainer(tmp_path)
        artifact_manifest, artifact_bytes = trainer.train(
            definition=definition,
            dataset_frame=loaded_ds,
            feature_columns=["volume_ratio_20d"],
            label_column="label",
        )
        artifact_store = ArtifactStore(tmp_path)
        from uq.contracts.model_layer import model_manifest_identities as _mmi
        artifact_generation = _mmi(
            {**artifact_manifest, "generation_id": DIGEST, "manifest_digest_sha256": DIGEST},
            schema_name="model_artifact", exclude_fields={"quality_report_checksum_sha256"},
        )[0]
        artifact_quality_report, _ = bind_reviewed_quality_decision(
            create_reviewed_quality_decision(
                binding_type="model_artifact_v1", policy="reject_all", status="passed",
                checks=[{"name": "artifact_checksum", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
                errors=[], warnings=[], producer_code_fingerprint=DIGEST,
                private_key_pem=REVIEWER_PRIVATE_KEY,
            ),
            binding_type="model_artifact_v1", subject_generation_id=artifact_generation,
            subject_content_sha256=artifact_generation,
        )
        artifact_partition = artifact_store.publish(
            artifact_manifest, artifact_bytes,
            quality_report=artifact_quality_report,
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
            model_artifact_generation_id=artifact_generation,
            model_artifact_checksum=artifact_manifest["artifact_checksum_sha256"],
            input_dataset_generation_id=loaded_ds_manifest["generation_id"],
            run_generation_id=artifact_manifest["model_run_content_generation_id"], artifact_store=None,
            decision_date="2026-01-09",
            scores=scores,
            eligibility_status="passed", quality_decision=prediction_decision(),
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
            "label_set": label_manifest,
            "universe_snapshot": universe_manifest,
            "factor_manifests": {factor_gen: factor_document},
            "model_definition": definition,
            "qlib_dataset_export": export_manifest,
            "qlib_init_receipt": receipt,
            "model_run": run_manifest,
            "model_artifact": {**artifact_manifest, "generation_id": artifact_generation},
            "prediction_set": pred_manifest,
        }, universe_root=tmp_path / "universes")
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


class TestQlibRuntimeTrainer:
    @pytest.mark.skipif(qlib is None, reason="Qlib extra is not installed")
    def test_tampered_provider_bin_rejects_training(self, tmp_path: Path) -> None:
        from uq.models.qlib_runtime import QlibRuntimeTrainer

        _publish_factor(tmp_path)
        runtime = AcceptedFactorIndexRuntime(tmp_path)
        factor_gen = runtime.list({
            "contract_version": 1, "filters": {}, "ordering": ["partition_date"],
            "visibility": "accepted_only", "pagination": {"limit": 10},
        })[0]["generation_id"]
        factor_data = runtime.read(factor_gen).fillna({"volume_ratio_20d": 0.0})
        adjusted_frame = factor_data.copy()
        adjusted_frame["close"] = 10.0
        adjusted_frame["adj_factor"] = 1.0
        adjusted_frame["limit_up"] = False
        adjusted_frame["limit_down"] = False
        adjusted_frame["delisted"] = False
        adjusted_frame["suspended"] = False
        adjusted_frame["listing_date"] = pd.Timestamp("2020-01-01", tz="UTC")
        adjusted_frame["label"] = 0.01
        label_manifest = LabelBuilder(name="return_5d", semantic_version="1.0.0").build(
            adjusted_frame,
            upstream_bindings=[{
                "binding": "adjusted_price", "dataset": "tamper_bars", "schema_version": "adjusted-v1",
                "partition_date": "2026-01-09", "generation_id": DIGEST,
                "data_checksum_sha256": sha256_json({"rows": [
                    [str(row[0]), pd.Timestamp(row[1]).isoformat(), float(row[2]), float(row[3]), bool(row[4]), str(pd.Timestamp(row[5]).date())]
                    for row in adjusted_frame[["instrument", "datetime", "close", "adj_factor", "suspended", "listing_date"]].sort_values(["instrument", "datetime"], kind="mergesort").itertuples(index=False)
                ]}),
                "visible_cutoff": "2026-01-09T15:00:00+08:00",
            }],
        )
        feature_schema = FeatureSchemaBuilder().build(
            factor_data.drop(columns=["label"], errors="ignore"), source_factor_set="basic", source_factor_version="1.0.0",
        )
        universe_manifest = _publish_universe(tmp_path)
        factor_document = _load_factor_document(tmp_path, factor_gen)
        dataset_manifest = DatasetBuilder(dataset_name="tamper_slice", semantic_version="1.0.0").build(
            ordered_features=["volume_ratio_20d"], factor_set="basic", factor_version="1.0.0",
            factor_generation_ids=[factor_gen], label_set_name="return_5d",
            label_generation_id=label_manifest["generation_id"],
            universe_snapshot_generation_id=universe_manifest["generation_id"],
            split_policy={"purge_trading_days": 0, "embargo_trading_days": 0, "splits": [
                {"name": "train", "start_date": "2026-01-05", "end_date": "2026-01-12"},
                {"name": "validation", "start_date": "2026-01-19", "end_date": "2026-01-23"},
            ]},
            row_count=len(factor_data),
        )
        DatasetWriter(tmp_path).write(
            dataset_manifest, factor_data, feature_schema=feature_schema, quality_report=dataset_decision(),
        )
        exporter = QlibDatasetExporter(tmp_path / "exports")
        export_manifest = exporter.export(
            dataset_name="tamper_slice", generation_id=dataset_manifest["generation_id"], frame=adjusted_frame,
            feature_mapping={"volume_ratio_20d": "VOLUME_RATIO"}, label_column="label", label_mapping="LABEL_5D",
            calendar_dates=sorted(factor_data["datetime"].dt.strftime("%Y-%m-%d").unique()),
            instruments=sorted(factor_data["instrument"].unique()),
            provider_uri="file:///correct", quality_decision=export_decision(),
        )
        receipt = QlibInitReceiptBuilder().build(
            export_manifest=export_manifest, resolved_provider_uri="file:///correct",
            qlib_import_path="qlib", qlib_version=qlib.__version__, cache_root=str(tmp_path / ".cache"),
            cache_files_before=set(), cache_files_after=set(),
            verified_export=exporter.read("tamper_slice", export_manifest["generation_id"]),
            governance_root=tmp_path, quality_decision=receipt_decision(),
        )
        definition = ModelDefinitionBuilder(run_content_generation_id=DIGEST, reviewed=True).build(
            algorithm="qlib_linear", hyperparameters={"alpha": 0.1},
            seed_policy={"base_seed": 42, "derivation": "fixed"},
            model_set="qlib", model_version="1.0.0", feature_schema_generation_id=feature_schema["generation_id"],
            compatible_dataset_versions=["1.0.0"], metrics=[{"name": "ic", "direction": "maximize"}],
            selection_rule="max ic", serializer_version="joblib-v1",
        )
        _, definition = ModelRunBuilder.build(
            definition=definition, dataset_manifest=dataset_manifest, export_manifest=export_manifest,
            receipt_manifest=receipt, environment_lock_sha256=DIGEST,
            determinism_controls={"random_seed": 42, "threads": 1}, label_manifest=label_manifest,
            universe_snapshot=universe_manifest, factor_manifests={factor_gen: factor_document},
            quality_decision=run_decision(), store_root=tmp_path,
        )
        verified_manifest, snapshot = exporter.read("tamper_slice", export_manifest["generation_id"])
        target = snapshot / "features" / "inst0" / "volume_ratio.day.bin"
        target.write_bytes(target.read_bytes()[:-1])
        with pytest.raises(ContractError, match="tampered Qlib export file"):
            QlibRuntimeTrainer().train(
                definition=definition, dataset_manifest=dataset_manifest,
                export_manifest=export_manifest, receipt_manifest=receipt,
                verified_export=(verified_manifest, snapshot),
                feature_columns=["volume_ratio_20d"], label_column="label",
            )
