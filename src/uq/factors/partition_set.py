from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from ..contracts.gate_contracts import sha256_json
from ..errors import ContractError
from .store import _read_verified_factor_manifest

DECISION_ZONE = ZoneInfo("Asia/Shanghai")


def _decision_time(partition_date: date) -> datetime:
    return datetime.combine(partition_date, time(15, 0), tzinfo=DECISION_ZONE)


def _entry(manifest: dict[str, Any]) -> dict[str, Any]:
    partition_date = date.fromisoformat(manifest["partition_date"])
    return {
        "partition_date": partition_date.isoformat(),
        "generation_id": manifest["generation_id"],
        "manifest_digest_sha256": manifest["manifest_digest_sha256"],
        "data_checksum_sha256": manifest["data_checksum_sha256"],
        "decision_time": manifest["decision_time"],
        "run_visible_cutoff": manifest["run_visible_cutoff"],
    }


def _check_entry(manifest: dict[str, Any], entry: dict[str, Any]) -> None:
    partition_date = date.fromisoformat(entry["partition_date"])
    if manifest["generation_id"] != entry["generation_id"]:
        raise ContractError("factor partition set generation mismatch")
    if manifest["manifest_digest_sha256"] != entry["manifest_digest_sha256"]:
        raise ContractError("factor partition set manifest digest mismatch")
    if manifest["data_checksum_sha256"] != entry["data_checksum_sha256"]:
        raise ContractError("factor partition set data checksum mismatch")
    if manifest["partition_date"] != partition_date.isoformat():
        raise ContractError("factor partition set partition date mismatch")
    expected_decision = _decision_time(partition_date).isoformat()
    if manifest["decision_time"] != expected_decision:
        raise ContractError("factor partition decision time is not the 15:00 decision point")
    if datetime.fromisoformat(manifest["run_visible_cutoff"]) > _decision_time(partition_date):
        raise ContractError("factor partition visible cutoff is later than its decision point")


def build_factor_partition_set(
    store: Any,
    *,
    input_dataset: str,
    input_schema_version: str,
    factor_set: str,
    factor_version: str,
    window_start: date,
    window_end: date,
    trading_days: list[date],
) -> dict[str, Any]:
    if window_end < window_start:
        raise ContractError("factor partition window end precedes start")
    window_days = [day for day in trading_days if window_start <= day <= window_end]
    if not window_days:
        raise ContractError("factor partition window has no trading days")
    partitions: list[dict[str, Any]] = []
    for day in window_days:
        partition = store._partition(
            input_dataset=input_dataset,
            input_schema_version=input_schema_version,
            factor_set=factor_set,
            factor_version=factor_version,
            partition_date=day,
        )
        manifest = _read_verified_factor_manifest(partition)
        if date.fromisoformat(manifest["partition_date"]) != day:
            raise ContractError("factor partition date does not match window day")
        partitions.append(_entry(manifest))
    document = {
        "binding_type": "factor_partition_set",
        "input_dataset": input_dataset,
        "input_schema_version": input_schema_version,
        "factor_set": factor_set,
        "factor_version": factor_version,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "partitions": partitions,
    }
    document["set_digest_sha256"] = sha256_json(
        {key: value for key, value in document.items() if key != "set_digest_sha256"}
    )
    return document


def verify_factor_partition_set(
    store: Any,
    document: dict[str, Any],
    trading_days: list[date],
) -> None:
    expected = sha256_json({key: value for key, value in document.items() if key != "set_digest_sha256"})
    if document.get("set_digest_sha256") != expected:
        raise ContractError("factor partition set digest mismatch")
    dates = [entry["partition_date"] for entry in document["partitions"]]
    if dates != sorted(set(dates)):
        raise ContractError("factor partition set partitions must be strictly ascending")
    window_start = date.fromisoformat(document["window_start"])
    window_end = date.fromisoformat(document["window_end"])
    expected_days = [day.isoformat() for day in trading_days if window_start <= day <= window_end]
    if dates != expected_days:
        raise ContractError("factor partition set does not cover every trading day in the window")
    for entry in document["partitions"]:
        manifest = store.read_manifest(entry["generation_id"])
        _check_entry(manifest, entry)
