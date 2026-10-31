#!/usr/bin/env python3
"""Publish window-level market inputs bound by versioned backtest configs.

`BacktestMarketDatasetStore.read_frame` had no writer: only
`tests/test_research_chain_phase4.py` ever produced its
`backtest_market_data/dataset=<generation>` layout, so no real run could supply
the calendar, suspension or corporate-action input that backtest configs
reference as one `{generation_id, checksum_sha256}` pair per family
(`specs/layers/paper-execution-research-integration-plan.md` phase 0.5 names this
gap: "They exist as binding names only ... Real runs need publishers").

This is that publisher.  Every input is derived from already-governed artifacts
in the same data root -- no new vendor, no live API call:

  * calendar        - the sessions present in `canonical/bars_daily/v2`, i.e. the
                      sessions the price panel itself is built from.  Deriving the
                      calendar from the price source (rather than a second vendor
                      calendar) is what makes the backtest's
                      `set(price_dates) == set(calendar_dates)` invariant hold by
                      construction instead of by reconciliation.
  * suspension      - per-session rows from the bars `status` column, which
                      `specs/layers/paper-execution-research-integration-plan.md`
                      names as the *primary* source; Tushare `suspend_d` is
                      recorded as reconciliation provenance, never a second truth.
  * corporate_action - per-ex-date instruments from the dividend disclosure
                      store's implemented records, the same `DividendEventStore`
                      the adjustment chain derives factors from.

Each family is one immutable whole-window artifact, matching the owning config
contract. v4 adds a detailed dividend-event panel and a complete-window
rights-issue exclusion attestation. This is deliberately *not* the paper path's
per-session `partition_set` layout, so this script does not write
`market-datasets/`; see `docs/subsystems/research-chain.md`.

Usage:
  python scripts/publish_backtest_market_data.py \
      --data-root data/bars-a500-store \
      --dividend-root data/dividend-store \
      --start 2025-08-29 --end 2025-09-30 \
      --out evidence/reference-data/backtest-market-inputs-....json

Exit 1 if any family fails; already-published windows are recorded as skipped so
an interrupted run resumes by re-running the same command.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from uq.contracts.dividend_events import DividendEventStore
from uq.contracts.gate_contracts import sha256_json
from uq.errors import ContractError
from uq.research_chain.owning_contracts import BacktestMarketDatasetStore
from uq.backtest.corporate_actions import (
    ACTION_EVENT_COLUMNS,
    dividend_coverage_row,
    normalize_action_events,
    rights_issue_coverage_row,
)

PRODUCER = "scripts/publish_backtest_market_data.py"


def _canonical_sessions(data_root: Path, start: dt.date, end: dt.date) -> list[dt.date]:
    base = data_root / "canonical" / "bars_daily" / "v2"
    if not base.is_dir():
        raise SystemExit(f"missing canonical bars root: {base}")
    sessions = sorted(
        dt.date.fromisoformat(child.name.split("=", 1)[1])
        for child in base.iterdir()
        if child.is_dir() and child.name.startswith("date=")
    )
    window = [session for session in sessions if start <= session <= end]
    if not window:
        raise SystemExit(f"no canonical bars partitions in {start}..{end}")
    return window


def _canonical_frame(data_root: Path, session: dt.date) -> pd.DataFrame:
    directory = data_root / "canonical" / "bars_daily" / "v2" / f"date={session.isoformat()}"
    if not (directory / "manifest.json").is_file():
        raise SystemExit(f"no canonical bars partition for {session}")
    return pd.read_parquet(directory / "data.parquet")


def _calendar_frame(sessions: list[dt.date]) -> pd.DataFrame:
    """One row per session the price panel is built from.

    The column is `date`, which is what `BacktestStageAdapter` requires of the
    calendar (`required_columns={"date"}`); the canonical store's own
    `trading_calendar_snapshot_v1` uses `session_date`, so the two layouts are not
    interchangeable by construction.
    """
    return pd.DataFrame(
        [{"date": session.isoformat(), "instrument": "CALENDAR"} for session in sessions],
        columns=["date", "instrument"],
    )


def _suspension_frame(data_root: Path, sessions: list[dt.date]) -> pd.DataFrame:
    """Per-session suspended instruments from the bars `status` column.

    `status` is the primary source; anything that is not `trading` (including
    `delisted` and `unknown`) is recorded as suspended so the backtest cannot
    trade it, which is the conservative direction.

    A name the universe carries but the session's panel does not carry at all is
    recorded as suspended too.  The bars partitions are per-session and only
    contain rows the vendor returned, so a genuinely suspended or otherwise
    absent name simply has no row -- and a `status`-only read is blind to it.
    Without this the backtest marks to market a holding it has no price for and
    refuses the whole run at the last stage.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for session in sessions:
        frame = _canonical_frame(data_root, session)
        status = frame["status"].astype(str) if "status" in frame else None
        if status is None:
            raise SystemExit(f"canonical bars partition {session} has no status column")
        present = set(frame["instrument"].astype(str))
        seen |= present
        suspended = frame.loc[status != "trading", ["instrument"]].copy()
        suspended["date"] = session.isoformat()
        suspended["suspended"] = True
        rows.extend(suspended.to_dict(orient="records"))
    # Absent-for-the-whole-window names are not part of this universe and are not
    # recorded: only names the panel carries on *some* session can be held, so
    # only those need an explicit absence row for the sessions they miss.
    for session in sessions:
        frame = _canonical_frame(data_root, session)
        present = set(frame["instrument"].astype(str))
        for instrument in sorted(seen - present):
            rows.append({"date": session.isoformat(), "instrument": instrument,
                         "suspended": True})
    frame = pd.DataFrame(rows, columns=["date", "instrument", "suspended"])
    if frame.empty:
        # The store refuses an empty frame, and an all-trading window is a real
        # state rather than a missing input, so publish one explicit negative row.
        frame = pd.DataFrame(
            [{"date": sessions[0].isoformat(), "instrument": "NONE_SUSPENDED", "suspended": False}],
            columns=["date", "instrument", "suspended"],
        )
    return frame


