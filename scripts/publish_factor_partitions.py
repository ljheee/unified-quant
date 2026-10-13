#!/usr/bin/env python3
"""Publish governed factor partitions from an ingested canonical bars store.

This is the production entry point that was missing for the A500 / full-A line:
it reads already-ingested canonical bars partitions through the manifest-first
reader, projects them onto the reviewed factor input contract, computes the
reviewed factor set, and publishes one immutable factor partition per session.
It never invents data and never relaxes a reviewed definition: the input line is
taken from the store, then checked against the definition's ``input_bindings``,
so an un-reviewed combination fails closed with the reviewed list quoted.

The visibility basis of the bound input is an explicit operator decision, not a
default. ``factor_governance`` fixes every factor partition's
``run_visible_cutoff`` at ``partition_date 15:00 Asia/Shanghai`` and requires each
bound input to be visible by then, so:

  * ``session_close`` treats the session's close as information available at the
    close instant (the convention the close-to-close decision model implies);
  * ``ingest_time`` binds the canonical partition's recorded download time, which
    for end-of-day vendor data is after the close and therefore fails closed.

Both are supported so the choice stays visible in the command that produced the
artifacts; neither is silently assumed.

Usage:
    python scripts/publish_factor_partitions.py \
        --bars-root data/bars-a500-store \
        --factor-root data/factors-a500-store \
        --factor-set basic --factor-version 2.0.0 \
        --start 2025-08-29 --end 2025-09-30 \
        --input-visibility session_close \
        --partition-set-out evidence/.../factor_partition_set.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.factors.inputs import factor_input_frame
from uq.factors.partition_set import build_factor_partition_set
from uq.factors.raw_price import calculate_raw_price_factors
from uq.factors.engine import FactorEngine
from uq.factors.store import FactorStore, factor_generation
from uq.store.reader import ManifestFirstReader

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DECISION_ZONE = ZoneInfo("Asia/Shanghai")
DECISION_CLOCK = dt.time(15, 0)

# Reviewed pairs for the two definitions written before input_bindings existed.
LEGACY_BARS_DAILY = {
    "basic": ("bars_daily", ["research-v1"]),
    "adjusted": ("bars_adjusted", ["adjusted-v1"]),
}


def _refuse(message: str) -> int:
    print(json.dumps({"status": "configuration_failure", "error": message}, ensure_ascii=False))
    return 2


def _canonical_partitions(bars_root: Path) -> dict[str, list[dt.date]]:
    """Map ``schema_version`` -> sessions present in the canonical store."""
    found: dict[str, list[dt.date]] = {}
    for dataset_dir in sorted((bars_root / "canonical" / "bars_daily").glob("*/")):
        sessions: list[dt.date] = []
        for partition in sorted((dataset_dir).glob("date=*/")):
            try:
                sessions.append(dt.date.fromisoformat(partition.name.split("=", 1)[1]))
            except ValueError:
                continue
        if sessions:
            found[dataset_dir.name] = sessions
    return found


def _upstream_created_at(basis: str, session: dt.date, manifest: dict[str, Any]) -> dt.datetime:
    if basis == "session_close":
        return dt.datetime.combine(session, DECISION_CLOCK, tzinfo=DECISION_ZONE)
    created_at = dt.datetime.fromisoformat(str(manifest["created_at"]))
    if created_at.tzinfo is None:
        raise ContractError(f"canonical manifest created_at is not timezone-aware: {manifest['created_at']}")
    return created_at


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-root", type=Path, required=True, help="canonical bars store root")
    parser.add_argument("--factor-root", type=Path, required=True, help="factor partition store root")
    parser.add_argument("--factor-set", required=True)
    parser.add_argument("--factor-version", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD, first session to publish")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD, last session to publish")
    parser.add_argument("--input-visibility", required=True, choices=("session_close", "ingest_time"),
                        help="visibility basis bound as the input's upstream_created_at")
    parser.add_argument("--partition-set-out", type=Path, default=None,
                        help="write the factor_partition_set binding covering the published window")
    args = parser.parse_args()

    try:
        start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    except ValueError as exc:
        return _refuse(f"invalid --start/--end: {exc}")
    if end < start:
        return _refuse("--end precedes --start")

    registry = FactorRegistry(PROJECT_ROOT)
    try:
        definition = registry.get(args.factor_set, args.factor_version)
    except ContractError as exc:
        return _refuse(f"unknown or unreviewed factor set: {exc}")
    bindings = definition.document.get("input_bindings")
    if bindings is None:
        # The two v1 definitions predate input_bindings; their reviewed pair is the
        # historical one, restated here only to refuse with a usable message. The
        # store still owns enforcement.
        bindings = {"legacy_pair": LEGACY_BARS_DAILY.get(args.factor_set)}
        if bindings["legacy_pair"] is None:
            return _refuse(
                f"{args.factor_set}/{args.factor_version} declares no input_bindings and no "
                "historical bars_daily pair; publish it through its own reviewed entry point"
            )
        if args.factor_set != "basic":
            return _refuse(
                f"{args.factor_set}/{args.factor_version} is reviewed over "
                f"{LEGACY_BARS_DAILY[args.factor_set][0]}, which this bars_daily entry point does not read"
            )
        reviewed_versions = bindings["legacy_pair"][1]
    else:
        if bindings["allowed_datasets"] != ["bars_daily"]:
            return _refuse(
                "this entry point publishes raw-price factor sets over bars_daily; "
                f"{args.factor_set}/{args.factor_version} binds {bindings['allowed_datasets']}"
            )
        reviewed_versions = bindings["required_schema_versions"]["bars_daily"]

    present = _canonical_partitions(args.bars_root)
    if not present:
        return _refuse(f"no canonical bars_daily partitions under {args.bars_root}")
    lines_in_window = {
        version: [day for day in sessions if start <= day <= end]
        for version, sessions in present.items()
        if any(start <= day <= end for day in sessions)
    }
    if not lines_in_window:
        return _refuse(f"no canonical bars partitions inside {start}..{end}")
    unreviewed = sorted(set(lines_in_window) - set(reviewed_versions))
    if unreviewed:
        return _refuse(
            f"canonical store holds bars_daily/{', '.join(unreviewed)} inside the window but "
            f"{args.factor_set}/{args.factor_version} is reviewed only for {', '.join(sorted(reviewed_versions))}"
        )
    if len(lines_in_window) > 1:
        return _refuse(f"mixed canonical lines inside the window: {sorted(lines_in_window)}")
    schema_version, target_days = next(iter(lines_in_window.items()))
    history_days = present[schema_version]
    lookback = max(int(factor.get("minimum_history", 1)) for factor in definition.factors)

    schema = load_schema(PROJECT_ROOT / "config" / "schemas" / f"bars_daily.{schema_version}.yaml")
    reader = ManifestFirstReader(args.bars_root)
    store = FactorStore(args.factor_root, registry)
    engine = FactorEngine(
        args.factor_root, registry,
        run_visible_cutoff=dt.datetime.combine(
            target_days[-1], DECISION_CLOCK, tzinfo=DECISION_ZONE),
    )
    report_root = args.factor_root
    cache: dict[dt.date, tuple[pd.DataFrame, dict[str, Any]]] = {}

    def verified(day: dt.date) -> tuple[pd.DataFrame, dict[str, Any]]:
        if day not in cache:
            frame = reader.read(schema, day)
            partition = (
                args.bars_root / "canonical" / "bars_daily" / schema_version / f"date={day.isoformat()}"
            )
            manifest = json.loads((partition / "manifest.json").read_text(encoding="utf-8"))
            digest = hashlib.sha256((partition / "data.parquet").read_bytes()).hexdigest()
            if manifest.get("data_checksum_sha256") != digest:
                raise ContractError(f"canonical data checksum mismatch for {day}")
            cache[day] = (frame, manifest)
        return cache[day]

    published: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    covered: list[dt.date] = []
    for day in sorted(target_days):
        window = [session for session in history_days if session <= day][-lookback:]
        if len(window) < lookback or window[-1] != day:
            skipped.append({"session": day.isoformat(), "reason": "insufficient_history",
                            "detail": f"{len(window)} of {lookback} sessions available"})
            continue
        try:
            rows = [verified(session)[0] for session in window]
            projected, statistics = factor_input_frame(
                pd.concat(rows, ignore_index=True),
                schema_version=schema_version,
                required_columns=definition.document["required_columns"],
            )
            computed = calculate_raw_price_factors(projected)
            frame = computed[computed["datetime"] == pd.Timestamp(day)].reset_index(drop=True)
            if frame.empty:
                skipped.append({"session": day.isoformat(), "reason": "no_factor_rows"})
                continue
            _bar_frame, bar_manifest = verified(day)
            arguments = {
                "factor_set": args.factor_set,
                "factor_version": args.factor_version,
                "frame": frame,
                "partition_date": day,
                "input_dataset": "bars_daily",
                "input_schema_version": schema_version,
                "upstream_generation_id": bar_manifest["generation_id"],
                "upstream_data_checksum": bar_manifest["data_checksum_sha256"],
                "quality_report_checksum": "",
                "upstream_created_at": _upstream_created_at(args.input_visibility, day, bar_manifest),
            }
            generation = factor_generation(**arguments)
            # The reviewed decision comes from the governed engine, whose report
            # FactorStore.publish re-derives from the same validator, so what the
            # run plans is exactly what publication accepts (cr.md R1).
            result = engine.create_result(
                engine.build_request(
                    trade_date=day, factor_set=args.factor_set,
                    factor_version=args.factor_version, session_dates=(day,),
                    intent="publication",
                    run_visible_cutoff=dt.datetime.combine(
                        day, DECISION_CLOCK, tzinfo=DECISION_ZONE),
                ),
                frame,
                input_lineage=({
                    "factor_set": args.factor_set,
                    "factor_version": args.factor_version,
                    "dataset": "bars_daily",
                    "schema_version": schema_version,
                    "generation_id": bar_manifest["generation_id"],
                    "data_checksum_sha256": bar_manifest["data_checksum_sha256"],
                },),
            )
            if result.status != "passed":
                raise ContractError(
                    f"governed factor engine refused {day.isoformat()}: status={result.status}"
                    + ("".join(f"; {message}" for message in result.errors))
                )
            local_quality = result.quality_report
            QualityReportStore().save(report_root, {
                "report_version": 1,
                "binding_type": "factor_v1",
                "bound_generation_id": generation,
                "policy": local_quality["policy"],
                "status": local_quality["status"],
                "checks": local_quality["checks"],
                "errors": local_quality["errors"],
                "warnings": local_quality["warnings"],
            })
            arguments["quality_report_checksum"] = file_sha256_bytes(
                (report_root / "reports" / "factor_v1" / generation / "report.json").read_bytes()
            )
            partition = store.publish(**arguments)
            published.append({
                "session": day.isoformat(),
                "generation_id": generation,
                "rows": int(len(frame)),
                "partition": str(partition),
                "history_sessions": len(window),
                "source_rows": statistics["source_rows"],
                "non_trading_rows_excluded": statistics["non_trading_rows_excluded"],
            })
            covered.append(day)
        except (ContractError, OSError, ValueError) as exc:
            if "already published" in str(exc):
                skipped.append({"session": day.isoformat(), "reason": "already_published"})
                covered.append(day)
                continue
            failed.append({"session": day.isoformat(), "error": f"{type(exc).__name__}: {exc}"})

    if failed:
        status = "failed"
    elif published:
        status = "published"
    elif covered:
        status = "already_published"
    else:
        status = "empty"
    result: dict[str, Any] = {
        "status": status,
        "input_line": f"bars_daily/{schema_version}",
        "factor_set": f"{args.factor_set}/{args.factor_version}",
        "input_visibility_basis": args.input_visibility,
        "lookback_sessions": lookback,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "published": published,
        "skipped": skipped,
        "failed": failed,
    }
    if args.partition_set_out is not None:
        # The set covers the sessions this run actually has partitions for, not the
        # requested window: a window whose first sessions lack factor history is a
        # legitimate partial cover, and binding days that were never published would
        # be a lineage lie.
        if failed:
            result["partition_set"] = "not_built_because_the_window_is_incomplete"
        elif not covered:
            result["partition_set"] = "not_built_because_nothing_is_published"
        else:
            covered_days = sorted(covered)
            try:
                document = build_factor_partition_set(
                    store, input_dataset="bars_daily", input_schema_version=schema_version,
                    factor_set=args.factor_set, factor_version=args.factor_version,
                    window_start=covered_days[0], window_end=covered_days[-1],
                    trading_days=covered_days,
                )
                args.partition_set_out.parent.mkdir(parents=True, exist_ok=True)
                args.partition_set_out.write_text(
                    json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                result["partition_set"] = {
                    "path": str(args.partition_set_out),
                    "set_digest_sha256": document["set_digest_sha256"],
                    "partitions": len(document["partitions"]),
                    "window": {"start": document["window_start"], "end": document["window_end"]},
                }
            except ContractError as exc:
                result["partition_set"] = f"not_built: {exc}"
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
