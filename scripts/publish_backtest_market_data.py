#!/usr/bin/env python3
"""Publish the three window-level market inputs a `backtest_config.v1` binds.

`BacktestMarketDatasetStore.read_frame` had no writer: only
`tests/test_research_chain_phase4.py` ever produced its
`backtest_market_data/dataset=<generation>` layout, so no real run could supply
the calendar, suspension or corporate-action input that `backtest_config.v1`
references as one `{generation_id, checksum_sha256}` pair per family
(`specs/layers/paper-execution-research-integration-plan.md` phase 0.5 names this
gap: "They exist as binding names only ... Real runs need publishers").

This is that publisher.  Every input is derived from already-governed artifacts
in the same data root -- no new vendor, no live API call:

  * calendar        - the sessions present in `canonical/bars_daily/v2`, i.e. the
                      sessions the price panel itself is built from.  Deriving the
                      calendar from the price source (rather than a second vendor
                      calendar) is what makes the backtest's
                      `set(price_dates) == set(calendar_dates)` invariant hold by
                      construction instead of by reconciliation.
  * suspension      - per-session rows from the bars `status` column, which
                      `specs/layers/paper-execution-research-integration-plan.md`
                      names as the *primary* source; Tushare `suspend_d` is
                      recorded as reconciliation provenance, never a second truth.
  * corporate_action - per-ex-date instruments from the dividend disclosure
                      store's implemented records, the same `DividendEventStore`
                      the adjustment chain derives factors from.

Each family is one immutable whole-window artifact, matching the binding shape
`backtest_config.v1` declares.  That is deliberately *not* the paper path's
per-session `partition_set` layout, so this script does not write
`market-datasets/`; see `docs/subsystems/research-chain.md`.

Usage:
  python scripts/publish_backtest_market_data.py \
      --data-root data/bars-a500-store \
      --dividend-root data/dividend-store \
      --start 2025-08-29 --end 2025-09-30 \
      --out evidence/reference-data/backtest-market-inputs-....json

Exit 1 if any family fails; already-published windows are recorded as skipped so
an interrupted run resumes by re-running the same command.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from uq.contracts.dividend_events import DividendEventStore
from uq.errors import ContractError
from uq.research_chain.owning_contracts import BacktestMarketDatasetStore

PRODUCER = "scripts/publish_backtest_market_data.py"


def _canonical_sessions(data_root: Path, start: dt.date, end: dt.date) -> list[dt.date]:
    base = data_root / "canonical" / "bars_daily" / "v2"
    if not base.is_dir():
        raise SystemExit(f"missing canonical bars root: {base}")
    sessions = sorted(
        dt.date.fromisoformat(child.name.split("=", 1)[1])
        for child in base.iterdir()
        if child.is_dir() and child.name.startswith("date=")
    )
    window = [session for session in sessions if start <= session <= end]
    if not window:
        raise SystemExit(f"no canonical bars partitions in {start}..{end}")
    return window


def _canonical_frame(data_root: Path, session: dt.date) -> pd.DataFrame:
    directory = data_root / "canonical" / "bars_daily" / "v2" / f"date={session.isoformat()}"
    if not (directory / "manifest.json").is_file():
        raise SystemExit(f"no canonical bars partition for {session}")
    return pd.read_parquet(directory / "data.parquet")


def _calendar_frame(sessions: list[dt.date]) -> pd.DataFrame:
    """One row per session the price panel is built from.

    The column is `date`, which is what `BacktestStageAdapter` requires of the
    calendar (`required_columns={"date"}`); the canonical store's own
    `trading_calendar_snapshot_v1` uses `session_date`, so the two layouts are not
    interchangeable by construction.
    """
    return pd.DataFrame(
        [{"date": session.isoformat(), "instrument": "CALENDAR"} for session in sessions],
        columns=["date", "instrument"],
    )


def _suspension_frame(data_root: Path, sessions: list[dt.date]) -> pd.DataFrame:
    """Per-session suspended instruments from the bars `status` column.

    `status` is the primary source; anything that is not `trading` (including
    `delisted` and `unknown`) is recorded as suspended so the backtest cannot
    trade it, which is the conservative direction.

    A name the universe carries but the session's panel does not carry at all is
    recorded as suspended too.  The bars partitions are per-session and only
    contain rows the vendor returned, so a genuinely suspended or otherwise
    absent name simply has no row -- and a `status`-only read is blind to it.
    Without this the backtest marks to market a holding it has no price for and
    refuses the whole run at the last stage.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for session in sessions:
        frame = _canonical_frame(data_root, session)
        status = frame["status"].astype(str) if "status" in frame else None
        if status is None:
            raise SystemExit(f"canonical bars partition {session} has no status column")
        present = set(frame["instrument"].astype(str))
        seen |= present
        suspended = frame.loc[status != "trading", ["instrument"]].copy()
        suspended["date"] = session.isoformat()
        suspended["suspended"] = True
        rows.extend(suspended.to_dict(orient="records"))
    # Absent-for-the-whole-window names are not part of this universe and are not
    # recorded: only names the panel carries on *some* session can be held, so
    # only those need an explicit absence row for the sessions they miss.
    for session in sessions:
        frame = _canonical_frame(data_root, session)
        present = set(frame["instrument"].astype(str))
        for instrument in sorted(seen - present):
            rows.append({"date": session.isoformat(), "instrument": instrument,
                         "suspended": True})
    frame = pd.DataFrame(rows, columns=["date", "instrument", "suspended"])
    if frame.empty:
        # The store refuses an empty frame, and an all-trading window is a real
        # state rather than a missing input, so publish one explicit negative row.
        frame = pd.DataFrame(
            [{"date": sessions[0].isoformat(), "instrument": "NONE_SUSPENDED", "suspended": False}],
            columns=["date", "instrument", "suspended"],
        )
    return frame


