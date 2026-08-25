from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

from .gate_contracts import canonical_json, validate_contract
from ..errors import ContractError

_SCHEMA_NAMES = {
    "accepted_factor_index_query",
    "accepted_factor_index_response",
    "label_set",
    "model_dataset",
    "model_definition",
    "model_run",
    "model_artifact",
    "qlib_dataset_export",
    "qlib_init_receipt",
    "prediction_set",
    "feature_schema",
}
_RUN_LOCAL_FIELDS = {"run_id", "created_at"}
_MODEL_CONTRACT_FAMILIES = {*_SCHEMA_NAMES, "model_quality_report"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ORDERING_FIELDS = ("factor_set", "factor_version", "partition_date", "generation_id")


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError("non-finite number is forbidden")
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def model_manifest_identities(
    payload: Mapping[str, Any],
    *,
    schema_name: str,
    exclude_fields: set[str] | None = None,
) -> tuple[str, str]:
    """Return stable content generation and complete manifest digest."""
    if schema_name not in _SCHEMA_NAMES:
        raise ContractError(f"unknown model contract family: {schema_name}")
    document = dict(payload)
    for key in ("generation_id", "manifest_digest_sha256"):
        value = document.get(key)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ContractError(f"{schema_name} missing valid {key}")
        del document[key]
    excluded_fields = _RUN_LOCAL_FIELDS | (exclude_fields or set())
    generation_payload = {key: value for key, value in document.items() if key not in excluded_fields}
    generation_id = sha256_json(generation_payload)
    manifest_digest = sha256_json({**document, "generation_id": generation_id})
    return generation_id, manifest_digest


def validate_model_contract(schema_name: str, payload: dict[str, Any]) -> None:
    if schema_name == "model_dataset":
        validate_contract("model_dataset.v1.json", payload)
        return
    if schema_name == "model_quality_report":
        validate_contract("model_quality_report.v1.json", payload)
    elif schema_name in _SCHEMA_NAMES:
        validate_contract(f"{schema_name}.v1.json", payload)
    else:
        raise ContractError(f"unknown model contract family: {schema_name}")


class ModelContractLoader:
    """Load and verify a durable model-layer contract document."""

    def __init__(self, accepted_root: Path | str | None = None) -> None:
        self.accepted_root = None if accepted_root is None else Path(accepted_root)

    def load(self, schema_name: str, path: Path | str) -> dict[str, Any]:
        artifact_path = Path(path)
        try:
            raw = artifact_path.read_bytes()
        except FileNotFoundError as exc:
            raise ContractError(f"missing {schema_name} contract") from exc
        except OSError as exc:
            raise ContractError(f"unreadable {schema_name} contract") from exc
        resolved = self._safe_resolve(artifact_path)
        if self.accepted_root is not None:
            root_resolved = self._safe_resolve(self.accepted_root)
            try:
                contained = resolved.is_relative_to(root_resolved)
            except ValueError:
                contained = False
            if not contained:
                raise ContractError("accepted contract lies outside approved root")
        try:
            payload = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(
                ContractError(f"non-finite JSON constant: {value}")
            ))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"malformed {schema_name} contract serialization") from exc
        if not isinstance(payload, dict):
            raise ContractError("contract must be a JSON object")
        self.validate(schema_name, payload)
        return payload

    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ContractError(f"unsafe or missing contract path: {path}") from exc
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise ContractError("symbolic links are forbidden in accepted contracts")
        return resolved

    @staticmethod
    def validate(schema_name: str, payload: dict[str, Any]) -> None:
        if schema_name not in _MODEL_CONTRACT_FAMILIES:
            raise ContractError(f"unknown model contract family: {schema_name}")
        _reject_non_finite(payload)
        validate_model_contract(schema_name, payload)
        if schema_name in {"accepted_factor_index_query", "accepted_factor_index_response", "model_quality_report"}:
            if schema_name == "model_quality_report":
                checksum_payload = {key: value for key, value in payload.items() if key != "report_checksum_sha256"}
                if payload["report_checksum_sha256"] != sha256_json(checksum_payload):
                    raise ContractError("model quality report checksum mismatch")
            return
        exclude_fields = {"logical_fingerprint"} if schema_name == "model_dataset" else None
        expected_generation, expected_digest = model_manifest_identities(
            payload, schema_name=schema_name, exclude_fields=exclude_fields
        )
        if payload["generation_id"] != expected_generation:
            raise ContractError(f"{schema_name} stable generation mismatch")
        if payload["manifest_digest_sha256"] != expected_digest:
            raise ContractError(f"{schema_name} manifest digest mismatch")


