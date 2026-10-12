from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from uq.adapters.tushare_2k import Tushare2kAdapter
from uq.adapters.tushare_free import _to_instrument
from uq.contracts.index_membership import IndexMembershipStore
from uq.errors import ContractError

# Conservative visibility lag in governed sessions.  Tushare's index_weight
# endpoint carries no visibility timestamp, so the true lag is bounded
# forward-only by scripts/measure_index_weight_lag.py (observations land in
# evidence/reference-data/index-weight-lag-observations.json).  The first
# observation (2026-10-12) bounds the 2026-09-30 snapshot at <=3 sessions and
# confirms the current month is unpublished intra-month, so 5 sessions stays
# above the measured bound and can only defer a snapshot, never expose it early.
PUBLISH_LAG_DAYS = 5
TUSHARE_ROW_LIMIT = 7000
PRESETS = {
    "a500": {"index_code": "000510.SH", "universe_id": "a500", "members": 500},
    "csi-all": {"index_code": "000985.CSI", "universe_id": "csi-all", "members": None},
}


def to_instrument(ts_code: str) -> str:
    return _to_instrument(ts_code)


def month_ranges(start: dt.date, end: dt.date) -> list[tuple[str, str]]:
    ranges: list[tuple[str, str]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        first = dt.date(year, month, 1)
        next_first = dt.date(year + (month == 12), month % 12 + 1, 1)
        last = next_first - dt.timedelta(days=1)
        ranges.append((first.strftime("%Y%m%d"), last.strftime("%Y%m%d")))
        year, month = next_first.year, next_first.month
    return ranges


def fetch_month(adapter: Tushare2kAdapter, index_code: str, start: str, end: str) -> pd.DataFrame:
    raw = adapter.index_weight(index_code, start, end)
    if len(raw) >= TUSHARE_ROW_LIMIT:
        raise ContractError(f"index_weight response for {index_code} {start}-{end} may be truncated")
    if raw.empty:
        return raw
    return raw.assign(instrument=raw["con_code"].map(to_instrument))


def group_snapshots(raw: pd.DataFrame, expected_members: int | None = 500) -> dict[str, pd.DataFrame]:
    snapshots: dict[str, pd.DataFrame] = {}
    if raw.empty:
        return snapshots
    for trade_date, group in raw.groupby("trade_date"):
        if group["instrument"].duplicated().any():
            raise ContractError(f"duplicate instrument within publish date {trade_date}")
        if expected_members is not None and len(group) != expected_members:
            raise ContractError(
                f"publish date {trade_date} has {len(group)} members, expected {expected_members}"
            )
        snapshots[str(trade_date)] = group[["instrument"]].reset_index(drop=True)
    return snapshots


def main() -> int:
    parser = argparse.ArgumentParser(description="publish A500 monthly index membership snapshots (approximate PIT)")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD, first month to fetch")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD, last month to fetch")
    parser.add_argument("--store", type=Path, required=True, help="snapshot store root (raw artifact root)")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="a500", help="index to fetch")
    args = parser.parse_args()
    preset = PRESETS[args.preset]
    index_code, universe_id = preset["index_code"], preset["universe_id"]
    source = f"tushare:index_weight:{index_code}"

    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    adapter = Tushare2kAdapter()
    fetched_at = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    calendar = sorted(adapter.trade_dates(start.isoformat(), (end + dt.timedelta(days=60)).isoformat()))

    store = IndexMembershipStore(args.store)
    published: list[str] = []
    skipped: list[str] = []
    for month_start, month_end in month_ranges(start, end):
        raw = fetch_month(adapter, index_code, month_start, month_end)
        for snapshot_key, members in group_snapshots(raw, preset["members"]).items():
            snapshot_date = dt.date.fromisoformat(f"{snapshot_key[:4]}-{snapshot_key[4:6]}-{snapshot_key[6:]}")
            try:
                store.publish(
                    universe_id=universe_id, index_code=index_code, source=source,
                    source_fetched_at=fetched_at, snapshot_date=snapshot_date,
                    publish_lag_days=PUBLISH_LAG_DAYS, trading_days=calendar, members=members,
                )
                published.append(snapshot_date.isoformat())
            except ContractError as exc:
                if "immutable" not in str(exc):
                    raise
                skipped.append(snapshot_date.isoformat())
    print(json.dumps({"published": published, "already_present": skipped, "store": str(args.store)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
