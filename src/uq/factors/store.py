from __future__ import annotations

import fcntl
import json
import os
import shutil
import uuid
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow as arrow
import pyarrow.parquet as parquet

from ..contracts.artifacts import QualityReportStore
from ..contracts.canonical_v2 import file_sha256_bytes, fsync_dir, fsync_tree
from ..contracts.factor_governance import FactorRegistry
from ..contracts.gate_contracts import canonical_json, factor_manifest_identities, validate_contract_path
from ..errors import ContractError
from .raw_price import logical_fingerprint
from .repro_staging import SERIALIZATION_PROFILE, environment_profile


class FactorStore:
    def __init__(self, root: Path, registry: FactorRegistry) -> None:
        self.root = root
        self.registry = registry

    def _partition(
        self,
        *,
        input_dataset: str,
        input_schema_version: str,
        factor_set: str,
        factor_version: str,
        partition_date: date,
    ) -> Path:
        return self.root / "factors" / (
            f"dataset={input_dataset}/schema_version={input_schema_version}/"
            f"factor_set={factor_set}/factor_version={factor_version}/date={partition_date.isoformat()}"
        )

    def publish(
        self,
        *,
        factor_set: str = "basic",
        factor_version: str = "1.0.0",
        frame: pd.DataFrame,
        partition_date: date,
        input_dataset: str,
        input_schema_version: str,
        upstream_generation_id: str,
        upstream_data_checksum: str,
        quality_report_checksum: str,
        adjustment_snapshot_id: str | None = None,
        effective_date_table_checksum: str | None = None,
        upstream_created_at: str | datetime,
    ) -> Path:
        arguments = {
            "factor_set": factor_set,
            "factor_version": factor_version,
            "frame": frame,
            "partition_date": partition_date,
            "input_dataset": input_dataset,
            "input_schema_version": input_schema_version,
            "upstream_generation_id": upstream_generation_id,
            "upstream_data_checksum": upstream_data_checksum,
            "quality_report_checksum": quality_report_checksum,
            "adjustment_snapshot_id": adjustment_snapshot_id,
            "effective_date_table_checksum": effective_date_table_checksum,
            "upstream_created_at": _iso_datetime(upstream_created_at),
        }
        definition = self.registry.get(factor_set, factor_version)
        if definition.factor_set == "alpha360" or not definition.factors:
            raise ContractError("contract-only factor sets cannot be published")
        local_quality = _validate_factor_frame(frame, definition)
        if definition.factor_set == "basic":
            if input_dataset != "bars_daily" or input_schema_version != "research-v1":
                raise ContractError("factor input binding does not match reviewed factor set")
            if adjustment_snapshot_id is not None or effective_date_table_checksum is not None:
                raise ContractError("raw-price factors cannot bind adjustment lineage")
        elif definition.factor_set == "adjusted":
            if input_dataset != "bars_adjusted" or input_schema_version != "adjusted-v1":
                raise ContractError("adjusted factor input binding does not match reviewed factor set")
            if adjustment_snapshot_id is None or effective_date_table_checksum is None:
                raise ContractError("adjusted factors require governed adjustment lineage")
        elif definition.factor_set.startswith("alpha"):
            bindings = definition.document.get("input_bindings")
            if not bindings or input_dataset not in bindings["allowed_datasets"]:
                raise ContractError(f"qlib factor input dataset is not reviewed: {input_dataset}")
            reviewed_schemas = bindings["required_schema_versions"].get(input_dataset, [])
            if input_schema_version not in reviewed_schemas:
                raise ContractError(f"qlib factor input schema is not reviewed: {input_schema_version}")
            if input_dataset == "bars_adjusted" and (
                adjustment_snapshot_id is None or effective_date_table_checksum is None
            ):
                raise ContractError("adjusted qlib factors require governed adjustment lineage")
        else:
            raise ContractError(f"unsupported governed factor set: {factor_set}")

        artifact, data_checksum = serialize_factor_frame(frame)
        generation_id = factor_generation(**arguments)
        manifest = _manifest_without_identities(
            local_quality=local_quality,
            frame=frame,
            partition_date=partition_date,
            input_dataset=input_dataset,
            input_schema_version=input_schema_version,
            upstream_generation_id=upstream_generation_id,
            upstream_data_checksum=upstream_data_checksum,
            quality_report_checksum=quality_report_checksum,
            adjustment_snapshot_id=adjustment_snapshot_id,
            effective_date_table_checksum=effective_date_table_checksum,
            upstream_created_at=upstream_created_at,
            definition=definition,
            data_checksum=data_checksum,
        )
        manifest_generation, manifest_digest = factor_manifest_identities(manifest)
        if manifest_generation != generation_id:
            raise ContractError("factor generation derivation mismatch")
        manifest.update(generation_id=generation_id, manifest_digest_sha256=manifest_digest)

        partition = self._partition(
            input_dataset=input_dataset,
            input_schema_version=input_schema_version,
            factor_set=factor_set,
            factor_version=factor_version,
            partition_date=partition_date,
        )
        staging = partition.with_name(f"{partition.name}.staging.{uuid.uuid4().hex}")
        staging.mkdir(parents=True)
        lock_path = partition.parent / "publication.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if partition.exists():
                    raise ContractError(
                        f"immutable factor partition already published: {partition}"
                    )
                data_path = staging / "data.parquet"
                data_path.write_bytes(artifact)
                restored = pd.read_parquet(data_path)
                _reconcile_output(frame, restored, logical_fingerprint(frame))

                quality_report_store=QualityReportStore()
                quality_report_path=(
                    self.root / "reports" / "factor_v1" / generation_id / "report.json"
                )
                self.registry.validate_manifest(manifest)
                quality_report = quality_report_store.read(
                    self.root, generation_id, binding_type="factor_v1"
                )
                accepted_statuses = {"passed"} if manifest["quality"]["policy"] == "reject_all" else {"passed", "warning"}
                expected_report_status = local_quality["status"]
                if (
                    quality_report["policy"] != manifest["quality"]["policy"]
                    or quality_report["status"] != expected_report_status
                    or quality_report["status"] not in accepted_statuses
                    or file_sha256_bytes(quality_report_path.read_bytes())
                    != quality_report_checksum
                    or not _reports_agree(local_quality, quality_report)
                ):
                    raise ContractError("factor quality report rejects publication")

                (staging / "manifest.json").write_text(
                    json.dumps(manifest, sort_keys=True, indent=2) + "\n"
                )
                fsync_tree(staging)
                os.replace(staging, partition)
                fsync_dir(partition.parent)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return partition


