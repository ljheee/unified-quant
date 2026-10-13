#!/usr/bin/env python3
"""Publish Tushare dividend disclosures as immutable, announcement-anchored records."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

from uq.adapters.tushare_2k import Tushare2kAdapter
from uq.contracts.dividend_events import DividendEventStore, normalize_dividend_event_rows, render_events_csv
from uq.errors import ContractError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD, first announcement date")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD, last announcement date")
    parser.add_argument("--store", type=Path, required=True, help="dividend event store root")
    args = parser.parse_args()
    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    if start > end:
        raise SystemExit("start date exceeds end date")
    adapter = Tushare2kAdapter()
    store = DividendEventStore(args.store)
    published: list[str] = []
    skipped: list[str] = []
    drifted: list[str] = []
    day = start
    while day <= end:
        frame = adapter.dividend(ann_date=day.isoformat())
        # each partition is stamped with its own fetch time, not the run's start
        fetched_at = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
        if not frame.empty:
            try:
                store.publish(
                    ann_date=day,
                    source="tushare:dividend",
                    source_fetched_at=fetched_at,
                    frame=frame,
                )
                published.append(day.isoformat())
            except ContractError as exc:
                if "immutable" not in str(exc):
                    raise
                # The partition stays frozen (immutability stands), but the
                # provider may have revised the rows since publication: compare
                # the freshly normalized bytes and warn on drift instead of
                # skipping silently.
                document = store.read_manifest(day)
                fresh_checksum = hashlib.sha256(
                    render_events_csv(normalize_dividend_event_rows(frame, ann_date=day))
                ).hexdigest()
                if fresh_checksum != document["events_artifact"]["checksum_sha256"]:
                    drifted.append(day.isoformat())
                    print(
                        f"WARNING: provider revised already-published partition {day.isoformat()}; "
                        "the immutable store keeps the originally published bytes",
                        file=sys.stderr,
                    )
                skipped.append(day.isoformat())
        day += dt.timedelta(days=1)
    print(
        json.dumps(
            {
                "published": published,
                "already_present": skipped,
                "content_drift": drifted,
                "store": str(args.store),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
