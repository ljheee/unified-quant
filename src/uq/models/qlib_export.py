from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..contracts.canonical_v2 import file_sha256_bytes, fsync_dir, fsync_tree
from ..contracts.model_layer import ModelContractLoader, bind_reviewed_quality_decision, model_manifest_identities, sha256_json
from ..errors import ContractError


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class QlibDatasetExporter:
    """Export a governed dataset snapshot in Qlib-compatible format.

    Does NOT install or import Qlib. Only produces a directory layout that
    Qlib can later initialize against via the init receipt.
    """

    EXPORTER_VERSION = "1.0.0"

    def __init__(self, export_root: Path | str) -> None:
        self.export_root = Path(export_root)

    @property
    def exporter_fingerprint(self) -> str:
        return _sha256_text(f"QlibDatasetExporter:{self.EXPORTER_VERSION}")

    def export(
        self,
        *,
        dataset_name: str,
        generation_id: str,
        frame: pd.DataFrame,
        feature_mapping: dict[str, str],
        calendar_dates: list[str],
        instruments: list[str],
        provider_uri: str,
        quality_decision: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.empty:
            raise ContractError("cannot export empty dataset")
        if not feature_mapping:
            raise ContractError("feature mapping cannot be empty")
        if not calendar_dates:
            raise ContractError("calendar dates cannot be empty")
        if not instruments:
            raise ContractError("instruments list cannot be empty")

        staging = self.export_root / f".staging.{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        lock_path = self.export_root / "publication.lock"
        try:
            with lock_path.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

                # 1. Calendar file
                calendar_content = "\n".join(calendar_dates) + "\n"
                (staging / "calendars" / "day.txt").parent.mkdir(parents=True, exist_ok=True)
                (staging / "calendars" / "day.txt").write_text(calendar_content)
                calendar_checksum = file_sha256_bytes((staging / "calendars" / "day.txt").read_bytes())

                # 2. Instruments file
                instruments_content = "\n".join(
                    f"{inst}\t0\t{calendar_dates[-1]}" for inst in instruments
                ) + "\n"
                inst_dir = staging / "instruments"
                inst_dir.mkdir(parents=True, exist_ok=True)
                (inst_dir / "all.txt").write_text(instruments_content)
                instruments_checksum = file_sha256_bytes((inst_dir / "all.txt").read_bytes())

                # 3. Feature mapping file
                mapping_content = json.dumps(feature_mapping, sort_keys=True, indent=2) + "\n"
                mapping_path = staging / "feature_mapping.json"
                mapping_path.write_text(mapping_content)
                mapping_checksum = file_sha256_bytes(mapping_path.read_bytes())

                # 4. Data parquet with Qlib-compatible columns
                qlib_frame = frame[["instrument", "datetime", *feature_mapping.keys()]].copy()
                qlib_frame = qlib_frame.rename(columns=feature_mapping)
                data_path = staging / "data.parquet"
                qlib_frame.to_parquet(data_path, index=False, compression="snappy")

                # Build file list
                # Verify all written files before building file list
                for path in sorted(staging.rglob("*")):
                    if path.is_file() and path.stat().st_size == 0:
                        raise ContractError(f"zero-byte export file: {path.name}")

                manifest: dict[str, Any] = {
                    "contract_version": 1,
                    "export_layout": {"root": f"dataset={dataset_name}/generation={generation_id}"},
                    "files": [],
                    "provider_uri_sha256": _sha256_text(provider_uri),
                    "calendar_checksum_sha256": calendar_checksum,
                    "instruments_checksum_sha256": instruments_checksum,
                    "feature_mapping_checksum_sha256": mapping_checksum,
                    "exporter_fingerprint": self.exporter_fingerprint,
                    "serialization_profile_id": "parquet-v1",
                    "quality_report_checksum_sha256": "0" * 64,
                    "empty_cache_precondition": True,
                    "run_id": str(uuid.uuid4()),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "generation_id": "0" * 64,
                    "manifest_digest_sha256": "0" * 64,
                }
                # Build complete file list including data.parquet and manifest itself
                complete_files = []
                for path in sorted(staging.rglob("*")):
                    if path.is_file() and path.name != "manifest.json":
                        rel = path.relative_to(staging).as_posix()
                        complete_files.append({
                            "path": rel,
                            "checksum_sha256": file_sha256_bytes(path.read_bytes()),
                            "byte_size": path.stat().st_size,
                        })
                manifest["files"] = complete_files
                export_generation_id,_=model_manifest_identities(
                    manifest,
                    schema_name="qlib_dataset_export",
                    exclude_fields={"export_layout","quality_report_checksum_sha256"},
                )
                manifest["export_layout"]["root"] = f"dataset={dataset_name}/generation={export_generation_id}"
                quality_report, report_checksum = bind_reviewed_quality_decision(
                    quality_decision, binding_type="qlib_dataset_export_v1",
                    subject_generation_id=export_generation_id,
                    subject_content_sha256=sha256_json({**manifest, "export_layout": {"root": ""}}),
                )
                manifest["quality_report_checksum_sha256"]=report_checksum
                manifest["generation_id"]=export_generation_id
                _, digest = model_manifest_identities(
                    manifest,
                    schema_name="qlib_dataset_export",
                    exclude_fields={"export_layout", "quality_report_checksum_sha256"},
                )
                manifest["manifest_digest_sha256"] = digest
                ModelContractLoader.validate("qlib_dataset_export", manifest)

                report_dir=self.export_root/"external_quality_reviews"
                report_dir.mkdir(parents=True,exist_ok=True)
                report_path=report_dir/f"{report_checksum}.json"
                report_staging=report_path.with_suffix(f".staging.{uuid.uuid4().hex}")
                report_staging.write_text(json.dumps(quality_report,sort_keys=True,indent=2)+"\n")
                os.replace(report_staging,report_path)
                fsync_dir(report_dir)

                snapshot = self.export_root / f"dataset={dataset_name}" / f"generation={export_generation_id}"
                if snapshot.exists():
                    raise ContractError(f"immutable Qlib export already exists: {snapshot}")
                snapshot.parent.mkdir(parents=True, exist_ok=True)

                manifest_path = staging / "manifest.json"
                manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
                # Readback verification
                readback_manifest = json.loads(manifest_path.read_text())
                if readback_manifest["generation_id"] != manifest["generation_id"]:
                    raise ContractError("export manifest readback identity mismatch")
                for entry in manifest["files"]:
                    fp = staging / entry["path"]
                    if not fp.is_file() or file_sha256_bytes(fp.read_bytes()) != entry["checksum_sha256"]:
                        raise ContractError(f"export file verification failed: {entry['path']}")
                fsync_tree(staging)
                os.replace(staging, snapshot)
                fsync_dir(snapshot.parent)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return manifest

    def read(self, dataset_name: str, generation_id: str) -> tuple[dict[str, Any], Path]:
        snapshot = self.export_root / f"dataset={dataset_name}" / f"generation={generation_id}"
        manifest_path = snapshot / "manifest.json"
        if not snapshot.is_dir() or not manifest_path.is_file():
            raise ContractError(f"unpublished or incomplete Qlib export: {snapshot}")
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            raise ContractError("malformed Qlib export manifest") from exc
        ModelContractLoader.validate("qlib_dataset_export", manifest)
        if manifest["generation_id"] != generation_id:
            raise ContractError("path generation does not match Qlib export manifest identity")
        checksum=manifest.get("quality_report_checksum_sha256"); report_path=self.export_root/"external_quality_reviews"/f"{checksum}.json"
        try: report=json.loads(report_path.read_text())
        except (OSError,json.JSONDecodeError) as exc: raise ContractError("Qlib export quality report unavailable") from exc
        ModelContractLoader.validate("model_quality_report",report)
        actual=sha256_json({k:v for k,v in report.items() if k!="report_checksum_sha256"})
        if checksum!=actual or report["binding_type"]!="qlib_dataset_export_v1" or report["bound_generation_id"]!=generation_id or report["status"] not in {"passed","warning"}:
            raise ContractError("Qlib export quality report rejects read")
        actual_paths: set[str] = set()
        for path in sorted(snapshot.rglob("*")):
            if path.is_file() and path.name != "manifest.json":
                actual_paths.add(path.relative_to(snapshot).as_posix())
        expected_paths = {entry["path"] for entry in manifest["files"]}
        if actual_paths != expected_paths:
            raise ContractError("Qlib export file list mismatch")
        for entry in manifest["files"]:
            path = snapshot / entry["path"]
            if file_sha256_bytes(path.read_bytes()) != entry["checksum_sha256"] or path.stat().st_size != entry["byte_size"]:
                raise ContractError(f"tampered Qlib export file: {entry['path']}")
        return manifest, snapshot


class QlibInitReceiptBuilder:
    """Build an init receipt after Qlib runtime initialization."""

    def build(
        self,
        *,
        export_manifest: dict[str, Any],
        resolved_provider_uri: str,
        qlib_import_path: str,
        qlib_version: str,
        cache_root: str,
        cache_files_before: set[str],
        cache_files_after: set[str],
        verified_export: tuple[dict[str, Any], Path],
        governance_root: Path | str,
        quality_decision: dict[str, Any],
    ) -> dict[str, Any]:
        verified_manifest, _ = verified_export
        if (
            verified_manifest["generation_id"] != export_manifest["generation_id"]
            or verified_manifest["manifest_digest_sha256"] != export_manifest["manifest_digest_sha256"]
        ):
            raise ContractError("verified export manifest does not match receipt input")
        actual_uri_digest = _sha256_text(resolved_provider_uri)
        if export_manifest.get("provider_uri_sha256") != actual_uri_digest:
            raise ContractError("resolved provider URI does not match export manifest binding")

        cache_root_path = Path(cache_root)
        if not cache_root_path.is_absolute():
            raise ContractError("cache root must be an absolute path")
        allowed_cache_root = str(cache_root_path.resolve())
        new_cache_files = {
            path for path in (cache_files_after - cache_files_before)
            if Path(path).absolute().resolve().is_relative_to(allowed_cache_root)
        }
        outside_cache = [
            path for path in (cache_files_after - cache_files_before)
            if not Path(path).absolute().resolve().is_relative_to(allowed_cache_root)
        ]
        if outside_cache:
            raise ContractError(f"governed runtime wrote cache outside approved root: {outside_cache[:3]}")

        receipt: dict[str, Any] = {
            "contract_version": 1,
            "resolved_provider_uri_sha256": actual_uri_digest,
            "export_generation_id": export_manifest["generation_id"],
            "export_manifest_digest_sha256": export_manifest["manifest_digest_sha256"],
            "file_list_checksum_sha256": _sha256_text(json.dumps([f["path"] for f in export_manifest["files"]])),
            "calendar_checksum_sha256": export_manifest["calendar_checksum_sha256"],
            "instruments_checksum_sha256": export_manifest["instruments_checksum_sha256"],
            "feature_mapping_checksum_sha256": export_manifest["feature_mapping_checksum_sha256"],
            "qlib_import_path": qlib_import_path,
            "qlib_version": qlib_version,
            "cache_root": cache_root,
            "cache_diff_checksum_sha256": _sha256_text("\n".join(sorted(new_cache_files))),
            "no_ungoverned_source_assertion": True,
            "quality_report_checksum_sha256": "0" * 64,
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation_id,_=model_manifest_identities(
            receipt,schema_name="qlib_init_receipt",exclude_fields={"quality_report_checksum_sha256"}
        )
        quality_report, report_checksum = bind_reviewed_quality_decision(
            quality_decision,binding_type="qlib_init_receipt_v1",
            subject_generation_id=generation_id,
            subject_content_sha256=sha256_json({key:value for key,value in receipt.items() if key not in {"generation_id","manifest_digest_sha256","run_id","created_at"}}),
        )
        governance_dir = Path(governance_root) / "external_quality_reviews"
        governance_dir.mkdir(parents=True, exist_ok=True)
        report_path = governance_dir / f"{report_checksum}.json"
        report_staging = report_path.with_suffix(f".staging.{uuid.uuid4().hex}")
        report_staging.write_text(json.dumps(quality_report,sort_keys=True,indent=2)+"\n")
        os.replace(report_staging,report_path)

        receipt["quality_report_checksum_sha256"]=report_checksum
        receipt["generation_id"] = generation_id
        _, digest = model_manifest_identities(receipt, schema_name="qlib_init_receipt", exclude_fields={"quality_report_checksum_sha256"})
        receipt["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("qlib_init_receipt", receipt)
        return receipt
