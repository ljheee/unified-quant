#!/usr/bin/env python3
"""Fetch the SW2021 industry catalog and its effective-date membership history."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any

from uq.adapters.tushare_2k import Tushare2kAdapter
from uq.contracts.tushare_dependency import TushareDependencyStore
from uq.errors import ContractError

DEFAULT_STORE_ROOT = Path("data/trading-grade-history/source")


def _redact(error: str) -> str:
    for token in os.environ.get("TUSHARE_2K_TOKEN", "").split(","):
        if token.strip():
            error = error.replace(token.strip(), "[redacted]")
    return error


def _record(
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
                "generation_id": existing["generation_id"],
                "row_count": existing["row_count"],
                "query": query,
            }
    frame = adapter.research_data(endpoint, **query)
    manifest = store.publish(
        endpoint,
        query=query,
        source_fetched_at=dt.datetime.now(dt.timezone.utc),
        frame=frame,
    )
    return {
        "status": "published",
        "generation_id": manifest["generation_id"],
        "row_count": manifest["row_count"],
        "query": query,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-root", type=Path, default=DEFAULT_STORE_ROOT)
    parser.add_argument("--max-requests", type=int, required=True,
                        help="budget including catalog lookup and each industry query")
    parser.add_argument("--request-interval-seconds", type=float, default=0.5)
    parser.add_argument("--refresh", action="store_true",
                        help="fetch queries again even when verified snapshots exist")
    parser.add_argument("--summary-out", type=Path)
    args = parser.parse_args(argv)
    if args.max_requests < 1:
        raise SystemExit("--max-requests must be positive")
    if args.request_interval_seconds < 0:
        raise SystemExit("request interval must not be negative")

    adapter = Tushare2kAdapter()
    store = TushareDependencyStore(args.store_root)
    completed: list[dict[str, Any]] = []
    try:
        catalog_query = {"level": "L3", "src": "SW2021"}
        catalog_result = _record(
            adapter, store, "index_classify", catalog_query, refresh=args.refresh
        )
        completed.append(catalog_result)
        catalog_manifest = store.read_manifest(
            catalog_result["generation_id"], dataset="index_classify"
        )
        _, catalog = store.read_frame(
            catalog_manifest["generation_id"], dataset="index_classify"
        )
        industry_codes = sorted(set(catalog["index_code"].dropna().astype(str)))
        queries = [
            (industry_code, is_new)
            for industry_code in industry_codes
            for is_new in ("Y", "N")
        ]
        if len(queries) + 1 > args.max_requests:
            raise ContractError(
                f"SW2021 catalog and current/historical members need {len(queries) + 1} requests, "
                f"over budget {args.max_requests}"
            )
        for index, (industry_code, is_new) in enumerate(queries):
            query = {"l3_code": industry_code, "is_new": is_new}
            completed.append(_record(
                adapter, store, "index_member_all", query, refresh=args.refresh
            ))
            if index + 1 < len(queries) and args.request_interval_seconds:
                time.sleep(args.request_interval_seconds)
        status = "published" if any(item["status"] == "published" for item in completed) else "already_present"
        error = None
    except (ContractError, OSError, ValueError) as exc:
        status = "failed"
        error = _redact(str(exc))
        industry_codes = []

    report = {
        "record_version": 1,
        "kind": "tushare_industry_history_fetch",
        "taxonomy": "SW2021",
        "store_root": str(args.store_root),
        "industry_count": len(industry_codes),
        "completed_queries": len(completed),
        "published_queries": sum(item["status"] == "published" for item in completed),
        "already_present_queries": sum(item["status"] == "already_present" for item in completed),
        "row_count": sum(item["row_count"] for item in completed),
        "results": completed,
        "status": status,
    }
    if error is not None:
        report["error"] = error
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.summary_out is None:
        print(text, end="")
    else:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.summary_out.write_text(text, encoding="utf-8")
        print(json.dumps({"summary": str(args.summary_out), "status": status}, ensure_ascii=False))
    return 0 if status in {"published", "already_present"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
