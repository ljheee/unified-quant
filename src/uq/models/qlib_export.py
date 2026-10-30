from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np
import pandas as pd

from ..contracts.canonical_v2 import file_sha256_bytes, fsync_dir, fsync_tree
from ..contracts.model_layer import ModelContractLoader, bind_reviewed_quality_decision, model_manifest_identities, resolve_subject_quality_decision, reviewed_allowed_statuses, sha256_json
from ..errors import ContractError


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@contextmanager
def _timed_export_step(
    name: str, callback: Callable[[str, float, str], None] | None,
) -> Iterator[None]:
    started = time.perf_counter()
    status = "passed"
    try:
        yield
    except BaseException:
        status = "failed"
        raise
    finally:
        if callback is not None:
            callback(f"qlib_export/{name}", time.perf_counter() - started, status)


def _write_qlib_feature_files(
    frame: pd.DataFrame,
    *,
    mapping: dict[str, str],
    calendar_dates: list[str],
    feature_dir: Path,
) -> None:
    """Write Qlib ``.bin`` files once per instrument/field.

    The previous implementation iterated rows and repeatedly read, expanded,
    and rewrote each file. That is O(rows x fields) file I/O even though the
    final layout is only one array per instrument/field. Group by instrument,
    align each field to the governed calendar, validate, then write exactly
    once.
    """
    if frame.empty:
        raise ContractError("cannot export empty dataset")
    required = ["instrument", "datetime", *mapping]
    missing_columns = [name for name in required if name not in frame.columns]
    if missing_columns:
        raise ContractError(
            f"Qlib export frame is missing columns: {missing_columns}"
        )

    calendar = pd.to_datetime(calendar_dates)
    calendar_index = {
        date.strftime("%Y-%m-%d"): index
        for index, date in enumerate(calendar)
    }
    working = frame[required].copy()
    working["instrument"] = working["instrument"].astype(str).str.lower()
    working["datetime"] = pd.to_datetime(working["datetime"])
    date_keys = working["datetime"].dt.strftime("%Y-%m-%d")
    unknown_dates = sorted(set(date_keys) - set(calendar_index))
    if unknown_dates:
        raise ContractError(
            f"export row date is missing from calendar: {unknown_dates[0]}"
        )
    working["_calendar_index"] = date_keys.map(calendar_index).astype(int)
    if working.duplicated(["instrument", "_calendar_index"]).any():
        raise ContractError("Qlib provider export has duplicate date keys")

    for source_column, target_column in mapping.items():
        values = working[source_column]
        if values.isna().any():
            raise ContractError("Qlib provider export cannot contain missing values")
        if not pd.api.types.is_numeric_dtype(values):
            raise ContractError(
                f"Qlib provider export requires finite numeric values: {target_column}"
            )
        numeric = values.to_numpy(dtype=np.float64, copy=False)
        if not np.isfinite(numeric).all():
            raise ContractError(
                f"Qlib provider export requires finite numeric values: {target_column}"
            )

    for instrument, group in working.groupby("instrument", sort=True):
        instrument_dir = feature_dir / instrument
        instrument_dir.mkdir(parents=True, exist_ok=True)
        positions = group["_calendar_index"].to_numpy(dtype=np.int64)
        start = int(positions.min())
        end = int(positions.max())
        length = end - start + 1
        offsets = positions - start
        for source_column, target_column in mapping.items():
            values = group[source_column].to_numpy(dtype=np.float32, copy=False)
            array = np.full(length, np.nan, dtype=np.float32)
            array[offsets] = values
            artifact = np.empty(length + 1, dtype="<f")
            artifact[0] = start
            artifact[1:] = array
            artifact.tofile(instrument_dir / f"{target_column.lower()}.day.bin")


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
        label_column: str,
        label_mapping: str,
        calendar_dates: list[str],
        instruments: list[str],
        provider_uri: str,
        quality_decision: dict[str, Any],
        timing_callback: Callable[[str, float, str], None] | None = None,
    ) -> dict[str, Any]:
        if frame.empty:
            raise ContractError("cannot export empty dataset")
        if not feature_mapping:
            raise ContractError("feature mapping cannot be empty")
        if not isinstance(label_column, str) or not label_column:
            raise ContractError("label column cannot be empty")
        if not isinstance(label_mapping, str) or not label_mapping:
            raise ContractError("label mapping cannot be empty")
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

                # 3. Feature and label mapping file
                complete_mapping = {**feature_mapping, label_column: label_mapping}
                mapping_content = json.dumps(complete_mapping, sort_keys=True, indent=2) + "\n"
                mapping_path = staging / "feature_mapping.json"
                mapping_path.write_text(mapping_content)
                mapping_checksum = file_sha256_bytes(mapping_path.read_bytes())

                # 4. Data parquet with Qlib-compatible columns
                feature_dir = staging / "features"
                with _timed_export_step("feature_files", timing_callback):
                    _write_qlib_feature_files(
                        frame,
                        mapping=complete_mapping,
                        calendar_dates=calendar_dates,
                        feature_dir=feature_dir,
                    )

                manifest: dict[str, Any] = {
                    "contract_version": 2,
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
                    "generation_id": "0" * 64,
                    "manifest_digest_sha256": "0" * 64,
                }
                # Check every file and build the list in one directory walk.
                # A full-A export can contain hundreds of thousands of bins.
                with _timed_export_step("file_inventory", timing_callback):
                    complete_files = []
                    for path in sorted(staging.rglob("*")):
                        if path.is_file() and path.name != "manifest.json":
                            size = path.stat().st_size
                            if size == 0:
                                raise ContractError(f"zero-byte export file: {path.name}")
                            rel = path.relative_to(staging).as_posix()
                            complete_files.append({
                                "path": rel,
                                "checksum_sha256": "0" * 64,
                                "byte_size": size,
                            })
                    for entry in complete_files:
                        entry["checksum_sha256"] = file_sha256_bytes((staging / entry["path"]).read_bytes())
                manifest["files"] = complete_files
                with _timed_export_step("review_binding", timing_callback):
                    export_generation_id,_=model_manifest_identities(
                        manifest,
                        schema_name="qlib_dataset_export",
                        exclude_fields={"export_layout","quality_report_checksum_sha256","provider_uri_sha256"},
                        digest_exclude_fields={"export_layout","quality_report_checksum_sha256","provider_uri_sha256"},
                    )
                    manifest["export_layout"]["root"] = f"dataset={dataset_name}/generation={export_generation_id}"
                    export_subject_content = sha256_json({
                        key: value for key, value in manifest.items()
                        if key not in {"export_layout", "quality_report_checksum_sha256", "provider_uri_sha256", "generation_id", "manifest_digest_sha256"}
                    })
                    quality_decision = resolve_subject_quality_decision(
                        quality_decision, subject_generation_id=export_generation_id, subject_content_sha256=export_subject_content
                    )
                    quality_report, report_checksum = bind_reviewed_quality_decision(
                        quality_decision, binding_type="qlib_dataset_export_v1",
                        subject_generation_id=export_generation_id,
                        subject_content_sha256=export_subject_content,
                    )
                    manifest["quality_report_checksum_sha256"]=report_checksum
                    manifest["generation_id"]=export_generation_id
                    _, digest = model_manifest_identities(
                        manifest,
                        schema_name="qlib_dataset_export",
                        exclude_fields={"export_layout", "quality_report_checksum_sha256", "provider_uri_sha256"},
                        digest_exclude_fields={"export_layout", "quality_report_checksum_sha256", "provider_uri_sha256"},
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

                with _timed_export_step("prepublish_readback", timing_callback):
                    manifest_path = staging / "manifest.json"
                    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
                    readback_manifest = json.loads(manifest_path.read_text())
                    if readback_manifest["generation_id"] != manifest["generation_id"]:
                        raise ContractError("export manifest readback identity mismatch")
                    for entry in manifest["files"]:
                        fp = staging / entry["path"]
                        if not fp.is_file() or file_sha256_bytes(fp.read_bytes()) != entry["checksum_sha256"]:
                            raise ContractError(f"export file verification failed: {entry['path']}")
                with _timed_export_step("durable_publish", timing_callback):
                    fsync_tree(staging)
                    os.replace(staging, snapshot)
                    fsync_dir(snapshot.parent)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return manifest

    def read(
        self,
        dataset_name: str,
        generation_id: str,
        *,
        provider_uri: str | None = None,
    ) -> tuple[dict[str, Any], Path]:
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
        # The manifest records the provider URI it was exported for
        # (`provider_uri_sha256`), and that field is deliberately *outside* the
        # identity: the URI describes how the artifact will be read, not what it
        # contains, so two runs with different URIs legitimately produce
        # byte-identical exports sharing one generation id. That makes it invisible
        # to any content comparison, and reusing an artifact across URIs would mint
        # a receipt attesting one URI with evidence produced under another. The URI
        # cannot be checked at write time -- there is no "correct" value then, it is
        # simply where this run wrote -- so the check belongs here, against the
        # value the caller is asking with. Optional because the existing callers
        # read back an artifact they have just written themselves.
        if provider_uri is not None and manifest.get("provider_uri_sha256") != _sha256_text(provider_uri):
            raise ContractError(
                "Qlib export was published for a different provider URI; refusing to reuse it"
            )
        checksum=manifest.get("quality_report_checksum_sha256"); report_path=self.export_root/"external_quality_reviews"/f"{checksum}.json"
        try: report=json.loads(report_path.read_text())
        except (OSError,json.JSONDecodeError) as exc: raise ContractError("Qlib export quality report unavailable") from exc
        ModelContractLoader.validate("model_quality_report",report)
        actual=sha256_json({k:v for k,v in report.items() if k!="report_checksum_sha256"})
        if checksum!=actual or report["binding_type"]!="qlib_dataset_export_v1" or report["bound_generation_id"]!=generation_id or report["status"] not in reviewed_allowed_statuses("qlib_dataset_export_v1"):
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
            "contract_version": 2,
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
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation_id,_=model_manifest_identities(
            receipt,schema_name="qlib_init_receipt",
            exclude_fields={"quality_report_checksum_sha256","resolved_provider_uri_sha256","cache_root"},
            digest_exclude_fields={"export_manifest_digest_sha256","resolved_provider_uri_sha256","cache_root"},
        )
        receipt_subject_content = sha256_json({
            key: value for key, value in receipt.items()
            if key not in {"quality_report_checksum_sha256", "export_manifest_digest_sha256", "resolved_provider_uri_sha256", "cache_root", "generation_id", "manifest_digest_sha256"}
        })
        quality_decision = resolve_subject_quality_decision(
            quality_decision, subject_generation_id=generation_id, subject_content_sha256=receipt_subject_content
        )
        quality_report, report_checksum = bind_reviewed_quality_decision(
            quality_decision,binding_type="qlib_init_receipt_v1",
            subject_generation_id=generation_id,
            subject_content_sha256=receipt_subject_content,
        )
        receipt["quality_report_checksum_sha256"] = report_checksum
        receipt["generation_id"] = generation_id
        _, digest = model_manifest_identities(
            receipt,
            schema_name="qlib_init_receipt",
            exclude_fields={"quality_report_checksum_sha256","resolved_provider_uri_sha256","cache_root"},
            digest_exclude_fields={"export_manifest_digest_sha256","resolved_provider_uri_sha256","cache_root"},
        )
        receipt["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("qlib_init_receipt", receipt)
        # Persist only once the identity fields are final. Writing the receipt
        # earlier left a document on disk whose `generation_id` was still the
        # `"0"*64` placeholder, so anything that read it back -- the resumed path in
        # particular, which has no in-memory receipt to fall back on -- failed the
        # stable-generation check (cr.md 5.4: qlib_export.py:336).
        governance_dir = Path(governance_root) / "research_exports"
        governance_dir.mkdir(parents=True, exist_ok=True)
        receipt_staging = governance_dir / f"{generation_id}.staging.{uuid.uuid4().hex}.json"
        receipt_staging.write_text(json.dumps(receipt,sort_keys=True,indent=2)+"\n")
        receipt_path = governance_dir / f"{generation_id}.json"
        os.replace(receipt_staging, receipt_path)
        return receipt
