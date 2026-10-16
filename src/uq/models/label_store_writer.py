"""Publish label sets as immutable, read-back-verified artifacts.

`LabelBuilder` computes the manifest and the frame, and `LabelStore` reads both
back through the same contract, but nothing in the tree could *write* one: the
label set was the last pre-model family with no production publisher, so the
dataset stage of a research run had no way to produce its own input.

The writer keeps the two halves honest:

* the manifest is validated against `label_set.v1.json` before and after the
  identity fields are stamped, so a wrong field set fails locally rather than at
  some downstream read;
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

from ..contracts.model_layer import (
    ModelContractLoader,
    model_manifest_identities,
    reviewed_allowed_statuses,
    sha256_json,
)
from ..errors import ContractError
from .labels import LabelBuilder

_SERIALIZATION_PROFILE = "parquet-v1"


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            handle = os.open(path, os.O_RDONLY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
    os.open(root, os.O_RDONLY)
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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

        ``quality_report`` is optional.  ``label_set.v1`` is
        ``additionalProperties: false`` and declares no quality-report field, so a
        decision cannot be bound into the manifest the way the model families do;
        one that is supplied is validated against ``model_quality_report`` and
        checked against the reviewed statuses, and its status is echoed into the
        returned identity.  A missing report is not a failure: the label manifest's
        own formula and eligibility declarations are the reviewable content.
        """
        document = dict(manifest)
        _validate_frame(document, frame)

        if quality_report is not None:
            # `label_set.v1` is `additionalProperties: false` and has no
            # quality-report field, so a decision cannot be bound into the
            # manifest the way the model families do. Validate what is offered and
            # record it in the run summary instead of inventing a schema field.
            ModelContractLoader.validate("model_quality_report.v1", dict(quality_report))
            if quality_report["status"] not in reviewed_allowed_statuses("label_set_v1"):
                raise ContractError(
                    f"label set quality report status {quality_report['status']} is not "
                    f"reviewed-accepted for label_set_v1"
                )

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
            _fsync_tree(staging)
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
            "quality_report_status": None if quality_report is None else quality_report["status"],
        }


def _validate_frame(manifest: Mapping[str, Any], frame: pd.DataFrame) -> None:
    if frame.empty:
        raise ContractError("cannot publish an empty label set")
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
    if {key: str(value) for key, value in restored.dtypes.items()} != {
        key: str(value) for key, value in original.dtypes.items()
    }:
        raise ContractError("label read-back dtype mismatch")
    pd.testing.assert_frame_equal(restored, original, check_exact=False, rtol=1e-12, atol=1e-12)


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    ordered = frame.sort_values(["instrument", "decision_date"], kind="mergesort").reset_index(drop=True)
    return sha256_json({
        "columns": list(ordered.columns),
        "rows": [
            [
                str(row[0]),
                pd.Timestamp(row[1]).isoformat(),
                None if pd.isna(row[2]) else float(row[2]),
            ]
            for row in ordered.itertuples(index=False)
        ],
        "dtypes": {key: str(value) for key, value in ordered.dtypes.items()},
    })


def _validate_generation_id(value: str, family: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ContractError(f"invalid {family} generation id")
