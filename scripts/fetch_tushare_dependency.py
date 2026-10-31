#!/usr/bin/env python3
"""Fetch bounded Tushare research inputs into the immutable source-data store.

This stores raw provider responses only. It does not approve their quality,
declare them canonical, or make them safe for same-session backtests. Tokens are
read from the process environment by ``Tushare2kAdapter``; this command never
loads ``.env`` or prints credential values.

Examples:
    python scripts/fetch_tushare_dependency.py --endpoint daily_basic \
      --filter trade_date=20261030
    python scripts/fetch_tushare_dependency.py --endpoint income \
      --filter ts_code=600000.SH --filter start_date=20200101 \
      --filter end_date=20261031
    python scripts/fetch_tushare_dependency.py --endpoint daily_basic \
      --range-start 2025-01-01 --range-end 2025-12-31 --max-requests 367
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from uq.adapters.tushare import TUSHARE_RESEARCH_ENDPOINTS
from uq.adapters.tushare_2k import Tushare2kAdapter
from uq.contracts.tushare_dependency import TushareDependencyStore
from uq.errors import ContractError

_DATE_RANGE_ENDPOINTS = frozenset({"daily_basic", "moneyflow", "hk_hold", "top_list"})
DEFAULT_STORE_ROOT = Path("data/trading-grade-history/source")


def parse_filters(items: list[str]) -> dict[str, str]:
    filters: dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key.strip() or not value.strip():
            raise ContractError("each --filter must be a non-empty KEY=VALUE pair")
        key = key.strip()
        if key in filters:
            raise ContractError(f"duplicate query filter: {key}")
        filters[key] = value.strip()
    return filters


def _redact(error: str, tokens: list[str]) -> str:
    for token in tokens:
        if token:
            error = error.replace(token, "[redacted]")
    return error


def _write_summary(path: Path | None, report: dict[str, Any]) -> None:
    text = json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    if path is None:
        print(text, end="")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(json.dumps({"summary": str(path), "status": report["status"]}, ensure_ascii=False))


def _one_query(
    adapter: Tushare2kAdapter,
    store: TushareDependencyStore,
    endpoint: str,
    query: dict[str, Any],
    *,
    refresh: bool,
) -> dict[str, Any]:
    if not refresh:
        existing = store.existing_generation_for_query(endpoint, query)
        if existing is not None:
            return {
                "status": "already_present",
                "query": query,
                "generation_id": existing["generation_id"],
                "row_count": existing["row_count"],
                "data_checksum_sha256": existing["data_artifact"]["checksum_sha256"],
            }
    frame = adapter.research_data(endpoint, **query)
    fetched_at = dt.datetime.now(dt.timezone.utc)
    manifest = store.publish(
        endpoint,
        query=query,
        source_fetched_at=fetched_at,
        frame=frame,
    )
    return {
        "status": "published",
        "query": query,
        "generation_id": manifest["generation_id"],
        "row_count": manifest["row_count"],
        "data_checksum_sha256": manifest["data_artifact"]["checksum_sha256"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, choices=sorted(TUSHARE_RESEARCH_ENDPOINTS))
    parser.add_argument("--store-root", type=Path, default=DEFAULT_STORE_ROOT)
    parser.add_argument("--filter", action="append", default=[], metavar="KEY=VALUE",
                        help="bounded Tushare query filter; repeat for multiple filters")
    parser.add_argument("--range-start", help="fetch each Tushare trading session in this date range")
    parser.add_argument("--range-end", help="fetch each Tushare trading session in this date range")
    parser.add_argument("--max-requests", type=int, default=0,
                        help="required request budget for a range fetch, including its calendar query")
    parser.add_argument("--session-interval-seconds", type=float, default=0.5)
    parser.add_argument("--refresh", action="store_true",
                        help="fetch even when an identical query already has a verified snapshot")
    parser.add_argument("--summary-out", type=Path,
                        help="optional run summary path; defaults to stdout")
    args = parser.parse_args(argv)

    if args.session_interval_seconds < 0:
        raise SystemExit("--session-interval-seconds must not be negative")

    adapter = Tushare2kAdapter()
    store = TushareDependencyStore(args.store_root)
    reports: list[dict[str, Any]] = []
    tokens = [value.strip() for value in os.environ.get("TUSHARE_2K_TOKEN", "").split(",") if value.strip()]
    try:
        range_requested = args.range_start is not None or args.range_end is not None
        if range_requested:
            if args.filter:
                raise ContractError("--filter cannot be combined with --range-start/--range-end")
            if args.endpoint not in _DATE_RANGE_ENDPOINTS:
                raise ContractError(
                    f"{args.endpoint} does not support per-session range fetches"
                )
            if args.range_start is None or args.range_end is None:
                raise ContractError("range fetch requires both --range-start and --range-end")
            start = dt.date.fromisoformat(args.range_start)
            end = dt.date.fromisoformat(args.range_end)
            if end < start:
                raise ContractError("range end precedes range start")
            if args.max_requests <= 0 or args.max_requests < (end - start).days + 1:
                raise ContractError(
                    "range fetch requires --max-requests at least as large as the calendar-day span"
                )
            sessions = sorted(adapter.trade_dates(start.isoformat(), end.isoformat()))
            if len(sessions) + 1 > args.max_requests:
                raise ContractError(
                    f"range requires {len(sessions) + 1} requests including trade_cal, "
                    f"over budget {args.max_requests}"
                )
            for index, session in enumerate(sessions):
                query = {"trade_date": session.strftime("%Y%m%d")}
                reports.append(_one_query(
                    adapter, store, args.endpoint, query, refresh=args.refresh
                ))
                if len(reports) % 50 == 0:
                    print(
                        f"{args.endpoint}: completed {len(reports)} of {len(sessions)} sessions",
                        file=sys.stderr,
                        flush=True,
                    )
                if index + 1 < len(sessions) and args.session_interval_seconds:
                    time.sleep(args.session_interval_seconds)
        else:
            if args.range_start is not None or args.range_end is not None:
                raise ContractError("date range requires both range endpoints")
            if args.max_requests not in (0, 1):
                raise ContractError("single-query fetch uses exactly one endpoint request")
            query = parse_filters(args.filter)
            reports.append(_one_query(
                adapter, store, args.endpoint, query, refresh=args.refresh
            ))
    except (ContractError, OSError, ValueError) as exc:
        report = {
            "record_version": 1,
            "kind": "tushare_dependency_fetch",
            "endpoint": args.endpoint,
            "store_root": str(args.store_root),
            "status": "failed",
            "completed_queries": reports,
            "error": _redact(str(exc), tokens),
        }
        _write_summary(args.summary_out, report)
        return 2

    report = {
        "record_version": 1,
        "kind": "tushare_dependency_fetch",
        "endpoint": args.endpoint,
        "store_root": str(args.store_root),
        "status": "published" if any(item["status"] == "published" for item in reports)
        else "already_present",
        "query_count": len(reports),
        "row_count": sum(item["row_count"] for item in reports),
        "results": reports,
    }
    _write_summary(args.summary_out, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
