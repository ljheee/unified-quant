from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

from .canonical_v2 import file_sha256_bytes
from .gate_contracts import adjustment_snapshot_generation, canonical_json, factor_manifest_identities, validate_contract, validate_contract_path
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
    "portfolio_definition",
    "target_weights",
    "backtest_config",
    "backtest_result",
}
_RUN_LOCAL_FIELDS = {"run_id", "created_at"}
_QUALITY_BOUND_FIELDS = {
    "model_dataset": {"quality_report_checksum_sha256", "logical_fingerprint"},
    "model_run": {"quality_report_checksum_sha256"},
    "qlib_dataset_export": {"export_layout", "quality_report_checksum_sha256"},
    "qlib_init_receipt": {"quality_report_checksum_sha256"},
    "prediction_set": {"quality_report_checksum_sha256"},
}
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
        validate_contract("model_quality_report.v2.json", payload)
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
        if schema_name == "model_quality_report.v1":
            _reject_non_finite(payload)
            validate_contract("model_quality_report.v1.json", payload)
            checksum_payload = {key: value for key, value in payload.items() if key != "report_checksum_sha256"}
            if payload["report_checksum_sha256"] != sha256_json(checksum_payload):
                raise ContractError("model quality report checksum mismatch")
            return
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
        if schema_name in ("portfolio_definition", "target_weights", "backtest_config", "backtest_result"):
            exclude_fields = set()
        else:
            exclude_fields = _QUALITY_BOUND_FIELDS.get(schema_name, {"quality_report_checksum_sha256"}).copy()
        if schema_name == "model_dataset":
            exclude_fields.add("logical_fingerprint")
        elif schema_name in {"accepted_factor_index_query", "accepted_factor_index_response", "feature_schema"}:
            exclude_fields = set()
        expected_generation, expected_digest = model_manifest_identities(
            payload, schema_name=schema_name, exclude_fields=exclude_fields
        )
        if payload["generation_id"] != expected_generation:
            raise ContractError(f"{schema_name} stable generation mismatch")
        if payload["manifest_digest_sha256"] != expected_digest:
            raise ContractError(f"{schema_name} manifest digest mismatch")


