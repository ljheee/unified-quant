from __future__ import annotations

import io
import fcntl
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as arrow
import pyarrow.parquet as parquet

from ..contracts.canonical_v2 import file_sha256_bytes, fsync_dir, fsync_tree
from ..contracts.gate_contracts import validate_contract
from ..contracts.model_layer import ModelContractLoader, bind_reviewed_quality_decision, model_manifest_identities, sha256_json
from ..errors import ContractError
from .dataset import SplitValidator
from .feature_preprocessing import FeaturePreprocessorBuilder
from .features import FeatureSchemaValidator
from ..factors.raw_price import logical_fingerprint as frame_logical_fingerprint


class DatasetWriter:
    """Write an immutable model dataset partition with manifest."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._datasets_dir = self.root / "datasets"

    def write(
        self,
        manifest: dict[str, Any],
        frame: pd.DataFrame,
        *,
        feature_schema: dict[str, Any],
        quality_report: dict[str, Any] | None = None,
        preprocessing_manifest: dict[str, Any] | None = None,
        preprocessing_frame: pd.DataFrame | None = None,
        preprocessing_input_frame: pd.DataFrame | None = None,
        preprocessing_input_feature_schema: dict[str, Any] | None = None,
        preprocessing_quality_report: dict[str, Any] | None = None,
    ) -> Path:
        published_manifest = dict(manifest)

        if quality_report is None:
            raise ContractError("dataset publication requires an externally reviewed quality decision")

        FeatureSchemaValidator.validate_against_frame(feature_schema, frame)
        self._validate_preprocessing(
            published_manifest, frame, feature_schema, preprocessing_manifest,
            preprocessing_input_frame, preprocessing_input_feature_schema, preprocessing_frame,
        )
        policy = published_manifest["split_policy"]
        trading_dates = sorted({timestamp.strftime("%Y-%m-%d") for timestamp in frame["datetime"]})
        SplitValidator.validate_splits(
            policy["splits"], horizon=policy["purge_trading_days"],
            embargo_days=policy["embargo_trading_days"], trading_dates=trading_dates,
        )

        artifact, data_checksum = self._serialize(frame)
        restored = pd.read_parquet(io.BytesIO(artifact))
        if list(restored.columns) != list(frame.columns) or len(restored) != len(frame):
            raise ContractError("dataset readback reconciliation failed")

        published_manifest["data_checksum_sha256"] = data_checksum
        published_manifest["logical_fingerprint"] = frame_logical_fingerprint(restored)
        published_manifest["generation_id"] = "0" * 64
        published_manifest["manifest_digest_sha256"] = "0" * 64
        generation_id, _ = model_manifest_identities(
            published_manifest,
            schema_name="model_dataset",
            exclude_fields={"logical_fingerprint", "quality_report_checksum_sha256"},
        )
        bound_report, report_checksum = bind_reviewed_quality_decision(
            quality_report, binding_type="model_dataset_v1",
            subject_generation_id=generation_id,
            subject_content_sha256=generation_id,
        )
        if preprocessing_manifest is not None:
            if preprocessing_quality_report is None:
                raise ContractError("preprocessing publication requires an externally reviewed quality decision")
            bound_preprocessing_report, preprocessing_report_checksum = bind_reviewed_quality_decision(
                preprocessing_quality_report, binding_type="feature_preprocessing_v1",
                subject_generation_id=preprocessing_manifest["generation_id"],
                subject_content_sha256=preprocessing_manifest["generation_id"],
            )
            preprocessing_manifest = {**preprocessing_manifest, "quality_report_checksum_sha256": preprocessing_report_checksum}
            _, preprocessing_manifest["manifest_digest_sha256"] = model_manifest_identities(
                preprocessing_manifest,
                schema_name="feature_preprocessing",
                exclude_fields={"quality_report_checksum_sha256"},
            )
            ModelContractLoader.validate("feature_preprocessing", preprocessing_manifest)
        quality_report_path = self.root / "external_quality_reviews" / f"{report_checksum}.json"
        quality_report_path.parent.mkdir(parents=True, exist_ok=True)
        report_staging = quality_report_path.with_suffix(f".staging.{uuid.uuid4().hex}")
        report_staging.write_text(json.dumps(bound_report, sort_keys=True, indent=2) + "\n")
        os.replace(report_staging, quality_report_path)
        fsync_dir(quality_report_path.parent)
        published_manifest["generation_id"] = generation_id
        published_manifest["quality_report_checksum_sha256"] = report_checksum
        _, manifest_digest = model_manifest_identities(
            published_manifest,
            schema_name="model_dataset",
            exclude_fields={"logical_fingerprint", "quality_report_checksum_sha256"},
        )
        published_manifest["manifest_digest_sha256"] = manifest_digest
        validate_contract("model_dataset.v1.json", published_manifest)

        dataset_name = published_manifest["dataset_name"]
        semantic_version = published_manifest["semantic_version"]
        partition = (
            self._datasets_dir
            / f"dataset={dataset_name}"
            / f"version={semantic_version}"
            / f"generation={generation_id}"
        )
        if partition.exists():
            raise ContractError(f"immutable dataset already exists: {partition}")

        lock_path = partition.parent / "publication.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                staging = partition.with_name(f"{partition.name}.staging.{uuid.uuid4().hex}")
                staging.mkdir(parents=True)
                (staging / "data.parquet").write_bytes(artifact)
                (staging / "data.sha256").write_text(data_checksum + "\n")

                (staging / "manifest.json").write_text(
                    json.dumps(published_manifest, sort_keys=True, indent=2) + "\n"
                )
                input_schema_dir = staging / "feature_schemas"
                input_schema_dir.mkdir()
                stored_input_schema = preprocessing_input_feature_schema or feature_schema
                input_schema_bytes = json.dumps(stored_input_schema, sort_keys=True, indent=2).encode() + b"\n"
                (input_schema_dir / "input.json").write_bytes(input_schema_bytes)
                (staging / "feature_schema.json").write_text(
                    json.dumps(feature_schema, sort_keys=True, indent=2) + "\n"
                )
                if preprocessing_manifest is not None:
                    (staging / "feature_preprocessing.json").write_text(
                        json.dumps(preprocessing_manifest, sort_keys=True, indent=2) + "\n"
                    )
                    preprocessing_path = self.root / "external_quality_reviews" / f"{preprocessing_report_checksum}.json"
                    preprocessing_path.parent.mkdir(parents=True, exist_ok=True)
                    if not preprocessing_path.exists():
                        preprocessing_staging = preprocessing_path.with_suffix(f".staging.{uuid.uuid4().hex}")
                        preprocessing_staging.write_text(json.dumps(bound_preprocessing_report, sort_keys=True, indent=2) + "\n")
                        os.replace(preprocessing_staging, preprocessing_path)
                        fsync_dir(preprocessing_path.parent)
                report_dir = self.root / "model_quality_reports"
                report_dir.mkdir(parents=True, exist_ok=True)
                report_path = report_dir / f"{report_checksum}.json"
                if not report_path.exists():
                    report_staging = report_path.with_suffix(f".staging.{uuid.uuid4().hex}")
                    report_staging.write_text(json.dumps(quality_report, sort_keys=True, indent=2) + "\n")
                    os.replace(report_staging, report_path)
                    fsync_dir(report_dir)
                fsync_tree(staging)
                os.replace(staging, partition)
                fsync_dir(partition.parent)
                self._last_published_manifest = dict(published_manifest)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return partition

    @property
    def last_published_manifest(self) -> dict[str, Any]:
        if not hasattr(self, "_last_published_manifest"):
            raise ContractError("no dataset has been published by this writer")
        return self._last_published_manifest

    def read(self, dataset_name: str, semantic_version: str, generation_id: str) -> tuple[dict[str, Any], pd.DataFrame]:
        partition = (
            self._datasets_dir
            / f"dataset={dataset_name}"
            / f"version={semantic_version}"
            / f"generation={generation_id}"
        )
        manifest_path = partition / "manifest.json"
        data_path = partition / "data.parquet"
        if not manifest_path.is_file() or not data_path.is_file():
            raise ContractError(f"unpublished or incomplete dataset: {partition}")
        sha_path = partition / "data.sha256"
        if not sha_path.is_file():
            raise ContractError(f"incomplete dataset checksum sidecar: {partition}")

        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise ContractError("malformed dataset manifest") from exc

        ModelContractLoader.validate("model_dataset", manifest)
        self._validate_bound_quality_report(manifest)
        expected_generation, expected_digest = model_manifest_identities(
            manifest,
            schema_name="model_dataset",
            exclude_fields={"logical_fingerprint", "quality_report_checksum_sha256"},
        )
        if manifest.get("manifest_digest_sha256") != expected_digest:
            raise ContractError("dataset manifest digest mismatch")
        if manifest.get("generation_id") != expected_generation:
            raise ContractError("dataset stable generation mismatch")
        if manifest.get("generation_id") != generation_id:
            raise ContractError("path generation does not match dataset manifest identity")
        fs_path = partition / "feature_schema.json"
        if not fs_path.is_file():
            raise ContractError(f"incomplete dataset feature schema: {partition}")
        try:
            fs_doc = json.loads(fs_path.read_text())
            ModelContractLoader.validate("feature_schema", fs_doc)
        except (json.JSONDecodeError, ContractError) as exc:
            raise ContractError("tampered or malformed feature schema in dataset partition") from exc
        preprocessing_doc: dict[str, Any] | None = None
        if manifest.get("feature_preprocessing_generation_id") is not None:
            preprocessing_path = partition / "feature_preprocessing.json"
            if not preprocessing_path.is_file():
                raise ContractError(f"incomplete dataset feature preprocessing: {partition}")
            try:
                preprocessing_doc = json.loads(preprocessing_path.read_text())
                ModelContractLoader.validate("feature_preprocessing", preprocessing_doc)
                input_schema_path = partition / manifest["input_feature_schema_path"]
                if not input_schema_path.is_file():
                    raise ContractError("dataset input feature schema is unavailable")
                input_feature_schema = json.loads(input_schema_path.read_text())
                self._validate_preprocessing_references(manifest, preprocessing_doc, fs_doc, input_feature_schema)
                self._validate_preprocessing_quality_report(preprocessing_doc)
            except (json.JSONDecodeError, ContractError) as exc:
                raise ContractError("tampered or malformed feature preprocessing in dataset partition") from exc
        actual_checksum = file_sha256_bytes(data_path.read_bytes())
        manifest_checksum = manifest.get("data_checksum_sha256")
        if manifest_checksum != actual_checksum:
            raise ContractError("tampered dataset data prevents read (manifest checksum mismatch)")
        sidecar_checksum = sha_path.read_text().strip()
        if sidecar_checksum != actual_checksum:
            raise ContractError("sidecar checksum does not match artifact bytes")
        if sidecar_checksum != manifest_checksum:
            raise ContractError("sidecar checksum conflicts with manifest checksum")

        frame = pd.read_parquet(data_path)
        FeatureSchemaValidator.validate_against_frame(fs_doc, frame)
        expected_columns = ["instrument", "datetime", *[column["name"] for column in fs_doc["columns"]]]
        if list(frame.columns) not in (expected_columns, expected_columns + ["label"]):
            raise ContractError("dataset frame columns do not match feature schema and label contract")
        if frame.duplicated(["instrument", "datetime"]).any():
            raise ContractError("duplicate dataset keys prevent read")
        if frame_logical_fingerprint(frame) != manifest.get("logical_fingerprint"):
            raise ContractError("dataset logical fingerprint mismatch")
        if preprocessing_doc is not None:
            if frame_logical_fingerprint(frame) != preprocessing_doc["output_frame_sha256"]:
                raise ContractError("preprocessing output fingerprint mismatch")
        policy = manifest["split_policy"]
        trading_dates = sorted({timestamp.strftime("%Y-%m-%d") for timestamp in frame["datetime"]})
        SplitValidator.validate_splits(
            policy["splits"], horizon=policy["purge_trading_days"],
            embargo_days=policy["embargo_trading_days"], trading_dates=trading_dates,
            covered_dates={str(value.date()) for value in pd.to_datetime(frame["datetime"])},
        )
        if len(frame) != manifest.get("row_count"):
            raise ContractError("dataset row count does not match manifest")
        return manifest, frame

    def _validate_bound_quality_report(self, manifest: dict[str, Any]) -> None:
        checksum = manifest.get("quality_report_checksum_sha256")
        path = self.root / "external_quality_reviews" / f"{checksum}.json"
        try:
            report = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("dataset quality report is unavailable or malformed") from exc
        ModelContractLoader.validate("model_quality_report", report)
        actual_checksum = sha256_json({key: value for key, value in report.items() if key != "report_checksum_sha256"})
        if (
            checksum != actual_checksum
            or report["binding_type"] != "model_dataset_v1"
            or report["bound_generation_id"] != manifest["generation_id"]
            or report["status"] not in {"passed", "warning"}
            or report.get("report_version") != 2
            or not report.get("reviewer")
            or report.get("subject_content_sha256") != manifest["generation_id"]
        ):
            raise ContractError("dataset quality report rejects read")

    @staticmethod
    def _serialize(frame: pd.DataFrame) -> tuple[bytes, str]:
        ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True)
        table = arrow.Table.from_pandas(ordered, preserve_index=False)
        sink = arrow.BufferOutputStream()
        parquet.write_table(table, sink, compression="snappy")
        artifact = sink.getvalue().to_pybytes()
        return artifact, file_sha256_bytes(artifact)

    def _validate_preprocessing(
        self,
        manifest: dict[str, Any],
        frame: pd.DataFrame,
        feature_schema: dict[str, Any],
        preprocessing_manifest: dict[str, Any] | None,
        preprocessing_input_frame: pd.DataFrame | None,
        preprocessing_input_feature_schema: dict[str, Any] | None,
        preprocessing_frame: pd.DataFrame | None,
    ) -> None:
        if (manifest.get("feature_preprocessing_generation_id") is None) != (preprocessing_manifest is None):
            raise ContractError("dataset manifest and supplied preprocessing manifest disagree")
        if preprocessing_manifest is None:
            return
        if preprocessing_frame is None:
            raise ContractError("preprocessing manifest requires preprocessing output frame")
        if preprocessing_input_frame is None:
            raise ContractError("preprocessing manifest requires preprocessing input frame")
        ModelContractLoader.validate("feature_preprocessing", preprocessing_manifest)
        if preprocessing_input_feature_schema is None:
            raise ContractError("preprocessing manifest requires input feature schema")
        self._validate_preprocessing_references(
            manifest, preprocessing_manifest, feature_schema, preprocessing_input_feature_schema
        )
        input_fingerprint = FeaturePreprocessorBuilder.frame_fingerprint(preprocessing_input_frame)
        if input_fingerprint != preprocessing_manifest["input_frame_sha256"]:
            raise ContractError("preprocessing input frame fingerprint mismatch")
        builder = FeaturePreprocessorBuilder(
            preprocess_name=preprocessing_manifest["preprocess_name"],
            semantic_version=preprocessing_manifest["semantic_version"],
            transform=preprocessing_manifest["transform"],
            min_group_observations=preprocessing_manifest["policy"]["min_group_observations"],
            code_fingerprint=preprocessing_manifest["code_fingerprint"],
        )
        builder.validate_frame_keys(frame)
        builder.validate_frame_keys(preprocessing_frame)
        ordered_features = preprocessing_manifest["ordered_features"]
        if list(preprocessing_frame.columns) != ["instrument", "datetime", *ordered_features]:
            raise ContractError("preprocessing output columns do not match manifest")
        canonical_output = preprocessing_frame.sort_values(
            ["instrument", "datetime"], kind="mergesort"
        ).reset_index(drop=True)
        expected = builder.transform(preprocessing_input_frame.copy(), ordered_features)
        pd.testing.assert_frame_equal(
            canonical_output, expected, check_exact=False, rtol=1e-12, atol=1e-12
        )
        if frame_logical_fingerprint(canonical_output) != preprocessing_manifest["output_frame_sha256"]:
            raise ContractError("preprocessing output fingerprint mismatch")

    def _validate_preprocessing_references(
        self,
        manifest: dict[str, Any],
        preprocessing_manifest: dict[str, Any],
        feature_schema: dict[str, Any],
        input_feature_schema: dict[str, Any],
    ) -> None:
        if manifest.get("feature_preprocessing_generation_id") != preprocessing_manifest.get("generation_id"):
            raise ContractError("dataset preprocessing generation mismatch")
        if preprocessing_manifest.get("input_factor_set") != manifest.get("factor_set"):
            raise ContractError("preprocessing input factor set mismatch")
        if preprocessing_manifest.get("input_factor_version") != manifest.get("factor_version"):
            raise ContractError("preprocessing input factor version mismatch")
        if preprocessing_manifest.get("input_factor_generation_ids") != manifest.get("factor_generation_ids"):
            raise ContractError("preprocessing input factor generation mismatch")
        if preprocessing_manifest.get("output_feature_schema_generation_id") != feature_schema.get("generation_id"):
            raise ContractError("preprocessing output feature schema generation mismatch")
        if preprocessing_manifest.get("output_feature_schema_manifest_digest_sha256") != feature_schema.get("manifest_digest_sha256"):
            raise ContractError("preprocessing output feature schema digest mismatch")
        if preprocessing_manifest.get("input_feature_schema_generation_id") != input_feature_schema.get("generation_id"):
            raise ContractError("preprocessing input feature schema generation mismatch")
        if preprocessing_manifest.get("input_feature_schema_manifest_digest_sha256") != input_feature_schema.get("manifest_digest_sha256"):
            raise ContractError("preprocessing input feature schema digest mismatch")
        if manifest.get("input_feature_schema_generation_id") != input_feature_schema.get("generation_id"):
            raise ContractError("dataset input feature schema generation mismatch")
        if manifest.get("input_feature_schema_manifest_digest_sha256") != input_feature_schema.get("manifest_digest_sha256"):
            raise ContractError("dataset input feature schema digest mismatch")
        if manifest.get("input_feature_schema_path") != "feature_schemas/input.json":
            raise ContractError("dataset input feature schema path mismatch")
        ordered_features = preprocessing_manifest.get("ordered_features")
        schema_features = [column["name"] for column in feature_schema["columns"]]
        if ordered_features != schema_features:
            raise ContractError("preprocessing ordered features do not match feature schema")
        statuses = {column["name"]: column["transform_status"] for column in feature_schema["columns"]}
        policy_type = preprocessing_manifest.get("transform")
        expected_status = "standardized" if policy_type == "standardize_cross_section" else "ranked"
        for name in ordered_features:
            if statuses.get(name) != expected_status:
                raise ContractError(f"feature schema transform status mismatch for {name}")

    def _validate_preprocessing_quality_report(self, preprocessing_manifest: dict[str, Any]) -> None:
        checksum = preprocessing_manifest.get("quality_report_checksum_sha256")
        path = self.root / "external_quality_reviews" / f"{checksum}.json"
        try:
            report = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("preprocessing quality report is unavailable or malformed") from exc
        ModelContractLoader.validate("model_quality_report", report)
        actual_checksum = sha256_json({key: value for key, value in report.items() if key != "report_checksum_sha256"})
        if (
            checksum != actual_checksum
            or report["binding_type"] != "feature_preprocessing_v1"
            or report["bound_generation_id"] != preprocessing_manifest["generation_id"]
            or report["subject_content_sha256"] != preprocessing_manifest["generation_id"]
            or report["status"] not in {"passed", "warning"}
        ):
            raise ContractError("preprocessing quality report rejects read")
