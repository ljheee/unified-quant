from __future__ import annotations

import io
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
from ..contracts.model_layer import ModelContractLoader
from ..errors import ContractError
from .features import FeatureSchemaValidator


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
        feature_schema: dict[str, Any] | None = None,
    ) -> Path:
        ModelContractLoader.validate("model_dataset", manifest)
        if feature_schema is not None:
            FeatureSchemaValidator.validate_against_frame(feature_schema, frame)

        dataset_name = manifest["dataset_name"]
        semantic_version = manifest["semantic_version"]
        generation_id = manifest["generation_id"]
        partition = (
            self._datasets_dir
            / f"dataset={dataset_name}"
            / f"version={semantic_version}"
            / f"generation={generation_id}"
        )
        if partition.exists():
            raise ContractError(f"immutable dataset already exists: {partition}")

        staging = partition.with_name(f"{partition.name}.staging.{uuid.uuid4().hex}")
        staging.mkdir(parents=True)
        lock_path = partition.parent / "publication.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        import fcntl
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                artifact, data_checksum = self._serialize(frame)
                (staging / "data.parquet").write_bytes(artifact)
                restored = pd.read_parquet(staging / "data.parquet")
                if list(restored.columns) != list(frame.columns) or len(restored) != len(frame):
                    raise ContractError("dataset readback reconciliation failed")

                final_manifest = {
                    **manifest,
                    "data_checksum_sha256": data_checksum,
                }
                from ..contracts.model_layer import sha256_json
                # Recompute identity with real checksum
                final_manifest["generation_id"] = "0" * 64
                final_manifest["manifest_digest_sha256"] = "0" * 64
                from ..contracts.model_layer import model_manifest_identities
                gen, digest = model_manifest_identities(final_manifest, schema_name="model_dataset")
                final_manifest["generation_id"] = gen
                final_manifest["manifest_digest_sha256"] = digest

                (staging / "manifest.json").write_text(
                    json.dumps(final_manifest, sort_keys=True, indent=2) + "\n"
                )
                if feature_schema is not None:
                    (staging / "feature_schema.json").write_text(
                        json.dumps(feature_schema, sort_keys=True, indent=2) + "\n"
                    )
                fsync_tree(staging)
                os.replace(staging, partition)
                fsync_dir(partition.parent)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return partition

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

        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise ContractError("malformed dataset manifest") from exc

        ModelContractLoader.validate("model_dataset", manifest)
        expected_checksum = manifest.get("data_checksum_sha256")
        actual_checksum = file_sha256_bytes(data_path.read_bytes())
        if expected_checksum != actual_checksum:
            raise ContractError("tampered dataset data prevents read")

        frame = pd.read_parquet(data_path)
        if len(frame) != manifest.get("row_count"):
            raise ContractError("dataset row count does not match manifest")
        return manifest, frame

    @staticmethod
    def _serialize(frame: pd.DataFrame) -> tuple[bytes, str]:
        ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True)
        table = arrow.Table.from_pandas(ordered, preserve_index=False)
        sink = arrow.BufferOutputStream()
        parquet.write_table(table, sink, compression="snappy")
        artifact = sink.getvalue().to_pybytes()
        return artifact, file_sha256_bytes(artifact)
