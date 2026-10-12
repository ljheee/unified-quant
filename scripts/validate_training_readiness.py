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
from uq.adapters.tushare_free import _to_instrument
from uq.contracts.index_membership import IndexMembershipStore
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.pipeline.readiness import assess_readiness
from uq.pipeline.readiness import sessions_by_instrument
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


def _suspensions_for_gaps(
    adapter: Tushare2kAdapter,
    sessions: list[dt.date],
    members_by_session: dict[dt.date, list[str]],
    bars: pd.DataFrame,
    notes: dict[str, Any],
) -> dict[str, list[dt.date]]:
    """Query suspend_d for the sessions where at least one instrument lacks bars.

    A suspension only matters on a session some instrument is missing, so the
    gate pays one whole-market call per candidate session instead of one per
    window session. Consequence: ``suspension_bar_conflict`` can only fire on
    the sessions listed in the report notes.
    """
    present = sessions_by_instrument(bars)
    universe = {name for values in members_by_session.values() for name in values} | set(present)
    gap_days = [
        day
        for day in sessions
        if universe - {name for name, values in present.items() if day in values}
    ]
    if not gap_days:
        notes["suspension_provenance"] = "tushare:suspend_d(not-required)"
        notes["suspension_sessions"] = []
        return {}
    suspensions: dict[str, list[dt.date]] = {}
    for day in gap_days:
        frame = adapter.suspension(trade_date=day.strftime("%Y%m%d"))
        if frame.empty:
            continue
        for row in frame["ts_code"].astype(str):
            try:
                instrument = _to_instrument(row)
            except ValueError as exc:
                notes.setdefault("suspension_unmapped_codes", []).append(f"{row}: {exc}")
                continue
            suspensions.setdefault(instrument, []).append(day)
    notes["suspension_provenance"] = f"tushare:suspend_d({len(gap_days)} sessions)"
    notes["suspension_sessions"] = [day.isoformat() for day in gap_days]
    return suspensions


def _read_factor_partition_set(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"unreadable factor partition set {path}: {exc}") from exc


def factor_sessions_from_document(document: Any) -> list[dt.date]:
    """Session dates a declared factor partition set covers.

    Accepts the ``factor_partition_set`` binding document produced by
    ``uq.factors.partition_set.build_factor_partition_set`` (``partitions`` of
    ``partition_date``) or a bare list of ISO dates, so a caller that has not
    built the binding yet can still assert the linkage.
    """
    raw: Any = None
    if isinstance(document, dict):
        raw = document.get("partitions", document.get("factor_sessions"))
    elif isinstance(document, list):
        raw = document
    if not isinstance(raw, list) or not raw:
        raise SystemExit(
            "factor partition set must be a non-empty list or an object with 'partitions'/'factor_sessions'"
        )
    sessions: list[dt.date] = []
    for item in raw:
        value = item.get("partition_date") if isinstance(item, dict) else item
        if not isinstance(value, str):
            raise SystemExit(f"factor partition entry has no date: {item!r}")
        try:
            sessions.append(dt.date.fromisoformat(value))
        except ValueError as exc:
            raise SystemExit(f"factor partition date is not ISO: {value!r}") from exc
    return sorted(set(sessions))


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
    parser.add_argument(
        "--skip-suspensions",
        action="store_true",
        help="do not query suspend_d; bar gaps are then reported without an explained/unexplained verdict",
    )
    parser.add_argument(
        "--skip-delistings",
        action="store_true",
        help="do not query stock_basic(list_status=D); delisted members stay unexplained gaps",
    )
    parser.add_argument("--schema", type=Path, default=Path("config/schemas/bars_daily.v2.yaml"))
    parser.add_argument(
        "--factor-partition-set",
        type=Path,
        default=None,
        help=(
            "optional factor_partition_set binding JSON (or a bare list of dates): asserts the "
            "feature side never reaches past the last labelable decision date"
        ),
    )
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

    bars = _bars_for_sessions(ManifestFirstReader(args.bars_root), schema, sessions, notes)
    membership = _members_by_session(IndexMembershipStore(args.membership_root), args.universe_id, sessions, notes)
    suspensions: dict[str, list[dt.date]] | None = None
    if not args.skip_suspensions:
        suspensions = _suspensions_for_gaps(adapter, sessions, membership, bars, notes)
    delisted: set[str] | None = None
    delisted_provenance = "unspecified"
    if not args.skip_delistings:
        delisted = adapter.delisted_instruments()
        delisted_provenance = "tushare:stock_basic(list_status=D)"
        notes["delisted_universe_size"] = len(delisted)
    factor_sessions: list[dt.date] | None = None
    if args.factor_partition_set is not None:
        document = _read_factor_partition_set(args.factor_partition_set)
        factor_sessions = factor_sessions_from_document(document)
        notes["factor_partition_set"] = str(args.factor_partition_set)
        notes["factor_sessions"] = len(factor_sessions)
        if isinstance(document, dict) and document.get("set_digest_sha256"):
            notes["factor_partition_set_digest"] = document["set_digest_sha256"]

    report = assess_readiness(
        window_start=args.start,
        window_end=args.end,
        sessions=sessions,
        members_by_session=membership,
        bars=bars,
        min_sessions_per_instrument=args.min_sessions,
        label_horizon_sessions=args.label_horizon,
        max_null_rate=args.max_null_rate,
        suspensions=suspensions,
        suspension_provenance=str(notes.get("suspension_provenance", "unspecified")),
        delisted=delisted,
        delisted_provenance=delisted_provenance,
        factor_sessions=factor_sessions,
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
