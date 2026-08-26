from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

import pandas as pd

from ..contracts.model_layer import ModelContractLoader, model_manifest_identities, sha256_json
from ..errors import ContractError
from ..factors.raw_price import logical_fingerprint


class SplitValidator:
    """Enforce purge and embargo rules on time-based splits."""

    @staticmethod
    def validate_splits(
        splits: list[dict[str, Any]],
        *,
        horizon: int,
        embargo_days: int,
        trading_dates: list[str],
        covered_dates: set[str] | None = None,
    ) -> None:
        if len(splits) < 2:
            raise ContractError("at least train and validation splits required")
        if covered_dates is not None:
            declared = {item for split in splits for item in (split["start_date"], split["end_date"])}
            missing = sorted(declared - set(trading_dates) - covered_dates)
            if missing:
                raise ContractError(f"split boundaries are not reconciled with dataset calendar: {missing}")
        names = [split["name"] for split in splits]
        if len(names) != len(set(names)):
            raise ContractError("duplicate split names")
        required = {"train", "validation"}
        if not required.issubset(names):
            raise ContractError("splits must include train and validation")
        date_index = {date: index for index, date in enumerate(trading_dates)}
        ranges: list[tuple[str, int, int]] = []
        for split in splits:
            name = split["name"]
            start = split["start_date"]
            end = split["end_date"]
            if start not in date_index or end not in date_index:
                raise ContractError(f"split {name} dates not in trading calendar")
            start_idx = date_index[start]
            end_idx = date_index[end]
            if start_idx > end_idx:
                raise ContractError(f"split {name} start after end")
            for prev_name, prev_start, prev_end in ranges:
                if start_idx <= prev_end + horizon + embargo_days and prev_name != name:
                    raise ContractError(
                        f"purge/embargo violation between {prev_name} and {name}"
                    )
            ranges.append((name, start_idx, end_idx))
        ordered_ranges = sorted(ranges, key=lambda item: item[1])
        for (_, start_a, end_a), (_, start_b, end_b) in zip(ordered_ranges, ordered_ranges[1:]):
            if start_b <= end_a:
                raise ContractError("overlapping split intervals")


class DatasetBuilder:
    """Build a governed model dataset manifest from factor/label/universe bindings."""

    def __init__(
        self,
        *,
        dataset_name: str,
        semantic_version: str,
        code_fingerprint: str | None = None,
    ) -> None:
        self.dataset_name = dataset_name
        self.semantic_version = semantic_version
        self.code_fingerprint = code_fingerprint or sha256_json({"component": "DatasetBuilder", "version": 1})

    def build(
        self,
        *,
        ordered_features: list[str],
        factor_set: str,
        factor_version: str,
        factor_generation_ids: list[str],
        label_set_name: str,
        label_generation_id: str,
        universe_snapshot_generation_id: str,
        split_policy: dict[str, Any],
        missing_policy: str = "fail_closed",
        row_count: int,
    ) -> dict[str, Any]:
        if missing_policy not in ("fail_closed", "declared_fill"):
            raise ContractError(f"unsupported missing policy: {missing_policy}")
        if not ordered_features:
            raise ContractError("dataset requires at least one feature")
        if not factor_generation_ids:
            raise ContractError("dataset requires at least one factor generation binding")

        manifest: dict[str, Any] = {
            "contract_version": 1,
            "dataset_name": self.dataset_name,
            "semantic_version": self.semantic_version,
            "ordered_features": ordered_features,
            "factor_set": factor_set,
            "factor_version": factor_version,
            "factor_generation_ids": factor_generation_ids,
            "label_set_name": label_set_name,
            "label_generation_id": label_generation_id,
            "universe_snapshot_generation_id": universe_snapshot_generation_id,
            "split_policy": split_policy,
            "missing_policy": missing_policy,
            "row_count": row_count,
            "data_checksum_sha256": sha256_json({"features": ordered_features, "row_count": row_count}),
            "logical_fingerprint": sha256_json({"features": ordered_features}),
            "code_fingerprint": self.code_fingerprint,
            "serialization_profile_id": "parquet-v1",
            "quality_report_checksum_sha256": "0" * 64,
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest["generation_id"] = "0" * 64
        manifest["manifest_digest_sha256"] = "0" * 64
        generation_id, manifest_digest = model_manifest_identities(
            manifest,
            schema_name="model_dataset",
            exclude_fields={"logical_fingerprint", "quality_report_checksum_sha256"},
        )
        manifest["generation_id"] = generation_id
        manifest["manifest_digest_sha256"] = manifest_digest
        ModelContractLoader.validate("model_dataset", manifest)
        return manifest
