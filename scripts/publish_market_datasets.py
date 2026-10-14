#!/usr/bin/env python3
"""Publish the four governed paper-execution input families for a research window.

`paper_execution_template.v2` binds four partition sets that must resolve against a
published `MarketDatasetPublisher` store:

  * `trading_calendar_snapshot_v1` (sessions, cross-checked against a second vendor)
  * `bars_daily_v2`               (byte-identical copies of canonical bars partitions)
  * `suspension_snapshot_v1`      (per-session suspended instruments)
  * `dividend_event_v1`           (per-ex-date corporate-action exclusion sets)

This script is the production entry point that turns already-ingested governed
artifacts into those four families, so a paper run does not have to be hand-fed
test fixtures.  It never invents data: bars are copied from the canonical store by
checksum, suspensions come from the canonical bars `status` column, and corporate
actions come from the dividend disclosure store's implemented records.

Usage:
    python scripts/publish_market_datasets.py \\
        --data-root data/bars-a500-store \\
        --market-dataset-root data/market-datasets \\
        --dividend-root data/dividend-store \\
        --start 2025-08-29 --end 2025-09-30 \\
        --bindings-out evidence/.../bindings.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

import pandas as pd

from uq.adapters.eastmoney import EastMoneySessionProvider
from uq.adapters.tushare_2k import Tushare2kAdapter
from uq.contracts.dividend_events import DividendEventStore
from uq.errors import ContractError
from uq.execution.market_dataset_publisher import MarketDatasetPublisher

FAMILIES = ("calendar", "bars", "suspension", "corporate-action")


def _canonical_manifest(data_root: Path, session: dt.date) -> tuple[Path, dict]:
    directory = data_root / "canonical" / "bars_daily" / "v2" / f"date={session.isoformat()}"
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise ContractError(f"no canonical bars partition for {session}")
    return directory, json.loads(manifest_path.read_text(encoding="utf-8"))


def _attempt(call, *, published: list, skipped: list, label: str):
    """Publish one immutable partition, tolerating an already-published one.

    Every publisher rejects overwrites, so a partially completed run must be
    resumable: an existing partition is recorded as skipped instead of aborting
    the whole window.
    """
    try:
        manifest = call()
    except ContractError as exc:
        if "already published" not in str(exc):
            raise
        skipped.append(label)
        return None
    published.append(label)
    return manifest


def _publish_calendar(
    publisher: MarketDatasetPublisher,
    *,
    start: dt.date,
    end: dt.date,
    primary_source: str,
    independent_source: str,
    sessions: set[dt.date],
    independent_sessions: set[dt.date] | None,
) -> dict:
    if independent_sessions is not None and independent_sessions != sessions:
        raise ContractError("primary and independent session lists diverge for the window")
    return publisher.publish_calendar_snapshot(
        partition_date=end,
        window_start=start,
        window_end=end,
        sessions=sorted(sessions),
        primary_source=primary_source,
        independent_source=independent_source,
        cross_check_result=("agreement" if independent_sessions is not None else "skipped"),
    )


def _publish_bars(
    publisher: MarketDatasetPublisher,
    data_root: Path,
    sessions: list[dt.date],
    *,
    start: dt.date,
    end: dt.date,
    published: list,
    skipped: list,
) -> list[dict]:
    manifests = []
    for session in sessions:
        directory, canonical = _canonical_manifest(data_root, session)
        manifest = _attempt(lambda: publisher.publish_bars_partition(
            partition_date=session,
            window_start=start,
            window_end=end,
            source_data_path=directory / "data.parquet",
            source_generation_id=canonical["generation_id"],
            source_data_checksum_sha256=canonical["data_checksum_sha256"],
            columns=canonical["columns"],
            row_count=canonical["row_count"],
        ), published=published, skipped=skipped, label=f"bars_daily_v2:{session.isoformat()}")
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def _publish_suspensions(
    publisher: MarketDatasetPublisher,
    data_root: Path,
    sessions: list[dt.date],
    *,
    start: dt.date,
    end: dt.date,
    published_log: list,
    skipped: list,
) -> list[dict]:
    manifests = []
    for session in sessions:
        directory, _ = _canonical_manifest(data_root, session)
        frame = pd.read_parquet(directory / "data.parquet")
        status = frame["status"].astype(str)
        suspended = frame.loc[status != "trading", ["instrument"]].copy()
        suspended_frame = suspended.assign(
            session_date=pd.to_datetime(session), status="suspended"
        )[["instrument", "session_date", "status"]]
        manifest = _attempt(
            lambda: publisher.publish_suspension_snapshot(
                partition_date=session,
                window_start=start,
                window_end=end,
                frame=suspended_frame,
                primary_source="canonical_bars_daily_v2_status",
                reconciliation_source="tushare_suspend_d",
            ),
            published=published_log, skipped=skipped, label=f"suspension_snapshot_v1:{session.isoformat()}",
        )
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def _corporate_actions(
    dividend_store: DividendEventStore,
    sessions: list[dt.date],
    *,
    start: dt.date,
    end: dt.date,
) -> dict[str, tuple[pd.DataFrame, int, list[str]]]:
    """Map each ex-date in the window to its {date, instrument} exclusion rows.

    The exclusion set only needs which instruments had an implemented
    corporate action on which session; the ten-share amounts and the
    revision-ambiguity rules belong to the adjustment deriver, not here.
    """
    by_ex_date: dict[str, tuple[set[str], int, set[str]]] = {}
    wanted = {session.isoformat() for session in sessions}
    for ann_date in dividend_store.list_partitions():
        if ann_date > end:
            continue
        frame = dividend_store.read_events(ann_date)
        if frame.empty:
            continue
        stage = frame["div_proc"].fillna("").astype(str).str.strip()
        implemented = frame.loc[stage == "实施"].dropna(subset=["ex_date"])
        if implemented.empty:
            continue
        generation = dividend_store.read_manifest(ann_date)["generation_id"]
        for instrument, ex_date in zip(implemented["instrument"], implemented["ex_date"]):
            ex_date = str(ex_date)
            if ex_date not in wanted or not (start.isoformat() <= ex_date <= end.isoformat()):
                continue
            instruments, count, generations = by_ex_date.setdefault(ex_date, (set(), 0, set()))
            instruments.add(str(instrument))
            generations.add(generation)
            by_ex_date[ex_date] = (instruments, count + 1, generations)
    return {
        ex_date: (
            pd.DataFrame({"date": [ex_date] * len(instruments), "instrument": sorted(instruments)}),
            count,
            sorted(generations),
        )
        for ex_date, (instruments, count, generations) in by_ex_date.items()
    }


def _publish_corporate_actions(
    publisher: MarketDatasetPublisher,
    dividend_store: DividendEventStore,
    sessions: list[dt.date],
    *,
    start: dt.date,
    end: dt.date,
    published_log: list,
    skipped: list,
) -> list[dict]:
    manifests = []
    by_ex_date = _corporate_actions(dividend_store, sessions, start=start, end=end)
    # Publish an (possibly empty) exclusion set for every session in the window:
    # the paper execution config binds one corporate-action partition per
    # decision date, so a session with no implemented ex-date still needs an
    # artifact recording that nothing was excluded.
    for session in sessions:
        frame, event_count, generations = by_ex_date.get(
            session.isoformat(),
            (pd.DataFrame({"date": [], "instrument": []}), 0, []),
        )
        ex_date = session.isoformat()
        manifest = _attempt(
            lambda: publisher.publish_corporate_action_snapshot(
                partition_date=dt.date.fromisoformat(ex_date),
                window_start=start,
                window_end=end,
                frame=frame,
                source_generation_ids=generations,
                event_count=event_count,
            ),
            published=published_log, skipped=skipped,
            label=f"dividend_event_v1:{ex_date}",
        )
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True, help="canonical bars store root")
    parser.add_argument("--market-dataset-root", type=Path, required=True)
    parser.add_argument("--dividend-root", type=Path, default=None)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--family", action="append", choices=FAMILIES, default=None)
    parser.add_argument(
        "--skip-independent-calendar",
        action="store_true",
        help="do not cross-check sessions against the EastMoney index series (offline runs)",
    )
    parser.add_argument("--bindings-out", type=Path, default=None)
    args = parser.parse_args()

    # cr.md P1: a malformed window is a configuration error, so it must take the
    # same structured shape as the other governed entry points
    # (``validate_training_readiness.py``, ``uq-ingest daily``) instead of
    # surfacing a raw ValueError traceback with exit code 1.
    try:
        start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    except ValueError as exc:
        print(json.dumps(
            {"status": "configuration_failure", "error": f"invalid --start/--end: {exc}"},
            ensure_ascii=False,
        ))
        return 2
    if end < start:
        print(json.dumps(
            {"status": "configuration_failure", "error": "--end precedes --start"},
            ensure_ascii=False,
        ))
        return 2
    families = args.family or list(FAMILIES)
    if "corporate-action" in families and args.dividend_root is None:
        # cr.md contracts MINOR-1: this refusal must precede every family
        # publish and take the same structured shape as the window checks
        # above, not a raw SystemExit after earlier families already landed.
        print(json.dumps(
            {"status": "configuration_failure",
             "error": "--family corporate-action requires --dividend-root"},
            ensure_ascii=False,
        ))
        return 2
    publisher = MarketDatasetPublisher(args.market_dataset_root)

    adapter = Tushare2kAdapter()
    sessions = sorted(adapter.trade_dates(start.isoformat(), end.isoformat()))
    independent: set[dt.date] | None = None
    if not args.skip_independent_calendar:
        independent = EastMoneySessionProvider().between(start, end)
        independent = {day for day in independent if start <= day <= end}
    canonical_sessions = [
        session for session in sessions
        if (args.data_root / "canonical" / "bars_daily" / "v2" / f"date={session.isoformat()}").is_dir()
    ]

    published_log: list[str] = []
    skipped_log: list[str] = []
    result: dict[str, object] = {"sessions": len(sessions), "families": {}}
    if "calendar" in families:
        manifest = _attempt(
            lambda: _publish_calendar(
                publisher, start=start, end=end,
                primary_source="tushare:trade_cal",
                independent_source=("eastmoney_index_kline_v1" if independent is not None else "not_checked"),
                sessions=set(sessions), independent_sessions=independent,
            ),
            published=published_log, skipped=skipped_log, label="trading_calendar_snapshot_v1",
        )
        result["families"]["calendar"] = manifest
    if "bars" in families:
        result["families"]["bars"] = [
            manifest["generation_id"]
            for manifest in _publish_bars(
                publisher, args.data_root, canonical_sessions, start=start, end=end,
                published=published_log, skipped=skipped_log,
            )
        ]
    if "suspension" in families:
        result["families"]["suspension"] = [
            manifest["generation_id"]
            for manifest in _publish_suspensions(
                publisher, args.data_root, canonical_sessions, start=start, end=end,
                published_log=published_log, skipped=skipped_log,
            )
        ]
    if "corporate-action" in families:
        result["families"]["corporate-action"] = [
            manifest["generation_id"]
            for manifest in _publish_corporate_actions(
                publisher, DividendEventStore(args.dividend_root), canonical_sessions, start=start, end=end,
                published_log=published_log, skipped=skipped_log,
            )
        ]
    result["published"] = published_log
    result["already_present"] = skipped_log

    bindings = {}
    for family in ("bars_daily_v2", "trading_calendar_snapshot_v1", "suspension_snapshot_v1", "dividend_event_v1"):
        try:
            bindings[family] = publisher.build_set_binding(family, window_start=start, window_end=end)
        except ContractError:
            continue
    result["bindings"] = bindings
    if args.bindings_out is not None:
        args.bindings_out.parent.mkdir(parents=True, exist_ok=True)
        args.bindings_out.write_text(json.dumps(bindings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
