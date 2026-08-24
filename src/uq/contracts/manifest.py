from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from ..errors import ContractError

_ROOT = Path(__file__).resolve().parents[3]
_CACHE: dict[Path, Draft202012Validator] = {}


def _validator(schema_path: Path) -> Draft202012Validator:
    if schema_path not in _CACHE:
        payload = json.loads(schema_path.read_text(encoding="utf-8"))
        _CACHE[schema_path] = Draft202012Validator(payload)
    return _CACHE[schema_path]


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def load_manifest_with_schema(
    manifest: dict[str, Any],
    schema_name: str = "canonical-v1.json",
    trust_anchor: str | None = None,
) -> dict[str, Any]:
    path = _ROOT / "config" / "schemas" / "manifests" / schema_name
    errors = sorted(_validator(path).iter_errors(manifest), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(f"{list(error.path)}: {error.message}" for error in errors)
        raise ContractError(f"manifest schema validation failed: {details}")
    expected = manifest.get("generation_id")
    if not isinstance(expected, str):
        raise ContractError("manifest generation_id is missing")
    payload = {
        key: value
        for key, value in manifest.items()
        if key not in {"generation_id", "trust_anchor_sha256"}
    }
    actual = sha256_json(payload)
    if actual != expected:
        raise ContractError("manifest generation digest mismatch")
    if trust_anchor is not None and sha256_bytes(expected.encode("ascii")) != trust_anchor:
        raise ContractError("manifest generation is not trusted by anchor")
    return manifest


def anchor_generation_id(generation_id: str) -> str:
    return sha256_bytes(generation_id.encode("ascii"))


def schema_file_checksum(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())
