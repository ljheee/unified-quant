from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT_DIR = _ROOT / "config" / "schemas" / "contracts"
_CACHE: dict[str, jsonschema.Draft202012Validator] = {}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_contract(schema_name: str, payload: dict[str, Any]) -> None:
    from ..errors import ContractError

    if schema_name not in _CACHE:
        schema = json.loads((_CONTRACT_DIR / schema_name).read_text(encoding="utf-8"))
        registry = Registry()
        for schema_path in _CONTRACT_DIR.glob("*.json"):
            resource = Resource.from_contents(
                json.loads(schema_path.read_text(encoding="utf-8")),
                default_specification=DRAFT202012,
            )
            registry = registry.with_resource(schema_path.name, resource)
        _CACHE[schema_name] = jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
            registry=registry,
        )
    errors = sorted(_CACHE[schema_name].iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(f"{list(error.path)}: {error.message}" for error in errors)
        raise ContractError(f"{schema_name} validation failed: {details}")


def validate_contract_path(schema_path: Path, payload: dict[str, Any]) -> None:
    from ..errors import ContractError

    cache_key = str(schema_path)
    if cache_key not in _CACHE:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        _CACHE[cache_key] = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = sorted(_CACHE[cache_key].iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(f"{list(error.path)}: {error.message}" for error in errors)
        raise ContractError(f"{schema_path.name} validation failed: {details}")


def canonical_v2_identities(manifest_without_digests: dict[str, Any]) -> tuple[str, str]:
    """Derive stable canonical content identity and complete run-local digest."""
    generation_payload = {
        key: value for key, value in manifest_without_digests.items()
        if key not in {
            "run_id", "created_at", "trust_anchor_sha256",
            "manifest_digest_sha256", "quality_report_checksum",
        }
    }
    generation_id = sha256_json(generation_payload)
    digest_payload = {
        key: value for key, value in manifest_without_digests.items()
        if key != "manifest_digest_sha256"
    }
    digest_payload["generation_id"] = generation_id
    return generation_id, sha256_json(digest_payload)


def factor_manifest_identities(manifest_without_digests: dict[str, Any]) -> tuple[str, str]:
    """Derive factor generation without the post-binding quality artifact checksum."""
    generation_payload = {
        key: value for key, value in manifest_without_digests.items()
        if key not in {"run_id", "created_at", "trust_anchor_sha256"}
    }
    if isinstance(generation_payload.get("quality"), dict):
        generation_payload["quality"] = {
            key: value for key, value in generation_payload["quality"].items()
            if key != "report_checksum_sha256"
        }
    generation_id = sha256_json(generation_payload)
    digest_payload = {
        key: value for key, value in manifest_without_digests.items()
        if key != "manifest_digest_sha256"
    }
    digest_payload["generation_id"] = generation_id
    return generation_id, sha256_json(digest_payload)


def adjustment_snapshot_generation(payload: dict[str, Any]) -> str:
    stable = {key: value for key, value in payload.items() if key != "generation_id"}
    return sha256_json(stable)
