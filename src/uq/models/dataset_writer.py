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
from ..contracts.model_layer import ModelContractLoader, model_manifest_identities, sha256_json
from ..errors import ContractError
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
    ) -> Path:
        published_manifest = dict(manifest)

        if quality_report is not None:
            ModelContractLoader.validate("model_quality_report", quality_report)
            if quality_report["binding_type"] != "model_dataset_v1" or quality_report["status"] not in {"passed", "warning"}:
                raise ContractError("dataset quality report does not approve publication")
        else:
            quality_report = {
                "report_version": 1, "binding_type": "model_dataset_v1",
                "bound_generation_id": "0" * 64, "policy": "reject_all", "status": "passed",
                "checks": [{"name": "readback_reconciliation", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}],
                "errors": [], "warnings": [],
                "producer_code_fingerprint": sha256_json({"component": "DatasetWriter", "version": 1}),
            }
            quality_report["report_checksum_sha256"] = sha256_json(quality_report)

        FeatureSchemaValidator.validate_against_frame(feature_schema, frame)

        artifact, data_checksum = self._serialize(frame)
        restored = pd.read_parquet(io.BytesIO(artifact))
        if list(restored.columns) != list(frame.columns) or len(restored) != len(frame):
            raise ContractError("dataset readback reconciliation failed")

        published_manifest["data_checksum_sha256"] = data_checksum
        published_manifest["logical_fingerprint"] = frame_logical_fingerprint(restored)
        published_manifest["generation_id"] = "0" * 64
        published_manifest["manifest_digest_sha256"] = "0" * 64
        report_checksum = sha256_json({
            key: value for key, value in quality_report.items() if key != "report_checksum_sha256"
        })
        generation_id, manifest_digest = model_manifest_identities(
            published_manifest,
            schema_name="model_dataset",
            exclude_fields={"logical_fingerprint", "quality_report_checksum_sha256"},
        )
        published_manifest["generation_id"] = generation_id
        unsigned_report = {
            **{key: value for key, value in quality_report.items() if key != "report_checksum_sha256"},
            "bound_generation_id": generation_id,
        }
        quality_report = {**unsigned_report, "report_checksum_sha256": sha256_json(unsigned_report)}
        report_checksum = sha256_json({
            key: value for key, value in quality_report.items() if key != "report_checksum_sha256"
        })
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
                (staging / "feature_schema.json").write_text(
                    json.dumps(feature_schema, sort_keys=True, indent=2) + "\n"
                )
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
        expected_generation, _ = model_manifest_identities(
            manifest,
            schema_name="model_dataset",
            exclude_fields={"logical_fingerprint", "quality_report_checksum_sha256"},
        )
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
        if len(frame) != manifest.get("row_count"):
            raise ContractError("dataset row count does not match manifest")
        return manifest, frame

    def _validate_bound_quality_report(self, manifest: dict[str, Any]) -> None:
        checksum = manifest.get("quality_report_checksum_sha256")
        path = self.root / "model_quality_reports" / f"{checksum}.json"
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
