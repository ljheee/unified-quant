from __future__ import annotations

import json
import os
import io
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as parquet

from ..contracts.model_layer import (
    ModelContractLoader,
    bind_reviewed_quality_decision,
    file_sha256_bytes,
    paper_execution_identities,
    sha256_json,
)
from ..errors import ContractError

if TYPE_CHECKING:
    from ..risk.publication import ConfigPublicationBinding


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(path: Path) -> None:
    fsync_dir(path)
    for child in sorted(path.iterdir()):
        if child.is_dir():
            fsync_tree(child)
        else:
            fd = os.open(child, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


class _ImmutablePaperManifestStore:
    def __init__(self, root: Path | str, *, family: str, directory: str) -> None:
        self.root = Path(root)
        self.family = family
        self.directory = self.root / directory

    def _safe_resolve(self, path: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ContractError("unsafe or missing paper path") from exc
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise ContractError("symbolic links are forbidden in paper paths")
        try:
            resolved.relative_to(self.root.resolve(strict=False))
        except ValueError as exc:
            raise ContractError("paper path escapes storage root") from exc
        return resolved

    def _partition_path(self, manifest: dict[str, Any]) -> Path:
        raise NotImplementedError

    def _validate_manifest_identity(self, manifest: dict[str, Any]) -> None:
        expected_generation, expected_digest = paper_execution_identities(
            manifest, schema_name=self.family
        )
        if (
            manifest.get("generation_id") != expected_generation
            or manifest.get("manifest_digest_sha256") != expected_digest
        ):
            raise ContractError(f"paper {self.family} manifest identity mismatch")

    def _read_manifest(self, generation_id: str) -> tuple[Path, dict[str, Any]]:
        candidates = list(self.directory.rglob(f"generation={generation_id}"))
        if len(candidates) != 1:
            raise ContractError(f"paper {self.family} partition is missing or ambiguous")
        manifest_path = candidates[0] / "manifest.json"
        self._safe_resolve(manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        ModelContractLoader.validate(self.family, manifest)
        self._validate_manifest_identity(manifest)
        return candidates[0], manifest

    def _publish_review(self, report: dict[str, Any], checksum: str) -> None:
        review_path = self.root / "external_quality_reviews" / f"{checksum}.json"
        if review_path.exists():
            existing = json.loads(review_path.read_text(encoding="utf-8"))
            if existing != report:
                raise ContractError("external quality review already exists with different content")
            return
        review_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(review_path, report)

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        fsync_dir(path.parent)

    def _publish_json(self, manifest: dict[str, Any]) -> Path:
        partition = self._partition_path(manifest)
        manifest_path = partition / "manifest.json"
        if manifest_path.exists() or partition.exists():
            raise ContractError(f"paper {self.family} partition already exists: {partition}")
        staging = partition.parent / f".staging_{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            self._atomic_write_json(staging / "manifest.json", manifest)
            fsync_tree(staging)
            os.replace(staging, partition)
            fsync_dir(partition.parent)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return partition

    def read(self, generation_id: str) -> dict[str, Any]:
        _, manifest = self._read_manifest(generation_id)
        return manifest


class ExecutionConfigStore(_ImmutablePaperManifestStore):
    def __init__(self, root: Path | str) -> None:
        super().__init__(root, family="execution_config", directory="execution-configs")

    def _partition_path(self, manifest: dict[str, Any]) -> Path:
        return self.directory / manifest["execution_id"] / f"generation={manifest['generation_id']}"

    def publish(
        self,
        manifest: dict[str, Any],
        *,
        quality_decision: dict[str, Any],
        risk_decision: dict[str, Any],
        config_publication: "ConfigPublicationBinding",
    ) -> Path:
        config_publication.assert_can_publish_config()
        _validate_executable_risk_decision(risk_decision, manifest)
        manifest["generation_id"], manifest["manifest_digest_sha256"] = paper_execution_identities(
            manifest, schema_name=self.family
        )
        report, checksum = bind_reviewed_quality_decision(
            quality_decision,
            binding_type="execution_config_v1",
            subject_generation_id=manifest["generation_id"],
            subject_content_sha256=manifest["generation_id"],
        )
        manifest["quality_report_checksum_sha256"] = checksum
        manifest["generation_id"], manifest["manifest_digest_sha256"] = paper_execution_identities(
            manifest, schema_name=self.family
        )
        ModelContractLoader.validate(self.family, manifest)
        partition = self._publish_json(manifest)
        self._publish_review(report, checksum)
        return partition

    def read(self, generation_id: str) -> dict[str, Any]:
        manifest = super().read(generation_id)
        _validate_bound_quality_report(self.root, manifest, "execution_config_v1")
        return manifest


class OrderPlanStore(_ImmutablePaperManifestStore):
    def __init__(self, root: Path | str) -> None:
        super().__init__(root, family="order_plan", directory="order-plans")

    def _partition_path(self, manifest: dict[str, Any]) -> Path:
        return (
            self.directory / manifest["execution_id"] / manifest["execution_date"]
            / f"generation={manifest['generation_id']}"
        )

    def publish(
        self,
        manifest: dict[str, Any],
        frame: Any,
        *,
        quality_decision: dict[str, Any],
    ) -> Path:
        artifact, payload_checksum = self._serialize(frame)
        manifest["data_checksum_sha256"] = payload_checksum
        manifest["generation_id"], manifest["manifest_digest_sha256"] = paper_execution_identities(
            manifest, schema_name=self.family
        )
        report, report_checksum = bind_reviewed_quality_decision(
            quality_decision,
            binding_type="order_plan_v1",
            subject_generation_id=manifest["generation_id"],
            subject_content_sha256=manifest["generation_id"],
        )
        manifest["quality_report_checksum_sha256"] = report_checksum
        manifest["generation_id"], manifest["manifest_digest_sha256"] = paper_execution_identities(
            manifest, schema_name=self.family
        )
        ModelContractLoader.validate(self.family, manifest)
        self._publish_review(report, report_checksum)
        partition = self._partition_path(manifest)
        data_path = partition / manifest["data_file"]
        manifest_path = partition / "manifest.json"
        if data_path.exists() or manifest_path.exists() or partition.exists():
            raise ContractError(f"paper {self.family} partition already exists: {partition}")
        staging = partition.parent / f".staging_{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            artifact, checksum = self._serialize(frame)
            if checksum != payload_checksum:
                raise ContractError("order plan serialization checksum changed")
            (staging / manifest["data_file"]).write_bytes(artifact)
            self._atomic_write_json(staging / "manifest.json", manifest)
            fsync_tree(staging)
            os.replace(staging, partition)
            fsync_dir(partition.parent)
            self._publish_review(report, report_checksum)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return partition

    def read(self, generation_id: str) -> tuple[dict[str, Any], Any]:
        partition, manifest = self._read_manifest(generation_id)
        _validate_bound_quality_report(self.root, manifest, "order_plan_v1")
        data_path = partition / manifest["data_file"]
        self._safe_resolve(data_path)
        artifact = data_path.read_bytes()
        if file_sha256_bytes(artifact) != manifest["data_checksum_sha256"]:
            raise ContractError("order plan payload checksum mismatch")
        return manifest, parquet.read_table(io.BytesIO(artifact)).to_pandas()

    @staticmethod
    def _serialize(frame: Any) -> tuple[bytes, str]:
        ordered = frame.sort_values(["plan_sequence"], kind="mergesort").reset_index(drop=True)
        table = pa.Table.from_pandas(ordered, preserve_index=False)
        sink = pa.BufferOutputStream()
        parquet.write_table(table, sink, compression="snappy")
        artifact = sink.getvalue().to_pybytes()
        return artifact, file_sha256_bytes(artifact)


class ExecutionResultStore(_ImmutablePaperManifestStore):
    def __init__(self, root: Path | str) -> None:
        super().__init__(root, family="execution_result", directory="execution-results")

    def _partition_path(self, manifest: dict[str, Any]) -> Path:
        return (
            self.directory / manifest["execution_id"] / manifest["execution_date"]
            / f"generation={manifest['generation_id']}"
        )

    def publish(
        self,
        manifest: dict[str, Any],
        frame: Any,
        *,
        quality_decision: dict[str, Any],
    ) -> Path:
        artifact, payload_checksum = self._serialize(frame)
        manifest["data_checksum_sha256"] = payload_checksum
        manifest["generation_id"], manifest["manifest_digest_sha256"] = paper_execution_identities(
            manifest, schema_name=self.family
        )
        report, report_checksum = bind_reviewed_quality_decision(
            quality_decision,
            binding_type="execution_result_v1",
            subject_generation_id=manifest["generation_id"],
            subject_content_sha256=manifest["generation_id"],
        )
        manifest["quality_report_checksum_sha256"] = report_checksum
        manifest["generation_id"], manifest["manifest_digest_sha256"] = paper_execution_identities(
            manifest, schema_name=self.family
        )
        ModelContractLoader.validate(self.family, manifest)
        partition = self._partition_path(manifest)
        staging = partition.parent / f".staging_{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            serialized, checksum = self._serialize(frame)
            if checksum != payload_checksum:
                raise ContractError("execution result serialization checksum changed")
            (staging / manifest["data_file"]).write_bytes(serialized)
            self._atomic_write_json(staging / "manifest.json", manifest)
            fsync_tree(staging)
            os.replace(staging, partition)
            fsync_dir(partition.parent)
            self._publish_review(report, report_checksum)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return partition

    def read(self, generation_id: str) -> tuple[dict[str, Any], Any]:
        partition, manifest = self._read_manifest(generation_id)
        _validate_bound_quality_report(self.root, manifest, "execution_result_v1")
        data_path = partition / manifest["data_file"]
        self._safe_resolve(data_path)
        artifact = data_path.read_bytes()
        if file_sha256_bytes(artifact) != manifest["data_checksum_sha256"]:
            raise ContractError("execution result payload checksum mismatch")
        frame = parquet.read_table(io.BytesIO(artifact)).to_pandas()
        if len(frame) != int(manifest["row_count"]):
            raise ContractError("execution result row count mismatch")
        if set(frame.columns) != set(manifest["columns"]):
            raise ContractError("execution result column mismatch")
        for key, value in manifest["dtypes"].items():
            if str(frame[key].dtype) != value:
                raise ContractError(f"execution result dtype mismatch for {key}")
        if frame["plan_sequence"].duplicated().any():
            raise ContractError("execution result key is not unique")
        return manifest, frame

    @staticmethod
    def _serialize(frame: Any) -> tuple[bytes, str]:
        ordered = frame.sort_values(["plan_sequence"], kind="mergesort").reset_index(drop=True)
        table = pa.Table.from_pandas(ordered, preserve_index=False)
        sink = pa.BufferOutputStream()
        parquet.write_table(table, sink, compression="snappy")
        artifact = sink.getvalue().to_pybytes()
        return artifact, file_sha256_bytes(artifact)




class PaperPortfolioStateStore(_ImmutablePaperManifestStore):
    """Publish and read immutable next paper portfolio state partitions."""

    def __init__(self, root: Path | str) -> None:
        super().__init__(root, family="paper_portfolio_state", directory="paper-states")

    def _partition_path(self, manifest: dict[str, Any]) -> Path:
        return (
            self.directory / manifest["execution_id"] / manifest["state_date"]
            / f"generation={manifest['generation_id']}"
        )

    def publish(
        self,
        manifest: dict[str, Any],
        frame: Any,
        *,
        quality_decision: dict[str, Any],
    ) -> Path:
        artifact, payload_checksum = self._serialize(frame)
        manifest["data_checksum_sha256"] = payload_checksum
        manifest["generation_id"], manifest["manifest_digest_sha256"] = paper_execution_identities(
            manifest, schema_name=self.family
        )
        report, report_checksum = bind_reviewed_quality_decision(
            quality_decision,
            binding_type="paper_portfolio_state_v1",
            subject_generation_id=manifest["generation_id"],
            subject_content_sha256=manifest["generation_id"],
        )
        manifest["quality_report_checksum_sha256"] = report_checksum
        manifest["generation_id"], manifest["manifest_digest_sha256"] = paper_execution_identities(
            manifest, schema_name=self.family
        )
        ModelContractLoader.validate(self.family, manifest)
        partition = self._partition_path(manifest)
        staging = partition.parent / f".staging_{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            serialized, checksum = self._serialize(frame)
            if checksum != payload_checksum:
                raise ContractError("paper state serialization checksum changed")
            (staging / manifest["data_file"]).write_bytes(serialized)
            self._atomic_write_json(staging / "manifest.json", manifest)
            fsync_tree(staging)
            os.replace(staging, partition)
            fsync_dir(partition.parent)
            self._publish_review(report, report_checksum)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return partition

    def read(self, generation_id: str) -> tuple[dict[str, Any], Any]:
        partition, manifest = self._read_manifest(generation_id)
        _validate_bound_quality_report(self.root, manifest, "paper_portfolio_state_v1")
        data_path = partition / manifest["data_file"]
        self._safe_resolve(data_path)
        artifact = data_path.read_bytes()
        if file_sha256_bytes(artifact) != manifest["data_checksum_sha256"]:
            raise ContractError("paper state payload checksum mismatch")
        frame = parquet.read_table(io.BytesIO(artifact)).to_pandas()
        if len(frame) != int(manifest["row_count"]):
            raise ContractError("paper state row count mismatch")
        if set(frame.columns) != set(manifest["columns"]):
            raise ContractError("paper state column mismatch")
        for key, value in manifest["dtypes"].items():
            if str(frame[key].dtype) != value:
                raise ContractError(f"paper state dtype mismatch for {key}")
        if frame["instrument"].duplicated().any():
            raise ContractError("paper state key is not unique")
        if frame["instrument"].tolist() != sorted(frame["instrument"].tolist()):
            raise ContractError("paper state payload ordering mismatch")
        if not frame["sellable_quantity"].eq(frame["quantity"] - frame["buy_locked_quantity"]).all():
            raise ContractError("paper state sellable quantity does not reconcile")
        return manifest, frame

    @staticmethod
    def _serialize(frame: Any) -> tuple[bytes, str]:
        ordered = frame.sort_values("instrument", kind="mergesort").reset_index(drop=True)
        table = pa.Table.from_pandas(ordered, preserve_index=False)
        sink = pa.BufferOutputStream()
        parquet.write_table(table, sink, compression="snappy")
        artifact = sink.getvalue().to_pybytes()
        return artifact, file_sha256_bytes(artifact)


def _validate_executable_risk_decision(
    decision: dict[str, Any], manifest: dict[str, Any]
) -> None:
    from ..contracts.gate_contracts import validate_contract
    from ..risk.contracts import risk_contract_identities

    validate_contract("risk_decision.v1.json", decision)
    decision_generation, decision_digest = risk_contract_identities(
        decision, schema_name="risk_decision"
    )
    binding = manifest.get("risk_decision_binding")
    if (
        binding is None
        or binding.get("family") != "risk_decision_v1"
        or binding.get("generation_id") != decision_generation
        or binding.get("manifest_digest_sha256") != decision_digest
    ):
        raise ContractError("risk decision does not bind this execution config")
    if decision.get("decision_scope") != "order_submission":
        raise ContractError("paper execution requires an order_submission risk decision")
    if decision.get("action") not in {"allow", "warn"}:
        raise ContractError(f"paper execution risk decision is not executable: {decision.get('action')}")


def _validate_bound_quality_report(root: Path, manifest: dict[str, Any], binding_type: str) -> dict[str, Any]:
    report = read_verified_quality_report(root, manifest)
    if report["binding_type"] != binding_type:
        raise ContractError(f"paper quality report binding mismatch: expected {binding_type}")
    if report["bound_generation_id"] != manifest["generation_id"]:
        raise ContractError("paper quality report is bound to another generation")
    if report["subject_content_sha256"] != manifest["generation_id"]:
        raise ContractError("paper quality report subject digest mismatch")
    return report


def read_verified_quality_report(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    from ..contracts.model_layer import ModelQualityReviewRegistry, verify_reviewed_quality_report_signature

    checksum = manifest["quality_report_checksum_sha256"]
    review_root = Path(root).resolve(strict=True)
    path = review_root / "external_quality_reviews" / f"{checksum}.json"
    resolved = path.resolve(strict=True)
    if review_root not in resolved.parents:
        raise ContractError("paper quality report path escapes storage root")
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ContractError("symbolic links are forbidden in paper quality report paths")
    if not resolved.is_file():
        raise ContractError("paper quality report is missing")
    report = json.loads(resolved.read_text(encoding="utf-8"))
    expected = sha256_json({key: value for key, value in report.items() if key != "report_checksum_sha256"})
    if expected != checksum:
        raise ContractError("paper quality report canonical checksum mismatch")
    ModelContractLoader.validate("model_quality_report", report)
    ModelQualityReviewRegistry().validate_report(report)
    verify_reviewed_quality_report_signature(report)
    return report