def factor_generation(
    *,
    factor_set: str = "basic",
    factor_version: str = "1.0.0",
    frame: pd.DataFrame,
    partition_date: date,
    input_dataset: str,
    input_schema_version: str,
    upstream_generation_id: str,
    upstream_data_checksum: str,
    quality_report_checksum: str,
    adjustment_snapshot_id: str | None = None,
    effective_date_table_checksum: str | None = None,
    upstream_created_at: str | datetime,
) -> str:
    _, data_checksum = serialize_factor_frame(frame)
    definition = FactorRegistry(Path(__file__).resolve().parents[3]).get(factor_set, factor_version)
    unsigned = _manifest_without_identities(
        local_quality=_validate_factor_frame(frame, definition),
        frame=frame,
        partition_date=partition_date,
        input_dataset=input_dataset,
        input_schema_version=input_schema_version,
        upstream_generation_id=upstream_generation_id,
        upstream_data_checksum=upstream_data_checksum,
        quality_report_checksum=quality_report_checksum,
        adjustment_snapshot_id=adjustment_snapshot_id,
        effective_date_table_checksum=effective_date_table_checksum,
        upstream_created_at=upstream_created_at,
        definition=definition,
        data_checksum=data_checksum,
    )
    generation, _ = factor_manifest_identities(unsigned)
    return generation


def serialize_factor_frame(frame: pd.DataFrame) -> tuple[bytes, str]:
    ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True)
    table = arrow.Table.from_pandas(ordered, preserve_index=False)
    sink = arrow.BufferOutputStream()
    parquet.write_table(
        table,
        sink,
        compression=SERIALIZATION_PROFILE["compression"],
    )
    artifact = sink.getvalue().to_pybytes()
    return artifact, file_sha256_bytes(artifact)


def _iso_datetime(value: str | datetime) -> str:
    return value if isinstance(value, str) else value.isoformat()


