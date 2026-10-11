"""Fail-closed pre-training readiness check over governed inputs (pendding item 5).

Reads PIT-visible membership from an IndexMembershipStore and canonical
bars_daily partitions through the manifest-first reader, then evaluates the
cross-input conditions a training run silently depends on. Exit code 0 means the
window is trainable; 1 means at least one rule blocked it.

Caveat: the trading calendar comes from the same vendor as the primary bars, so
``bar_session_gap`` proves internal consistency, not vendor-independent session
coverage. See pendding.md ("v2 链路尚无独立交易日历").
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd

from uq.adapters.tushare_2k import Tushare2kAdapter
from uq.contracts.index_membership import IndexMembershipStore
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.pipeline.readiness import assess_readiness
from uq.store.reader import ManifestFirstReader


def _members_by_session(
    store: IndexMembershipStore,
    universe_id: str,
    sessions: list[dt.date],
    notes: dict[str, Any],
) -> dict[dt.date, list[str]]:
    visible: dict[dt.date, list[str]] = {}
    unavailable: list[str] = []
    for session in sessions:
        try:
            _, members = store.as_of(universe_id, session)
        except (ContractError, OSError, ValueError) as exc:
            unavailable.append(f"{session.isoformat()}: {exc}")
            continue
        visible[session] = sorted(str(value) for value in members["instrument"])
    if unavailable:
        notes["membership_unavailable_sessions"] = unavailable
    return visible


def _bars_for_sessions(
    reader: ManifestFirstReader,
    schema,
    sessions: list[dt.date],
    notes: dict[str, Any],
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    missing: list[str] = []
    for session in sessions:
        try:
            frames.append(reader.read(schema, session))
        except (ContractError, OSError, ValueError) as exc:
            missing.append(f"{session.isoformat()}: {exc}")
    if missing:
        notes["bar_partitions_unreadable"] = missing
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="validate-training-readiness")
    parser.add_argument("--bars-root", type=Path, required=True, help="canonical store root")
    parser.add_argument("--membership-root", type=Path, required=True, help="index membership store root")
    parser.add_argument("--universe-id", required=True)
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--min-sessions", type=int, default=60)
    parser.add_argument("--label-horizon", type=int, default=5)
    parser.add_argument("--max-null-rate", type=float, default=0.0)
    parser.add_argument("--schema", type=Path, default=Path("config/schemas/bars_daily.v2.yaml"))
    parser.add_argument("--exchange", default="SSE")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    schema = load_schema(args.schema)
    adapter = Tushare2kAdapter()
    notes: dict[str, Any] = {"calendar_provenance": f"tushare:trade_cal:{args.exchange}"}
    try:
        sessions = sorted(adapter.trade_dates(args.start.isoformat(), args.end.isoformat(), exchange=args.exchange))
    except ContractError as exc:
        print(json.dumps({"status": "configuration_failure", "error": str(exc)}, ensure_ascii=False))
        return 2

    report = assess_readiness(
        window_start=args.start,
        window_end=args.end,
        sessions=sessions,
        members_by_session=_members_by_session(IndexMembershipStore(args.membership_root), args.universe_id, sessions, notes),
        bars=_bars_for_sessions(ManifestFirstReader(args.bars_root), schema, sessions, notes),
        min_sessions_per_instrument=args.min_sessions,
        label_horizon_sessions=args.label_horizon,
        max_null_rate=args.max_null_rate,
    )
    payload = report.to_json()
    payload["universe_id"] = args.universe_id
    payload["notes"] = notes
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": payload["status"],
                "sessions": payload["sessions"],
                "members": payload["members"],
                "instruments_with_bars": payload["instruments_with_bars"],
                "usable_label_end": payload["usable_label_end"],
                "finding_codes": sorted({item["code"] for item in payload["findings"]}),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
