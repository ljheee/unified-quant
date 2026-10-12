#!/usr/bin/env python3
"""Measure the empirical publication lag of Tushare ``index_weight`` snapshots.

Tushare's ``index_weight`` endpoint exposes no visibility/update timestamp, so the
lag between a snapshot's ``trade_date`` and the moment that snapshot becomes
queryable cannot be read off the payload and cannot be reconstructed
retroactively.  It can only be *observed forward in time*: run this probe once per
trading day and it records, for each observation, which snapshot was the newest
one the API already returned.

From a series of observations the lag is bounded precisely: a snapshot S with
trade date D became available no later than the first observation whose
``latest_available_snapshot_date`` is >= S, i.e. within that many governed
sessions after D.

Usage:
    python scripts/measure_index_weight_lag.py --env-file .env
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path

from uq.adapters.tushare_2k import Tushare2kAdapter

DEFAULT_LOG = Path("evidence/reference-data/index-weight-lag-observations.json")


def load_env_file(path: Path) -> None:
    """Populate ``os.environ`` from a simple ``KEY=VALUE`` file without python-dotenv."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def sessions_between(start: dt.date, end: dt.date, trading_days: list[dt.date]) -> int:
    """Number of governed sessions strictly after ``start`` and up to ``end``."""
    return sum(1 for day in trading_days if start < day <= end)


def latest_snapshot(adapter: Tushare2kAdapter, index_code: str, start: dt.date, end: dt.date) -> str | None:
    raw = adapter.index_weight(index_code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if raw is None or raw.empty:
        return None
    return max(str(value) for value in raw["trade_date"].unique())


def summarize(observations: list[dict[str, object]], trading_days: list[dt.date]) -> dict[str, object]:
    """Per-snapshot first-seen bounds and the tightest observed lag."""
    first_seen: dict[str, dict[str, object]] = {}
    for observation in sorted(observations, key=lambda item: item["observed_at"]):
        snapshot = observation.get("latest_available_snapshot_date")
        if not snapshot or snapshot in first_seen:
            continue
        first_seen[snapshot] = {
            "snapshot_date": snapshot,
            "first_seen_at": observation["observed_at"],
            "lag_sessions_upper_bound": sessions_between(
                dt.date.fromisoformat(f"{snapshot[:4]}-{snapshot[4:6]}-{snapshot[6:]}"),
                dt.date.fromisoformat(str(observation["observed_on"])),
                trading_days,
            ),
        }
    return {
        "observation_count": len(observations),
        "per_snapshot": sorted(first_seen.values(), key=lambda item: item["snapshot_date"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-code", default="000510.SH")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--months", type=int, default=4, help="how many past months to scan")
    parser.add_argument("--observed-on", default=None, help="YYYY-MM-DD override (testing)")
    args = parser.parse_args()

    load_env_file(args.env_file)
    adapter = Tushare2kAdapter()
    today = dt.date.fromisoformat(args.observed_on) if args.observed_on else dt.date.today()
    observed_at = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()

    scan_start = today - dt.timedelta(days=31 * args.months)
    snapshot = latest_snapshot(adapter, args.index_code, scan_start, today)
    current_month_start = today.replace(day=1)
    current_month_snapshot = latest_snapshot(
        adapter, args.index_code, current_month_start, today
    )
    trading_days = sorted(adapter.trade_dates(scan_start.isoformat(), today.isoformat()))

    observation = {
        "observed_at": observed_at,
        "observed_on": today.isoformat(),
        "index_code": args.index_code,
        "scan_start": scan_start.isoformat(),
        "latest_available_snapshot_date": snapshot,
        "current_month_snapshot_available": current_month_snapshot is not None,
    }

    log: dict[str, object] = {"index_code": args.index_code, "observations": []}
    if args.log.is_file():
        log = json.loads(args.log.read_text(encoding="utf-8"))
    observations = [item for item in log.get("observations", []) if item.get("observed_on") != today.isoformat()]
    observations.append(observation)
    log = {
        "index_code": args.index_code,
        "note": (
            "index_weight exposes no visibility timestamp; the lag is bounded forward-only "
            "as the session count between a snapshot trade_date and the first observation "
            "that already returned it."
        ),
        "observations": observations,
        "summary": summarize(observations, trading_days),
    }
    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.log.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(observation, indent=2, ensure_ascii=False))
    print(json.dumps(log["summary"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
