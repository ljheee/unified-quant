#!/usr/bin/env python3
"""Publish a window-level union of visible index-membership snapshots.

`publish_universe_snapshots.py` tiles monthly membership snapshots into
non-overlapping validity windows.  A research window that spans several tiles
therefore has no single `universe_snapshot.v1` it can bind, even though the v6
dataset stage already applies the authoritative per-day membership rule through
`index_membership_binding`.

This publisher creates the missing identity anchor: one immutable snapshot whose
members are the union of every membership snapshot visible inside the research
window.  It is deliberately *not* a PIT membership claim.  The evidence string
states that the daily member rule remains `index_membership_binding`, and the
artifact is only used to satisfy the request's single-snapshot binding.

Usage:
    python scripts/publish_universe_union.py \
        --membership-root data/csi-all-store \
        --universe-root data/root-2025 \
        --universe-id csi-all \
        --start 2025-05-13 --end 2025-12-31 \
        --out evidence/.../universe-union.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from uq.contracts.artifacts import UniverseSnapshotStore
from uq.contracts.index_membership import IndexMembershipStore, build_membership_set
from uq.errors import ContractError
from uq.execution.market_dataset_publisher import MarketDatasetPublisher
from uq.execution.market_source import PublishedPaperMarketData

MARKET_ZONE = ZoneInfo("Asia/Shanghai")
LIMITATION = (
    "limitation: monthly approximation, not strict daily PIT; intra-month index "
    "adjustments cannot be distinguished because the source publishes no "
    "announcement or effective date"
)


def _refuse(message: str) -> int:
    print(json.dumps({"status": "configuration_failure", "error": message}, ensure_ascii=False))
    return 2


def _trading_days(
    market_root: Path, *, start: dt.date, end: dt.date
) -> list[dt.date]:
    publisher = MarketDatasetPublisher(market_root)
    return PublishedPaperMarketData(
        publisher, window_start=start, window_end=end
    ).sessions()


def _union_document(
    store: IndexMembershipStore,
    membership_set: dict[str, Any],
    *,
    universe_id: str,
    start: dt.date,
    end: dt.date,
) -> tuple[dict[str, Any], pd.DataFrame]:
    entries = membership_set["snapshots"]
    if not entries:
        raise ContractError("membership set has no visible snapshots")
    members: set[str] = set()
    contributing: list[str] = []
    for entry in entries:
        snapshot_date = dt.date.fromisoformat(entry["snapshot_date"])
        frame = store.read_members(universe_id, snapshot_date)
        members.update(frame["instrument"].astype(str))
        contributing.append(
            f"{entry['snapshot_date']}({len(frame)},visible_from={entry['visible_from']},"
            f"membership_generation={entry['generation_id']})"
        )
    latest_snapshot = dt.date.fromisoformat(entries[-1]["snapshot_date"])
    latest_visible = dt.date.fromisoformat(entries[-1]["visible_from"])
    evidence = " ".join((
        f"source={store.read_manifest(universe_id, latest_snapshot)['source']}",
        f"index_code={membership_set['index_code']}",
        "pit_precision=monthly_approximation",
        f"window={start.isoformat()}..{end.isoformat()}",
        f"member_rule=union_of_visible_monthly_snapshots member_count={len(members)}",
        f"contributing_snapshots={';'.join(contributing)}",
        "semantics=window_level_union_not_daily_pit; every member is in the index "
        "on at least one session of the window, and none is guaranteed present on "
        "every session; the daily membership rule that governs the dataset is "
        "index_membership_binding, not this artifact",
        LIMITATION,
        "valid_to_is_open_ended",
    ))
    document = {
        "universe_version": 1,
        "universe_id": universe_id,
        "source": f"tushare:index_weight:{membership_set['index_code']}",
        "snapshot_time": dt.datetime.combine(
            latest_snapshot, dt.time(0), tzinfo=MARKET_ZONE
        ).isoformat(),
        "visibility_time": dt.datetime.combine(
            latest_visible, dt.time(0), tzinfo=MARKET_ZONE
        ).isoformat(),
        "valid_from": start.isoformat(),
        "valid_to": None,
        "members_artifact": {"path": "members.csv", "checksum_sha256": "0" * 64},
        "membership_evidence": evidence,
    }
    frame = pd.DataFrame({"instrument": sorted(members)})
    return document, frame


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--membership-root", type=Path, required=True)
    parser.add_argument("--universe-root", type=Path, required=True)
    parser.add_argument("--universe-id", required=True)
    parser.add_argument("--market-dataset-root", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        start = dt.date.fromisoformat(args.start)
        end = dt.date.fromisoformat(args.end)
    except ValueError as exc:
        return _refuse(f"invalid --start/--end: {exc}")
    if end < start:
        return _refuse("--end precedes --start")

    store = IndexMembershipStore(args.membership_root)
    try:
        trading_days = _trading_days(args.market_dataset_root, start=start, end=end)
        membership_set = build_membership_set(
            store,
            universe_id=args.universe_id,
            window_start=start,
            window_end=end,
            trading_days=trading_days,
        )
        document, members = _union_document(
            store,
            membership_set,
            universe_id=args.universe_id,
            start=start,
            end=end,
        )
        directory = UniverseSnapshotStore(args.universe_root).save(
            args.universe_root, document, members
        )
    except (ContractError, OSError, ValueError) as exc:
        if "already published" not in str(exc):
            return _refuse(f"{type(exc).__name__}: {exc}")
        # The identity is content-derived, so a rerun names the same directory.
        directory = Path(str(exc).split("already published: ", 1)[-1])
        status = "already_published"
    else:
        status = "published"

    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    report = {
        "status": status,
        "universe_id": args.universe_id,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "generation_id": manifest["generation_id"],
        "valid_from": manifest["valid_from"],
        "valid_to": manifest["valid_to"],
        "instrument_count": int(len(members)),
        "contributing_snapshots": membership_set["snapshots"],
        "semantics": "window_level_union_not_daily_pit",
    }
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
