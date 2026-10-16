#!/usr/bin/env python3
"""Publish the reviewed adjusted-price line over a canonical bars window.

``publish_adjusted_partition`` publishes one session behind a quality report that
names the cross-check that actually ran. This is its window driver: it reads the
canonical ``bars_daily/v2`` partitions, fetches TDX corporate-action events once
for the instruments in the window, and publishes one ``bars_adjusted/adjusted-v2``
partition per session with the TDX-event cross-check.

The cross-check compares the *jump* between adjacent partitions rather than the
level: the event product anchors at the earliest event the vendor's daily history
reaches, while the vendor factor anchors at listing, so the two levels differ by a
constant scale per instrument (measured 9x..712x) while their jumps agree. That is
why each session needs the previous session's frame, so a run over a window
requires both ``raw`` and ``previous_raw`` per session.

A session whose partition already exists is reported as ``already_published`` and
skipped, so an interrupted run resumes by re-running the same command. Exit 1 if
any session was refused, so the report is a gate rather than a log.

Usage:
  scripts/publish_adjusted_window.py --bars-root data/bars-a500-store \
      --adjusted-root data/adjusted-store \
      --start 2025-08-29 --end 2025-09-30 \
      --out evidence/reference-data/adjusted-window-....json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd

from uq.adapters.mootdx_source import MootdxSourceAdapter
from uq.contracts.canonical_v2 import CanonicalV2Store
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.pipeline.adjusted_price import publish_adjusted_partition, restate
from uq.pipeline.adjustment_events import events_from_xdxr
from uq.store.reader import ManifestFirstReader

INPUT_SCHEMA = "config/schemas/bars_daily.v2.yaml"


def _partitions(root: Path, start: dt.date, end: dt.date) -> list[dt.date]:
    base = root / "canonical" / "bars_daily" / "v2"
    if not base.is_dir():
        raise SystemExit(f"missing canonical bars root: {base}")
    sessions = sorted(
        dt.date.fromisoformat(child.name.split("=", 1)[1])
        for child in base.iterdir()
        if child.is_dir() and child.name.startswith("date=")
    )
    return [session for session in sessions if start <= session <= end]


def _published(root: Path, session: dt.date) -> bool:
    partition = root / "canonical" / "bars_adjusted" / "adjusted-v2" / f"date={session.isoformat()}"
    return (partition / "manifest.json").is_file() and (partition / "data.parquet").is_file()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-root", type=Path, required=True)
    parser.add_argument("--adjusted-root", type=Path, required=True)
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--tolerance", type=float, default=1e-3)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    sessions = _partitions(args.bars_root, args.start, args.end)
    if not sessions:
        raise SystemExit(f"no bars partitions in {args.start}..{args.end}")
    # The cross-check compares each partition against the previous session, so the
    # first session in the window has nothing to compare with. Rather than refusing
    # it (which would look like a data problem), the window is extended by one
    # earlier session when one exists: that session is used as the jump baseline and
    # is not itself published unless it is inside the requested window.
    # Any partition strictly before the first in-window session is outside the
    # requested window by construction (the first session is the earliest one in
    # range), so it is used as the baseline and never published.
    earlier = [session for session in _partitions(args.bars_root, dt.date(1990, 1, 1), args.end)
               if session < sessions[0]]
    if earlier:
        sessions = [earlier[-1], *sessions]
    schema = load_schema(INPUT_SCHEMA)
    reader = ManifestFirstReader(args.bars_root)
    frames: dict[dt.date, pd.DataFrame] = {
        session: reader.read(schema, session) for session in sessions
    }

    instruments = sorted({name for frame in frames.values() for name in frame["instrument"]})
    symbols = [name.split(".")[0] for name in instruments]
    adapter = MootdxSourceAdapter()
    try:
        raw_events = adapter.fetch_xdxr_events(symbols)
    except Exception as exc:
        raise SystemExit(f"TDX xdxr fetch failed: {exc}")
    events = {
        name: events_from_xdxr(raw_events.get(name.split(".")[0], pd.DataFrame()))
        for name in instruments
    }

    # Each event's ratio needs the close of the session before its ex-date, so the
    # history spans every session in the window (plus one earlier where available).
    closes: dict[str, dict[dt.date, float]] = {}
    for name in instruments:
        history: dict[dt.date, float] = {}
        for session, frame in frames.items():
            rows = frame[frame["instrument"] == name]
            if not rows.empty:
                history[session] = float(rows["close"].iloc[-1])
        closes[name] = history

    store = CanonicalV2Store(args.adjusted_root)
    published: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for index, session in enumerate(sessions):
        if index == 0 and session < args.start:
            # the injected jump baseline: used, never published
            skipped.append({"session": session.isoformat(), "reason": "jump_baseline"})
            continue
        if _published(args.adjusted_root, session):
            skipped.append({"session": session.isoformat(), "reason": "already_published"})
            continue
        previous = sessions[index - 1] if index else None
        if previous is None:
            # Only reachable when the store has exactly one partition: there is no
            # baseline at all, and the run cannot be resumed by widening --start.
            refused.append({
                "session": session.isoformat(),
                "reason": "no earlier partition exists to use as the jump baseline",
            })
            continue
        frame = frames[session]
        previous_frame = frames[previous]
        try:
            identity = publish_adjusted_partition(
                store, session, frame,
                events=events,
                closes=closes,
                previous_restated=restate(previous_frame),
                previous_raw=previous_frame,
                lineage={"upstream": ["bars_daily/v2"], "events": "tdx:xdxr"},
                source_versions={"tushare": "tushare", "tdx": "mootdx xdxr"},
                tolerance=args.tolerance,
            )
        except ContractError as exc:
            refused.append({"session": session.isoformat(), "reason": str(exc)})
            continue
        published.append({
            "session": session.isoformat(),
            "generation_id": identity["generation_id"],
            "rows": identity["rows"],
            "cross_check_basis": identity["cross_check_basis"],
            "skipped_events": identity.get("skipped_events"),
        })

    report = {
        "record_version": 1,
        "kind": "adjusted_price_window",
        "schema": "bars_adjusted/adjusted-v2",
        "window": {
            "requested_start": args.start.isoformat(),
            "requested_end": args.end.isoformat(),
            "sessions": len(sessions),
            "baseline_session": sessions[0].isoformat(),
            "baseline_note": "the first session is the jump baseline (an earlier partition "
                             "injected outside the requested window); it is used but never "
                             "published. It is absent only when no earlier partition exists, "
                             "in which case the requested window's first session is refused",
        },
        "stores": {
            "bars_root": str(args.bars_root),
            "adjusted_root": str(args.adjusted_root),
            "tracked": False,
            "note": "store roots are gitignored; the generation ids below are the reviewable identity",
        },
        "sources": {
            "prices": "tushare (bars_daily/v2)",
            "cross_check_events": "tdx:xdxr",
            "server": adapter.connected_server,
        },
        "tolerance": args.tolerance,
        "instruments": len(instruments),
        "published": len(published),
        "skipped": len(skipped),
        "refused": len(refused),
        "records": published,
        "skipped_sessions": skipped,
        "refusals": refused,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "published": report["published"], "skipped": report["skipped"], "refused": report["refused"],
    }, ensure_ascii=False))
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
