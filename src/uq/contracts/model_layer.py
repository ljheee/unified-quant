from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .canonical_v2 import file_sha256_bytes
from .gate_contracts import adjustment_snapshot_generation, canonical_json, factor_manifest_identities, sha256_bytes, validate_contract, validate_contract_path
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
    "feature_preprocessing",
    "portfolio_definition",
    "target_weights",
    "backtest_config",
    "backtest_result",
}
_RUN_LOCAL_FIELDS = {"run_id", "created_at"}
_QUALITY_BOUND_FIELDS = {
    "model_dataset": {"quality_report_checksum_sha256", "logical_fingerprint"},
    "feature_preprocessing": {"quality_report_checksum_sha256"},
    "model_run": {"quality_report_checksum_sha256"},
    "qlib_dataset_export": {"export_layout", "quality_report_checksum_sha256"},
    "qlib_init_receipt": {"quality_report_checksum_sha256"},
    "prediction_set": {"quality_report_checksum_sha256"},
}
_MODEL_CONTRACT_FAMILIES = {*_SCHEMA_NAMES, "model_quality_report"}
_RESEARCH_SCHEMA_NAMES = {
    "research_run_request",
    "research_run_state",
    "research_run_result",
    "model_definition_template",
    "portfolio_definition_template",
    "quality_decision",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ED25519_SIGNATURE_HEX = re.compile(r"^[0-9a-f]{128}$")
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


def validate_quality_decision_owning_report(report: Mapping[str, Any]) -> str:
    """Validate factor or signed model owning reports without re-signing them."""
    if report.get("binding_type") == "factor_v1":
        from .gate_contracts import sha256_bytes

        validate_contract("quality_report.v1.json", dict(report))
        factor_checksum = sha256_bytes(canonical_json(report))
        failed_error_checks = [
            check for check in report.get("checks", [])
            if check.get("result") == "failed" and check.get("level", "error") == "error"
        ]
        if report.get("status") == "passed" and failed_error_checks:
            raise ContractError("passed factor quality report contains failed checks")
        return factor_checksum
    verify_reviewed_quality_report_signature(report)
    checksum = report.get("report_checksum_sha256")
    if not isinstance(checksum, str):
        raise ContractError("model quality report has no canonical checksum")
    return checksum


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


def research_stage_plan_sha256() -> str:
    """Return the normative Research Chain stage-plan digest."""
    return sha256_json({
        "schema_version": "v1",
        "stage_plan": [
            "resolve_request",
            "factor_computation",
            "dataset_preparation",
            "qlib_export",
            "model_training",
            "prediction_publication",
            "portfolio_construction",
            "backtest_execution",
            "result_reconciliation",
        ],
    })


def _without_physical_path(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_physical_path(item) for item in value]
    if isinstance(value, dict):
        return {key: _without_physical_path(item) for key, item in value.items() if key != "physical_path"}
    return value


def research_contract_identities(
    payload: Mapping[str, Any],
    *,
    schema_name: str,
) -> tuple[str, str]:
    """Derive research-chain content identity and complete manifest digest."""
    document = dict(payload)
    if schema_name == "model_definition_template":
        content_field, digest_field = "template_generation_id", "template_manifest_digest_sha256"
    elif schema_name == "portfolio_definition_template":
        content_field, digest_field = "template_generation_id", "template_manifest_digest_sha256"
    elif schema_name == "research_run_request":
        content_field, digest_field = "request_content_generation_id", "manifest_digest_sha256"
    elif schema_name == "research_run_state":
        content_field, digest_field = "state_content_generation_id", "manifest_digest_sha256"
    elif schema_name == "research_run_result":
        content_field, digest_field = "result_content_generation_id", "manifest_digest_sha256"
    else:
        raise ContractError(f"unknown research contract family: {schema_name}")

    for key in (content_field, digest_field):
        value = document.get(key)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ContractError(f"{schema_name} missing valid {key}")
        del document[key]

    excluded_fields = {content_field, digest_field}
    if schema_name == "research_run_request":
        excluded_fields.update(_RUN_LOCAL_FIELDS)
    elif schema_name == "research_run_result":
        excluded_fields.update(_RUN_LOCAL_FIELDS)
        excluded_fields.update({"request_manifest_digest_sha256", "state_created_at", "state_attempt_digest_sha256"})

    generation_document = _without_physical_path(document) if schema_name == "research_run_result" else document
    generation = sha256_json({
        key: value for key, value in generation_document.items() if key not in excluded_fields
    })
    digest_document = {key: value for key, value in document.items() if key != digest_field}
    digest_document[content_field] = generation
    return generation, sha256_json(digest_document)


def _validate_stage_record_order(stages: list[dict[str, Any]], *, require_complete: bool) -> None:
    stage_order = [
        "resolve_request", "factor_computation", "dataset_preparation", "qlib_export",
        "model_training", "prediction_publication", "portfolio_construction",
        "backtest_execution", "result_reconciliation",
    ]
    stages_seen = [item["stage"] for item in stages]
    if len(stages_seen) != len(set(stages_seen)):
        raise ContractError("stage records contain duplicates")
    indexes = [stage_order.index(stage) for stage in stages_seen]
    if indexes != sorted(indexes):
        raise ContractError("stage records are not in normative order")
    if require_complete and stages_seen != stage_order:
        raise ContractError("result must contain the complete normative stage order")


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
        if schema_name in _RESEARCH_SCHEMA_NAMES:
            _reject_non_finite(payload)
            validate_contract(f"{schema_name}.v1.json", payload)
            if schema_name == "research_run_result":
                _validate_stage_record_order(payload["stage_records"], require_complete=True)
            if schema_name == "research_run_state":
                _validate_stage_record_order(payload["stage_records"], require_complete=False)
            content_field = {
                "model_definition_template": "template_generation_id",
                "portfolio_definition_template": "template_generation_id",
                "research_run_request": "request_content_generation_id",
                "research_run_state": "state_content_generation_id",
                "research_run_result": "result_content_generation_id",
                "quality_decision": "decision_checksum_sha256",
            }[schema_name]
            if schema_name == "quality_decision":
                report = payload.get("owning_report")
                if not isinstance(report, dict):
                    raise ContractError("quality decision missing owning report")
                expected_checksum = validate_quality_decision_owning_report(report)
                if payload[content_field] != expected_checksum:
                    raise ContractError("quality decision checksum mismatch")
                if payload.get("binding_type") != report.get("binding_type"):
                    raise ContractError("quality decision binding mismatch")
                if payload.get("subject_generation_id") != report.get("bound_generation_id"):
                    raise ContractError("quality decision subject generation mismatch")
                subject_digest = report.get("subject_content_sha256")
                if payload.get("subject_manifest_digest_sha256") != (None if subject_digest is None else subject_digest):
                    raise ContractError("quality decision subject digest mismatch")
                if report.get("binding_type") == "factor_v1":
                    if not isinstance(payload.get("trust_anchor_id"), str) or not payload["trust_anchor_id"]:
                        raise ContractError("quality decision trust anchor mismatch")
                elif payload.get("trust_anchor_id") != report.get("key_id"):
                    raise ContractError("quality decision trust anchor mismatch")
                return
            expected_generation, expected_digest = research_contract_identities(
                payload, schema_name=schema_name
            )
            if payload[content_field] != expected_generation:
                raise ContractError(f"{schema_name} stable content identity mismatch")
            digest_field = {
                "model_definition_template": "template_manifest_digest_sha256",
                "portfolio_definition_template": "template_manifest_digest_sha256",
                "research_run_request": "manifest_digest_sha256",
                "research_run_state": "manifest_digest_sha256",
                "research_run_result": "manifest_digest_sha256",
            }[schema_name]
            if payload[digest_field] != expected_digest:
                raise ContractError(f"{schema_name} manifest digest mismatch")
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
                verify_reviewed_quality_report_signature(payload)
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


class ModelQualityReviewTrustAnchor:
    """Typed view over the out-of-band external reviewer public key."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or Path(__file__).resolve().parents[3] / "config/model-quality-trust-anchor.v1.json")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("model quality trust anchor is unavailable or malformed") from exc
        if payload.get("trust_anchor_version") != 1 or payload.get("status") != "active":
            raise ContractError("model quality trust anchor is not active")
        if payload.get("algorithm") != "Ed25519":
            raise ContractError("unsupported model quality trust anchor algorithm")
        public_key_hex = payload.get("public_key_hex")
        if not isinstance(public_key_hex, str) or not re.fullmatch(r"[0-9a-f]{64}", public_key_hex):
            raise ContractError("model quality trust anchor has an invalid public key")
        if payload.get("registry_sha256") != sha256_bytes_file(self.path.parent / "model-quality-reviews.v1.json"):
            raise ContractError("model quality review registry is not anchored")
        self.key_id = payload.get("key_id")
        self.public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))

    def verify(self, payload: Mapping[str, Any], signature_hex: str) -> None:
        if not isinstance(signature_hex, str) or not _ED25519_SIGNATURE_HEX.fullmatch(signature_hex):
            raise ContractError("invalid model quality review signature length")
        try:
            self.public_key.verify(bytes.fromhex(signature_hex), canonical_json(dict(payload)))
        except (ValueError, InvalidSignature) as exc:
            raise ContractError("model quality review signature mismatch") from exc


def sha256_bytes_file(path: Path) -> str:
    return file_sha256_bytes(path.read_bytes())


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
        failed_error_checks = [
            check for check in checks
            if check.get("result") == "failed" and check.get("level", "error") == "error"
        ]
        if report.get("status") == "passed" and failed_error_checks:
            raise ContractError("passed quality report contains failed checks")
        if report.get("status") == "warning" and failed_error_checks:
            raise ContractError("warning quality report contains failed error checks")


_MODEL_QUALITY_REVIEW_REGISTRY = ModelQualityReviewRegistry()
_MODEL_QUALITY_TRUST_ANCHOR = ModelQualityReviewTrustAnchor()


def verify_reviewed_quality_report_signature(report: Mapping[str, Any]) -> None:
    """Verify that the reviewer-signed decision fields remain unchanged."""
    signed_fields = {
        "report_version", "binding_type", "policy", "status", "checks",
        "errors", "warnings", "key_id", "producer_code_fingerprint", "reviewer",
    }
    if not signed_fields.issubset(report):
        raise ContractError("quality report does not contain a reviewer-signed decision")
    unsigned_payload = {key: report[key] for key in signed_fields}
    _MODEL_QUALITY_TRUST_ANCHOR.verify(
        unsigned_payload,
        signature_hex=report.get("review_signature_sha256"),
    )


def review_signature(
    payload: dict[str, Any],
    *,
    private_key_pem: Path | str,
) -> str:
    """Sign a review payload with the separately held reviewer private key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        key = serialization.load_pem_private_key(Path(private_key_pem).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ContractError("reviewer private key is unavailable or malformed") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ContractError("reviewer private key is not Ed25519")
    return key.sign(canonical_json(payload)).hex()


def create_reviewed_quality_decision(
    *,
    binding_type: str,
    policy: str,
    status: str,
    checks: list[dict[str, Any]],
    errors: list[str],
    warnings: list[str],
    producer_code_fingerprint: str,
    private_key_pem: Path | str,
) -> dict[str, Any]:
    """Create an immutable decision from the separately held reviewer role."""
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
    unsigned_payload = {
        "binding_type": binding_type, "checks": checks, "errors": errors,
        "key_id": _MODEL_QUALITY_TRUST_ANCHOR.key_id, "policy": policy,
        "producer_code_fingerprint": producer_code_fingerprint,
        "reviewer": _MODEL_QUALITY_REVIEW_REGISTRY.reviewer, "status": status,
        "warnings": warnings,
    }
    return {
        **unsigned_payload,
        "report_version": 2,
        "review_signature_sha256": review_signature(
            {**unsigned_payload, "report_version": 2}, private_key_pem=private_key_pem
        ),
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
        "errors", "warnings", "key_id", "producer_code_fingerprint", "reviewer",
        "review_signature_sha256",
    }
    if set(decision) != required:
        raise ContractError("external quality decision has unexpected or missing fields")
    if decision.get("report_version") != 2 or decision.get("binding_type") != binding_type:
        raise ContractError(f"quality decision does not match {binding_type}")
    _MODEL_QUALITY_REVIEW_REGISTRY.validate_report({**decision, "bound_generation_id": subject_generation_id})
    if decision.get("key_id") != _MODEL_QUALITY_TRUST_ANCHOR.key_id:
        raise ContractError("external quality decision is bound to another trust anchor")
    unsigned_payload = {
        key: value for key, value in decision.items()
        if key != "review_signature_sha256"
    }
    _MODEL_QUALITY_TRUST_ANCHOR.verify(
        unsigned_payload, signature_hex=decision.get("review_signature_sha256")
    )
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
