"""Point-in-time index membership and factor partition set bindings for v4 requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

import pandas as pd

from ..contracts.index_membership import (
    IndexMembershipStore,
    build_membership_set,
    verify_membership_set,
)
from ..errors import ContractError
from ..factors.partition_set import build_factor_partition_set, verify_factor_partition_set
from ..factors.store import _read_verified_factor_manifest, read_factor_partition


@dataclass(frozen=True)
class VerifiedInputSets:
    index_membership_set: dict[str, Any]
    factor_partition_set: dict[str, Any]


def _window_days(request: Mapping[str, Any], trading_days: Sequence[date]) -> list[date]:
    # The request schema pins only the shape of these dates
    # (^\d{4}-\d{2}-\d{2}$), not the calendar: "2026-99-99" passes validation.
    # A bare ValueError would slip past the resolver's typed-error seam and
    # surface at the CLI as an untyped configuration_failure.
    try:
        start = date.fromisoformat(request["window_start_date"])
        end = date.fromisoformat(request["window_end_date"])
    except ValueError as exc:
        raise ContractError(
            f"request window date is not a real calendar date: {exc}"
        ) from exc
    if end < start:
        raise ContractError("research window end precedes start")
    days = [day for day in trading_days if start <= day <= end]
    if not days:
        raise ContractError("research window has no governed trading days")
    return days


def verify_request_input_sets(
    request: Mapping[str, Any],
    *,
    membership_store: IndexMembershipStore,
    factor_store: Any,
    trading_days: Sequence[date],
) -> VerifiedInputSets:
    """Rebuild both set bindings from their owning stores and bind them to the request."""
    days = _window_days(request, trading_days)
    membership_binding = request["index_membership_binding"]
    factor_binding = request["factor_partition_set_binding"]
    if (
        membership_binding["window_start"] != request["window_start_date"]
        or membership_binding["window_end"] != request["window_end_date"]
    ):
        raise ContractError("index membership binding window does not match the research window")
    if (
        factor_binding["window_start"] != request["window_start_date"]
        or factor_binding["window_end"] != request["window_end_date"]
    ):
        raise ContractError("factor partition set binding window does not match the research window")

    membership_document = _membership_document(membership_store, membership_binding, days)
    if membership_document["set_digest_sha256"] != membership_binding["set_digest_sha256"]:
        raise ContractError("index membership set digest mismatch")
    if membership_document["index_code"] != membership_binding["index_code"]:
        raise ContractError("index membership set index code mismatch")
    if membership_document["universe_id"] != membership_binding["universe_id"]:
        raise ContractError("index membership set universe mismatch")

    factor_document = _factor_document(factor_store, factor_binding, days)
    if factor_document["set_digest_sha256"] != factor_binding["set_digest_sha256"]:
        raise ContractError("factor partition set digest mismatch")
    for field in ("input_dataset", "input_schema_version", "factor_set", "factor_version"):
        if factor_document[field] != factor_binding[field]:
            raise ContractError(f"factor partition set {field} mismatch")
    return VerifiedInputSets(
        index_membership_set=membership_document,
        factor_partition_set=factor_document,
    )


def _membership_document(
    store: IndexMembershipStore,
    binding: Mapping[str, Any],
    days: Sequence[date],
) -> dict[str, Any]:
    document = build_membership_set(
        store,
        universe_id=binding["universe_id"],
        window_start=days[0],
        window_end=days[-1],
        trading_days=list(days),
    )
    verify_membership_set(store, document, list(days))
    return document


def _factor_document(
    factor_store: Any,
    binding: Mapping[str, Any],
    days: Sequence[date],
) -> dict[str, Any]:
    document = build_factor_partition_set(
        factor_store,
        input_dataset=binding["input_dataset"],
        input_schema_version=binding["input_schema_version"],
        factor_set=binding["factor_set"],
        factor_version=binding["factor_version"],
        window_start=days[0],
        window_end=days[-1],
        trading_days=list(days),
    )
    verify_factor_partition_set(factor_store, document, list(days))
    return document


def _visible_members(
    membership_store: IndexMembershipStore,
    membership_document: Mapping[str, Any],
) -> list[tuple[date, frozenset[str]]]:
    """One member read per snapshot; each entry is (visible_from, instruments)."""
    universe_id = membership_document["universe_id"]
    visible: list[tuple[date, frozenset[str]]] = []
    for entry in membership_document["snapshots"]:
        members = membership_store.read_members(
            universe_id, date.fromisoformat(entry["snapshot_date"])
        )
        visible.append((
            date.fromisoformat(entry["visible_from"]),
            frozenset(members["instrument"].astype(str)),
        ))
    return sorted(visible, key=lambda item: item[0])


def filtered_factor_frame(
    request: Mapping[str, Any],
    *,
    membership_store: IndexMembershipStore,
    factor_store: Any,
    trading_days: Sequence[date],
    verified: VerifiedInputSets | None = None,
) -> pd.DataFrame:
    """Intersect every window day's factor partition with that day's visible members."""
    days = _window_days(request, trading_days)
    sets = verified if verified is not None else verify_request_input_sets(
        request,
        membership_store=membership_store,
        factor_store=factor_store,
        trading_days=trading_days,
    )
    binding = request["factor_partition_set_binding"]
    visible = _visible_members(membership_store, sets.index_membership_set)
    partitions = {
        entry["partition_date"]: entry for entry in sets.factor_partition_set["partitions"]
    }
    frames: list[pd.DataFrame] = []
    for day in days:
        entries = [item for item in visible if item[0] <= day]
        if not entries:
            raise ContractError(f"no index membership visible at {day.isoformat()}")
        member_ids = entries[-1][1]
        partition = factor_store._partition(
            input_dataset=binding["input_dataset"],
            input_schema_version=binding["input_schema_version"],
            factor_set=binding["factor_set"],
            factor_version=binding["factor_version"],
            partition_date=day,
        )
        manifest = _read_verified_factor_manifest(partition)
        if manifest["generation_id"] != partitions[day.isoformat()]["generation_id"]:
            raise ContractError("factor partition generation does not match the bound set")
        frame = read_factor_partition(partition, registry=factor_store.registry)
        keyed = frame["instrument"].astype(str)
        day_rows = frame[keyed.isin(member_ids)].copy()
        if day_rows.empty:
            raise ContractError(f"factor partition {day.isoformat()} has no visible index members")
        frames.append(day_rows)
    return pd.concat(frames, ignore_index=True)
