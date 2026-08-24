from __future__ import annotations

import json
import os
import shutil
import io
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from .canonical_v2 import file_sha256_bytes
from .gate_contracts import adjustment_snapshot_generation, validate_contract
from ..errors import ContractError


_MEMBERS_COLUMNS = ["instrument"]


class UniverseSnapshotStore:
    def save(self, root: Path, document_without_generation: dict[str, Any], members: pd.DataFrame) -> Path:
        candidate = {**document_without_generation, "generation_id": "0" * 64}
        validate_contract("universe_snapshot.v1.json", candidate)
        if Path(document_without_generation["members_artifact"]["path"]).name != document_without_generation["members_artifact"]["path"]:
            raise ContractError("universe member artifact path must be a filename")
        _validate_dates(document_without_generation["valid_from"], document_without_generation["valid_to"])
        if members.columns.tolist() != _MEMBERS_COLUMNS:
            raise ContractError("universe member artifact column mismatch")
        content = members.to_csv(index=False, lineterminator="\n").encode("utf-8")
        unsigned = {
            **document_without_generation,
            "members_artifact": {
                **document_without_generation["members_artifact"],
                "checksum_sha256": file_sha256_bytes(content),
            },
        }
        generation_id = adjustment_snapshot_generation(unsigned)
        document = {**unsigned, "generation_id": generation_id}
        validate_contract("universe_snapshot.v1.json", document)
        directory = root / "universes" / document["universe_id"] / generation_id
        if directory.exists():
            raise ContractError(f"immutable universe snapshot already published: {directory}")
        staging = directory.with_name(f"{directory.name}.staging.{uuid.uuid4().hex}")
        staging.mkdir(parents=True)
        try:
            artifact_name = Path(document["members_artifact"]["path"]).name
            if artifact_name != document["members_artifact"]["path"]:
                raise ContractError("universe member artifact path must be a filename")
            (staging / artifact_name).write_bytes(content)
            (staging / "manifest.json").write_text(json.dumps(document, sort_keys=True, indent=2) + "\n")
            _fsync_tree(staging)
            os.replace(staging, directory)
            _fsync_dir(directory.parent)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return directory


class QualityReportStore:
    def save(self, root: Path, report: dict[str, Any]) -> Path:
        validate_contract("quality_report.v1.json", report)
        if report["binding_type"] != "factor_v1":
            raise ContractError("quality report is not bound to factor run")
        allowed_checks = {"duplicate_keys", "null_rate", "missing_dependency", "coverage", "semantic_version"}
        if any(item["name"] not in allowed_checks for item in report["checks"]):
            raise ContractError("unknown quality check taxonomy")
        content = json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
        directory = root / "reports" / "factor_v1" / report["bound_generation_id"]
        if directory.exists():
            raise ContractError(f"immutable quality report already published: {directory}")
        staging = directory.with_name(f"{directory.name}.staging.{uuid.uuid4().hex}")
        staging.mkdir(parents=True)
        try:
            (staging / "report.json").write_bytes(content)
            (staging / "report.sha256").write_text(file_sha256_bytes(content) + "\n")
            _fsync_tree(staging)
            os.replace(staging, directory)
            _fsync_dir(directory.parent)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return directory

    def read(self, root: Path, bound_generation_id: str) -> dict[str, Any]:
        if "/" in bound_generation_id or ".." in bound_generation_id:
            raise ContractError("unsafe quality report binding identity")
        directory = root / "reports" / "factor_v1" / bound_generation_id
        try:
            content = (directory / "report.json").read_bytes()
        except FileNotFoundError as exc:
            raise ContractError(f"missing quality report: {bound_generation_id}") from exc
        digest_path = directory / "report.sha256"
        try:
            recorded_digest = digest_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise ContractError(f"missing quality report checksum: {bound_generation_id}") from exc
        if file_sha256_bytes(content) != recorded_digest:
            raise ContractError("tampered quality report bytes")
        try:
            report = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ContractError("malformed quality report serialization") from exc
        validate_contract("quality_report.v1.json", report)
        if report["binding_type"] != "factor_v1":
            raise ContractError("quality report is not bound to factor run")
        allowed_checks = {"duplicate_keys", "null_rate", "missing_dependency", "coverage", "semantic_version"}
        if any(item["name"] not in allowed_checks for item in report["checks"]):
            raise ContractError("unknown quality check taxonomy")
        if report["bound_generation_id"] != bound_generation_id:
            raise ContractError("quality report bound to another run")
        actual_files = {path.name for path in directory.iterdir()}
        if not actual_files <= {"report.json", "report.sha256"}:
            raise ContractError(f"unexpected quality report files: {sorted(actual_files - {'report.json', 'report.sha256'})}")
        if any(path.is_symlink() for path in directory.rglob("*")):
            raise ContractError("quality report contains symbolic links")
        return report


class UniverseSnapshotReader:
    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self, universe_id: str, generation_id: str, *, requested_valid_from: date, requested_valid_to: date | None = None) -> pd.DataFrame:
        directory = self.root / "universes" / universe_id / generation_id
        manifest_path = directory / "manifest.json"
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ContractError(f"missing or malformed universe snapshot: {universe_id}/{generation_id}") from exc
        validate_contract("universe_snapshot.v1.json", document)
        expected_generation = adjustment_snapshot_generation({key: value for key, value in document.items() if key != "generation_id"})
        if document["generation_id"] != expected_generation or "/" in document["generation_id"] or ".." in document["generation_id"]:
            raise ContractError("universe snapshot generation mismatch")
        valid_from = date.fromisoformat(document["valid_from"])
        valid_to = None if document["valid_to"] is None else date.fromisoformat(document["valid_to"])
        if requested_valid_from < valid_from or (valid_to is not None and (requested_valid_to or requested_valid_from) > valid_to):
            raise ContractError("universe snapshot reuse outside PIT validity")
        path = directory / document["members_artifact"]["path"]
        artifact_path = Path(document["members_artifact"]["path"])
        resolved = directory / artifact_path
        try:
            safe = resolved.resolve().is_relative_to(directory.resolve())
        except ValueError:
            safe = False
        if artifact_path.is_absolute() or ".." in artifact_path.parts or not safe or resolved.is_symlink() or resolved.is_file() and resolved.is_symlink():
            raise ContractError("unsafe universe member artifact path")
        if directory.is_symlink() or any(parent.is_symlink() for parent in [directory, *directory.parents]):
            raise ContractError("unsafe universe snapshot directory link")
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise ContractError("absent universe membership artifact") from exc
        if file_sha256_bytes(content) != document["members_artifact"]["checksum_sha256"]:
            raise ContractError("tampered universe membership bytes")
        frame = pd.read_csv(io.BytesIO(content))
        if frame.columns.tolist() != _MEMBERS_COLUMNS:
            raise ContractError("malformed universe member serialization")
        actual_files = {path.name for path in directory.iterdir()}
        allowed_files = {"manifest.json", document["members_artifact"]["path"]}
        if not actual_files <= allowed_files:
            raise ContractError(f"unexpected universe snapshot files: {sorted(actual_files - allowed_files)}")
        if any(path.is_symlink() for path in directory.rglob("*")):
            raise ContractError("universe snapshot contains symbolic links")
        return frame


def _validate_dates(*values: str | None) -> None:
    for value in values:
        if value is None:
            continue
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ContractError(f"invalid universe calendar date: {value}") from exc


def _fsync_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    _fsync_dir(root)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
