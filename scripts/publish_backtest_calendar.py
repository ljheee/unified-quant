#!/usr/bin/env python3
"""Publish an immutable calendar after Tushare/Sina session-set agreement."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from uq.adapters.sina import SinaSessionProvider
from uq.adapters.tushare_2k import Tushare2kAdapter
from uq.errors import ContractError
from uq.research_chain.owning_contracts import BacktestMarketDatasetStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-dataset-root", type=Path, required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--exchange", default="SSE", help="Tushare trade_cal exchange; default SSE")
    args = parser.parse_args(argv)

    try:
        start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    except ValueError as exc:
        print(json.dumps({"status": "configuration_failure", "error": str(exc)}))
        return 2
    if start > end:
        print(json.dumps({"status": "configuration_failure", "error": "--start must not follow --end"}))
        return 2

    try:
        primary = Tushare2kAdapter().trade_dates(start.isoformat(), end.isoformat(), exchange=args.exchange)
        independent = SinaSessionProvider().between(start, end)
        if not primary or not independent:
            raise ContractError("calendar cross-check returned an empty session set")
        if primary != independent:
            raise ContractError(
                "Tushare and Sina calendars disagree: "
                f"Tushare-only={sorted(day.isoformat() for day in primary - independent)[:10]}, "
                f"Sina-only={sorted(day.isoformat() for day in independent - primary)[:10]}"
            )
        sessions = sorted(day.isoformat() for day in primary)
        frame = pd.DataFrame({"date": sessions, "instrument": ["CALENDAR"] * len(sessions)})
        manifest = BacktestMarketDatasetStore(args.market_dataset_root).publish(
            frame,
            dataset_type="calendar",
            window_start=start,
            window_end=end,
            sources={
                "primary": f"tushare:trade_cal(exchange={args.exchange})",
                "independent": SinaSessionProvider.provenance,
                "cross_check": "exact_session_set_agreement",
            },
            producer="publish_backtest_calendar.py",
        )
    except ContractError as exc:
        print(json.dumps({"status": "refused", "error": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps({
        "status": "published",
        "generation_id": manifest["generation_id"],
        "data_checksum_sha256": manifest["data_checksum_sha256"],
        "manifest_digest_sha256": manifest["manifest_digest_sha256"],
        "sessions": len(sessions),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "sources": manifest["sources"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
