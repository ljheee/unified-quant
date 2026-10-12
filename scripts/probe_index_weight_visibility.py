#!/usr/bin/env python3
"""Observe the publication lag of Tushare ``index_weight`` month-end snapshots.

Tushare's ``index_weight`` endpoint exposes no visibility/update timestamp and
returns only one snapshot per month (at the month-end trade date), so the lag
between a snapshot's trade date and the moment it becomes queryable cannot be
read off the payload and cannot be reconstructed retroactively.  It can only be
*observed forward in time*: run this probe once per trading day and it appends
one JSONL record per newly seen ``(trade_date, fingerprint)`` pair.  ``event`` is
``first_seen`` the first time a snapshot appears and ``content_changed`` when
Tushare revises a snapshot it had already published, so revisions are visible
too.

Every run also prints the per-snapshot lag upper bound: the number of governed
sessions between a snapshot's trade date and the first observation that already
returned it.  ``PUBLISH_LAG_DAYS`` in ``scripts/fetch_index_membership.py`` must
stay at or above that bound, otherwise a snapshot could be treated as visible
before Tushare actually publishes it.

Usage:
    python scripts/probe_index_weight_visibility.py --log evidence/reference-data/index-weight-visibility-log.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

from uq.adapters.tushare_2k import Tushare2kAdapter

TZ = dt.timezone(dt.timedelta(hours=8))


def load_env_file(path: Path) -> None:
    """Populate ``os.environ`` from a simple ``KEY=VALUE`` file without python-dotenv."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def snapshots(adapter: Tushare2kAdapter, index_code: str, start: str, end: str) -> dict[str, str]:
    """Fingerprint every returned snapshot so revisions are detectable."""
    raw = adapter.index_weight(index_code, start, end)
    result: dict[str, str] = {}
    if raw is None or raw.empty:
        return result
    for trade_date, group in raw.groupby("trade_date"):
        rows = sorted(zip(group["con_code"], group["weight"].round(6)))
        result[str(trade_date)] = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
    return result


def sessions_between(start: dt.date, end: dt.date, trading_days: list[dt.date]) -> int:
    """Governed sessions strictly after ``start`` and up to ``end``."""
    return sum(1 for day in trading_days if start < day <= end)


def summarize(records: list[dict[str, object]], trading_days: list[dt.date]) -> dict[str, object]:
    first_seen: dict[str, dict[str, object]] = {}
    for record in records:
        trade_date = str(record["trade_date"])
        if trade_date in first_seen:
            continue
        day = dt.date.fromisoformat(f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}")
        first_seen[trade_date] = {
            "snapshot_date": trade_date,
            "first_seen_on": record["observed_on"],
            "lag_sessions_upper_bound": sessions_between(
                day, dt.date.fromisoformat(str(record["observed_on"])), trading_days
            ),
        }
    return {
        "record_count": len(records),
        "per_snapshot": sorted(first_seen.values(), key=lambda item: item["snapshot_date"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-code", default="000510.SH")
    parser.add_argument("--start", default=None, help="YYYYMMDD; defaults to --months before today")
    parser.add_argument("--end", default=None, help="YYYYMMDD; defaults to today")
    parser.add_argument("--months", type=int, default=4, help="scan depth when --start is omitted")
    parser.add_argument("--log", type=Path, required=True, help="append-only JSONL observation log")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--observed-on", default=None, help="YYYY-MM-DD override (testing)")
    args = parser.parse_args()

    load_env_file(args.env_file)
    adapter = Tushare2kAdapter()
    today = dt.date.fromisoformat(args.observed_on) if args.observed_on else dt.date.today()
    start = args.start or (today - dt.timedelta(days=31 * args.months)).strftime("%Y%m%d")
    end = args.end or today.strftime("%Y%m%d")
    observed_on = today.isoformat()
    observed_at = dt.datetime.now(TZ).isoformat(timespec="seconds")

    current = snapshots(adapter, args.index_code, start, end)
    seen: dict[str, str] = {}
    if args.log.is_file():
        for line in args.log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                seen.setdefault(record["trade_date"], record["fingerprint"])

    new_records: list[dict[str, object]] = []
    for trade_date, fingerprint in sorted(current.items()):
        if seen.get(trade_date) == fingerprint:
            continue
        new_records.append({
            "observed_at": observed_at,
            "observed_on": observed_on,
            "index_code": args.index_code,
            "trade_date": trade_date,
            "fingerprint": fingerprint,
            "event": "first_seen" if trade_date not in seen else "content_changed",
        })
    if new_records:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        with args.log.open("a", encoding="utf-8") as handle:
            for record in new_records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")

    records: list[dict[str, object]] = []
    if args.log.is_file():
        records = [json.loads(line) for line in args.log.read_text(encoding="utf-8").splitlines() if line.strip()]
    observed_dates = sorted({str(record["trade_date"]) for record in records})
    if observed_dates:
        first = dt.date.fromisoformat(
            f"{observed_dates[0][:4]}-{observed_dates[0][4:6]}-{observed_dates[0][6:]}"
        )
        trading_days = sorted(adapter.trade_dates(first.isoformat(), observed_on))
    else:
        trading_days = []
    summary = summarize(records, trading_days)

    latest = max(current) if current else None
    current_month = today.replace(day=1).strftime("%Y%m%d")
    print(json.dumps({
        "observed_at": observed_at,
        "observed_on": observed_on,
        "index_code": args.index_code,
        "latest_available_snapshot_date": latest,
        "current_month_snapshot_available": any(day >= current_month for day in current),
        "new_records": len(new_records),
        "summary": summary,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
