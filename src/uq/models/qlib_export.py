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


def _write_export_file(staging: Path, path: Path, content: bytes) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "path": path.relative_to(staging).as_posix(),
        "checksum_sha256": hashlib.sha256(content).hexdigest(),
        "byte_size": len(content),
    }


def _iter_files_sorted(root: Path) -> Iterator[Path]:
    """Yield files in Path sort order without retaining the whole tree in memory."""
    with os.scandir(root) as entries:
        children = sorted(entries, key=lambda entry: entry.name)
    for child in children:
        path = Path(child.path)
        if child.is_dir(follow_symlinks=False):
            yield from _iter_files_sorted(path)
        elif child.is_file(follow_symlinks=False) or (child.is_symlink() and child.is_file()):
            yield path


def _verify_export_files(snapshot: Path, entries: list[dict[str, Any]], *, verify_content: bool) -> None:
    """Check exact inventory and file contents without retaining the filesystem tree."""
    expected = sorted(entries, key=lambda entry: Path(entry["path"]).parts)
    index = 0
    first_tampered: str | None = None
    for path in _iter_files_sorted(snapshot):
        if path.name == "manifest.json":
            continue
        relative = path.relative_to(snapshot).as_posix()
        if index >= len(expected) or expected[index]["path"] != relative:
            raise ContractError("Qlib export file list mismatch")
        start = index
        while index < len(expected) and expected[index]["path"] == relative:
            index += 1
        if first_tampered is not None:
            continue
        try:
            size = path.stat().st_size
            if any(entry["byte_size"] != size for entry in expected[start:index]):
                first_tampered = relative
                continue
            digest = file_sha256_bytes(path.read_bytes()) if verify_content else None
        except OSError:
            first_tampered = relative
            continue
        if any(verify_content and entry["checksum_sha256"] != digest
               for entry in expected[start:index]):
            first_tampered = relative
    if index != len(expected):
        raise ContractError("Qlib export file list mismatch")
    if first_tampered is not None:
        raise ContractError(f"tampered Qlib export file: {first_tampered}")


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
    file_entries: list[dict[str, Any]],
    write_files: bool = True,
) -> None:
    """Write Qlib ``.bin`` files once per instrument/field.

    The previous implementation iterated rows and repeatedly read, expanded,
    and rewrote each file. That is O(rows x fields) file I/O even though the
    final layout is only one array per instrument/field. Group by instrument,
    align each field to the governed calendar, validate, then write exactly
    once. Append each file's checksum and size to ``file_entries`` from the
    same in-memory bytes that are written.
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
    # The common governed input already has exactly these columns.  Its
    # feature blocks are read-only here; assigning normalized keys below only
    # changes the shallow working frame.
    working = frame.copy(deep=False) if list(frame.columns) == required else frame[required].copy()
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

    staging = feature_dir.parent
    for instrument, group in working.groupby("instrument", sort=True):
        instrument_dir = feature_dir / instrument
        if write_files:
            instrument_dir.mkdir(parents=True, exist_ok=True)
        positions = group["_calendar_index"].to_numpy(dtype=np.int64)
        start = int(positions.min())
        end = int(positions.max())
        length = end - start + 1
        offsets = positions - start
        for source_column, target_column in sorted(
            mapping.items(), key=lambda item: f"{item[1].lower()}.day.bin",
        ):
            values = group[source_column].to_numpy(dtype=np.float32, copy=False)
            array = np.full(length, np.nan, dtype=np.float32)
            array[offsets] = values
            artifact = np.empty(length + 1, dtype="<f")
            artifact[0] = start
            artifact[1:] = array
            path = instrument_dir / f"{target_column.lower()}.day.bin"
            checksum = hashlib.sha256(memoryview(artifact).cast("B")).hexdigest()
            if write_files:
                artifact.tofile(path)
            file_entries.append({
                "path": path.relative_to(staging).as_posix(),
                "checksum_sha256": checksum,
                "byte_size": artifact.nbytes,
            })


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

    def find_reusable_export(
        self, *, dataset_name: str, frame: pd.DataFrame,
        feature_mapping: dict[str, str], label_column: str, label_mapping: str,
        calendar_dates: list[str], instruments: list[str], provider_uri: str,
        quality_decision: Any,
    ) -> tuple[dict[str, Any], Path] | None:
        """Derive the exact export identity without writing bins, then strictly read it.

        An existing directory is only a hint. The identity is recomputed from
        the current input and every existing file is verified before reuse.
        """
        dataset_dir = self.export_root / f"dataset={dataset_name}"
        if not dataset_dir.is_dir() or not any(dataset_dir.glob("generation=*")):
            return None
        if frame.empty or not feature_mapping or not label_column or not label_mapping:
            raise ContractError("invalid Qlib export input")
        if not calendar_dates or not instruments:
            raise ContractError("Qlib export requires calendar dates and instruments")
        calendar_bytes = ("\n".join(calendar_dates) + "\n").encode("utf-8")
        instruments_bytes = ("\n".join(
            f"{inst}\t0\t{calendar_dates[-1]}" for inst in instruments
        ) + "\n").encode("utf-8")
        mapping = {**feature_mapping, label_column: label_mapping}
        mapping_bytes = (json.dumps(mapping, sort_keys=True, indent=2) + "\n").encode("utf-8")

        def entry(path: str, content: bytes) -> dict[str, Any]:
            return {"path": path, "checksum_sha256": hashlib.sha256(content).hexdigest(),
                    "byte_size": len(content)}

        entries = [entry("calendars/day.txt", calendar_bytes),
                   entry("feature_mapping.json", mapping_bytes)]
        _write_qlib_feature_files(
            frame, mapping=mapping, calendar_dates=calendar_dates,
            feature_dir=self.export_root / ".identity-only" / "features",
            file_entries=entries, write_files=False,
        )
        entries.append(entry("instruments/all.txt", instruments_bytes))
        manifest: dict[str, Any] = {
            "contract_version": 2,
            "export_layout": {"root": ""},
            "files": entries,
            "provider_uri_sha256": _sha256_text(provider_uri),
            "calendar_checksum_sha256": hashlib.sha256(calendar_bytes).hexdigest(),
            "instruments_checksum_sha256": hashlib.sha256(instruments_bytes).hexdigest(),
            "feature_mapping_checksum_sha256": hashlib.sha256(mapping_bytes).hexdigest(),
            "exporter_fingerprint": self.exporter_fingerprint,
            "serialization_profile_id": "parquet-v1",
            "quality_report_checksum_sha256": "0" * 64,
            "empty_cache_precondition": True,
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        exclusions = {"export_layout", "quality_report_checksum_sha256", "provider_uri_sha256"}
        generation_id, _ = model_manifest_identities(
            manifest, schema_name="qlib_dataset_export", exclude_fields=exclusions,
            digest_exclude_fields=exclusions,
        )
        if not (dataset_dir / f"generation={generation_id}").exists():
            return None
        subject = sha256_json({
            key: value for key, value in manifest.items()
            if key not in exclusions | {"generation_id", "manifest_digest_sha256"}
        })
        decision = resolve_subject_quality_decision(
            quality_decision, subject_generation_id=generation_id, subject_content_sha256=subject,
        )
        bind_reviewed_quality_decision(
            decision, binding_type="qlib_dataset_export_v1", subject_generation_id=generation_id,
            subject_content_sha256=subject,
        )
        return self.read(dataset_name, generation_id, provider_uri=provider_uri)

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

                file_entries: list[dict[str, Any]] = []

                # 1. Calendar file
                calendar_content = "\n".join(calendar_dates) + "\n"
                calendar_bytes = calendar_content.encode("utf-8")
                calendar_entry = _write_export_file(
                    staging, staging / "calendars" / "day.txt", calendar_bytes,
                )
                calendar_checksum = hashlib.sha256(calendar_bytes).hexdigest()

                # 2. Instruments file
                instruments_content = "\n".join(
                    f"{inst}\t0\t{calendar_dates[-1]}" for inst in instruments
                ) + "\n"
                inst_dir = staging / "instruments"
                inst_dir.mkdir(parents=True, exist_ok=True)
                instruments_bytes = instruments_content.encode("utf-8")
                instruments_entry = _write_export_file(
                    staging, inst_dir / "all.txt", instruments_bytes,
                )
                instruments_checksum = hashlib.sha256(instruments_bytes).hexdigest()

                # 3. Feature and label mapping file
                complete_mapping = {**feature_mapping, label_column: label_mapping}
                mapping_content = json.dumps(complete_mapping, sort_keys=True, indent=2) + "\n"
                mapping_path = staging / "feature_mapping.json"
                mapping_bytes = mapping_content.encode("utf-8")
                mapping_entry = _write_export_file(staging, mapping_path, mapping_bytes)
                mapping_checksum = hashlib.sha256(mapping_bytes).hexdigest()

                # 4. Data parquet with Qlib-compatible columns
                feature_dir = staging / "features"
                file_entries.extend((calendar_entry, mapping_entry))
                with _timed_export_step("feature_files", timing_callback):
                    _write_qlib_feature_files(
                        frame,
                        mapping=complete_mapping,
                        calendar_dates=calendar_dates,
                        feature_dir=feature_dir,
                        file_entries=file_entries,
                    )
                file_entries.append(instruments_entry)

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
                # Reconcile the write-time inventory against the staging tree.
                # Feature checksums were computed from the exact buffers written;
                # avoid reopening every small bin for a redundant first hash pass.
                with _timed_export_step("file_inventory", timing_callback):
                    expected_index = 0
                    for path in _iter_files_sorted(staging):
                        if path.name == "manifest.json":
                            continue
                        size = path.stat().st_size
                        if size == 0:
                            raise ContractError(f"zero-byte export file: {path.name}")
                        rel = path.relative_to(staging).as_posix()
                        if (expected_index >= len(file_entries)
                                or file_entries[expected_index]["path"] != rel):
                            raise ContractError(f"Qlib export file inventory mismatch: {rel}")
                        if file_entries[expected_index]["byte_size"] != size:
                            raise ContractError(f"Qlib export file size mismatch: {rel}")
                        expected_index += 1
                    if expected_index != len(file_entries):
                        raise ContractError("Qlib export file inventory is incomplete")
                manifest["files"] = file_entries
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
        verify_content: bool = True,
    ) -> tuple[dict[str, Any], Path]:
        """Read an export, checking its manifest, report, file set and file sizes.

        ``verify_content`` defaults to the full SHA-256 check. The Research Chain
        may set it to false only when the immediate next consumer is
        ``QlibRuntimeTrainer``, which independently hashes every manifest file
        before loading it. This avoids two adjacent full-content scans while
        retaining the structural and review gates here.
        """
        if not isinstance(verify_content, bool):
            raise ContractError("Qlib export verify_content must be a boolean")
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
        _verify_export_files(snapshot, manifest["files"], verify_content=verify_content)
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
