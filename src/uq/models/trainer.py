from __future__ import annotations

import io
import json
import os
import shutil
import uuid
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..contracts.canonical_v2 import file_sha256_bytes, fsync_dir, fsync_tree
from ..contracts.model_layer import ModelContractLoader, model_manifest_identities, sha256_json
from ..errors import ContractError


class ModelTrainer:
    """Train a regularized linear model on a governed dataset snapshot."""

    def __init__(self, store_root: Path | str) -> None:
        self.store_root = Path(store_root)

    def train(
        self,
        *,
        definition: dict[str, Any],
        dataset_frame: pd.DataFrame,
        feature_columns: list[str],
        label_column: str,
    ) -> tuple[dict[str, Any], bytes]:
        if definition["algorithm"] != "regularized_linear":
            raise ContractError(f"trainer only supports regularized_linear, got {definition['algorithm']}")

        seed = definition["seed_policy"]["base_seed"]
        alpha = float(definition["hyperparameters"].get("alpha", 1.0))
        np.random.seed(seed)
        X = dataset_frame[feature_columns].values.astype(np.float64)
        y = dataset_frame[label_column].values.astype(np.float64)
        valid = ~np.isnan(y)
        X_v, y_v = X[valid], y[valid]
        n_features = X_v.shape[1]
        weights = np.linalg.solve(X_v.T @ X_v + alpha * np.eye(n_features), X_v.T @ y_v)

        model_state = {"weights": weights.tolist(), "alpha": alpha, "seed": seed}
        artifact_bytes = json.dumps(model_state).encode()
        artifact_checksum = file_sha256_bytes(artifact_bytes)

        manifest: dict[str, Any] = {
            "contract_version": 1,
            "artifact_filename": "artifact.bin",
            "artifact_checksum_sha256": artifact_checksum,
            "byte_size": len(artifact_bytes),
            "runtime_name": "numpy_ridge",
            "runtime_version": np.__version__,
            "runtime_import_path": "uq.models.trainer",
            "model_run_content_generation_id": definition["generation_id"],
            
            "serialization_profile_id": "json-numpy-v1",
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        return manifest, artifact_bytes


class ArtifactStore:
    """Publish immutable model artifacts with staging/quarantine/accepted boundaries."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.models_dir = self.root / "models"
        self.quarantine_dir = self.root / "quarantine"

    def publish(
        self,
        manifest: dict[str, Any],
        artifact_bytes: bytes,
        *,
        quality_report_checksum: str | None = None,
    ) -> Path:
        if quality_report_checksum is None:
            raise ContractError("artifact publication requires an explicit quality report checksum")
        manifest["quality_report_checksum_sha256"] = quality_report_checksum
        manifest["generation_id"] = "0" * 64
        manifest["manifest_digest_sha256"] = "0" * 64
        generation_id, digest = model_manifest_identities(manifest, schema_name="model_artifact")
        manifest["generation_id"] = generation_id
        manifest["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("model_artifact", {**manifest, "generation_id": manifest.get("generation_id", "")})
        actual_checksum = file_sha256_bytes(artifact_bytes)
        if manifest.get("artifact_checksum_sha256") != actual_checksum:
            raise ContractError("artifact checksum mismatch before publication")

        run_gen = manifest["model_run_content_generation_id"]
        artifact_gen = manifest["generation_id"]
        partition = (
            self.models_dir / f"run_generation={run_gen}" / f"artifact_generation={artifact_gen}"
        )
        if partition.exists():
            raise ContractError(f"immutable artifact already exists: {partition}")

        staging = partition.with_name(f"{partition.name}.staging.{uuid.uuid4().hex[:8]}")
        staging.mkdir(parents=True)
        lock_path = partition.parent / "publication.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        import fcntl
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                (staging / "artifact.bin").write_bytes(artifact_bytes)
                readback = (staging / "artifact.bin").read_bytes()
                if file_sha256_bytes(readback) != manifest["artifact_checksum_sha256"]:
                    raise ContractError("artifact readback checksum mismatch")

                final_manifest = dict(manifest)
                (staging / "manifest.json").write_text(json.dumps(final_manifest, sort_keys=True, indent=2) + "\n")
                fsync_tree(staging)
                partition.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, partition)
                fsync_dir(partition.parent)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return partition

    def read(self, run_generation_id: str, artifact_generation_id: str) -> tuple[dict[str, Any], bytes]:
        partition = (
            self.models_dir / f"run_generation={run_generation_id}" / f"artifact_generation={artifact_generation_id}"
        )
        manifest_path = partition / "manifest.json"
        artifact_path = partition / "artifact.bin"
        if not manifest_path.is_file() or not artifact_path.is_file():
            raise ContractError(f"unpublished or incomplete artifact: {partition}")
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise ContractError("malformed artifact manifest") from exc

        expected = manifest.get("artifact_checksum_sha256")
        actual = file_sha256_bytes(artifact_path.read_bytes())
        if expected != actual:
            raise ContractError("tampered artifact data prevents read")
        if manifest.get("generation_id") != artifact_generation_id:
            raise ContractError("path generation does not match manifest identity")
        return manifest, artifact_path.read_bytes()

    def quarantine(self, reason: str, *, artifact_bytes: bytes = b"", operator: str = "model-store") -> Path:
        directory = self.quarantine_dir / uuid.uuid4().hex
        staging = directory.with_name(directory.name + ".staging")
        staging.mkdir(parents=True)
        try:
            artifact_path = staging / "rejected.bin"
            artifact_path.write_bytes(artifact_bytes)
            manifest = {
                "quarantine_version": 1,
                "reason": reason,
                "operator": operator,
                "data_checksum_sha256": file_sha256_bytes(artifact_path.read_bytes()),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "retention_policy": "manual-review; no automatic accepted promotion",
            }
            content = json.dumps(manifest, sort_keys=True).encode()
            (staging / "manifest.json").write_bytes(content)
            (staging / "manifest.sha256").write_text(file_sha256_bytes(content) + "\n")
            fsync_tree(staging)
            os.replace(staging, directory)
            fsync_dir(self.quarantine_dir)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return directory


class ModelRunBuilder:
    """Build a model_run manifest linking definition, dataset, export, and receipt."""

    @staticmethod
    def build(
        *,
        definition_generation_id: str,
        dataset_generation_id: str,
        qlib_export_generation_id: str,
        init_receipt_generation_id: str,
        environment_lock_sha256: str,
        determinism_controls: dict[str, Any],
        reproducibility_mode: str = "logical_fingerprint",
    ) -> dict[str, Any]:
        code_fingerprint = sha256_json({"component": "ModelRunBuilder", "version": 1})
        content_payload = {
            "definition": definition_generation_id,
            "dataset": dataset_generation_id,
            "export": qlib_export_generation_id,
            "receipt": init_receipt_generation_id,
        }
        run_content_generation_id = sha256_json(content_payload)
        manifest: dict[str, Any] = {
            "contract_version": 1,
            "run_content_generation_id": run_content_generation_id,
            "model_definition_generation_id": definition_generation_id,
            "model_dataset_generation_id": dataset_generation_id,
            "qlib_export_generation_id": qlib_export_generation_id,
            "init_receipt_generation_id": init_receipt_generation_id,
            "code_fingerprint": code_fingerprint,
            "environment_lock_sha256": environment_lock_sha256,
            "determinism_controls": determinism_controls,
            "reproducibility_mode": reproducibility_mode,
            "logical_tolerance": None,
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation_id, manifest_digest = model_manifest_identities(manifest, schema_name="model_run")
        manifest["generation_id"] = generation_id
        manifest["manifest_digest_sha256"] = manifest_digest
        ModelContractLoader.validate("model_run", manifest)
        return manifest
