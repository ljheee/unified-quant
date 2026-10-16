"""Publish label sets as immutable, read-back-verified artifacts.

`LabelBuilder` computes the manifest and the frame, and `LabelStore` reads both
back through the same contract, but nothing in the tree could *write* one: the
label set was the last pre-model family with no production publisher, so the
dataset stage of a research run had no way to produce its own input.

The writer keeps the two halves honest:

* the manifest is validated against `label_set.v1.json` after the identity
  fields are stamped (a manifest that cannot carry a valid generation id is
  refused first), so a wrong field set fails locally rather than at some
  downstream read;
* the frame is written, read back from disk, and compared column-for-column,
  dtype-for-dtype and row-for-row against what the manifest declares, so a
  partition that claims `N` rows while carrying `M` can never be published.

The layout matches what `LabelStore.read_manifest` / `read_frame` expect
(`label_sets/generation=<id>/{manifest.json,data.parquet}`).
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from ..contracts.canonical_v2 import fsync_tree
from ..contracts.model_layer import (
    ModelContractLoader,
    model_manifest_identities,
    resolve_subject_quality_decision,
    reviewed_allowed_statuses,
    sha256_json,
)
from ..errors import ContractError
from .labels import LabelBuilder

_SERIALIZATION_PROFILE = "parquet-v1"


class LabelSetWriter:
    """Write an immutable label-set partition under a governed store root."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def directory_for(self, generation_id: str) -> Path:
        _validate_generation_id(generation_id, "label set")
        return self.root / "label_sets" / f"generation={generation_id}"

    def publish(
        self,
        manifest: Mapping[str, Any],
        frame: pd.DataFrame,
        *,
        quality_report: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write one label set and return its identity.

        ``manifest`` must be the output of ``LabelBuilder.build`` (no identity
        digests yet).  ``frame`` must be the same frame the builder computed over,
        because the manifest's ``data_checksum_sha256`` / ``logical_fingerprint``
        describe that frame; passing a different one is refused rather than
        reconciled.

        ``quality_report`` is optional and may be a static decision or a provider
        callable (the shape ``DatasetWriter`` accepts).  ``label_set.v1`` is
        ``additionalProperties: false`` and declares no quality-report field, so a
        decision cannot be stored *in* the manifest the way the model families do;
        it is resolved against this label set's own generation, checked for the
        ``label_set_v1`` family and reviewed status, and its status is echoed into
        the returned identity.  A missing report is not a failure: the label
        manifest's own formula and eligibility declarations are the reviewable
        content.
        """
        document = dict(manifest)
        _validate_frame(document, frame)

        # LabelBuilder already computed these over the frame it was handed, and the
        # upstream adjusted-price binding was checked against the same value, so
        # recomputing here would only risk a second, different definition of
        # "fingerprint". The frame must match what the builder described.
        if not isinstance(document.get("data_checksum_sha256"), str) or len(document["data_checksum_sha256"]) != 64:
            raise ContractError("label manifest lacks a valid data_checksum_sha256")
        if not isinstance(document.get("logical_fingerprint"), str) or len(document["logical_fingerprint"]) != 64:
            raise ContractError("label manifest lacks a valid logical_fingerprint")
        document["generation_id"] = "0" * 64
        document["manifest_digest_sha256"] = "0" * 64
        generation_id, manifest_digest = model_manifest_identities(
            document, schema_name="label_set"
        )
        document["generation_id"] = generation_id
        document["manifest_digest_sha256"] = manifest_digest
        ModelContractLoader.validate("label_set", document)

        # `label_set.v1` is `additionalProperties: false` with no quality field, so
        # a decision cannot be bound into the manifest the way the model families
        # do. It is still resolved against *this* subject: a caller cannot pre-sign
        # a report for a generation id it does not know yet, so a provider callable
        # is resolved with the derived id (the shape DatasetWriter accepts), and a
        # static report must already name that subject -- otherwise it is a review
        # of something else.
        if quality_report is not None:
            report = resolve_subject_quality_decision(
                quality_report, subject_generation_id=generation_id,
                subject_content_sha256=manifest_digest,
            )
            ModelContractLoader.validate("model_quality_report.v1", dict(report))
            if report["binding_type"] != "label_set_v1":
                raise ContractError(
                    f"label set quality report binding_type {report['binding_type']} "
                    "is not label_set_v1"
                )
            if report["bound_generation_id"] != generation_id:
                raise ContractError(
                    "label set quality report is bound to a different subject: "
                    f"{report['bound_generation_id']} != {generation_id}"
                )
            if report["status"] not in reviewed_allowed_statuses("label_set_v1"):
                raise ContractError(
                    f"label set quality report status {report['status']} is not "
                    f"reviewed-accepted for label_set_v1"
                )
        else:
            report = None

        directory = self.directory_for(generation_id)
        if directory.exists():
            raise ContractError(f"immutable label set already published: {directory}")
        staging = directory.with_name(f"{directory.name}.staging.{uuid.uuid4().hex}")
        staging.mkdir(parents=True)
        try:
            data_path = staging / "data.parquet"
            frame.to_parquet(data_path, index=False)
            restored = pd.read_parquet(data_path)
            _reconcile(document, frame, restored)
            (staging / "manifest.json").write_text(
                json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            fsync_tree(staging)
            os.replace(staging, directory)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return {
            "generation_id": generation_id,
            "manifest_digest_sha256": manifest_digest,
            "partition": str(directory),
            "rows": int(len(frame)),
            "columns": list(frame.columns),
            "quality_report_status": None if report is None else report["status"],
        }


def _validate_frame(manifest: Mapping[str, Any], frame: pd.DataFrame) -> None:
    if frame.empty:
        raise ContractError("cannot publish an empty label set")
    required = {"instrument", "decision_date", "label"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ContractError(f"label frame is missing columns: {missing}")
    if bool(frame.duplicated(["instrument", "decision_date"]).any()):
        raise ContractError("label set has duplicate instrument/decision_date keys")
    declared_columns = list(manifest.get("columns") or [])
    if declared_columns and list(frame.columns) != declared_columns:
        raise ContractError(
            f"label frame columns {list(frame.columns)} do not match the manifest's {declared_columns}"
        )
    declared_rows = manifest.get("row_count")
    if declared_rows is not None and int(declared_rows) != len(frame):
        raise ContractError(
            f"label frame carries {len(frame)} rows but the manifest declares {declared_rows}"
        )
    # The manifest's checksum is the frame's identity, so a same-shaped frame with
    # different values is a different artifact. Publishing it under this manifest
    # would produce a store whose declared checksum does not describe its rows.
    declared_checksum = manifest.get("data_checksum_sha256")
    if isinstance(declared_checksum, str) and len(declared_checksum) == 64:
        ordered = frame.sort_values(["instrument", "decision_date"], kind="mergesort")
        actual = sha256_json({"rows": [
            [str(row[0]), pd.Timestamp(row[1]).isoformat(), None if pd.isna(row[2]) else float(row[2])]
            for row in ordered.itertuples(index=False)
        ]})
        if actual != declared_checksum:
            raise ContractError(
                "label frame does not match the manifest's data_checksum_sha256; "
                "publish the frame the builder computed over"
            )


def _reconcile(manifest: Mapping[str, Any], original: pd.DataFrame, restored: pd.DataFrame) -> None:
    if list(restored.columns) != list(original.columns):
        raise ContractError("label read-back column mismatch")
    if len(restored) != len(original):
        raise ContractError("label read-back row count mismatch")
    # Compare against the manifest's declared dtypes with the same normalization
    # the governed reader applies (`LabelStore.read_frame` maps any `datetime*`
    # dtype to `datetime64[ns]`). Comparing the restored frame to the in-memory
    # frame instead would let the writer publish a partition the reader refuses,
    # because the parquet round-trip and the manifest need not agree on dtypes.
    actual_dtypes = {key: str(value) for key, value in restored.dtypes.items()}
    expected_dtypes = {
        key: ("datetime64[ns]" if str(value).startswith("datetime") else str(value))
        for key, value in manifest["dtypes"].items()
    }
    if actual_dtypes != expected_dtypes:
        raise ContractError(
            f"label read-back dtype mismatch against the manifest: "
            f"restored {actual_dtypes}, manifest {expected_dtypes}"
        )
    pd.testing.assert_frame_equal(restored, original, check_exact=False, rtol=1e-12, atol=1e-12)


def _validate_generation_id(value: str, family: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ContractError(f"invalid {family} generation id")
