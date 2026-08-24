from __future__ import annotations

import fcntl
from dataclasses import dataclass
import hashlib
import json
import os
import shutil
import sys
import uuid
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .gate_contracts import canonical_json, canonical_v2_identities, sha256_bytes, sha256_json, validate_contract
from .manifest import schema_file_checksum
from .schema import Schema
from ..errors import ContractError



def _validate_manifest(manifest: dict[str, Any], *, expected_anchor: str | None = None) -> None:
    from .gate_contracts import validate_contract

    validate_contract("canonical_manifest.v2.json", manifest)
    unsigned_manifest = {
        key: deepcopy(value) for key, value in manifest.items()
        if key not in {"generation_id", "manifest_digest_sha256", "trust_anchor_sha256"}
    }
    expected_generation, expected_manifest_digest = canonical_v2_identities(unsigned_manifest)
    if manifest["generation_id"] != expected_generation:
        raise ContractError("canonical-v2 generation digest mismatch")
    if manifest["manifest_digest_sha256"] != expected_manifest_digest:
        raise ContractError("canonical-v2 manifest digest mismatch")
    if (
        not isinstance(expected_anchor, str)
        or sha256_bytes(manifest["generation_id"].encode("ascii")) != expected_anchor
        or manifest["trust_anchor_sha256"] != expected_anchor
    ):
        raise ContractError("canonical-v2 trust anchor mismatch")


def package_provenance() -> dict[str, str]:
    import numpy
    import pandas
    import pyarrow

    lockfile = Path(__file__).resolve().parents[3] / "uv.lock"
    return {
        "project_version": "0.1.0",
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "pandas_version": pandas.__version__,
        "pyarrow_version": pyarrow.__version__,
        "numpy_version": numpy.__version__,
        "dependency_lock_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest(),
    }


def build_canonical_v2_manifest(
    *,
    schema: Schema,
    partition_date: date,
    frame: pd.DataFrame,
    code_fingerprint: str,
    lineage: dict[str, Any],
    source_versions: dict[str, str],
    quality_checksum: str = "",
    raw_artifacts: dict[str, object] | None = None,
) -> dict[str, Any]:
    columns = list(frame.columns)
    dtypes = {name: str(dtype) for name, dtype in frame.dtypes.items()}
    if set(dtypes) != set(columns):
        raise ContractError("dtype map must exactly match frame columns")
    return {
        "manifest_version": 2,
        "dataset": schema.dataset,
        "schema_version": schema.version,
        "partition_date": partition_date.isoformat(),
        "row_count": len(frame),
        "columns": columns,
        "dtypes": dtypes,
        "data_checksum_sha256": "0" * 64,
        "schema_checksum_sha256": schema_file_checksum(schema.source_path),
        "code_fingerprint": code_fingerprint,
        "package_provenance": package_provenance(),
        "run_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "lineage": lineage,
        "source_versions": source_versions,
        "quality_report_checksum": quality_checksum,
        "raw_artifacts": raw_artifacts or {},
    }


def finalize_canonical_v2_identities(manifest_without_digests: dict[str, Any]) -> dict[str, Any]:
    clean_manifest = {
        key: deepcopy(value) for key, value in manifest_without_digests.items()
        if key not in {"generation_id", "manifest_digest_sha256", "trust_anchor_sha256"}
    }
    generation_id, manifest_digest = canonical_v2_identities(clean_manifest)
    return {
        **clean_manifest,
        "generation_id": generation_id,
        "manifest_digest_sha256": manifest_digest,
        "trust_anchor_sha256": sha256_bytes(generation_id.encode("ascii")),
    }


def serialize_canonical_v2(schema: Schema, frame: pd.DataFrame) -> tuple[bytes, pd.DataFrame]:
    """Serialize the deterministic sorted artifact and return its readback frame."""
    import io

    buffer = io.BytesIO()
    frame.sort_values(list(schema.sort_key)).to_parquet(buffer, index=False)
    artifact = buffer.getvalue()
    restored = pd.read_parquet(io.BytesIO(artifact))
    schema.validate(restored)
    return artifact, restored


