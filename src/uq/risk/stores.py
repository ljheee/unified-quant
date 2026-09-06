"""Immutable stores for Risk Control Plane Phase 1 artifacts."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..contracts.canonical_v2 import file_sha256_bytes
from ..errors import ContractError
from .contracts import (
    validate_risk_file_payload,
    validate_risk_manifest,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def make_binding(document: dict[str, Any], *, family: str) -> dict[str, str]:
    return {
        "family": family,
        "generation_id": document["generation_id"],
        "manifest_digest_sha256": document["manifest_digest_sha256"],
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


class _ImmutableRiskManifestStore:
    family: str
    directory_name: str

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.directory = self.root / "risk" / self.directory_name

    def _partition(self, document: dict[str, Any]) -> Path:
        return self.directory / f"generation={document['generation_id']}"

    def publish(self, document: dict[str, Any]) -> Path:
        validate_risk_manifest(self.family, dict(document))
        partition = self._partition(document)
        manifest_path = partition / "manifest.json"
        if partition.exists() and any(partition.iterdir()):
            raise ContractError(f"risk {self.family} partition already exists: {partition}")
        _atomic_write_json(manifest_path, document)
        return partition

    def read(self, generation_id: str) -> dict[str, Any]:
        if not _SHA256.fullmatch(generation_id):
            raise ContractError("invalid risk generation id")
        manifest_path = self.directory / f"generation={generation_id}" / "manifest.json"
        if not manifest_path.is_file():
            raise ContractError(f"risk {self.family} is unavailable: {generation_id}")
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"malformed risk {self.family} manifest") from exc
        validate_risk_manifest(self.family, document)
        if document["generation_id"] != generation_id:
            raise ContractError(f"risk {self.family} generation mismatch on read")
        return document


class RiskPolicyStore(_ImmutableRiskManifestStore):
    family = "risk_policy"
    directory_name = "policies"


class RiskEventStore(_ImmutableRiskManifestStore):
    family = "risk_event"
    directory_name = "events"


class RiskRunStore(_ImmutableRiskManifestStore):
    family = "risk_run"
    directory_name = "runs"


class RiskStateStore(_ImmutableRiskManifestStore):
    family = "risk_state"
    directory_name = "states"

    def _partition(self, document: dict[str, Any]) -> Path:
        return (
            self.directory
            / f"scope={document['state_scope_type']}:{document['scope_id']}"
            / f"date={document['as_of_date']}"
            / f"generation={document['generation_id']}"
        )

    def publish(self, document: dict[str, Any], payload: dict[str, Any]) -> Path:
        validate_risk_manifest(self.family, dict(document))
        if len(document.get("files", [])) != 1:
            raise ContractError("risk state must have exactly one artifact")
        file_entry = document["files"][0]
        if file_entry["path"] != "state.json":
            raise ContractError("risk state artifact path must be state.json")
        content = (
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        validate_risk_file_payload(document, path="state.json", content=content)
        partition = self._partition(document)
        manifest_path = partition / "manifest.json"
        data_path = partition / "state.json"
        if manifest_path.exists() or data_path.exists():
            raise ContractError(f"risk state partition already exists: {partition}")
        partition.mkdir(parents=True, exist_ok=False)
        _atomic_write_json(manifest_path, document)
        tmp = data_path.with_suffix(".json.tmp")
        tmp.write_bytes(content)
        os.replace(tmp, data_path)
        return partition

    def read(self, generation_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not _SHA256.fullmatch(generation_id):
            raise ContractError("invalid risk state generation id")
        partitions = list(self.directory.glob(f"*/date=*/generation={generation_id}"))
        if len(partitions) != 1:
            raise ContractError(f"risk state is unavailable: {generation_id}")
        partition = partitions[0]
        manifest_path = partition / "manifest.json"
        data_path = partition / "state.json"
        if not manifest_path.is_file() or not data_path.is_file():
            raise ContractError(f"incomplete risk state partition: {partition}")
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload = json.loads(data_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("malformed risk state artifact") from exc
        validate_risk_manifest(self.family, document)
        if document["generation_id"] != generation_id:
            raise ContractError("risk state generation mismatch on read")
        validate_risk_file_payload(document, path="state.json", content=data_path.read_bytes())
        return document, payload


class RiskDecisionStore(_ImmutableRiskManifestStore):
    family = "risk_decision"
    directory_name = "decisions"

    def _partition(self, document: dict[str, Any]) -> Path:
        scope = document["decision_scope"].replace("_", "-")
        return (
            self.directory
            / f"scope={scope}"
            / f"date={document['as_of_date']}"
            / f"generation={document['generation_id']}"
        )

    def publish(self, document: dict[str, Any], payload: dict[str, Any]) -> Path:
        validate_risk_manifest(self.family, dict(document))
        if len(document.get("files", [])) != 1:
            raise ContractError("risk decision must have exactly one artifact")
        expected_payload = {
            "action": document["action"],
            "constraints": document["constraints"],
            "decision_digest": document["decision_digest"],
            "findings": document["findings"],
        }
        if payload != expected_payload:
            raise ContractError("risk decision payload does not match its manifest")
        file_entry = document["files"][0]
        if file_entry["path"] != "decision.json":
            raise ContractError("risk decision artifact path must be decision.json")
        content = (
            json.dumps(expected_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        validate_risk_file_payload(document, path="decision.json", content=content)
        partition = self._partition(document)
        manifest_path = partition / "manifest.json"
        data_path = partition / "decision.json"
        if manifest_path.exists() or data_path.exists():
            raise ContractError(f"risk decision partition already exists: {partition}")
        partition.mkdir(parents=True, exist_ok=False)
        _atomic_write_json(manifest_path, document)
        tmp = data_path.with_suffix(".json.tmp")
        tmp.write_bytes(content)
        os.replace(tmp, data_path)
        return partition

    def read(self, generation_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not _SHA256.fullmatch(generation_id):
            raise ContractError("invalid risk decision generation id")
        partitions = list(self.directory.glob(f"*/date=*/generation={generation_id}"))
        if len(partitions) != 1:
            raise ContractError(f"risk decision is unavailable: {generation_id}")
        partition = partitions[0]
        manifest_path = partition / "manifest.json"
        data_path = partition / "decision.json"
        if not manifest_path.is_file() or not data_path.is_file():
            raise ContractError(f"incomplete risk decision partition: {partition}")
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload = json.loads(data_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError("malformed risk decision artifact") from exc
        validate_risk_manifest(self.family, document)
        if document["generation_id"] != generation_id:
            raise ContractError("risk decision generation mismatch on read")
        validate_risk_file_payload(document, path="decision.json", content=data_path.read_bytes())
        return document, payload
