from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd

from uq.adapters.tushare_2k import Tushare2kAdapter
from uq.adapters.tushare_free import _to_instrument
from uq.contracts.index_membership import IndexMembershipStore
from uq.errors import ContractError

DATASETS = ("adj_factor", "stk_limit", "suspension", "dividend")


def normalize(raw: pd.DataFrame, *, label: str) -> pd.DataFrame:
    """Map Tushare codes to canonical instruments and reject unknown codes."""
    if raw.empty:
        return raw.assign(instrument=pd.Series([], dtype=str, index=raw.index))
    try:
        instruments = raw["ts_code"].map(_to_instrument)
    except ValueError as exc:
        raise ContractError(f"{label} uses unsupported Tushare codes: {exc}") from exc
    return raw.assign(instrument=instruments)


def dataset_coverage(universe: set[str], frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for name, frame in frames.items():
        present = set(frame["instrument"].astype(str))
        covered = universe & present
        nulls = int(frame["ts_code"].isna().sum()) if not frame.empty else 0
        entry: dict[str, Any] = {
            "rows": int(len(frame)),
            "universe_covered": len(covered),
            "universe_size": len(universe),
            "coverage": round(len(covered) / len(universe), 6) if universe else None,
            "null_keys": nulls,
        }
        if name == "adj_factor" and not frame.empty:
            factors = pd.to_numeric(frame["adj_factor"], errors="coerce")
            entry["adj_factor_nonpositive"] = int((factors <= 0).sum())
            entry["adj_factor_null"] = int(factors.isna().sum())
        if name == "suspension":
            entry["suspended_in_universe"] = len(covered)
        report[name] = entry
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="probe Tushare reference-data coverage for an index universe")
    parser.add_argument("--store", type=Path, required=True, help="index membership store root")
    parser.add_argument("--universe-id", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--calendar", type=Path, help="JSON list of YYYY-MM-DD trading days")
    parser.add_argument("--sample-days", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.calendar is None:
        raise SystemExit("--calendar is required so coverage is measured on governed trading days")
    calendar = [dt.date.fromisoformat(value) for value in json.loads(args.calendar.read_text())]
    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    window = [day for day in calendar if start <= day <= end]
    if not window:
        raise SystemExit("calendar has no trading days inside the requested window")
    sampled = window if args.sample_days <= 0 else window[:: max(1, len(window) // args.sample_days)][:args.sample_days]

    store = IndexMembershipStore(args.store)
    adapter = Tushare2kAdapter()
    days: dict[str, Any] = {}
    for day in sampled:
        stamp = day.strftime("%Y%m%d")
        _, members = store.as_of(args.universe_id, day)
        universe = set(members["instrument"].astype(str))
        frames = {
            "adj_factor": normalize(adapter.adj_factor(trade_date=stamp), label="adj_factor"),
            "stk_limit": normalize(adapter.stk_limit(trade_date=stamp), label="stk_limit"),
            "suspension": normalize(adapter.suspension(trade_date=stamp), label="suspend_d"),
            "dividend": normalize(adapter.dividend(ann_date=stamp), label="dividend"),
        }
        days[day.isoformat()] = dataset_coverage(universe, frames)

    summary = {
        "universe_id": args.universe_id,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "sampled_days": [day.isoformat() for day in sampled],
        "coverage_by_day": days,
        "note": "suspension and dividend are event datasets; low row counts are expected and are not a defect",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"sampled_days": len(sampled), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