class CanonicalV2Store:
    """Publish immutable canonical-v2 partitions."""

    def __init__(self, root: Path, *, code_fingerprint: str | None = None) -> None:
        self.root = root
        self.code_fingerprint = code_fingerprint or sha256_json({"component": "CanonicalStore", "version": 2})

    def prepare_generation(
        self,
        schema: Schema,
        partition_date: date,
        frame: pd.DataFrame,
        lineage: dict[str, Any],
        source_versions: dict[str, str],
        raw_artifacts: dict[str, object] | None = None,
    ) -> str:
        """Return the quality-independent generation for deterministic binding."""
        artifact, restored = serialize_canonical_v2(schema, frame)
        data_checksum = file_sha256_bytes(artifact)
        manifest = build_canonical_v2_manifest(
            schema=schema,
            partition_date=partition_date,
            frame=restored,
            code_fingerprint=self.code_fingerprint,
            lineage=lineage,
            source_versions=source_versions,
            quality_checksum="0" * 64,
            raw_artifacts=raw_artifacts,
        )
        manifest["data_checksum_sha256"] = data_checksum
        manifest.pop("generation_id", None)
        manifest.pop("manifest_digest_sha256", None)
        manifest.pop("trust_anchor_sha256", None)
        return finalize_canonical_v2_identities(manifest)["generation_id"]

    def publish(
        self,
        schema: Schema,
        partition_date: date,
        frame: pd.DataFrame,
        lineage: dict[str, Any],
        source_versions: dict[str, str],
        quality_checksum: str,
        raw_artifacts: dict[str, object] | None = None,
    ) -> Path:
        schema.validate(frame)
        partition = self.root / "canonical" / schema.dataset / schema.version / f"date={partition_date.isoformat()}"
        if partition.exists():
            raise ContractError(f"immutable partition already published: {partition}")
        staging = self._staging_path(partition)
        staging.mkdir(parents=True)
        try:
            data_path = staging / "data.parquet"
            sorted_frame = frame.sort_values(list(schema.sort_key))
            sorted_frame.to_parquet(data_path, index=False)
            restored = pd.read_parquet(data_path)
            schema.validate(restored)
            manifest = build_canonical_v2_manifest(
                schema=schema,
                partition_date=partition_date,
                frame=restored,
                code_fingerprint=self.code_fingerprint,
                lineage=lineage,
                source_versions=source_versions,
                quality_checksum=quality_checksum,
                raw_artifacts=raw_artifacts,
            )
            manifest["data_checksum_sha256"] = file_sha256(data_path)
            manifest.pop("generation_id", None)
            manifest.pop("manifest_digest_sha256", None)
            manifest.pop("trust_anchor_sha256", None)
            manifest = finalize_canonical_v2_identities(manifest)

            from .artifacts import QualityReportStore

            report_path = self.root / "reports" / "canonical_v2" / manifest["generation_id"] / "report.json"
            report = QualityReportStore().read(
                self.root, manifest["generation_id"], binding_type="canonical_v2"
            )
            if (
                report["policy"] != "reject_all"
                or report["status"] != "passed"
                or file_sha256(report_path) != quality_checksum
            ):
                raise ContractError("canonical-v2 quality report rejects publication")

            from .gate_contracts import validate_contract

            validate_contract("canonical_manifest.v2.json", manifest)
            (staging / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            fsync_tree(staging)
            os.replace(staging, partition)
            fsync_dir(partition.parent)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return partition / "data.parquet"

    def _staging_path(self, partition: Path) -> Path:
        return partition.with_name(f"{partition.name}.staging.{uuid.uuid4().hex}")


def read_canonical_v2(
    root: Path,
    schema: Schema,
    partition_date: date,
    *,
    expected_anchor: str,
    verify_checksum: bool = True,
) -> pd.DataFrame:
    partition = root / "canonical" / schema.dataset / schema.version / f"date={partition_date.isoformat()}"
    manifest_path = partition / "manifest.json"
    data_path = partition / "data.parquet"
    if not manifest_path.is_file() or not data_path.is_file():
        raise ContractError(f"unpublished or incomplete partition: {partition}")

    from .gate_contracts import validate_contract

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError("malformed canonical-v2 manifest") from exc
    validate_contract("canonical_manifest.v2.json", manifest)
    _validate_manifest(manifest, expected_anchor=expected_anchor)
    from .artifacts import QualityReportStore

    report_path = root / "reports" / "canonical_v2" / manifest["generation_id"] / "report.json"
    report = QualityReportStore().read(
        root, manifest["generation_id"], binding_type="canonical_v2"
    )
    if (
        report["policy"] != "reject_all"
        or report["status"] != "passed"
        or file_sha256(report_path) != manifest["quality_report_checksum"]
    ):
        raise ContractError("canonical-v2 quality report rejects read")
    if (
        manifest["dataset"] != schema.dataset
        or manifest["schema_version"] != schema.version
        or manifest["partition_date"] != partition_date.isoformat()
    ):
        raise ContractError("physical path does not match canonical-v2 manifest identity")
    if verify_checksum and manifest["data_checksum_sha256"] != file_sha256(data_path):
        raise ContractError("canonical-v2 data checksum mismatch")
    frame = pd.read_parquet(data_path)
    if list(frame.columns) != manifest["columns"]:
        raise ContractError("canonical-v2 column order mismatch")
    if len(frame) != manifest["row_count"]:
        raise ContractError("canonical-v2 row count mismatch")
    if set(frame.dtypes.index) != set(manifest["dtypes"]):
        raise ContractError("canonical-v2 dtype map does not exactly match frame")
    for column, dtype in manifest["dtypes"].items():
        if str(frame[column].dtype) != dtype:
            raise ContractError(f"canonical-v2 dtype mismatch: {column}")
    schema.validate(frame)
    return frame


@dataclass(frozen=True)
class MigrationResult:
    audit_path: Path
    target_partition: Path | None
    mapping_checksum_sha256: str


class CanonicalMigrationLedger:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.ledger_path = root / "canonical-migrations" / "v1-to-v2.jsonl"

    @property
    def audit_path(self) -> Path:
        return self.ledger_path

    def append(self, record_without_checksum: dict[str, Any]) -> MigrationResult:
        candidate = {
            **record_without_checksum,
            "mapping_checksum_sha256": "0" * 64,
        }
        validate_contract("canonical_migration.v1.json", candidate)
        existing = self.records()
        source_key = (
            record_without_checksum["source_dataset"],
            record_without_checksum["source_schema_version"],
            record_without_checksum["source_partition_path"],
        )
        if any(existing_source_key[:3] == source_key for existing_record, existing_source_key in existing):
            raise ContractError("duplicate canonical migration source mapping")
        if record_without_checksum["action"] == "republish_v2":
            target_key = (
                record_without_checksum["target_dataset"],
                record_without_checksum["target_schema_version"],
                record_without_checksum["target_partition_path"],
            )
            if any(existing_record["action"] == "republish_v2" and existing_target_key[3:] == target_key for existing_record, existing_target_key in existing):
                raise ContractError("reused canonical migration destination")

        checksum = file_sha256_bytes(canonical_json(candidate))
        record = {**candidate, "mapping_checksum_sha256": checksum}
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.ledger_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0, os.SEEK_END)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return MigrationResult(self.ledger_path, None, checksum)

    def records(self) -> list[tuple[dict[str, Any], tuple[str, str, str, str, str, str]]]:
        if not self.ledger_path.exists():
            return []
        result: list[tuple[dict[str, Any], tuple[str, str, str, str, str, str]]] = []
        for line_number, line in enumerate(self.ledger_path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
                validate_contract("canonical_migration.v1.json", record)
            except json.JSONDecodeError as exc:
                raise ContractError(f"malformed canonical migration ledger at line {line_number}") from exc
            except Exception as exc:
                raise ContractError(f"invalid canonical migration ledger at line {line_number}") from exc
            unsigned = {**record, "mapping_checksum_sha256": "0" * 64}
            expected = file_sha256_bytes(canonical_json(unsigned))
            if record["mapping_checksum_sha256"] != expected:
                raise ContractError(f"tampered canonical migration mapping at line {line_number}")
            result.append((record, (
                record["source_dataset"], record["source_schema_version"],
                record["source_partition_path"], record["target_dataset"],
                record["target_schema_version"], record["target_partition_path"],
            )))
        return result

def file_sha256(path: Path) -> str:
    return file_sha256_bytes(path.read_bytes())


def file_sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    fsync_dir(root)