class AcceptedFactorIndexContract:
    """Validate contract-only accepted-store queries; never perform reads."""

    def __init__(self) -> None:
        self._verified_generations: set[str] = set()

    def register_verified_generation(self, generation_id: str) -> None:
        if not isinstance(generation_id, str) or not _SHA256.fullmatch(generation_id):
            raise ContractError("invalid factor generation identity")
        self._verified_generations.add(generation_id)

    @staticmethod
    def _validate_cursor(query: dict[str, Any]) -> None:
        ordering = query["ordering"]
        cursor = query.get("pagination", {}).get("after_sort_key")
        if cursor is None:
            return
        if len(cursor) != len(ordering):
            raise ContractError("pagination cursor does not match ordering arity")

    def list(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        request = dict(query)
        request.setdefault("pagination", {"limit": 10000})
        validate_model_contract("accepted_factor_index_query", request)
        self._validate_cursor(request)
        requested = request.get("filters", {}).get("generation_id")
        if requested is not None and requested not in self._verified_generations:
            raise ContractError("requested factor generation is not verified as accepted")
        return []

    def index(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        return self.list(query)


def resolve_bindings(
    documents: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    """Validate cross-manifest generation bindings among model contracts."""
    errors: list[str] = []
    dataset = documents.get("model_dataset")
    if dataset:
        label_gen = dataset.get("label_generation_id")
        label = documents.get("label_set")
        if label is None and "label_set" in [k for k in documents]:
            pass
        elif label is None and "label_set" not in documents:
            pass  # label binding deferred when label_set document not provided
        else:
            if label.get("generation_id") != label_gen:
                errors.append("label_generation_id mismatch")
            if label.get("name") != dataset.get("label_set_name"):
                errors.append("label_set_name mismatch")
        for factor_gen in dataset.get("factor_generation_ids", []):
            if not factor_gen or len(factor_gen) != 64:
                errors.append("invalid factor_generation_id")
        universe_gen = dataset.get("universe_snapshot_generation_id")
        if not universe_gen or len(universe_gen) != 64:
            errors.append("missing or invalid universe_snapshot_generation_id")

    run = documents.get("model_run")
    if run:
        definition_gen = run.get("model_definition_generation_id")
        definition = documents.get("model_definition")
        if definition and definition.get("generation_id") != definition_gen:
            errors.append("run.definition_generation_id mismatch")
        dataset_gen = run.get("model_dataset_generation_id")
        if dataset and dataset.get("generation_id") != dataset_gen:
            errors.append("run.dataset_generation_id mismatch")
        export_gen = run.get("qlib_export_generation_id")
        export = documents.get("qlib_dataset_export")
        if export and export.get("generation_id") != export_gen:
            errors.append("run.qlib_export_generation_id mismatch")

    artifact = documents.get("model_artifact")
    if artifact:
        run_content_gen = artifact.get("model_run_content_generation_id")
        if run and run.get("run_content_generation_id") != run_content_gen:
            errors.append("artifact.run_content_generation_id mismatch")

    prediction = documents.get("prediction_set")
    if prediction:
        artifact_gen = prediction.get("model_artifact_generation_id")
        if artifact and artifact.get("generation_id") != artifact_gen:
            errors.append("prediction.artifact_generation_id mismatch")
        input_gen = prediction.get("input_dataset_generation_id")
        if dataset and dataset.get("generation_id") != input_gen:
            errors.append("prediction.input_dataset_generation_id mismatch")

    receipt_doc = documents.get("qlib_init_receipt")
    if receipt_doc:
        export_gen = receipt_doc.get("export_generation_id")
        export_doc = documents.get("qlib_dataset_export")
        if export_doc and export_doc.get("generation_id") != export_gen:
            errors.append("receipt.export_generation_id mismatch")
        if export_doc and receipt_doc.get("export_manifest_digest_sha256") != export_doc.get("manifest_digest_sha256"):
            errors.append("receipt.export_manifest_digest_sha256 mismatch")

    if errors:
        raise ContractError("; ".join(errors))

    return {"resolved_families": sorted(documents.keys()), "errors": []}
