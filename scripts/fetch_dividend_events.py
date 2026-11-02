#!/usr/bin/env python3
"""Publish Tushare dividend disclosures as immutable, announcement-anchored records."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

from uq.adapters.tushare_2k import Tushare2kAdapter
from uq.contracts.dividend_events import DividendEventStore, normalize_dividend_event_rows, render_events_csv
from uq.errors import ContractError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD, first announcement date")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD, last announcement date")
    parser.add_argument("--store", type=Path, required=True, help="dividend event store root")
    parser.add_argument(
        "--checkpoint",
        type=Path,
        help="atomically persist query coverage after each successful day and resume an interrupted window",
    )
    args = parser.parse_args()
    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    if start > end:
        raise SystemExit("start date exceeds end date")
    if args.checkpoint is not None and args.checkpoint.is_symlink():
        raise SystemExit("--checkpoint must not be a symlink")
    adapter = Tushare2kAdapter()
    store = DividendEventStore(args.store)
    published: list[str] = []
    skipped: list[str] = []
    drifted: list[str] = []
    empty: list[str] = []
    queried_days = 0
    last_completed_date: dt.date | None = None
    day = start

    if args.checkpoint is not None and args.checkpoint.exists():
        if args.checkpoint.is_symlink():
            raise SystemExit("--checkpoint must not be a symlink")
        try:
            previous = json.loads(args.checkpoint.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"unreadable dividend fetch checkpoint: {exc}") from exc
        if (
            previous.get("status") == "running"
            and previous.get("query_window") == {"start": start.isoformat(), "end": end.isoformat()}
            and previous.get("store") == str(args.store)
        ):
            last_value = previous.get("last_completed_date")
            if last_value is not None:
                last_completed_date = dt.date.fromisoformat(last_value)
                if not start <= last_completed_date <= end:
                    raise SystemExit("checkpoint last_completed_date is outside the requested window")
                day = max(day, last_completed_date + dt.timedelta(days=1))
            queried_days = int(previous.get("queried_days", 0))
            empty = list(previous.get("empty_days", []))
            published = list(previous.get("published", []))
            skipped = list(previous.get("already_present", []))
            drifted = list(previous.get("content_drift", []))

    def report(status: str) -> dict[str, object]:
        return {
            "status": status,
            "query_window": {"start": start.isoformat(), "end": end.isoformat()},
            "queried_days": queried_days,
            "last_completed_date": (
                last_completed_date.isoformat() if last_completed_date is not None else None
            ),
            "empty_days": empty,
            "published": published,
            "already_present": skipped,
            "content_drift": drifted,
            "store": str(args.store),
        }

    def persist_checkpoint() -> None:
        if args.checkpoint is None:
            return
        args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.checkpoint.with_name(
            f".{args.checkpoint.name}.tmp.{uuid.uuid4().hex}"
        )
        temporary.write_text(
            json.dumps(report("running"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, args.checkpoint)

    while day <= end:
        frame = adapter.dividend(ann_date=day.isoformat())
        queried_days += 1
        last_completed_date = day
        # each partition is stamped with its own fetch time, not the run's start
        fetched_at = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
        if frame.empty:
            empty.append(day.isoformat())
        else:
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
        persist_checkpoint()
        day += dt.timedelta(days=1)
    final_report = report("completed")
    if args.checkpoint is not None:
        temporary = args.checkpoint.with_name(
            f".{args.checkpoint.name}.tmp.{uuid.uuid4().hex}"
        )
        temporary.write_text(
            json.dumps(final_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, args.checkpoint)
    print(json.dumps(final_report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
