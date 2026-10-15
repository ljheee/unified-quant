#!/usr/bin/env python3
"""Ingest a whole window of canonical daily bars through the governed v2 pipeline.

`uq-ingest daily --pipeline v2` publishes exactly one session.  A research window
is many sessions, and the A500 pilot drove that loop inline, so nothing in the
repository could reproduce a window from a command.  This entry point is that
driver: it resolves the PIT-visible members per session from an
`IndexMembershipStore`, builds the same `build_tushare_v2_pipeline`, and
publishes one immutable partition per session.

It never invents data and never relaxes a governed rule: every session goes
through the pipeline's own router, quality gate and canonical store.  A session
whose partition already exists is reported as ``already_published`` and skipped,
so an interrupted multi-hour run resumes by re-running the same command.

The session list comes from the pipeline's own independent calendar
(EastMoney index sessions by default), not from a caller-supplied list, so a
window that is not covered by the calendar fails instead of silently narrowing.

Usage:
    python scripts/ingest_window.py \
        --data-root data/bars-csiall-store \
        --membership-root data/csi-all-store --universe-id csi-all \
        --start 2025-09-09 --end 2025-10-15 \
        --summary-out evidence/.../ingest-window.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path
from typing import Sequence

from uq.adapters.eastmoney import EastMoneySessionProvider
from uq.errors import IndependentCalendarError
from uq.contracts.index_membership import IndexMembershipStore
from uq.errors import ContractError
from uq.market.calendar import TradingCalendar
from uq.pipeline.daily import build_tushare_v2_pipeline

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "v2"


def _partition_published(data_root: Path, session: dt.date) -> bool:
    partition = data_root / "canonical" / "bars_daily" / SCHEMA_VERSION / f"date={session.isoformat()}"
    return (partition / "manifest.json").is_file() and (partition / "data.parquet").is_file()


def _members(store: IndexMembershipStore, universe_id: str, session: dt.date) -> list[str]:
    _, frame = store.as_of(universe_id, session)
    return sorted(str(value) for value in frame["instrument"])


def _tail_members(
    store: IndexMembershipStore,
    universe_id: str,
    session: dt.date,
    members: list[str],
    prior_sessions: Sequence[dt.date],
    tail: int,
) -> list[str]:
    """Names that were members recently but are not members today.

    A forward label needs bars through ``D + horizon``, so a name that leaves the
    universe mid-window has decision dates whose labels cannot be computed unless
    its bars are still fetched for a few sessions after its last member day.
    The session list is supplied by the caller so this never widens the window.

    This is deliberately an explicit argument rather than the default: per-day
    PIT ingest means "the members visible today", and fetching for a departed
    name is a widening of that contract that has to be recorded in the run
    summary (``tail_members``) to stay auditable.
    """
    if tail <= 0:
        return members
    prior = list(prior_sessions)
    if not prior:
        return members
    window_start = max(0, len(prior) - tail)
    for earlier in reversed(prior[window_start:]):
        try:
            _, frame = store.as_of(universe_id, earlier)
        except (ContractError, OSError, ValueError):
            continue
        for name in sorted(str(value) for value in frame["instrument"]):
            if name not in members:
                members.append(name)
    return sorted(set(members))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True, help="canonical bars store root")
    parser.add_argument("--membership-root", type=Path, required=True)
    parser.add_argument("--universe-id", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD, first session")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD, last session")
    parser.add_argument("--summary-out", type=Path, default=None)
    parser.add_argument(
        "--skip-health-probe",
        action="store_true",
        help="skip the per-session primary-source health probe (the pipeline runs one otherwise)",
    )
    parser.add_argument(
        "--calendar-attempts",
        type=int,
        default=8,
        help="retries for the flaky public independent-calendar endpoint before giving up",
    )
    parser.add_argument(
        "--calendar-retry-seconds",
        type=float,
        default=3.0,
        help="pause between independent-calendar attempts",
    )
    parser.add_argument(
        "--tail-sessions",
        type=int,
        default=0,
        help=(
            "also fetch bars for names that were PIT-visible members in the last N "
            "sessions but are not members of this session, so their last decision "
            "dates can still be labelled. Default 0 keeps ingest strictly per-day "
            "PIT; passing N (typically the label horizon) widens that contract, so "
            "the run summary records the widened membership under `tail_members`."
        ),
    )
    parser.add_argument(
        "--session-interval-seconds",
        type=float,
        default=25.0,
        help=(
            "pause between sessions. One full-A session costs about 104 Tushare "
            "requests (50-code batches plus the whole-market reference calls) and "
            "the free tier caps daily at 300/min, so a back-to-back loop trips the "
            "limit on the second session; 25s keeps a session's request burst under "
            "the documented rate."
        ),
    )
    args = parser.parse_args(argv)

    try:
        start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    except ValueError as exc:
        raise SystemExit(f"invalid --start/--end: {exc}")
    if end < start:
        raise SystemExit("--end precedes --start")

    provider = EastMoneySessionProvider()
    calendar = TradingCalendar(provider=provider, provenance=provider.provenance)
    # The independent calendar is a governance input, not a nicety: it is what
    # keeps a vendor calendar from silently narrowing the session set.  It is
    # also a flaky public endpoint (measured: roughly one in six requests
    # answers with a dropped connection), so retry it -- a transient network
    # failure must not be turned into a permanent "unreachable" verdict, and it
    # equally must not be worked around by dropping the calendar.
    sessions: list[dt.date] = []
    last_error: Exception | None = None
    for attempt in range(args.calendar_attempts):
        try:
            sessions = sorted(provider.between(start, end))
            break
        except (IndependentCalendarError, ContractError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < args.calendar_attempts:
                time.sleep(args.calendar_retry_seconds)
    else:
        raise SystemExit(f"independent calendar could not certify {start}..{end}: {last_error}")
    if not sessions:
        raise SystemExit(f"independent calendar reports no sessions in {start}..{end}")
    membership = IndexMembershipStore(args.membership_root)
    pipeline = build_tushare_v2_pipeline(
        args.data_root,
        PROJECT_ROOT,
        run_health_probe=not args.skip_health_probe,
        independent_calendar=True,
        calendar=calendar,
    )

    published: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    interrupted = False

    def _write_summary() -> dict:
        summary = {
            "record_version": 1,
            "data_root": str(args.data_root),
            "membership_root": str(args.membership_root),
            "universe_id": args.universe_id,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "tail_sessions": args.tail_sessions,
            "calendar_provenance": provider.provenance,
            "calendar_evidence": provider.evidence,
            "sessions": len(sessions),
            "published": published,
            "skipped": skipped,
            "failed": failed,
            "interrupted": interrupted,
            "status": "interrupted" if interrupted else (
                "failed" if failed else ("published" if published else "already_published")
            ),
        }
        text = json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if args.summary_out is not None:
            args.summary_out.parent.mkdir(parents=True, exist_ok=True)
            args.summary_out.write_text(text, encoding="utf-8")
        return summary

    try:
        for index, session in enumerate(sessions):
            if _partition_published(args.data_root, session):
                skipped.append({"session": session.isoformat(), "reason": "already_published"})
                continue
            try:
                members = _members(membership, args.universe_id, session)
            except (ContractError, OSError, ValueError) as exc:
                failed.append({"session": session.isoformat(), "stage": "membership", "error": f"{type(exc).__name__}: {exc}"})
                continue
            if not members:
                failed.append({"session": session.isoformat(), "stage": "membership", "error": "no PIT-visible members"})
                continue
            # The tail is resolved from the sessions already visited in this window,
            # so it can never reach outside the certified session list.
            instruments = _tail_members(
                membership, args.universe_id, session, list(members),
                sessions[:index], args.tail_sessions,
            )
            tail_added = sorted(set(instruments) - set(members))
            if published and args.session_interval_seconds > 0:
                time.sleep(args.session_interval_seconds)
            report = pipeline.run(session, instruments)
            payload = json.loads(report.to_json())
            if payload.get("status") != "published":
                failed.append({
                    "session": session.isoformat(),
                    "stage": "ingest",
                    "status": payload.get("status"),
                    "errors": payload.get("errors", []),
                    "warnings": payload.get("warnings", []),
                })
                continue
            published.append({
                "session": session.isoformat(),
                "rows": payload.get("rows"),
                "members": len(members),
                **({"tail_members": tail_added} if tail_added else {}),
                "warnings": payload.get("warnings", []),
            })
    except BaseException:
        # A multi-hour window must not lose its audit record to an unexpected
        # raise: the partitions already published are durable and the run is
        # resumable, so the summary that names which sessions landed has to be
        # written before the exception leaves this function.
        interrupted = True
        _write_summary()
        raise

    summary = _write_summary()
    print(json.dumps({
        "status": summary["status"],
        "sessions": summary["sessions"],
        "published": len(published),
        "skipped": len(skipped),
        "failed": len(failed),
        "failed_sessions": [item["session"] for item in failed],
    }, ensure_ascii=False))
    return 1 if failed or interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