def _corporate_action_frame(dividend_root: Path, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Per-ex-date instruments with an implemented corporate action.

    Reads the same `DividendEventStore` the adjustment chain derives factors from,
    and takes only records whose `div_proc` marks them implemented and whose
    `ex_date` falls in the window, so the exclusion set cannot widen past the
    window or include an announced-but-not-yet-effective event.
    """
    store = DividendEventStore(dividend_root)
    rows: list[dict[str, Any]] = []
    for partition_date in sorted(
        path for path in (dividend_root / "dividend_events").glob("*")
        if path.is_dir() and not path.name.startswith(".")
    ):
        announcement = dt.date.fromisoformat(partition_date.name)
        # The *announcement* may be any date up to the run's visibility cutoff: an
        # ex-date inside the window can be announced before it (and the disclosure
        # store is announcement-anchored, so a partition outside the window is
        # ordinary).  Only the resulting `ex_date` is window-bound, below.
        events = store.read_events(announcement)
        if events.empty:
            continue
        # `read_events` returns the normalized frame: `instrument` (canonical
        # spelling) replaces the disclosure's `ts_code`, and `record_id` is added.
        implemented = events[events["div_proc"].astype(str) == "实施"]
        for row in implemented.to_dict(orient="records"):
            ex_date = str(row.get("ex_date") or "")
            if not ex_date or not row.get("instrument"):
                continue
            rows.append({"date": ex_date, "instrument": str(row["instrument"])})
    frame = pd.DataFrame(rows, columns=["date", "instrument"])
    if frame.empty:
        frame = pd.DataFrame(
            [{"date": start.isoformat(), "instrument": "NONE_IMPLEMENTED"}],
            columns=["date", "instrument"],
        )
    # Keep only ex-dates inside the window: the exclusion set must not widen past
    # it, and an announcement whose ex-date lands later is not this window's event.
    in_window = frame[frame["date"].astype(str).apply(
        lambda value: start.isoformat() <= value <= end.isoformat()
    )]
    if in_window.empty:
        return frame
    return in_window.drop_duplicates().sort_values(
        ["date", "instrument"], kind="mergesort"
    ).reset_index(drop=True)


def _corporate_action_events_frame(
    dividend_root: Path, start: dt.date, end: dt.date, sessions: list[dt.date],
    rights_document_path: Path,
) -> pd.DataFrame:
    """Build the reviewed v4 settlement panel from implemented disclosures.

    Rights issues have no governed source, so v4 requires a separately reviewed,
    complete-window exclusion document. Dividend revisions are selected by the
    latest announcement visible by ex-date; conflicting latest rows fail closed.
    """
    try:
        document = json.loads(rights_document_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("rights-issue exclusion document is unavailable or malformed") from exc
    if not isinstance(document, dict):
        raise ContractError("rights-issue exclusion document must be an object")
    approval_ref = document.get("source_approval_ref")
    dividend_approval_ref = document.get("dividend_source_approval_ref")
    exclusions = document.get("exclusions")
    if (document.get("window_start") != start.isoformat()
            or document.get("window_end") != end.isoformat()
            or document.get("coverage_complete") is not True
            or document.get("dividend_coverage_complete") is not True
            or not isinstance(dividend_approval_ref, str) or not dividend_approval_ref.strip()
            or not isinstance(exclusions, list)):
        raise ContractError("corporate-action review does not attest complete dividend and rights coverage")
    rows: list[dict[str, Any]] = []
    store = DividendEventStore(dividend_root)
    candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for announcement in store.list_partitions():
        events = store.read_events(announcement)
        implemented = events.loc[events["div_proc"].astype(str) == "实施"]
        for source in implemented.to_dict(orient="records"):
            ex_date = source.get("ex_date")
            if not ex_date or not start.isoformat() <= str(ex_date) <= end.isoformat():
                continue
            if announcement.isoformat() > str(ex_date):
                raise ContractError("implemented dividend was announced after its ex-date")
            implementation_date = source.get("imp_ann_date")
            if not implementation_date or str(implementation_date) > str(ex_date):
                raise ContractError("implemented dividend lacks an implementation announcement visible by ex-date")
            if source.get("record_date") and str(source["record_date"]) < start.isoformat():
                # The backtest starts in cash, so a record date before the run
                # cannot create an entitlement for its holdings.
                continue
            key = (str(source["instrument"]), str(ex_date))
            candidates.setdefault(key, []).append({**source, "_announcement": announcement.isoformat()})

    for (instrument, ex_date), revisions in sorted(candidates.items()):
        latest_date = max(row["_announcement"] for row in revisions)
        latest = [row for row in revisions if row["_announcement"] == latest_date]
        economics = set()
        for row in latest:
            required = ("record_date", "cash_div", "stk_div",
                        "stk_bo_rate", "stk_co_rate")
            if any(row.get(field) in (None, "") for field in required):
                raise ContractError("implemented dividend lacks a required date or amount")
            cash = float(row["cash_div"])
            reported_stock = float(row["stk_div"])
            stock_components = float(row["stk_bo_rate"]) + float(row["stk_co_rate"])
            if (not all(map(lambda value: pd.notna(value) and value >= 0,
                            (cash, reported_stock, stock_components)))
                    or abs(reported_stock - stock_components) > 1e-9):
                raise ContractError("implemented dividend distribution amounts are incomplete or inconsistent")
            economics.add((row["record_date"], row["pay_date"], row["div_listdate"],
                           cash, reported_stock, row["record_id"], row["_announcement"]))
        if len(economics) != 1:
            raise ContractError(f"ambiguous latest implemented dividend on {ex_date} for {instrument}")
        row = latest[0]
        if not row.get("record_date"):
            raise ContractError("implemented dividend lacks record date")
        if float(row["cash_div"]) > 0 and not row.get("pay_date"):
            raise ContractError("cash dividend lacks payment date")
        if float(row["stk_div"]) > 0 and not row.get("div_listdate"):
            raise ContractError("stock dividend lacks listing date")
        rows.append({
            "event_type": "dividend", "event_id": row["record_id"],
            "instrument": instrument, "record_date": row["record_date"],
            "ex_date": ex_date, "pay_date": row["pay_date"],
            "div_listdate": row["div_listdate"],
            "cash_div_after_tax": float(row["cash_div"]),
            "stock_div_per_share": float(row["stk_div"]),
            "source_announcement_date": row["_announcement"],
            "implementation_announcement_date": row["imp_ann_date"],
            "source_record_id": row["record_id"], "source_approval_ref": dividend_approval_ref,
            "reason": "implemented_dividend", "window_start": None,
            "window_end": None, "coverage_complete": None,
        })

    coverage = rights_issue_coverage_row(
        start_date=start.isoformat(), end_date=end.isoformat(),
        source_approval_ref=approval_ref,
    )
    rows.extend([
        coverage,
        dividend_coverage_row(
            start_date=start.isoformat(), end_date=end.isoformat(),
            source_approval_ref=dividend_approval_ref,
        ),
    ])
    seen: set[str] = set()
    for exclusion in exclusions:
        if (not isinstance(exclusion, dict)
                or set(exclusion) != {"instrument", "reason"}
                or not isinstance(exclusion["instrument"], str)
                or not exclusion["instrument"]
                or not isinstance(exclusion["reason"], str)
                or not exclusion["reason"].strip()
                or exclusion["instrument"] in seen):
            raise ContractError("rights-issue exclusion rows must have unique instruments and reasons")
        seen.add(exclusion["instrument"])
        identity = sha256_json({
            "event_type": "rights_issue_exclusion", "instrument": exclusion["instrument"],
            "source_approval_ref": approval_ref, "reason": exclusion["reason"],
            "window_start": start.isoformat(), "window_end": end.isoformat(),
        })
        rows.append({
            **coverage, "event_type": "rights_issue_exclusion", "event_id": identity,
            "instrument": exclusion["instrument"], "source_record_id": None,
            "reason": exclusion["reason"],
        })
    frame = pd.DataFrame(rows, columns=ACTION_EVENT_COLUMNS)
    normalize_action_events(
        frame, start_date=start.isoformat(), end_date=end.isoformat(),
        trading_dates=[session.isoformat() for session in sessions],
    )
    return frame


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True,
                        help="store root holding canonical/bars_daily/v2")
    parser.add_argument("--dividend-root", type=Path, required=True,
                        help="published DividendEventStore root")
    parser.add_argument("--contract-version", type=int, choices=(2, 3, 4), default=3)
    parser.add_argument("--rights-issue-exclusions-json", type=Path,
                        help="v4 reviewed complete-window rights issue exclusion attestation")
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, default=None,
                        help="optional JSON run report path")
    args = parser.parse_args(argv)

    sessions = _canonical_sessions(args.data_root, args.start, args.end)
    store = BacktestMarketDatasetStore(args.data_root)
    if args.contract_version == 4 and args.rights_issue_exclusions_json is None:
        parser.error("v4 requires --rights-issue-exclusions-json")
    if args.contract_version != 4 and args.rights_issue_exclusions_json is not None:
        parser.error("--rights-issue-exclusions-json is only valid for v4")
    corporate_actions = (
        _corporate_action_events_frame(args.dividend_root, args.start, args.end, sessions,
                                       args.rights_issue_exclusions_json)
        if args.contract_version == 4
        else _corporate_action_frame(args.dividend_root, args.start, args.end)
    )
    corporate_action_type = "corporate_action_events" if args.contract_version == 4 else "corporate_action"
    frames = {
        "calendar": _calendar_frame(sessions),
        "suspension": _suspension_frame(args.data_root, sessions),
        corporate_action_type: corporate_actions,
    }
    sources = {
        "calendar": {
            "primary": "canonical_bars_daily_v2_session_directories",
            "note": "the calendar is the price panel's own sessions, so price/calendar equality holds by construction",
        },
        "suspension": {
            "primary": "canonical_bars_daily_v2_status",
            "reconciliation": "tushare_suspend_d",
            "note": "bars status is the primary source; suspend_d reconciles, it is not a second truth",
        },
        corporate_action_type: {
            "primary": "DividendEventStore:implemented_ex_date",
            "note": "the same disclosure store the adjustment chain derives factors from",
        },
    }
    manifests: dict[str, dict[str, Any]] = {}
    for dataset_type, frame in frames.items():
        manifests[dataset_type] = store.publish(
            frame,
            dataset_type=dataset_type,
            window_start=args.start,
            window_end=args.end,
            sources=sources[dataset_type],
            producer=PRODUCER,
        )
    report = {
        "producer": PRODUCER,
        "window_start": args.start.isoformat(),
        "window_end": args.end.isoformat(),
        "sessions": [session.isoformat() for session in sessions],
        "bindings": {
            "calendar_binding": {
                "generation_id": manifests["calendar"]["generation_id"],
                "checksum_sha256": manifests["calendar"]["data_checksum_sha256"],
            },
            "suspension_binding": {
                "dataset_generation_id": manifests["suspension"]["generation_id"],
                "data_checksum_sha256": manifests["suspension"]["data_checksum_sha256"],
            },
            "corporate_action_binding": {
                "dataset_generation_id": manifests[corporate_action_type]["generation_id"],
                "data_checksum_sha256": manifests[corporate_action_type]["data_checksum_sha256"],
                **({"dataset_type": corporate_action_type} if args.contract_version == 4 else {}),
            },
        },
        "row_counts": {key: int(value["row_count"]) for key, value in manifests.items()},
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