class ModelQualityReviewRegistry:
    """Typed view over the externally reviewed quality-check registry."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or Path(__file__).resolve().parents[3] / "config/model-quality-reviews.v1.json")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("model quality review registry is unavailable or malformed") from exc
        if payload.get("registry_version") != 1 or payload.get("status") != "reviewed":
            raise ContractError("model quality review registry is not reviewed")
        if not isinstance(payload.get("reviewer"), str) or not payload["reviewer"]:
            raise ContractError("model quality review registry has no reviewer")
        bindings = payload.get("bindings")
        if not isinstance(bindings, dict):
            raise ContractError("model quality review registry has no bindings")
        self.reviewer = payload["reviewer"]
        self.bindings = bindings

    def validate_report(self, report: dict[str, Any]) -> None:
        binding_type = report.get("binding_type")
        binding = self.bindings.get(binding_type)
        if not isinstance(binding, dict):
            raise ContractError(f"no reviewed quality policy for {binding_type}")
        if report.get("policy") != binding.get("policy"):
            raise ContractError("quality report policy does not match reviewed registry")
        if report.get("status") not in binding.get("allowed_statuses", []):
            raise ContractError("quality report status is not approved by reviewed registry")
        checks = report.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ContractError("quality report requires reviewed checks")
        allowed = set(binding.get("allowed_checks", []))
        for check in checks:
            if not isinstance(check, dict) or check.get("name") not in allowed:
                raise ContractError(f"quality check is not approved: {check.get('name')}")
            if check.get("result") not in {"passed", "failed"}:
                raise ContractError("quality check result is invalid")
        if report.get("status") == "passed" and any(check.get("result") == "failed" for check in checks):
            raise ContractError("passed quality report contains failed checks")


_MODEL_QUALITY_REVIEW_REGISTRY = ModelQualityReviewRegistry()


def review_signature(
    *,
    reviewer: str,
    binding_type: str,
    subject_content_sha256: str,
    policy: str,
    status: str,
    checks: list[dict[str, Any]],
    errors: list[str],
    warnings: list[str],
) -> str:
    """Return the deterministic signature used by the external review registry."""
    return sha256_json({
        "binding_type": binding_type, "checks": checks, "errors": errors,
        "policy": policy, "reviewer": reviewer, "status": status,
        "subject_content_sha256": subject_content_sha256, "warnings": warnings,
    })


def create_reviewed_quality_decision(
    *,
    binding_type: str,
    policy: str,
    status: str,
    checks: list[dict[str, Any]],
    errors: list[str],
    warnings: list[str],
    producer_code_fingerprint: str,
) -> dict[str, Any]:
    """Create an immutable decision. Caller must hold the external reviewer role."""
    registry_binding = _MODEL_QUALITY_REVIEW_REGISTRY.bindings.get(binding_type)
    if registry_binding is None:
        raise ContractError(f"binding type {binding_type} is not registered in reviewer registry")
    if policy != registry_binding["policy"]:
        raise ContractError(f"policy {policy} does not match registered policy {registry_binding['policy']}")
    if status not in registry_binding["allowed_statuses"]:
        raise ContractError(f"status {status} not in allowed statuses {registry_binding['allowed_statuses']}")
    allowed_check_names = set(registry_binding["allowed_checks"])
    for check in checks:
        if check.get("name") not in allowed_check_names:
            raise ContractError(f"check '{check.get('name')}' not in allowed checks {sorted(allowed_check_names)}")
    reviewer = _MODEL_QUALITY_REVIEW_REGISTRY.reviewer
    signature = sha256_json({
        "binding_type": binding_type, "checks": checks, "errors": errors,
        "policy": policy, "reviewer": reviewer, "status": status,
        "warnings": warnings,
    })
    return {
        "report_version": 2, "binding_type": binding_type, "policy": policy,
        "status": status, "checks": checks, "errors": errors,
        "warnings": warnings, "producer_code_fingerprint": producer_code_fingerprint,
        "reviewer": reviewer, "review_signature_sha256": signature,
    }


def bind_reviewed_quality_decision(
    decision: dict[str, Any],
    *,
    binding_type: str,
    subject_generation_id: str,
    subject_content_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Mechanically bind an unchanged external decision to a stable subject."""
    if not isinstance(decision, dict):
        raise ContractError("external quality decision must be an object")
    required = {
        "report_version", "binding_type", "policy", "status", "checks",
        "errors", "warnings", "producer_code_fingerprint", "reviewer",
        "review_signature_sha256",
    }
    if set(decision) != required:
        raise ContractError("external quality decision has unexpected or missing fields")
    if decision.get("report_version") != 2 or decision.get("binding_type") != binding_type:
        raise ContractError(f"quality decision does not match {binding_type}")
    _MODEL_QUALITY_REVIEW_REGISTRY.validate_report({**decision, "bound_generation_id": subject_generation_id})
    expected_signature = sha256_json({
        "binding_type": binding_type, "checks": decision["checks"],
        "errors": decision["errors"], "policy": decision["policy"],
        "reviewer": decision["reviewer"], "status": decision["status"],
        "warnings": decision["warnings"],
    })
    if decision.get("review_signature_sha256") != expected_signature:
        raise ContractError("external quality decision signature mismatch")
    subject_digest = subject_content_sha256 or subject_generation_id
    unsigned_report = {
        **decision, "bound_generation_id": subject_generation_id,
        "subject_content_sha256": subject_digest,
    }
    report = {**unsigned_report, "report_checksum_sha256": sha256_json(unsigned_report)}
    ModelContractLoader.validate("model_quality_report", report)
    return report, report["report_checksum_sha256"]


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
    *,
    universe_root: Path | str | None = None,
) -> dict[str, list[str]]:
    """Validate cross-manifest generation bindings among model contracts."""
    universe_root = Path(universe_root) if universe_root is not None else Path.cwd() / "universes"
    errors: list[str] = []
    def validate_factor_document(generation_id: str, document: Any) -> None:
        if not isinstance(document, dict):
            errors.append(f"invalid factor manifest for generation {generation_id}")
            return
        try:
            validate_contract_path(Path(__file__).resolve().parents[3] / "config/schemas/manifests/factor_manifest.v1.json", document)
            unsigned = {key: value for key, value in document.items() if key != "manifest_digest_sha256"}
            expected_generation, expected_digest = factor_manifest_identities({
                key: value for key, value in unsigned.items() if key != "generation_id"
            })
            if document.get("generation_id") != expected_generation or document.get("manifest_digest_sha256") != expected_digest:
                raise ContractError("factor identity mismatch")
        except ContractError:
            errors.append(f"invalid factor manifest content for generation {generation_id}")

    def validate_universe_document(document: Any, generation_id: str | None) -> None:
        if not isinstance(document, dict):
            errors.append("missing or invalid universe snapshot binding")
            return
        try:
            validate_contract("universe_snapshot.v1.json", document)
            unsigned = {key: value for key, value in document.items() if key != "generation_id"}
            if document.get("generation_id") != adjustment_snapshot_generation(unsigned):
                raise ContractError("universe identity mismatch")
        except ContractError:
            errors.append("invalid universe snapshot content")
            return
        artifact = document.get("members_artifact") or {}
        path = universe_root / document["universe_id"] / document["generation_id"] / artifact.get("path", "")
        try:
            resolved = path.resolve(strict=True)
            root_resolved = universe_root.resolve(strict=True)
            contained = resolved.is_relative_to(root_resolved)
        except (OSError, RuntimeError, ValueError):
            contained = False
        if not contained:
            errors.append("universe member artifact is missing or outside approved store")
        elif file_sha256_bytes(path.read_bytes()) != artifact.get("checksum_sha256"):
            errors.append("universe member artifact checksum mismatch")
        if generation_id and document.get("generation_id") != generation_id:
            errors.append("universe_snapshot_generation_id mismatch")

    dataset = documents.get("model_dataset")
    if dataset:
        label_gen = dataset.get("label_generation_id")
        label = documents.get("label_set")
        if label is None:
            errors.append("missing label_set document")
        else:
            if label.get("generation_id") != label_gen:
                errors.append("label_generation_id mismatch")
            if label.get("name") != dataset.get("label_set_name"):
                errors.append("label_set_name mismatch")
        factor_documents = documents.get("factor_manifests") or {}
        for factor_gen in dataset.get("factor_generation_ids", []):
            if not factor_gen or len(factor_gen) != 64:
                errors.append("invalid factor_generation_id")
            elif factor_gen not in factor_documents:
                errors.append(f"missing factor manifest for generation {factor_gen}")
            else:
                validate_factor_document(factor_gen, factor_documents[factor_gen])
        universe_gen = dataset.get("universe_snapshot_generation_id")
        universe_document = documents.get("universe_snapshot")
        if not universe_gen or len(universe_gen) != 64:
            errors.append("missing or invalid universe snapshot generation")
        else:
            validate_universe_document(universe_document, universe_gen)

    run = documents.get("model_run")
    if run:
        definition_gen = run.get("model_definition_generation_id")
        definition = documents.get("model_definition")
        if definition is None:
            errors.append("missing model_definition document for run")
        elif definition.get("generation_id") != definition_gen:
            errors.append("run.definition_generation_id mismatch")
        dataset_gen = run.get("model_dataset_generation_id")
        if dataset and dataset.get("generation_id") != dataset_gen:
            errors.append("run.dataset_generation_id mismatch")
        export_gen = run.get("qlib_export_generation_id")
        export = documents.get("qlib_dataset_export")
        if export is None or export.get("generation_id") != export_gen:
            errors.append("missing or mismatched run.qlib_export_generation_id")
        receipt_gen = run.get("init_receipt_generation_id")
        receipt_for_run = documents.get("qlib_init_receipt")
        if receipt_for_run is None or receipt_for_run.get("generation_id") != receipt_gen:
            errors.append("missing or mismatched run.init_receipt_generation_id")

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
