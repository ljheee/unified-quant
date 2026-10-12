#!/usr/bin/env python3
"""Derive governed adjustment snapshots from the dividend store over a window.

Reads canonical bars and the announcement-anchored dividend store, runs the
production adjustment chain for every instrument that has an implemented
disclosure in the window, publishes the snapshots and writes a reviewable
report. Exits 1 if any instrument is refused, so the report is a gate rather
than a log.

Usage:
  scripts/build_dividend_adjustments.py --bars-root data/bars-a500-store \
      --dividend-root data/dividend-store --adjustment-root data/adjustment-store \
      --start 2025-08-29 --end 2025-09-30 --out evidence/reference-data/adjustment-chain-....json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd

from uq.contracts.dividend_events import DividendEventStore
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.market.adjustment_chain import PROVIDER_TOLERANCE, build_adjustment_chain, publish_adjustment_snapshot
from uq.store.reader import ManifestFirstReader


def _window_sessions(store: Path, start: dt.date, end: dt.date) -> list[dt.date]:
    base = store / "canonical" / "bars_daily" / "v2"
    if not base.is_dir():
        raise SystemExit(f"missing canonical bars root: {base}")
    sessions = sorted(
        dt.date.fromisoformat(child.name.split("=", 1)[1])
        for child in base.iterdir()
        if child.is_dir() and child.name.startswith("date=")
    )
    return [session for session in sessions if start <= session <= end]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-root", type=Path, required=True)
    parser.add_argument("--dividend-root", type=Path, required=True)
    parser.add_argument("--adjustment-root", type=Path, required=True)
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--schema", type=Path, default=Path("config/schemas/bars_daily.v2.yaml"))
    parser.add_argument("--tolerance", type=float, default=PROVIDER_TOLERANCE)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    sessions = _window_sessions(args.bars_root, args.start, args.end)
    if not sessions:
        raise SystemExit(f"no bars partitions in {args.start}..{args.end}")
    schema = load_schema(args.schema)
    reader = ManifestFirstReader(args.bars_root)
    bars = pd.concat([reader.read(schema, session) for session in sessions], ignore_index=True)
    bars["session_date"] = pd.to_datetime(bars["session_date"]).dt.date

    records = DividendEventStore(args.dividend_root).as_of(args.end)
    implemented = records[(records["div_proc"] == "实施") & records["ex_date"].notna()]
    in_window = implemented[(implemented["ex_date"] >= args.start.isoformat()) & (implemented["ex_date"] <= args.end.isoformat())]
    instruments = sorted(set(in_window["instrument"]) & set(bars["instrument"]))
    if not instruments:
        raise SystemExit("no instrument has both an implemented disclosure and bars in the window")

    visibility = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    published: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for instrument in instruments:
        sub = bars[bars["instrument"] == instrument]
        window_start = min(sub["session_date"])
        window_end = max(sub["session_date"])
        try:
            result = build_adjustment_chain(
                records=records,
                bars=bars,
                instrument=instrument,
                window_start=window_start,
                window_end=window_end,
                tolerance=args.tolerance,
            )
            generation_id = publish_adjustment_snapshot(
                args.adjustment_root, result, visibility_time=visibility
            )
        except ContractError as exc:
            refused.append({"instrument": instrument, "reason": str(exc)})
            continue
        published.append(
            {
                "instrument": instrument,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "sessions": len(result.sessions),
                "snapshot_generation_id": generation_id,
                "events": [
                    {
                        "ex_date": dt.date(int(item["year"]), int(item["month"]), int(item["day"])).isoformat(),
                        "cash_per_ten": item["fenhong"],
                        "bonus_per_ten": item["songzhuangu"],
                        "source_record_id": item["source_event_id"],
                    }
                    for item in result.events.to_dict(orient="records")
                ],
                "dropped": list(result.dropped),
                "provider_check": result.provider_check,
            }
        )

    gaps = [item["provider_check"].get("max_abs_diff") or 0.0 for item in published]
    report = {
        "record_version": 1,
        "kind": "dividend_adjustment_chain",
        "window": {"start": args.start.isoformat(), "end": args.end.isoformat(), "sessions": len(sessions)},
        "stores": {
            "bars_root": str(args.bars_root),
            "dividend_root": str(args.dividend_root),
            "adjustment_root": str(args.adjustment_root),
            "tracked": False,
            "note": "store roots are gitignored; the snapshot generation ids below are the reviewable identity",
        },
        "tolerance": args.tolerance,
        "instruments_considered": len(instruments),
        "published": len(published),
        "refused": len(refused),
        "provider_reconciliation": {
            "max_abs_diff": max(gaps) if gaps else None,
            "worst_instrument": max(published, key=lambda item: item["provider_check"].get("max_abs_diff") or 0.0)["instrument"] if published else None,
            "all_within_public_resolution": all(
                item["provider_check"].get("within_public_resolution", False) for item in published
            ),
        },
        "records": published,
        "refusals": refused,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("published", "refused", "provider_reconciliation")}, ensure_ascii=False))
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
