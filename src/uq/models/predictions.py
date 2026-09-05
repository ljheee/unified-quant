from __future__ import annotations

import json
import fcntl
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as arrow
import pyarrow.parquet as parquet

from ..contracts.canonical_v2 import file_sha256_bytes, fsync_dir, fsync_tree
from ..contracts.gate_contracts import validate_contract
from ..contracts.model_layer import ModelContractLoader, ModelQualityReviewRegistry, bind_reviewed_quality_decision, model_manifest_identities, sha256_json
from ..errors import ContractError
from .trainer import ArtifactStore


class PredictionBuilder:
    """Build a prediction set manifest and data from model artifact inference."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.predictions_dir = self.root / "predictions"
        self.artifact_store = ArtifactStore(self.root)

    def build(
        self,
        *,
        prediction_set_name: str,
        model_artifact_generation_id: str,
        model_artifact_checksum: str,
        input_dataset_generation_id: str,
        run_generation_id: str,
        artifact_store: ArtifactStore | None,
        decision_date: str,
        scores: pd.DataFrame,
        eligibility_policy: str,
        eligibility_status: str,
        score_semantics: dict[str, Any] | None = None,
        quality_decision: dict[str, Any],
    ) -> tuple[dict[str, Any], bytes]:
        from datetime import date as _date
        try:
            _date.fromisoformat(decision_date)
        except ValueError as exc:
            raise ContractError(f"invalid decision_date: {decision_date}") from exc
        if scores.empty:
            raise ContractError("cannot publish empty prediction set")
        key_columns = ["instrument"] + (["datetime"] if "datetime" in scores.columns else [])
        if scores.duplicated(key_columns).any():
            raise ContractError(f"duplicate prediction keys on {key_columns}")
        score_columns = [c for c in scores.columns if c not in {"instrument", "datetime"}]
        for col in score_columns:
            if scores[col].isna().any() or not np.isfinite(scores[col].dropna()).all():
                raise ContractError(f"non-finite score detected in column {col}")
        if eligibility_policy != "reviewed-v1" or eligibility_status != "passed":
            raise ContractError("prediction eligibility policy and passed status are mandatory")
        expected_score_semantics = {
            "unit": {"raw_score", "rank_percent"},
            "direction": {"higher_better", "lower_better"},
            "ranking_scope": {"universe"},
            "tie_policy": {"instrument_order", "score_stable"},
            "normalization": {"none", "cross_sectional_rank"},
        }
        score_semantics = score_semantics or {
            "unit": "raw_score",
            "direction": "higher_better",
            "ranking_scope": "universe",
            "tie_policy": "instrument_order",
            "normalization": "none",
        }
        for key, allowed in expected_score_semantics.items():
            if score_semantics.get(key) not in allowed:
                raise ContractError(f"unsupported reviewed prediction {key}: {score_semantics.get(key)}")
        eligibility_checks = [
            check for check in quality_decision.get("checks", [])
            if check.get("name") == "eligibility_coverage"
        ]
        if len(eligibility_checks) != 1 or eligibility_checks[0].get("result") != "passed":
            raise ContractError("prediction quality decision must contain passed eligibility_coverage")

        store = artifact_store or self.artifact_store
        try:
            artifact_manifest, _ = store.read(run_generation_id, model_artifact_generation_id)
        except ContractError as exc:
            raise ContractError("prediction requires an accepted model artifact") from exc
        if (
            artifact_manifest["artifact_checksum_sha256"] != model_artifact_checksum
        ):
            raise ContractError("prediction model artifact checksum mismatch")

        output_columns = list(scores.columns)
        artifact, data_checksum = self._serialize(scores)

        manifest: dict[str, Any] = {
            "contract_version": 1,
            "prediction_set_name": prediction_set_name,
            "model_artifact_generation_id": model_artifact_generation_id,
            "model_artifact_checksum_sha256": model_artifact_checksum,
            "input_dataset_generation_id": input_dataset_generation_id,
            "model_run_generation_id": run_generation_id,
            "decision_date": decision_date,
            "visible_cutoff": f"{decision_date}T15:00:00+08:00",
            "score_semantics": {
                "column": score_columns[0] if score_columns else "score",
                **score_semantics,
            },
            "declared_output_columns": output_columns,
            "actual_output_columns": output_columns,
            "column_set_exact_match": True,
            "eligibility_policy": "reviewed-v1",
            "eligibility_status": eligibility_status,
            "row_count": len(scores),
            "data_checksum_sha256": data_checksum,
            "serialization_profile_id": "parquet-v1",
            "quality_report_checksum_sha256": "0" * 64,
            "run_id": "00000000-0000-4000-8000-000000000000",
            "created_at": "1970-01-01T00:00:00+00:00",
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation_id,_=model_manifest_identities(manifest,schema_name="prediction_set",exclude_fields={"quality_report_checksum_sha256"})
        bound_report, report_checksum = bind_reviewed_quality_decision(
            quality_decision,binding_type="prediction_set_v1",
            subject_generation_id=generation_id,
            subject_content_sha256=sha256_json({key:value for key,value in manifest.items() if key not in {"quality_report_checksum_sha256","generation_id","manifest_digest_sha256","run_id","created_at"}}),
        )
        governance_dir=self.root/"external_quality_reviews";governance_dir.mkdir(parents=True,exist_ok=True)
        report_path=governance_dir/f"{report_checksum}.json"
        report_staging=report_path.with_suffix(f".staging.{uuid.uuid4().hex}")
        report_staging.write_text(json.dumps(bound_report,sort_keys=True,indent=2)+"\n");os.replace(report_staging,report_path);fsync_dir(governance_dir)
        manifest["quality_report_checksum_sha256"]=report_checksum
        _,manifest_digest=model_manifest_identities(manifest,schema_name="prediction_set",exclude_fields={"quality_report_checksum_sha256"})
        manifest["generation_id"] = generation_id
        manifest["manifest_digest_sha256"] = manifest_digest
        validate_contract("prediction_set.v1.json", manifest)
        expected_generation, expected_digest = model_manifest_identities(
            manifest, schema_name="prediction_set", exclude_fields={"quality_report_checksum_sha256"}
        )
        if manifest["generation_id"] != expected_generation or manifest["manifest_digest_sha256"] != expected_digest:
            raise ContractError("prediction manifest identity mismatch")
        return manifest, artifact

    def publish(self, manifest: dict[str, Any], artifact_bytes: bytes) -> Path:
        if manifest.get("eligibility_status") != "passed":
            raise ContractError("prediction publication requires passed eligibility status")
        ModelContractLoader.validate("prediction_set", manifest)
        checksum = manifest.get("quality_report_checksum_sha256")
        report_path = self.root / "external_quality_reviews" / f"{checksum}.json"
        try:
            quality_report = json.loads(report_path.read_text())
            ModelContractLoader.validate("model_quality_report", quality_report)
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("prediction quality report unavailable or malformed") from exc
        if (
            quality_report["binding_type"] != "prediction_set_v1"
            or quality_report["bound_generation_id"] != manifest["generation_id"]
            or quality_report["status"] not in {"passed", "warning"}
        ):
            raise ContractError("prediction quality report rejects publication")
        partition = (
            self.predictions_dir
            / f"prediction_set={manifest['generation_id']}"
            / f"date={manifest['decision_date']}"
        )
        if partition.exists():
            raise ContractError(f"immutable prediction already exists: {partition}")

        self.artifact_store.read(
            manifest["model_run_generation_id"], manifest["model_artifact_generation_id"]
        )
        staging = partition.with_name(f"{partition.name}.staging.{uuid.uuid4().hex[:8]}")
        staging.mkdir(parents=True)
        lock_path = partition.parent / "publication.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                (staging / "data.parquet").write_bytes(artifact_bytes)
                readback = file_sha256_bytes((staging / "data.parquet").read_bytes())
                if readback != manifest["data_checksum_sha256"]:
                    raise ContractError("prediction readback checksum mismatch")
                (staging / "manifest.json").write_text(
                    json.dumps(manifest, sort_keys=True, indent=2) + "\n"
                )
                fsync_tree(staging)
                os.replace(staging, partition)
                fsync_dir(partition.parent)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return partition

    def read(self, prediction_generation_id: str, decision_date: str) -> tuple[dict[str, Any], pd.DataFrame]:
        partition = (
            self.predictions_dir
            / f"prediction_set={prediction_generation_id}"
            / f"date={decision_date}"
        )
        manifest_path = partition / "manifest.json"
        data_path = partition / "data.parquet"
        if not manifest_path.is_file() or not data_path.is_file():
            raise ContractError(f"unpublished or incomplete prediction: {partition}")
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise ContractError("malformed prediction manifest") from exc

        ModelContractLoader.validate("prediction_set", manifest)
        if manifest.get("decision_date") != decision_date:
            raise ContractError("decision date does not match prediction path partition")
        expected_generation, _ = model_manifest_identities(manifest, schema_name="prediction_set", exclude_fields={"quality_report_checksum_sha256"})
        if manifest["generation_id"] != expected_generation:
            raise ContractError("prediction stable generation mismatch")
        checksum=manifest["quality_report_checksum_sha256"]
        try: report=json.loads((self.root/"external_quality_reviews"/f"{checksum}.json").read_text())
        except (OSError,json.JSONDecodeError) as exc: raise ContractError("prediction quality report unavailable") from exc
        ModelContractLoader.validate("model_quality_report",report)
        actual=sha256_json({k:v for k,v in report.items() if k!="report_checksum_sha256"})
        if checksum!=actual or report["binding_type"]!="prediction_set_v1" or report["bound_generation_id"]!=manifest["generation_id"] or report["status"] not in {"passed","warning"}:
            raise ContractError("prediction quality report rejects read")
        self.artifact_store.read(
            manifest["model_run_generation_id"], manifest["model_artifact_generation_id"]
        )
        actual_checksum = file_sha256_bytes(data_path.read_bytes())
        if actual_checksum != manifest["data_checksum_sha256"]:
            raise ContractError("tampered prediction data prevents read")
        frame = pd.read_parquet(data_path)
        if list(frame.columns) != manifest.get("actual_output_columns"):
            raise ContractError("prediction column mismatch on read")
        if len(frame) != manifest.get("row_count"):
            raise ContractError("prediction row count does not match manifest")
        key_columns = ["instrument"] + (["datetime"] if "datetime" in frame.columns else [])
        if frame.duplicated(key_columns).any():
            raise ContractError("duplicate prediction keys on read")
        score_column = manifest["score_semantics"]["column"]
        if score_column not in frame.columns:
            raise ContractError("declared prediction score column missing")
        if frame[score_column].isna().any() or not np.isfinite(frame[score_column].dropna()).all():
            raise ContractError("non-finite prediction scores on read")
        return manifest, frame

    @staticmethod
    def _serialize(frame: pd.DataFrame) -> tuple[bytes, str]:
        ordered = frame.sort_values(["instrument", "datetime"] if "datetime" in frame.columns else ["instrument"], kind="mergesort").reset_index(drop=True)
        table = arrow.Table.from_pandas(ordered, preserve_index=False)
        sink = arrow.BufferOutputStream()
        parquet.write_table(table, sink, compression="snappy")
        artifact = sink.getvalue().to_pybytes()
        return artifact, file_sha256_bytes(artifact)