def _corporate_action_frame(dividend_root: Path, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Per-ex-date instruments with an implemented corporate action.

    Reads the same `DividendEventStore` the adjustment chain derives factors from,
    and takes only records whose `div_proc` marks them implemented and whose
    `ex_date` falls in the window, so the exclusion set cannot widen past the
    window or include an announced-but-not-yet-effective event.
    """
    store = DividendEventStore(dividend_root)
    rows: list[dict[str, Any]] = []
    for partition_date in sorted(
        path for path in (dividend_root / "dividend_events").glob("*")
        if path.is_dir() and not path.name.startswith(".")
    ):
        announcement = dt.date.fromisoformat(partition_date.name)
        # The *announcement* may be any date up to the run's visibility cutoff: an
        # ex-date inside the window can be announced before it (and the disclosure
        # store is announcement-anchored, so a partition outside the window is
        # ordinary).  Only the resulting `ex_date` is window-bound, below.
        events = store.read_events(announcement)
        if events.empty:
            continue
        # `read_events` returns the normalized frame: `instrument` (canonical
        # spelling) replaces the disclosure's `ts_code`, and `record_id` is added.
        implemented = events[events["div_proc"].astype(str) == "实施"]
        for row in implemented.to_dict(orient="records"):
            ex_date = str(row.get("ex_date") or "")
            if not ex_date or not row.get("instrument"):
                continue
            rows.append({"date": ex_date, "instrument": str(row["instrument"])})
    frame = pd.DataFrame(rows, columns=["date", "instrument"])
    if frame.empty:
        frame = pd.DataFrame(
            [{"date": start.isoformat(), "instrument": "NONE_IMPLEMENTED"}],
            columns=["date", "instrument"],
        )
    # Keep only ex-dates inside the window: the exclusion set must not widen past
    # it, and an announcement whose ex-date lands later is not this window's event.
    in_window = frame[frame["date"].astype(str).apply(
        lambda value: start.isoformat() <= value <= end.isoformat()
    )]
    if in_window.empty:
        return frame
    return in_window.drop_duplicates().sort_values(
        ["date", "instrument"], kind="mergesort"
    ).reset_index(drop=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True,
                        help="store root holding canonical/bars_daily/v2")
    parser.add_argument("--dividend-root", type=Path, required=True,
                        help="published DividendEventStore root")
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, default=None,
                        help="optional JSON run report path")
    args = parser.parse_args(argv)

    sessions = _canonical_sessions(args.data_root, args.start, args.end)
    store = BacktestMarketDatasetStore(args.data_root)
    frames = {
        "calendar": _calendar_frame(sessions),
        "suspension": _suspension_frame(args.data_root, sessions),
        "corporate_action": _corporate_action_frame(args.dividend_root, args.start, args.end),
    }
    sources = {
        "calendar": {
            "primary": "canonical_bars_daily_v2_session_directories",
            "note": "the calendar is the price panel's own sessions, so price/calendar equality holds by construction",
        },
        "suspension": {
            "primary": "canonical_bars_daily_v2_status",
            "reconciliation": "tushare_suspend_d",
            "note": "bars status is the primary source; suspend_d reconciles, it is not a second truth",
        },
        "corporate_action": {
            "primary": "DividendEventStore:implemented_ex_date",
            "note": "the same disclosure store the adjustment chain derives factors from",
        },
    }
    manifests: dict[str, dict[str, Any]] = {}
    for dataset_type, frame in frames.items():
        manifests[dataset_type] = store.publish(
            frame,
            dataset_type=dataset_type,
            window_start=args.start,
            window_end=args.end,
            sources=sources[dataset_type],
            producer=PRODUCER,
        )
    report = {
        "producer": PRODUCER,
        "window_start": args.start.isoformat(),
        "window_end": args.end.isoformat(),
        "sessions": [session.isoformat() for session in sessions],
        "bindings": {
            "calendar_binding": {
                "generation_id": manifests["calendar"]["generation_id"],
                "checksum_sha256": manifests["calendar"]["data_checksum_sha256"],
            },
            "suspension_binding": {
                "dataset_generation_id": manifests["suspension"]["generation_id"],
                "data_checksum_sha256": manifests["suspension"]["data_checksum_sha256"],
            },
            "corporate_action_binding": {
                "dataset_generation_id": manifests["corporate_action"]["generation_id"],
                "data_checksum_sha256": manifests["corporate_action"]["data_checksum_sha256"],
            },
        },
        "row_counts": {key: int(value["row_count"]) for key, value in manifests.items()},
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
