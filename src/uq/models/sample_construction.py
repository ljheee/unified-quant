"""Training-sample construction: select rows before feature preprocessing.

Preprocessing is deliberately label-agnostic. A label that is null or absent
cannot be observed until the factor frame is joined to its label set, so the
selection belongs here, in sample construction, not in `feature_preprocessing`.
The output is the exact training frame: keys, ordered features and label.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from ..contracts.canonical_v2 import file_sha256_bytes
from ..contracts.model_layer import ModelContractLoader, model_manifest_identities, sha256_json
from ..errors import ContractError
from ..factors.raw_price import logical_fingerprint
from .feature_preprocessing import complete_rows


_POLICIES = {
    "fail_closed": {
        "type": "pit_label_join_v1",
        "feature_null": "refuse",
        "label_null": "refuse",
        "absent_label_key": "refuse",
    },
    "drop_incomplete_rows": {
        "type": "pit_label_join_v1",
        "feature_null": "drop",
        "label_null": "refuse",
        "absent_label_key": "refuse",
    },
    "drop_unlabeled_rows": {
        "type": "pit_label_join_v1",
        "feature_null": "drop",
        "label_null": "drop",
        "absent_label_key": "drop",
    },
}


def selection_policy(missing_policy: str) -> dict[str, str]:
    try:
        return dict(_POLICIES[missing_policy])
    except KeyError as exc:
        raise ContractError(f"unsupported sample-construction missing policy: {missing_policy}") from exc


def _label_frame_with_keys(label_frame: pd.DataFrame) -> pd.DataFrame:
    if list(label_frame.columns) != ["instrument", "decision_date", "label"]:
        raise ContractError("label artifact columns do not match the dataset contract")
    labels = label_frame[["instrument", "decision_date", "label"]].copy()
    labels["instrument"] = labels["instrument"].astype(str)
    if labels.duplicated(["instrument", "decision_date"]).any():
        raise ContractError("duplicate label keys prevent sample construction")
    return labels


def select_training_sample(
    factor_frame: pd.DataFrame,
    label_frame: pd.DataFrame,
    *,
    ordered_features: list[str],
    missing_policy: str,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    """Select the final training rows and audit every removal.

    `drop_incomplete_rows` removes feature-null rows but still refuses a null
    label. `drop_unlabeled_rows` removes both feature-null and label-null/absent
    rows. `fail_closed` refuses any surviving null. The result is ordered by
    `(instrument, datetime)` and carries exactly keys + features + label.
    """
    policy = selection_policy(missing_policy)
    required = ["instrument", "datetime", *ordered_features]
    if list(factor_frame.columns) != required:
        raise ContractError("factor frame columns do not match sample-construction input")
    if factor_frame.duplicated(["instrument", "datetime"]).any():
        raise ContractError("duplicate factor keys prevent sample construction")
    before = len(factor_frame)
    dropped_rows: dict[str, Any] = {}
    dropped_labels: dict[str, Any] = {}
    frame = factor_frame.copy()

    incomplete = frame[ordered_features].isna().any(axis=1)
    if policy["feature_null"] == "drop" and incomplete.any():
        kept = complete_rows(frame, ordered_features)
        dropped_rows = {
            "rows": int(before - len(kept)),
            "instruments_fully_dropped": int(
                len(set(frame["instrument"]) - set(kept["instrument"]))
            ),
            "per_feature": {
                name: int(count)
                for name, count in frame[ordered_features].isna().sum().items()
                if count
            },
        }
        frame = kept
    elif policy["feature_null"] == "refuse" and incomplete.any():
        counts = {
            name: int(count)
            for name, count in frame[ordered_features].isna().sum().items()
            if count
        }
        raise ContractError(f"fail_closed sample construction contains null features: {counts}")

    labels = _label_frame_with_keys(label_frame)
    factor_keys = frame[["instrument", "datetime"]].copy()
    factor_keys["instrument"] = factor_keys["instrument"].astype(str)
    factor_keys["decision_date"] = pd.to_datetime(factor_keys["datetime"]).dt.tz_localize(None)
    labels["decision_date"] = pd.to_datetime(labels["decision_date"]).dt.tz_localize(None)
    joined = factor_keys.merge(
        labels,
        on=["instrument", "decision_date"],
        how="left",
        validate="one_to_one",
        indicator=True,
        suffixes=("", "__label"),
    )
    null_label = joined["_merge"].eq("both") & joined["label"].isna()
    absent_key = joined["_merge"].eq("left_only")
    removed = null_label | absent_key
    if removed.any():
        dropped_labels = {
            "rows": int(removed.sum()),
            "instruments": int(frame.loc[removed.to_numpy(), "instrument"].nunique()),
            "null_labels": int(null_label.sum()),
            "absent_keys": int(absent_key.sum()),
        }
        if policy["label_null"] == "refuse" or policy["absent_label_key"] == "refuse":
            raise ContractError(
                "sample construction refuses null or absent labels under "
                f"{missing_policy}: {dropped_labels}"
            )
        frame = frame.loc[~removed.to_numpy()].reset_index(drop=True)
        joined = joined.loc[~removed].reset_index(drop=True)
    if frame.empty:
        raise ContractError("sample construction left no training rows")

    output = frame[["instrument", "datetime", *ordered_features]].copy()
    output["label"] = joined["label"].astype("float64").to_numpy()
    output = output.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True)
    if output["label"].isna().any():
        raise ContractError("sample construction output still contains null labels")
    if output[ordered_features].isna().any().any():
        raise ContractError("sample construction output still contains null features")
    return output, dropped_rows, dropped_labels


class SampleConstructionBuilder:
    """Build the immutable manifest for a selected training frame."""

    def __init__(self, *, sample_name: str, semantic_version: str, code_fingerprint: str | None = None) -> None:
        self.sample_name = sample_name
        self.semantic_version = semantic_version
        self.code_fingerprint = code_fingerprint or sha256_json({
            "component": "SampleConstructionBuilder",
            "version": 1,
        })

    def build(
        self,
        frame: pd.DataFrame,
        *,
        window_start: str,
        window_end: str,
        source_factor_set: str,
        source_factor_version: str,
        source_factor_generation_ids: list[str],
        source_factor_partition_set_digest_sha256: str,
        source_label_set_name: str,
        source_label_generation_id: str,
        source_label_manifest_digest_sha256: str,
        source_label_data_checksum_sha256: str,
        source_universe_snapshot_generation_id: str,
        source_index_membership_set_digest_sha256: str,
        ordered_features: list[str],
        missing_policy: str,
        rows_before_selection: int,
        dropped_rows: dict[str, Any],
        dropped_labels: dict[str, Any],
    ) -> dict[str, Any]:
        expected = ["instrument", "datetime", *ordered_features, "label"]
        if list(frame.columns) != expected:
            raise ContractError("sample-construction frame columns do not match its declared order")
        if frame.empty:
            raise ContractError("sample construction cannot publish an empty frame")
        if frame.duplicated(["instrument", "datetime"]).any():
            raise ContractError("sample-construction frame has duplicate keys")
        if not ordered_features or len(ordered_features) != len(set(ordered_features)):
            raise ContractError("sample-construction ordered_features must be non-empty and unique")
        policy = selection_policy(missing_policy)
        manifest: dict[str, Any] = {
            "contract_version": 1,
            "sample_name": self.sample_name,
            "semantic_version": self.semantic_version,
            "window_start": window_start,
            "window_end": window_end,
            "source_factor_set": source_factor_set,
            "source_factor_version": source_factor_version,
            "source_factor_generation_ids": list(source_factor_generation_ids),
            "source_factor_partition_set_digest_sha256": source_factor_partition_set_digest_sha256,
            "source_label_set_name": source_label_set_name,
            "source_label_generation_id": source_label_generation_id,
            "source_label_manifest_digest_sha256": source_label_manifest_digest_sha256,
            "source_label_data_checksum_sha256": source_label_data_checksum_sha256,
            "source_universe_snapshot_generation_id": source_universe_snapshot_generation_id,
            "source_index_membership_set_digest_sha256": source_index_membership_set_digest_sha256,
            "ordered_features": list(ordered_features),
            "selection_policy": policy,
            "rows_before_selection": int(rows_before_selection),
            "rows_after_selection": int(len(frame)),
            "dropped_rows": dict(dropped_rows),
            "dropped_labels": dict(dropped_labels),
            "columns": list(frame.columns),
            "dtypes": {name: str(dtype) for name, dtype in frame.dtypes.items()},
            "row_count": int(len(frame)),
            "data_checksum_sha256": "",
            "logical_fingerprint": logical_fingerprint(frame),
            "code_fingerprint": self.code_fingerprint,
            "serialization_profile_id": "parquet-v1",
            "quality_report_checksum_sha256": "0" * 64,
            "run_id": "00000000-0000-4000-8000-000000000000",
            "created_at": "1970-01-01T00:00:00+00:00",
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        _artifact, data_checksum = serialize_sample_frame(frame)
        manifest["data_checksum_sha256"] = data_checksum
        generation, digest = model_manifest_identities(
            manifest,
            schema_name="sample_construction",
            exclude_fields={"quality_report_checksum_sha256"},
        )
        manifest["generation_id"] = generation
        manifest["manifest_digest_sha256"] = digest
        ModelContractLoader.validate("sample_construction", manifest)
        return manifest


def serialize_sample_frame(frame: pd.DataFrame) -> tuple[bytes, str]:
    """Serialize a sample frame deterministically and return its byte checksum."""
    import io

    import pyarrow as arrow
    import pyarrow.parquet as parquet

    ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort").reset_index(drop=True)
    table = arrow.Table.from_pandas(ordered, preserve_index=False)
    sink = arrow.BufferOutputStream()
    parquet.write_table(table, sink, compression="snappy")
    artifact = sink.getvalue().to_pybytes()
    return artifact, file_sha256_bytes(artifact)
