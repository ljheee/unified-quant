from __future__ import annotations

import hashlib
import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import qlib
from qlib.contrib.model.linear import LinearModel
from qlib.data import D
from qlib.data.dataset import DatasetH
from qlib.data.dataset.handler import DataHandlerLP
from qlib.data.dataset.loader import QlibDataLoader

from ..contracts.canonical_v2 import file_sha256_bytes
from ..contracts.model_layer import ModelContractLoader, model_manifest_identities
from ..errors import ContractError


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class QlibRuntimeTrainer:
    """Train through the Qlib model API against a verified governed export."""

    def train(
        self,
        *,
        definition: dict[str, Any],
        dataset_manifest: dict[str, Any],
        export_manifest: dict[str, Any],
        receipt_manifest: dict[str, Any],
        verified_export: tuple[dict[str, Any], Path],
        feature_columns: list[str],
        label_column: str,
    ) -> tuple[dict[str, Any], bytes]:
        if definition["algorithm"] != "qlib_linear":
            raise ContractError(f"Qlib trainer only supports qlib_linear, got {definition['algorithm']}")
        if definition["serializer_version"] != "joblib-v1":
            raise ContractError("Qlib trainer requires serializer joblib-v1")

        verified_manifest, snapshot = verified_export
        for family, document in (
            ("model_definition", definition),
            ("model_dataset", dataset_manifest),
            ("qlib_dataset_export", export_manifest),
            ("qlib_init_receipt", receipt_manifest),
        ):
            ModelContractLoader.validate(family, document)

        expected_generation, expected_digest = model_manifest_identities(
            verified_manifest,
            schema_name="qlib_dataset_export",
            exclude_fields={"export_layout", "quality_report_checksum_sha256"},
        )
        if (
            verified_manifest["generation_id"] != expected_generation
            or verified_manifest["manifest_digest_sha256"] != expected_digest
            or verified_manifest["generation_id"] != export_manifest["generation_id"]
            or verified_manifest["manifest_digest_sha256"] != export_manifest["manifest_digest_sha256"]
        ):
            raise ContractError("verified Qlib export identity mismatch")
        if receipt_manifest["export_generation_id"] != export_manifest["generation_id"]:
            raise ContractError("Qlib receipt does not bind the verified export")
        if receipt_manifest["export_manifest_digest_sha256"] != export_manifest["manifest_digest_sha256"]:
            raise ContractError("Qlib receipt export digest mismatch")
        file_list_checksum = _sha256_text(json.dumps([entry["path"] for entry in export_manifest["files"]]))
        if receipt_manifest["file_list_checksum_sha256"] != file_list_checksum:
            raise ContractError("Qlib receipt file-list mismatch")
        for field in ("calendar_checksum_sha256", "instruments_checksum_sha256", "feature_mapping_checksum_sha256"):
            if receipt_manifest[field] != export_manifest[field]:
                raise ContractError(f"Qlib receipt {field.removesuffix('_checksum_sha256')} mismatch")
        if receipt_manifest["qlib_import_path"] != "qlib" or receipt_manifest["qlib_version"] != qlib.__version__:
            raise ContractError("Qlib receipt runtime identity mismatch")
        if receipt_manifest["no_ungoverned_source_assertion"] is not True:
            raise ContractError("Qlib receipt does not assert a governed source boundary")
        for entry in export_manifest["files"]:
            path = snapshot / entry["path"]
            if (
                not path.is_file()
                or file_sha256_bytes(path.read_bytes()) != entry["checksum_sha256"]
                or path.stat().st_size != entry["byte_size"]
            ):
                raise ContractError(f"tampered Qlib export file: {entry['path']}")

        mapping_path = snapshot / "feature_mapping.json"
        feature_mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        expected_columns = set(feature_columns) | {label_column}
        if set(feature_mapping) != expected_columns:
            raise ContractError("Qlib export feature mapping does not match training schema")
        if len(feature_columns) != len(set(feature_columns)):
            raise ContractError("Qlib training feature columns must be unique")
        if label_column in feature_columns:
            raise ContractError("Qlib label column cannot also be a feature column")

        ordered_features = dataset_manifest["ordered_features"]
        if feature_columns != ordered_features:
            raise ContractError("Qlib training features do not match dataset ordered features")

        calendar_dates = (snapshot / "calendars" / "day.txt").read_text(encoding="utf-8").splitlines()
        calendar_dates = [date for date in calendar_dates if date]
        if not calendar_dates:
            raise ContractError("Qlib export calendar is empty")

        qlib.init(
            provider_uri=str(snapshot),
            region="cn",
            expression_cache=None,
            dataset_cache=None,
            kernels=1,
            logging_level="WARNING",
        )

        instruments = sorted(
            line.split("\t", 1)[0].lower()
            for line in (snapshot / "instruments" / "all.txt").read_text(encoding="utf-8").splitlines()
            if line
        )
        if not instruments:
            raise ContractError("Qlib export instruments are empty")

        feature_fields = [f"${feature_mapping[column]}" for column in feature_columns]
        label_field = f"${feature_mapping[label_column]}"
        data_loader = QlibDataLoader(
            config={"feature": feature_fields, "label": [label_field]},
            freq="day",
        )
        handler = DataHandlerLP(
            instruments=instruments,
            start_time=calendar_dates[0],
            end_time=calendar_dates[-1],
            data_loader=data_loader,
            infer_processors=[],
            learn_processors=[],
            process_type="append",
        )
        dataset = DatasetH(handler, segments={"train": (calendar_dates[0], calendar_dates[-1])})
        prepared = dataset.prepare("train", col_set=["feature", "label"])
        prepared = prepared.dropna()
        if prepared.empty or len(prepared) < 2:
            raise ContractError("Qlib training requires at least two supervised observations")

        alpha = float(definition["hyperparameters"].get("alpha", 1.0))
        fit_intercept = bool(definition["hyperparameters"].get("fit_intercept", False))
        np.random.seed(int(definition["seed_policy"]["base_seed"]))
        model = LinearModel(estimator="ridge", alpha=alpha, fit_intercept=fit_intercept)
        model.fit(dataset)

        artifact_buffer = io.BytesIO()
        joblib.dump(model, artifact_buffer, compress=3)
        artifact_bytes = artifact_buffer.getvalue()
        artifact_checksum = file_sha256_bytes(artifact_bytes)
        run_content_generation_id = definition["model_run_content_generation_id"]
        if not isinstance(run_content_generation_id, str) or len(run_content_generation_id) != 64:
            raise ContractError("model definition is not bound to a validated model_run_content_generation_id")

        manifest: dict[str, Any] = {
            "contract_version": 1,
            "artifact_filename": "model.joblib",
            "artifact_checksum_sha256": artifact_checksum,
            "byte_size": len(artifact_bytes),
            "runtime_name": "qlib_linear",
            "runtime_version": qlib.__version__,
            "runtime_import_path": "qlib.contrib.model.linear.LinearModel",
            "model_run_content_generation_id": run_content_generation_id,
            "serialization_profile_id": "joblib-v1",
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        return manifest, artifact_bytes
