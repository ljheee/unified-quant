from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .gate_contracts import canonical_json, validate_contract
from ..errors import ContractError

_SCHEMA_NAMES = {
    "accepted_factor_index_query",
    "label_set",
    "model_dataset",
    "model_definition",
    "model_run",
    "model_artifact",
    "qlib_dataset_export",
    "qlib_init_receipt",
    "prediction_set",
}

_RUN_LOCAL_FIELDS = {"run_id", "created_at"}
_MODEL_CONTRACT_FAMILIES = {
    *_SCHEMA_NAMES,
    "model_quality_report",
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def model_manifest_identities(
    payload: Mapping[str, Any],
    *,
    schema_name: str,
) -> tuple[str, str]:
    """Return stable generation and complete manifest digest.

    ``run_id`` and ``created_at`` are always excluded from the generation. The
    complete manifest digest includes both run-local values and the derived
    generation.
    """
    if schema_name not in _SCHEMA_NAMES:
        raise ContractError(f"unknown model contract family: {schema_name}")
    document = dict(payload)
    for key in ("generation_id", "manifest_digest_sha256"):
        value = document.get(key)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ContractError(f"{schema_name} missing valid {key}")
        del document[key]
    generation_payload = {key: value for key, value in document.items() if key not in _RUN_LOCAL_FIELDS}
    generation_id = sha256_json(generation_payload)
    digest_payload = {**document, "generation_id": generation_id}
    manifest_digest = sha256_json(digest_payload)
    return generation_id, manifest_digest


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def validate_model_contract(schema_name: str, payload: dict[str, Any]) -> None:
    if schema_name == "model_quality_report":
        _validate_quality_report(payload)
    elif schema_name in _SCHEMA_NAMES:
        validate_contract(f"{schema_name}.v1.json", payload)
    else:
        raise ContractError(f"unknown model contract family: {schema_name}")


class ModelContractLoader:
    """Load and verify a durable model-layer contract document."""

    def load(self, schema_name: str, path: Path | str) -> dict[str, Any]:
        artifact_path = Path(path)
        try:
            raw = artifact_path.read_bytes()
        except FileNotFoundError as exc:
            raise ContractError(f"missing {schema_name} contract") from exc
        except OSError as exc:
            raise ContractError(f"unreadable {schema_name} contract") from exc
        if any(item.is_symlink() for item in (artifact_path, *artifact_path.parents)):
            raise ContractError("symbolic links are forbidden in accepted contracts")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"malformed {schema_name} contract serialization") from exc
        if not isinstance(payload, dict):
            raise ContractError("contract must be a JSON object")
        self.validate(schema_name, payload)
        return payload

    @staticmethod
    def validate(schema_name: str, payload: dict[str, Any]) -> None:
        if schema_name not in _MODEL_CONTRACT_FAMILIES:
            raise ContractError(f"unknown model contract family: {schema_name}")
        validate_model_contract(schema_name, payload)
        if schema_name == "accepted_factor_index_query":
            return
        if schema_name == "model_quality_report":
            expected_checksum_payload = {key: value for key, value in payload.items() if key != "report_checksum_sha256"}
            if payload["report_checksum_sha256"] != sha256_json(expected_checksum_payload):
                raise ContractError("model quality report checksum mismatch")
            return
        expected_generation, expected_digest = model_manifest_identities(payload, schema_name=schema_name)
        if payload["generation_id"] != expected_generation:
            raise ContractError(f"{schema_name} stable generation mismatch")
        if payload["manifest_digest_sha256"] != expected_digest:
            raise ContractError(f"{schema_name} manifest digest mismatch")


class AcceptedFactorIndexContract:
    """Validate contract-only accepted-store queries; never perform reads."""

    def __init__(self) -> None:
        self._validated_generation_cache: set[str] = set()

    def register_verified_generation(self, generation_id: str) -> None:
        if not isinstance(generation_id, str) or not _SHA256.fullmatch(generation_id):
            raise ContractError("invalid factor generation identity")
        self._validated_generation_cache.add(generation_id)

    def list(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        request = dict(query)
        request.setdefault("pagination", {"limit": 10000})
        validate_model_contract("accepted_factor_index_query", request)
        filters = request.get("filters", {})
        requested = filters.get("generation_id")
        if requested is not None and requested not in self._validated_generation_cache:
            raise ContractError("requested factor generation is not verified as accepted")
        return []

    def index(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.list(query)
        return []


def _validate_quality_report(report: dict[str, Any]) -> None:
    validate_contract("model_quality_report.v1.json", report)
    checksum_payload = {key: value for key, value in report.items() if key != "report_checksum_sha256"}
    if report["report_checksum_sha256"] != sha256_json(checksum_payload):
        raise ContractError("model quality report checksum mismatch")