def _reports_agree(left: dict[str, Any], right: dict[str, Any]) -> bool:
    def normalized(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
        checks = report.get("checks", [])
        normalized_checks = {
            item["name"]: {
                "threshold": item["threshold"],
                "observed": item["observed"],
                "level": item["level"],
                "result": item["result"],
            }
            for item in checks
        }
        if len(normalized_checks) != len(checks):
            raise ContractError("duplicate quality checks reject publication")
        return normalized_checks

    left_checks = normalized(left)
    right_checks = normalized(right)
    return bool(left_checks) and left_checks == right_checks


def _validate_factor_frame(
    frame: pd.DataFrame,
    definition: Any | None = None,
) -> dict[str, Any]:
    if frame.empty:
        raise ContractError("empty factor results cannot be published")
    required = {"instrument", "datetime"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ContractError(f"missing factor output columns: {missing}")
    if definition is not None:
        reviewed_factors = {factor["name"] for factor in definition.factors}
        if definition.factor_set.startswith("alpha"):
            if set(frame.columns) != required | reviewed_factors:
                raise ContractError("factor output columns do not match reviewed factor set")
        elif not reviewed_factors.issuperset(set(frame.columns) - required):
            raise ContractError("factor output columns are outside the reviewed factor set")
    if frame.duplicated(["instrument", "datetime"]).any():
        raise ContractError("duplicate factor keys reject publication")
    thresholds = (definition.document.get("quality_thresholds", {}) if definition else {})
    maximum_null_rate = float(thresholds.get("null_rate_maximum", 0.0))
    minimum_coverage = float(thresholds.get("coverage_minimum", 1.0))
    value_columns = [column for column in frame.columns if column not in required]
    try:
        numeric_values = frame[value_columns].to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise ContractError("factor values must be numeric") from exc
    observed_null_rate = float(np.isnan(numeric_values).mean())
    observed_coverage = 1.0 - observed_null_rate
    finite_failures = int(np.isinf(numeric_values).sum())
    warnings: list[str] = []
    errors: list[str] = []
    checks = [
        {"name": "null_rate", "threshold": maximum_null_rate, "observed": observed_null_rate, "level": "error", "result": "passed" if observed_null_rate <= maximum_null_rate else "failed"},
        {"name": "coverage", "threshold": minimum_coverage, "observed": observed_coverage, "level": "error", "result": "passed" if observed_coverage >= minimum_coverage else "failed"},
    ]
    for column in value_columns:
        values = frame[column].to_numpy(dtype=float)
        null_rate = float(np.isnan(values).mean())
        infinite_count = int(np.isinf(values).sum())
        checks.append({
            "name": f"null_rate__{column}", "threshold": maximum_null_rate,
            "observed": null_rate, "level": "error",
            "result": "passed" if null_rate <= maximum_null_rate else "failed",
        })
        checks.append({
            "name": f"finite_values__{column}", "threshold": 0,
            "observed": infinite_count, "level": "error",
            "result": "passed" if infinite_count == 0 else "failed",
        })
    for check in checks:
        if check["result"] == "failed":
            errors.append(f'{check["name"]} threshold failed')
    return {
        "status": "rejected" if errors else ("warning" if warnings else "passed"),
        "policy": definition.document["quality_policy"] if definition else "reject_all",
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
    }


def _manifest_without_identities(
    *,
    local_quality: dict[str, Any],
    frame: pd.DataFrame,
    partition_date: date,
    input_dataset: str,
    input_schema_version: str,
    upstream_generation_id: str,
    upstream_data_checksum: str,
    quality_report_checksum: str,
    adjustment_snapshot_id: str | None,
    effective_date_table_checksum: str | None,
    upstream_created_at: str | datetime,
    definition: Any,
    data_checksum: str,
) -> dict[str, Any]:
    environment = environment_profile()
    is_qlib = definition.factor_set.startswith("alpha")
    decision_time = datetime.combine(
        partition_date, time(15, 0), tzinfo=ZoneInfo("Asia/Shanghai")
    )
    code_fingerprint = file_sha256_bytes(Path(__file__).read_bytes())
    if is_qlib:
        from .qlib_adapter import QlibFactorAdapter

        code_fingerprint = QlibFactorAdapter._fingerprint()
    manifest = {
        "manifest_version": 2 if is_qlib else 1,
        "input_dataset": input_dataset,
        "input_schema_version": input_schema_version,
        "factor_set": definition.factor_set,
        "factor_version": definition.factor_version,
        "partition_date": partition_date.isoformat(),
        "decision_time": decision_time.isoformat(),
        "run_visible_cutoff": decision_time.isoformat(),
        "inputs": [{
            "binding": "bars",
            "dataset": input_dataset,
            "schema_version": input_schema_version,
            "partition_date": partition_date.isoformat(),
            "manifest_generation_id": upstream_generation_id,
            "data_checksum_sha256": upstream_data_checksum,
            "upstream_created_at": _iso_datetime(upstream_created_at),
            "adjustment_snapshot_id": adjustment_snapshot_id,
            "effective_date_table_checksum": effective_date_table_checksum,
        }],
        "factor_definitions": definition.factors,
        "universe_snapshot": None,
        "row_count": len(frame),
        "columns": list(frame.columns),
        "dtypes": {column: str(dtype) for column, dtype in frame.dtypes.items()},
        "data_checksum_sha256": data_checksum,
        "logical_fingerprint": logical_fingerprint(frame),
        "engine_version": "v0",
        "code_fingerprint": code_fingerprint,
        "serialization_profile_id": SERIALIZATION_PROFILE["profile_id"],
        "engine_package_provenance": {
            "project_version": "0.1.0",
            "python_version": environment["python_version"],
            "dependency_lock_digest_sha256": environment["lockfile_sha256"],
        },
        "run_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "quality": {
            "status": local_quality["status"],
            "policy": definition.document["quality_policy"],
            "report_checksum_sha256": quality_report_checksum,
        },
    }
    if is_qlib:
        from .qlib_adapter import QlibFactorAdapter

        installed_qlib_version = QlibFactorAdapter.qlib_version()
        if installed_qlib_version not in definition.document.get("reviewed_engine_versions", []):
            raise ContractError(
                f"qlib version {installed_qlib_version} is not reviewed for "
                f"{definition.factor_set}/{definition.factor_version}"
            )
        manifest["engine_contract"] = {
            "engine_name": "qlib",
            "engine_version": installed_qlib_version,
            "qlib_expression_set": definition.document["qlib_expression_set"],
        }
    return manifest


def _reconcile_output(expected: pd.DataFrame, actual: pd.DataFrame, fingerprint: str) -> None:
    if len(actual) != len(expected) or list(actual.columns) != list(expected.columns):
        raise ContractError("factor readback reconciliation failed")
    actual_dtypes = {str(key): str(value) for key, value in actual.dtypes.items()}
    expected_dtypes = {str(key): str(value) for key, value in expected.dtypes.items()}
    if actual_dtypes != expected_dtypes:
        raise ContractError("factor dtype readback mismatch")
    expected_keys = set(map(tuple, expected[["instrument", "datetime"]].to_numpy()))
    actual_keys = set(map(tuple, actual[["instrument", "datetime"]].to_numpy()))
    if actual_keys != expected_keys:
        raise ContractError("factor key readback reconciliation failed")
    if logical_fingerprint(actual) != fingerprint:
        raise ContractError("factor logical fingerprint readback mismatch")


def read_factor_partition(partition: Path) -> pd.DataFrame:
    manifest_path = partition / "manifest.json"
    data_path = partition / "data.parquet"
    if not manifest_path.is_file() or not data_path.is_file():
        raise ContractError("unpublished or incomplete factor partition")
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise ContractError("malformed factor manifest") from exc

    schema_path = (
        Path(__file__).resolve().parents[3]
        / f"config/schemas/manifests/factor_manifest.v{manifest.get('manifest_version', 1)}.json"
    )
    expected_components = {
        "dataset": "input_dataset",
        "schema_version": "input_schema_version",
        "factor_set": "factor_set",
        "factor_version": "factor_version",
        "date": "partition_date",
    }
    components = [partition.name, *(item.name for item in partition.parents[:4])]
    observed = [
        f"{key}={manifest.get(field)}"
        for key, field in reversed(list(expected_components.items()))
    ]
    if len(components) != 5 or components != observed:
        raise ContractError("physical path does not match manifest identity")

    validate_contract_path(schema_path, manifest)
    unsigned = {
        key: value for key, value in manifest.items() if key != "manifest_digest_sha256"
    }
    expected_generation, expected_digest = factor_manifest_identities({
        key: value for key, value in unsigned.items() if key != "generation_id"
    })
    if (
        manifest.get("generation_id") != expected_generation
        or manifest.get("manifest_digest_sha256") != expected_digest
    ):
        raise ContractError("tampered factor manifest identity")
    if manifest.get("data_checksum_sha256") != file_sha256_bytes(data_path.read_bytes()):
        raise ContractError("tampered factor data prevents factor read")

    registry = FactorRegistry(Path(__file__).resolve().parents[3])
    definition = registry.get(
        manifest["factor_set"], manifest["factor_version"]
    )
    registry.validate_manifest(manifest)
    if definition.factor_set.startswith("alpha"):
        engine = manifest.get("engine_contract", {})
        if engine.get("qlib_expression_set") != definition.document.get("qlib_expression_set"):
            raise ContractError("factor engine expression set does not match reviewed definition")
        if engine.get("engine_version") not in definition.document.get(
            "reviewed_engine_versions", []
        ):
            raise ContractError("factor engine version is not reviewed")
    frame = pd.read_parquet(data_path)
    if len(frame) != manifest.get("row_count") or list(frame.columns) != manifest.get("columns"):
        raise ContractError("factor artifact shape does not match manifest")
    expected_dtypes = manifest.get("dtypes", {})
    if {
        str(key): str(value) for key, value in frame.dtypes.items()
    } != {key: str(value) for key, value in expected_dtypes.items()}:
        raise ContractError("factor artifact dtype does not match manifest")
    try:
        if logical_fingerprint(frame) != manifest.get("logical_fingerprint"):
            raise ContractError("tampered factor logical content")
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid factor artifact for logical verification") from exc
    report_root = partition.parents[5]
    report_path = report_root / "reports" / "factor_v1" / manifest["generation_id"] / "report.json"
    report = QualityReportStore().read(report_root, manifest["generation_id"], binding_type="factor_v1")
    accepted_statuses = {"passed"} if manifest["quality"]["policy"] == "reject_all" else {"passed", "warning"}
    reviewed_factors = {factor["name"] for factor in definition.factors}
    if definition.factor_set.startswith("alpha"):
        expected_columns = {"instrument", "datetime", *reviewed_factors}
        if set(frame.columns) != expected_columns:
            raise ContractError("factor artifact columns do not match reviewed factor set")
    elif not reviewed_factors.issuperset(set(frame.columns) - {"instrument", "datetime"}):
        raise ContractError("factor artifact columns are outside the reviewed factor set")
    try:
        factor_values = frame[list(set(frame.columns) - {"instrument", "datetime"})].to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise ContractError("factor artifact values must be numeric") from exc
    if np.isinf(factor_values).any():
        raise ContractError("factor artifact contains non-finite values")
    if (
        report["policy"] != manifest["quality"]["policy"]
        or report["status"] not in accepted_statuses
        or file_sha256_bytes(report_path.read_bytes())
        != manifest["quality"]["report_checksum_sha256"]
    ):
        raise ContractError("factor quality report rejects read")
    return frame


def quarantine_rejected(root: Path, frame: pd.DataFrame, reason: str, *, operator: str = "factor-store") -> Path:
    directory = root / "quarantine" / uuid.uuid4().hex
    staging = directory.with_name(directory.name + ".staging")
    staging.mkdir(parents=True)
    try:
        artifact = staging / "rejected.parquet"
        frame.to_parquet(artifact, index=False)
        created_at = datetime.now(timezone.utc).isoformat()
        manifest = {
            "quarantine_version": 1,
            "reason": reason,
            "operator": operator,
            "created_at": created_at,
            "row_count": len(frame),
            "columns": list(frame.columns),
            "data_checksum_sha256": file_sha256_bytes(artifact.read_bytes()),
            "retention_policy": "manual-review; no automatic accepted promotion",
        }
        content = canonical_json(manifest)
        (staging / "manifest.json").write_bytes(content)
        (staging / "manifest.sha256").write_text(file_sha256_bytes(content) + "\n")
        fsync_tree(staging)
        os.replace(staging, directory)
        fsync_dir(directory.parent)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return directory


def read_quarantine_manifest(directory: Path) -> dict[str, Any]:
    manifest_path = directory / "manifest.json"
    digest_path = directory / "manifest.sha256"
    try:
        content = manifest_path.read_bytes()
        recorded = digest_path.read_text().strip()
    except FileNotFoundError as exc:
        raise ContractError("missing quarantine governance files") from exc
    if file_sha256_bytes(content) != recorded:
        raise ContractError("tampered quarantine manifest")
    try:
        manifest = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ContractError("malformed quarantine manifest") from exc
    if file_sha256_bytes((directory / "rejected.parquet").read_bytes()) != manifest.get("data_checksum_sha256"):
        raise ContractError("tampered quarantine data")
    if manifest.get("retention_policy") != "manual-review; no automatic accepted promotion":
        raise ContractError("unsafe quarantine retention policy")
    return manifest
