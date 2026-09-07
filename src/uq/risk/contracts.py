"""Machine contract support for the Risk Control Plane."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from ..errors import ContractError
from ..runtime import require_production_review_key

_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT_DIR = _ROOT / "config" / "schemas" / "contracts"
_ANCHOR_PATH = _ROOT / "config" / "risk-review-trust-anchor.v1.json"

RISK_CONTRACT_NAMES = {
    "risk_policy": "risk_policy.v1.json",
    "risk_state": "risk_state.v1.json",
    "risk_decision": "risk_decision.v1.json",
    "risk_event": "risk_event.v1.json",
    "risk_exception": "risk_exception.v1.json",
    "risk_review_decision": "risk_review_decision.v1.json",
    "risk_review_trust_anchor": "risk_review_trust_anchor.v1.json",
    "risk_run": "risk_run.v1.json",
    "risk_de_risk_contract": "risk_de_risk_contract.v1.json",
}

_RUN_LOCAL_FIELDS = {"run_id", "request_id", "created_at"}
_EVENT_LOCAL_FIELDS = {"run_id", "request_id", "created_at", "event_sequence_number"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{128}$")
_VALIDATORS: dict[str, Draft202012Validator] = {}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def validate_risk_contract(schema_name: str, payload: dict[str, Any]) -> None:
    if schema_name not in RISK_CONTRACT_NAMES:
        raise ContractError(f"unknown risk contract family: {schema_name}")
    schema_path = _CONTRACT_DIR / RISK_CONTRACT_NAMES[schema_name]
    cache_key = schema_path.name
    if cache_key not in _VALIDATORS:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        registry = Registry()
        for contract_path in _CONTRACT_DIR.glob("risk_*.json"):
            resource = Resource.from_contents(
                json.loads(contract_path.read_text(encoding="utf-8")),
                default_specification=DRAFT202012,
            )
            registry = registry.with_resource(contract_path.name, resource)
        _VALIDATORS[cache_key] = Draft202012Validator(
            schema, format_checker=FormatChecker(), registry=registry
        )
    errors = sorted(_VALIDATORS[cache_key].iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(f"{list(error.path)}: {error.message}" for error in errors)
        raise ContractError(f"{schema_path.name} validation failed: {details}")


def risk_contract_identities(
    payload: Mapping[str, Any],
    *,
    schema_name: str,
) -> tuple[str, str]:
    """Return stable generation and durable manifest digest."""
    if schema_name not in RISK_CONTRACT_NAMES:
        raise ContractError(f"unknown risk contract family: {schema_name}")
    document = dict(payload)
    for field in ("generation_id", "manifest_digest_sha256"):
        value = document.get(field)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ContractError(f"{schema_name} missing valid {field}")
        document.pop(field)
    excluded = _EVENT_LOCAL_FIELDS if schema_name == "risk_event" else _RUN_LOCAL_FIELDS
    producer = document.get("producer")
    if isinstance(producer, Mapping):
        generation_document = {
            **document,
            "producer": {
                key: value for key, value in producer.items() if key not in excluded
            },
        }
    else:
        generation_document = document
    generation_payload = {
        key: value for key, value in generation_document.items() if key not in excluded
    }
    generation_id = sha256_json(generation_payload)
    digest_document = {**document, "generation_id": generation_id}
    return generation_id, sha256_json(digest_document)


def _registry_payload() -> dict[str, Any]:
    try:
        payload = json.loads(_ANCHOR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("risk review trust anchor registry is unavailable") from exc
    validate_risk_contract("risk_review_trust_anchor", payload)
    return payload


def _active_anchor(
    key_id: str, *, review_type: str, reviewer: str, as_of: date
) -> dict[str, Any]:
    for anchor in _registry_payload()["anchors"]:
        valid_from = date.fromisoformat(anchor["valid_from"])
        valid_to_text = anchor.get("valid_to")
        valid_to = date.fromisoformat(valid_to_text) if valid_to_text else None
        if (
            anchor["key_id"] == key_id
            and anchor["algorithm"] == "Ed25519"
            and anchor["reviewer"] == reviewer
            and review_type in anchor["review_types"]
            and valid_from <= as_of
            and (valid_to is None or as_of <= valid_to)
        ):
            return anchor
    raise ContractError("unregistered, expired, or unauthorized risk review trust anchor")


def _verify_risk_signature(
    unsigned_payload: Mapping[str, Any],
    signature_hex: str,
    key_id: str,
    review_type: str,
    reviewer: str,
) -> None:
    if not isinstance(signature_hex, str) or not _SIGNATURE.fullmatch(signature_hex):
        raise ContractError("invalid risk review signature length")
    reviewed_at = datetime.fromisoformat(unsigned_payload["reviewed_at_utc"].replace("Z", "+00:00"))
    anchor = _active_anchor(
        key_id,
        review_type=review_type,
        reviewer=unsigned_payload["reviewer"],
        as_of=reviewed_at.date(),
    )
    require_production_review_key(
        anchor["public_key_hex"], context="risk review trust anchor"
    )
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(anchor["public_key_hex"]))
        public_key.verify(bytes.fromhex(signature_hex), canonical_json(dict(unsigned_payload)))
    except (ValueError, InvalidSignature) as exc:
        raise ContractError("risk review signature mismatch") from exc


_SIGNED_REVIEW_FIELDS = {
    "contract_version",
    "schema_version",
    "review_type",
    "subject_generation_id",
    "subject_manifest_digest_sha256",
    "subject_content_sha256",
    "review_status",
    "reviewer",
    "key_id",
    "policy",
    "reviewed_at_utc",
    "errors",
    "warnings",
}


def verify_risk_review_decision(
    payload: Mapping[str, Any],
    *,
    expected_review_type: str,
    expected_subject_generation_id: str,
    expected_subject_manifest_digest_sha256: str,
    expected_subject_content_sha256: str,
) -> None:
    validate_risk_contract("risk_review_decision", dict(payload))
    if payload["review_type"] != expected_review_type:
        raise ContractError("risk review review_type mismatch")
    if payload["subject_generation_id"] != expected_subject_generation_id:
        raise ContractError("risk review subject generation mismatch")
    if payload["subject_manifest_digest_sha256"] != expected_subject_manifest_digest_sha256:
        raise ContractError("risk review subject manifest digest mismatch")
    if payload["subject_content_sha256"] != expected_subject_content_sha256:
        raise ContractError("risk review subject content digest mismatch")
    if payload["review_status"] == "approved" and payload["errors"]:
        raise ContractError("approved risk review contains errors")
    signed = {key: payload[key] for key in _SIGNED_REVIEW_FIELDS}
    _verify_risk_signature(
        signed,
        payload["review_signature_sha256"],
        payload["key_id"],
        payload["review_type"],
        payload["reviewer"],
    )
    generation, digest = risk_contract_identities(payload, schema_name="risk_review_decision")
    if payload["generation_id"] != generation or payload["manifest_digest_sha256"] != digest:
        raise ContractError("risk review identity mismatch")


def risk_stable_content_id(
    payload: Mapping[str, Any],
    *,
    schema_name: str,
) -> str:
    """Return the run-metadata-free canonical content digest used for review."""
    if schema_name not in RISK_CONTRACT_NAMES:
        raise ContractError(f"unknown risk contract family: {schema_name}")
    excluded = _EVENT_LOCAL_FIELDS if schema_name == "risk_event" else _RUN_LOCAL_FIELDS
    return sha256_json(risk_stable_generation_payload(payload, schema_name=schema_name))


def build_risk_review_decision(
    *,
    review_type: str,
    subject: Mapping[str, Any],
    schema_name: str,
    review_status: str,
    reviewer: str,
    errors: list[str],
    warnings: list[str],
    private_key_pem: Path | str,
) -> dict[str, Any]:
    """Create an externally signed risk review decision."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if review_type not in {
        "risk_policy_activation",
        "risk_exception_grant",
        "risk_de_risk_contract_activation",
    }:
        raise ContractError("invalid risk review type")
    if schema_name not in {"risk_policy", "risk_exception", "risk_de_risk_contract"}:
        raise ContractError("invalid risk review subject schema")
    if review_status not in {"approved", "rejected"}:
        raise ContractError("invalid risk review status")
    if not isinstance(reviewer, str) or not reviewer:
        raise ContractError("invalid risk reviewer identity")
    if not isinstance(errors, list) or not isinstance(warnings, list):
        raise ContractError("invalid risk review findings")
    validate_risk_manifest(schema_name, dict(subject))
    subject_generation_id = subject.get("generation_id")
    subject_manifest_digest = subject.get("manifest_digest_sha256")
    if not isinstance(subject_generation_id, str) or not _SHA256.fullmatch(subject_generation_id):
        raise ContractError("risk review subject generation is missing")
    if not isinstance(subject_manifest_digest, str) or not _SHA256.fullmatch(subject_manifest_digest):
        raise ContractError("risk review subject manifest digest is missing")
    subject_content_sha256 = risk_stable_content_id(subject, schema_name=schema_name)
    unsigned = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "review_type": review_type,
        "subject_generation_id": subject_generation_id,
        "subject_manifest_digest_sha256": subject_manifest_digest,
        "subject_content_sha256": subject_content_sha256,
        "review_status": review_status,
        "reviewer": reviewer,
        "key_id": "risk-reviewer-v1-2026-09",
        "policy": "reject_all",
        "reviewed_at_utc": "2026-09-06T00:00:00Z",
        "errors": errors,
        "warnings": warnings,
    }
    if review_status == "approved" and errors:
        raise ContractError("approved risk review contains errors")
    try:
        key = serialization.load_pem_private_key(Path(private_key_pem).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ContractError("risk reviewer private key is unavailable or malformed") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ContractError("risk reviewer private key is not Ed25519")
    signature = key.sign(canonical_json(unsigned)).hex()
    review = {
        **unsigned,
        "review_signature_sha256": signature,
    }
    generation_id, manifest_digest_sha256 = risk_contract_identities(
        {
            **review,
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        },
        schema_name="risk_review_decision",
    )
    return {
        **review,
        "generation_id": generation_id,
        "manifest_digest_sha256": manifest_digest_sha256,
    }


def risk_stable_generation_payload(
    payload: Mapping[str, Any], *, schema_name: str
) -> dict[str, Any]:
    """Return the exact canonical payload used for stable generation."""
    if schema_name not in RISK_CONTRACT_NAMES:
        raise ContractError(f"unknown risk contract family: {schema_name}")
    excluded = _EVENT_LOCAL_FIELDS if schema_name == "risk_event" else _RUN_LOCAL_FIELDS
    document = dict(payload)
    producer = dict(document.get("producer") or {})
    if producer:
        document["producer"] = {
            key: value for key, value in producer.items() if key not in excluded
        }
    return {
        key: value
        for key, value in document.items()
        if key not in {"generation_id", "manifest_digest_sha256", *excluded}
    }


def validate_risk_policy_scope_compatibility(payload: Mapping[str, Any]) -> None:
    if payload.get("policy_scope") != payload.get("rules", [{}])[0].get("rule_scope"):
        raise ContractError("risk policy scope and rule scope are incompatible")
    if any(rule.get("rule_scope") != payload.get("policy_scope") for rule in payload.get("rules", [])):
        raise ContractError("risk policy contains a cross-scope rule")


def validate_industry_membership_prerequisite(payload: Mapping[str, Any]) -> None:
    if any(rule["metric"] == "industry_weight" for rule in payload["rules"]):
        if payload.get("industry_membership_binding") is None:
            raise ContractError("industry limit requires industry_membership_binding")


def validate_enforceable_de_risk_contract(
    payload: Mapping[str, Any], *, action: str
) -> None:
    if payload.get("status") != "approved" or not payload.get("execution_available"):
        raise ContractError("risk de-risk contract is not enforceable")
    if action not in payload.get("supported_actions", []):
        raise ContractError(f"risk de-risk contract does not support {action}")


def validate_risk_event_sequence(events: list[Mapping[str, Any]]) -> None:
    seen_events: set[str] = set()
    previous_sequence: int | None = None
    for event in events:
        validate_risk_manifest("risk_event", dict(event))
        event_id = event["event_id"]
        if event_id in seen_events:
            raise ContractError("duplicate risk event id")
        seen_events.add(event_id)
        sequence = event["event_sequence_number"]
        if previous_sequence is not None and sequence <= previous_sequence:
            raise ContractError("stale or duplicate risk event sequence")
        previous_sequence = sequence


def validate_risk_file_path(path: str) -> None:
    candidate = Path(path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ContractError("unsafe risk artifact path")
    if candidate.as_posix() != path:
        raise ContractError("non-canonical risk artifact path")


def validate_risk_file_payload(
    payload: Mapping[str, Any], *, path: str, content: bytes
) -> None:
    entries = [entry for entry in payload.get("files", []) if entry.get("path") == path]
    if len(entries) != 1:
        raise ContractError("risk artifact path is missing or duplicated")
    entry = entries[0]
    validate_risk_file_path(path)
    if entry.get("byte_size") != len(content):
        raise ContractError("risk artifact byte size mismatch")
    actual = hashlib.sha256(content).hexdigest()
    if entry.get("sha256") != actual:
        raise ContractError("tampered risk artifact bytes")


def _validate_run_local_metadata(payload: Mapping[str, Any], *, schema_name: str) -> None:
    if schema_name in {"risk_review_trust_anchor"}:
        return
    producer = payload.get("producer")
    if schema_name != "risk_review_decision":
        if not isinstance(producer, dict):
            raise ContractError(f"{schema_name} missing producer metadata")
        required = {"producer_code_fingerprint", "run_id", "created_at"}
        if not required.issubset(producer):
            raise ContractError(f"{schema_name} producer metadata incomplete")
    if "run_id" in payload.get("producer", {}) and not re.fullmatch(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        payload["producer"]["run_id"],
    ):
        raise ContractError("risk producer run_id is invalid")


def risk_policy_draft_subject(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical zero-binding policy manifest signed by a reviewer."""
    draft = dict(payload)
    draft["approval_binding"] = {
        "family": "risk_review_decision_v1",
        "review_generation_id": "0" * 64,
        "review_manifest_digest_sha256": "0" * 64,
    }
    draft["generation_id"] = "0" * 64
    draft["manifest_digest_sha256"] = "0" * 64
    generation, digest = risk_contract_identities(draft, schema_name="risk_policy")
    draft["generation_id"] = generation
    draft["manifest_digest_sha256"] = digest
    return draft


def risk_de_risk_contract_draft_subject(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical zero-binding de-risk contract manifest signed by a reviewer."""
    draft = dict(payload)
    draft["review_binding"] = {
        "family": "risk_review_decision_v1",
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    draft["generation_id"] = "0" * 64
    draft["manifest_digest_sha256"] = "0" * 64
    generation, digest = risk_contract_identities(draft, schema_name="risk_de_risk_contract")
    draft["generation_id"] = generation
    draft["manifest_digest_sha256"] = digest
    return draft


def risk_exception_draft_subject(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical zero-binding exception manifest signed by a reviewer."""
    draft = dict(payload)
    draft["review_binding"] = {
        "family": "risk_review_decision_v1",
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    draft["generation_id"] = "0" * 64
    draft["manifest_digest_sha256"] = "0" * 64
    generation, digest = risk_contract_identities(draft, schema_name="risk_exception")
    draft["generation_id"] = generation
    draft["manifest_digest_sha256"] = digest
    return draft


def validate_risk_de_risk_contract_governance(
    payload: Mapping[str, Any], review_document: Mapping[str, Any]
) -> None:
    """Validate an approved de-risk transition against a signed external review."""
    if payload.get("status") != "approved":
        return
    review = payload.get("review_binding")
    if not isinstance(review, dict):
        raise ContractError("approved risk de-risk contract lacks review binding")
    if review.get("family") != "risk_review_decision_v1":
        raise ContractError("risk de-risk contract binds the wrong review family")
    subject = risk_de_risk_contract_draft_subject(payload)
    verify_risk_review_decision(
        review_document,
        expected_review_type="risk_de_risk_contract_activation",
        expected_subject_generation_id=subject["generation_id"],
        expected_subject_manifest_digest_sha256=subject["manifest_digest_sha256"],
        expected_subject_content_sha256=risk_stable_content_id(
            subject, schema_name="risk_de_risk_contract"
        ),
    )
    if (
        review_document["generation_id"] != review["generation_id"]
        or review_document["manifest_digest_sha256"] != review["manifest_digest_sha256"]
    ):
        raise ContractError("risk de-risk contract review binding mismatch")


def validate_risk_exception_governance(
    payload: Mapping[str, Any], review_document: Mapping[str, Any]
) -> None:
    """Validate an approved exception against a signed external review."""
    if payload.get("status") != "approved":
        return
    review = payload.get("review_binding")
    if not isinstance(review, dict):
        raise ContractError("approved risk exception lacks review binding")
    if review.get("family") != "risk_review_decision_v1":
        raise ContractError("risk exception binds the wrong review family")
    subject = risk_exception_draft_subject(payload)
    verify_risk_review_decision(
        review_document,
        expected_review_type="risk_exception_grant",
        expected_subject_generation_id=subject["generation_id"],
        expected_subject_manifest_digest_sha256=subject["manifest_digest_sha256"],
        expected_subject_content_sha256=risk_stable_content_id(
            subject, schema_name="risk_exception"
        ),
    )
    if (
        review_document["generation_id"] != review["generation_id"]
        or review_document["manifest_digest_sha256"] != review["manifest_digest_sha256"]
    ):
        raise ContractError("risk exception review binding mismatch")


def validate_risk_policy_governance(
    payload: Mapping[str, Any], review_document: Mapping[str, Any]
) -> None:
    """Validate policy governance against its resolved, signed review decision."""
    if payload.get("activation_status") != "approved":
        return
    validate_risk_policy_scope_compatibility(payload)
    validate_industry_membership_prerequisite(payload)
    review = payload.get("approval_binding")
    if not isinstance(review, dict):
        raise ContractError("approved risk policy lacks review binding")
    if review.get("family") != "risk_review_decision_v1":
        raise ContractError("risk policy approval binds the wrong family")
    draft_subject = risk_policy_draft_subject(payload)
    verify_risk_review_decision(
        review_document,
        expected_review_type="risk_policy_activation",
        expected_subject_generation_id=draft_subject["generation_id"],
        expected_subject_manifest_digest_sha256=draft_subject["manifest_digest_sha256"],
        expected_subject_content_sha256=risk_stable_content_id(
            draft_subject, schema_name="risk_policy"
        ),
    )
    if (
        review_document["generation_id"] != review["review_generation_id"]
        or review_document["manifest_digest_sha256"] != review["review_manifest_digest_sha256"]
    ):
        raise ContractError("risk policy approval binding mismatch")


def _validate_policy_governance(payload: Mapping[str, Any]) -> None:
    validate_risk_policy_scope_compatibility(payload)
    validate_industry_membership_prerequisite(payload)
    if payload["activation_status"] == "approved":
        review = payload.get("approval_binding")
        if not isinstance(review, dict):
            raise ContractError("approved risk policy lacks review binding")
        if review.get("family") != "risk_review_decision_v1":
            raise ContractError("risk policy approval binds the wrong family")


def validate_risk_manifest(schema_name: str, payload: dict[str, Any]) -> tuple[str, str]:
    validate_risk_contract(schema_name, payload)
    generation, digest = risk_contract_identities(payload, schema_name=schema_name)
    if payload["generation_id"] != generation or payload["manifest_digest_sha256"] != digest:
        raise ContractError(f"risk {schema_name} identity mismatch")
    _validate_run_local_metadata(payload, schema_name=schema_name)
    if schema_name == "risk_policy":
        _validate_policy_governance(payload)
    if schema_name == "risk_de_risk_contract" and payload["status"] == "approved":
        if not payload.get("review_binding"):
            raise ContractError("approved risk de-risk contract lacks review binding")
    if schema_name == "risk_exception" and payload["status"] == "approved":
        if not payload.get("review_binding"):
            raise ContractError("approved risk exception lacks review binding")
    return generation, digest
