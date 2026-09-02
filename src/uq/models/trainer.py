from __future__ import annotations

import fcntl
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
from ..contracts.model_layer import ModelContractLoader, ModelQualityReviewRegistry, bind_reviewed_quality_decision, model_manifest_identities, sha256_json
from ..errors import ContractError


class ModelTrainer:
    """Train a deterministic NumPy ridge baseline with Qlib-compatible exports."""

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
        ModelContractLoader.validate("model_definition", definition)
        if definition.get("status") != "reviewed":
            raise ContractError("training requires an externally reviewed model definition")
        if definition["algorithm"] != "regularized_linear":
            raise ContractError(f"trainer only supports regularized_linear, got {definition['algorithm']}")

        seed = definition["seed_policy"]["base_seed"]
        alpha = float(definition["hyperparameters"].get("alpha", 1.0))
        np.random.seed(seed)
        X = dataset_frame[feature_columns].values.astype(np.float64)
        y = dataset_frame[label_column].values.astype(np.float64)
        valid = ~np.isnan(y)
        X_v, y_v = X[valid], y[valid]
        if len(X_v) == 0:
            raise ContractError("training requires at least one supervised observation")
        n_features = X_v.shape[1]
        weights = np.linalg.solve(X_v.T @ X_v + alpha * np.eye(n_features), X_v.T @ y_v)

        model_state = {"weights": weights.tolist(), "alpha": alpha, "seed": seed}
        artifact_bytes = json.dumps(model_state).encode()
        artifact_checksum = file_sha256_bytes(artifact_bytes)

        run_content_generation_id = definition["model_run_content_generation_id"]
        if not isinstance(run_content_generation_id, str) or len(run_content_generation_id) != 64:
            raise ContractError("model definition is not bound to a validated model_run_content_generation_id")

        manifest: dict[str, Any] = {
            "contract_version": 1,
            "artifact_filename": "artifact.bin",
            "artifact_checksum_sha256": artifact_checksum,
            "byte_size": len(artifact_bytes),
            "runtime_name": "numpy_ridge",
            "runtime_version": np.__version__,
            "runtime_import_path": "uq.models.trainer",
            "model_run_content_generation_id": run_content_generation_id,
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
        quality_report: dict[str, Any],
    ) -> Path:
        if quality_report is None:
            raise ContractError("artifact publication requires a quality report")
        ModelContractLoader.validate("model_quality_report", quality_report)
        ModelQualityReviewRegistry().validate_report(quality_report)
        if (
            quality_report.get("report_version") != 2
            or quality_report["binding_type"] != "model_artifact_v1"
            or quality_report["status"] not in {"passed", "warning"}
            or not quality_report.get("reviewer")
            or not quality_report.get("subject_content_sha256")
            or not quality_report.get("review_signature_sha256")
        ):
            raise ContractError("artifact requires an externally reviewed v2 quality report")
        quality_report_checksum = sha256_json({
            key: value for key, value in quality_report.items() if key != "report_checksum_sha256"
        })
        if quality_report["report_checksum_sha256"] != quality_report_checksum:
            raise ContractError("artifact publication requires an explicit quality report checksum")
        if "quality_report_checksum_sha256" in manifest:
            raise ContractError("artifact manifest already carries a quality report binding")
        published_manifest = dict(manifest)
        published_manifest["generation_id"] = "0" * 64
        published_manifest["manifest_digest_sha256"] = "0" * 64
        generation_id, _ = model_manifest_identities(
            published_manifest,
            schema_name="model_artifact",
            exclude_fields={"quality_report_checksum_sha256"},
        )
        published_manifest["generation_id"] = generation_id
        if quality_report["bound_generation_id"] != generation_id:
            raise ContractError("quality report does not bind to the artifact generation")
        published_manifest["quality_report_checksum_sha256"] = quality_report_checksum
        _, digest = model_manifest_identities(
            published_manifest,
            schema_name="model_artifact",
            exclude_fields={"quality_report_checksum_sha256"},
        )
        published_manifest["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("model_artifact", published_manifest)
        actual_checksum = file_sha256_bytes(artifact_bytes)
        if published_manifest.get("artifact_checksum_sha256") != actual_checksum:
            raise ContractError("artifact checksum mismatch before publication")
        if len(published_manifest.get("quality_report_checksum_sha256", "")) != 64:
            raise ContractError("invalid quality report checksum binding")

        run_gen = published_manifest["model_run_content_generation_id"]
        artifact_gen = published_manifest["generation_id"]
        partition = (
            self.models_dir / f"run_generation={run_gen}" / f"artifact_generation={artifact_gen}"
        )
        if partition.exists():
            raise ContractError(f"immutable artifact already exists: {partition}")

        lock_path = partition.parent / "publication.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                staging = partition.with_name(f"{partition.name}.staging.{uuid.uuid4().hex[:8]}")
                staging.mkdir(parents=True)
                artifact_path = staging / published_manifest["artifact_filename"]
                artifact_path.write_bytes(artifact_bytes)
                readback = artifact_path.read_bytes()
                if file_sha256_bytes(readback) != published_manifest["artifact_checksum_sha256"]:
                    raise ContractError("artifact readback checksum mismatch")

                final_manifest = dict(published_manifest)
                (staging / "manifest.json").write_text(json.dumps(final_manifest, sort_keys=True, indent=2) + "\n")
                report_dir = self.models_dir / "quality_reports"
                report_dir.mkdir(parents=True, exist_ok=True)
                report_path = report_dir / f"{quality_report_checksum}.json"
                if report_path.exists():
                    raise ContractError("immutable artifact quality report already exists")
                report_staging = report_path.with_suffix(f".staging.{uuid.uuid4().hex}")
                report_staging.write_text(json.dumps(quality_report, sort_keys=True, indent=2) + "\n")
                os.replace(report_staging, report_path)
                fsync_dir(report_dir)
                fsync_tree(staging)
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
        if not manifest_path.is_file():
            raise ContractError(f"unpublished or incomplete artifact: {partition}")
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise ContractError("malformed artifact manifest") from exc
        if manifest.get("artifact_filename") not in {"artifact.bin", "model.joblib"}:
            raise ContractError("unsupported artifact filename")
        artifact_path = partition / manifest["artifact_filename"]
        if not artifact_path.is_file():
            raise ContractError(f"unpublished or incomplete artifact: {partition}")

        expected = manifest.get("artifact_checksum_sha256")
        actual = file_sha256_bytes(artifact_path.read_bytes())
        if expected != actual:
            raise ContractError("tampered artifact data prevents read")
        expected_generation, expected_digest = model_manifest_identities(
            manifest,
            schema_name="model_artifact",
            exclude_fields={"quality_report_checksum_sha256"},
        )
        if manifest.get("generation_id") != expected_generation or manifest.get("manifest_digest_sha256") != expected_digest:
            raise ContractError("artifact manifest identity mismatch")
        if manifest.get("byte_size") != artifact_path.stat().st_size:
            raise ContractError("artifact byte size mismatch")
        if not isinstance(manifest.get("quality_report_checksum_sha256"), str) or len(manifest["quality_report_checksum_sha256"]) != 64:
            raise ContractError("missing artifact quality report checksum")
        report_path = partition.parents[1] / "quality_reports" / f"{manifest['quality_report_checksum_sha256']}.json"
        try:
            report = json.loads(report_path.read_text())
            ModelContractLoader.validate("model_quality_report", report)
            ModelQualityReviewRegistry().validate_report(report)
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("artifact quality report is unavailable or malformed") from exc
        if (
            sha256_json({key: value for key, value in report.items() if key != "report_checksum_sha256"})
            != manifest["quality_report_checksum_sha256"]
            or report["binding_type"] != "model_artifact_v1"
            or report["status"] == "rejected"
            or report["bound_generation_id"] != artifact_generation_id
        ):
            raise ContractError("artifact quality report rejects read")
        if manifest.get("generation_id") != artifact_generation_id:
            raise ContractError("path generation does not match manifest identity")
        return manifest, artifact_path.read_bytes()

    def quarantine(self, reason: str, *, artifact_bytes: bytes = b"", input_generations: dict[str, str] | None = None, operator: str = "model-store") -> Path:
        if not isinstance(input_generations or {}, dict) or not all(
            isinstance(key, str) and isinstance(value, str) and len(value) == 64
            for key, value in (input_generations or {}).items()
        ):
            raise ContractError("quarantine input_generations must map names to 64-character generation IDs")
        if reason not in {"quality_failed", "checksum_mismatch", "lineage_mismatch", "operator_rejected"}:
            raise ContractError("quarantine reason is not in the approved taxonomy")
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
                "review_status": "rejected",
                "input_generations": dict(input_generations or {}),
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
    """Build a model_run manifest after validating every supplied upstream document."""

    @staticmethod
    def build(
        *,
        definition: dict[str, Any],
        dataset_manifest: dict[str, Any],
        export_manifest: dict[str, Any],
        receipt_manifest: dict[str, Any],
        environment_lock_sha256: str,
        determinism_controls: dict[str, Any],
        label_manifest: dict[str, Any],
        universe_snapshot: dict[str, Any],
        factor_manifests: dict[str, dict[str, Any]],
        quality_decision: dict[str, Any],
        reproducibility_mode: str = "logical_fingerprint",
        store_root: Path | str = ".",
    ) -> dict[str, Any]:
        from ..contracts.model_layer import resolve_bindings
        required_documents = {
            "model_definition": definition,
            "model_dataset": dataset_manifest,
            "qlib_dataset_export": export_manifest,
            "qlib_init_receipt": receipt_manifest,
            "label_set": label_manifest,
            "universe_snapshot": universe_snapshot,
        }
        required_documents["factor_manifests"] = factor_manifests
        for family, document in required_documents.items():
            if family not in {"universe_snapshot", "factor_manifests"}:
                ModelContractLoader.validate(family, document)
        resolve_bindings(required_documents, universe_root=Path(store_root) / "universes")
        code_fingerprint = sha256_json({"component": "ModelRunBuilder", "version": 1})
        content_payload = {
            "definition": definition["generation_id"],
            "dataset": dataset_manifest["generation_id"],
            "export": export_manifest["generation_id"],
            "receipt": receipt_manifest["generation_id"],
        }
        run_content_generation_id = sha256_json(content_payload)
        bound_definition = dict(definition)
        bound_definition["model_run_content_generation_id"] = run_content_generation_id
        bound_definition["generation_id"] = "0" * 64
        bound_definition["manifest_digest_sha256"] = "0" * 64
        definition_generation_id, definition_manifest_digest = model_manifest_identities(
            bound_definition, schema_name="model_definition"
        )
        bound_definition["generation_id"] = definition_generation_id
        bound_definition["manifest_digest_sha256"] = definition_manifest_digest

        manifest: dict[str, Any] = {
            "contract_version": 1,
            "run_content_generation_id": run_content_generation_id,
            "model_definition_generation_id": definition_generation_id,
            "model_dataset_generation_id": dataset_manifest["generation_id"],
            "qlib_export_generation_id": export_manifest["generation_id"],
            "init_receipt_generation_id": receipt_manifest["generation_id"],
            "code_fingerprint": code_fingerprint,
            "environment_lock_sha256": environment_lock_sha256,
            "determinism_controls": determinism_controls,
            "reproducibility_mode": reproducibility_mode,
            "quality_report_checksum_sha256": "0" * 64,
            **({"logical_tolerance": 1e-12} if reproducibility_mode == "logical_fingerprint" else {}),
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation_id,_=model_manifest_identities(manifest,schema_name="model_run",exclude_fields={"quality_report_checksum_sha256"})
        bound_report, report_checksum = bind_reviewed_quality_decision(
            quality_decision,binding_type="model_run_v1",
            subject_generation_id=generation_id,
            subject_content_sha256=sha256_json({key:value for key,value in manifest.items() if key not in {"quality_report_checksum_sha256","generation_id","manifest_digest_sha256","run_id","created_at"}}),
        )
        governance_dir=Path(store_root)/"external_quality_reviews";governance_dir.mkdir(parents=True,exist_ok=True)
        report_path=governance_dir/f"{report_checksum}.json"
        report_staging=report_path.with_suffix(f".staging.{uuid.uuid4().hex}")
        report_staging.write_text(json.dumps(bound_report,sort_keys=True,indent=2)+"\n");os.replace(report_staging,report_path);fsync_dir(governance_dir)
        manifest["quality_report_checksum_sha256"]=report_checksum
        manifest["generation_id"] = generation_id
        _,manifest_digest=model_manifest_identities(manifest,schema_name="model_run",exclude_fields={"quality_report_checksum_sha256"})
        manifest["manifest_digest_sha256"] = manifest_digest
        ModelContractLoader.validate("model_run", manifest)
        return manifest, bound_definition
