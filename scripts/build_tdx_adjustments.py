#!/usr/bin/env python3
"""Derive governed adjustment snapshots from TDX corporate actions over a window.

The sibling of ``build_dividend_adjustments.py``: that one derives from Tushare
dividend *disclosures* mapped into the exchange event shape
(``adj_factor.exchange_v2``), this one from TDX's own ``xdxr`` events
(``adj_factor.derived_v1``). Having both is the point -- a factor restated from
one vendor's events can be checked against the other's, which a single-source
derivation cannot do.

TDX is asked only for the instruments that already have canonical bars in the
window, so the run is bounded by what the store actually contains rather than by
whatever the server happens to return. A symbol whose events are unavailable is
refused with its reason, never silently skipped, and the report is a gate: exit 1
if anything was refused.

Usage:
  scripts/build_tdx_adjustments.py --bars-root data/bars-a500-store \
      --adjustment-root data/adjustment-store \
      --start 2025-08-29 --end 2025-09-30 \
      --out evidence/reference-data/tdx-adjustment-chain-....json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd

from uq.adapters.mootdx_source import MootdxSourceAdapter
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.market.adjustment_chain import (
    PROVIDER_TOLERANCE,
    build_tdx_adjustment_chain,
    publish_adjustment_snapshot,
)
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
    parser.add_argument("--adjustment-root", type=Path, required=True)
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--schema", type=Path, default=Path("config/schemas/bars_daily.v2.yaml"))
    parser.add_argument("--tolerance", type=float, default=PROVIDER_TOLERANCE)
    parser.add_argument(
        "--instrument", action="append", default=None,
        help="restrict to these instruments (repeatable); default is every instrument with bars",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    sessions = _window_sessions(args.bars_root, args.start, args.end)
    if not sessions:
        raise SystemExit(f"no bars partitions in {args.start}..{args.end}")
    schema = load_schema(args.schema)
    reader = ManifestFirstReader(args.bars_root)
    bars = pd.concat([reader.read(schema, session) for session in sessions], ignore_index=True)
    bars["session_date"] = pd.to_datetime(bars["session_date"]).dt.date

    instruments = sorted(set(bars["instrument"]))
    if args.instrument is not None:
        wanted = set(args.instrument)
        instruments = [name for name in instruments if name in wanted]
        missing = sorted(wanted - set(instruments))
        if missing:
            raise SystemExit(f"--instrument names not present in the window's bars: {missing}")
    if not instruments:
        raise SystemExit("no instruments to derive")

    # Fetch once for the whole set: TDX is keyed by the bare code, and one request
    # per symbol keeps a failure local to the symbol that caused it.
    adapter = MootdxSourceAdapter()
    symbols = [name.split(".")[0] for name in instruments]
    try:
        frames = adapter.fetch_xdxr_events(symbols)
    except (ContractError, RuntimeError, OSError) as exc:
        raise SystemExit(f"TDX xdxr fetch failed: {exc}")
    raw_events = {name: frames.get(name.split(".")[0], pd.DataFrame()) for name in instruments}

    visibility = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    published: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for instrument in instruments:
        raw = raw_events[instrument]
        if raw is None or raw.empty:
            refused.append({"instrument": instrument, "reason": "no TDX xdxr events returned"})
            continue
        sub = bars[bars["instrument"] == instrument]
        window_start = min(sub["session_date"])
        window_end = max(sub["session_date"])
        try:
            result = build_tdx_adjustment_chain(
                events=raw,
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
        published.append({
            "instrument": instrument,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "sessions": len(result.sessions),
            "lineage": result.derivation.version,
            "snapshot_generation_id": generation_id,
            "events": [
                {
                    "ex_date": dt.date(int(item["year"]), int(item["month"]), int(item["day"])).isoformat(),
                    "cash_per_ten": float(item["fenhong"]),
                    "bonus_per_ten": float(item["songzhuangu"]),
                    "source_event_id": item["source_event_id"],
                }
                for item in result.events.to_dict(orient="records")
            ],
            "provider_check": result.provider_check,
        })

    verified = [item for item in published if item["provider_check"].get("status") == "passed"]
    unverified = [
        {"instrument": item["instrument"], "reason": item["provider_check"].get("reason")}
        for item in published
        if item["provider_check"].get("status") != "passed"
    ]
    gaps = [item["provider_check"]["max_abs_diff"] for item in verified]
    report = {
        "record_version": 1,
        "kind": "tdx_adjustment_chain",
        "lineage": "adj_factor.derived_v1",
        "source": {
            "events": "tdx:xdxr",
            "server": adapter.connected_server,
            "probe_note": (
                "the adapter probes with xdxr, not bars: on some networks the "
                "quote command family is blocked while xdxr still answers"
            ),
        },
        "window": {"start": args.start.isoformat(), "end": args.end.isoformat(), "sessions": len(sessions)},
        "stores": {
            "bars_root": str(args.bars_root),
            "adjustment_root": str(args.adjustment_root),
            "tracked": False,
            "note": "store roots are gitignored; the snapshot generation ids below are the reviewable identity",
        },
        "tolerance": args.tolerance,
        "instruments_considered": len(instruments),
        "published": len(published),
        "refused": len(refused),
        "provider_reconciliation": {
            "verified": len(verified),
            "skipped": len(unverified),
            "skipped_instruments": unverified,
            "max_abs_diff": max(gaps) if gaps else None,
            "worst_instrument": max(verified, key=lambda item: item["provider_check"]["max_abs_diff"])["instrument"] if verified else None,
            "all_within_public_resolution": all(
                item["provider_check"].get("within_public_resolution", False) for item in verified
            ) if verified else None,
        },
        "records": published,
        "refusals": refused,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "published": report["published"], "refused": report["refused"],
        "provider_reconciliation": report["provider_reconciliation"],
    }, ensure_ascii=False))
    return 1 if refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
