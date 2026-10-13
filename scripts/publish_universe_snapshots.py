#!/usr/bin/env python3
"""Publish PIT universe snapshots from a governed index-membership store.

The research chain reads ``universe_snapshot`` artifacts but nothing in the
repository produced them: only test helpers wrote them.  This entry point turns
the already-governed ``index_membership`` snapshots (published by
``scripts/fetch_index_membership.py``) into ``universe_snapshot.v1`` artifacts, so
a research run can bind a universe without hand-written fixtures.

Coverage tiling follows the membership rule recorded in ``pendding.md``: a
snapshot becomes usable on its ``visible_from`` (snapshot date plus
``publish_lag_days`` trading days) and stays in force until the next snapshot
becomes visible, so each published snapshot covers ``[visible_from,
next_visible_from)``.  The newest snapshot is deliberately open-ended
(``valid_to = null``) and says so in its evidence string.

The monthly-approximation limitation is carried into every artifact: Tushare
``index_weight`` exposes no announcement or effective date, so intra-month
adjustments cannot be distinguished and this is not strict daily PIT.

Usage:
    python scripts/publish_universe_snapshots.py \
        --membership-root data/a500-store \
        --universe-root data/research-root \
        --universe-id a500
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from uq.contracts.artifacts import UniverseSnapshotStore
from uq.contracts.index_membership import IndexMembershipStore
from uq.errors import ContractError

MARKET_ZONE = ZoneInfo("Asia/Shanghai")
MEMBERS_COLUMNS = ["instrument"]
LIMITATION = (
    "limitation: monthly approximation, not strict daily PIT; intra-month index "
    "adjustments cannot be distinguished because the source publishes no "
    "announcement or effective date"
)


def _refuse(message: str) -> int:
    print(json.dumps({"status": "configuration_failure", "error": message}, ensure_ascii=False))
    return 2


def _tiles(store: IndexMembershipStore, universe_id: str) -> list[dict[str, Any]]:
    """Order snapshots by visibility and tile them into [valid_from, valid_to)."""
    records: list[dict[str, Any]] = []
    for snapshot_date in store.snapshot_dates(universe_id):
        manifest = store.read_manifest(universe_id, snapshot_date)
        records.append({
            "snapshot_date": snapshot_date,
            "manifest": manifest,
            "visible_from": dt.date.fromisoformat(manifest["visible_from"]),
        })
    if not records:
        raise ContractError(f"no index membership snapshots for {universe_id}")
    records.sort(key=lambda item: (item["visible_from"], item["snapshot_date"]))
    if len({item["visible_from"] for item in records}) != len(records):
        overlaps = [item["snapshot_date"].isoformat() for item in records[1:]
                    if item["visible_from"] == records[records.index(item) - 1]["visible_from"]]
        raise ContractError(
            f"index membership snapshots share a visible_from date and cannot be tiled: {overlaps}"
        )
    for position, record in enumerate(records):
        if position + 1 < len(records):
            record["valid_to"] = records[position + 1]["visible_from"] - dt.timedelta(days=1)
            record["open_ended"] = False
        else:
            record["valid_to"] = None
            record["open_ended"] = True
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--membership-root", type=Path, required=True,
                        help="index membership store root (holds index_membership/<universe_id>/...)")
    parser.add_argument("--universe-root", type=Path, required=True,
                        help="research data root that holds universes/ artifacts")
    parser.add_argument("--universe-id", required=True)
    parser.add_argument("--from", dest="valid_from", default=None,
                        help="only publish snapshots whose valid_from is on/after this date")
    parser.add_argument("--to", dest="valid_to", default=None,
                        help="only publish snapshots whose valid_from is on/before this date")
    parser.add_argument("--summary-out", type=Path, default=None)
    args = parser.parse_args()

    boundaries: dict[str, dt.date | None] = {}
    for name, flag in (("valid_from", "--from"), ("valid_to", "--to")):
        raw = getattr(args, name)
        if raw is None:
            boundaries[name] = None
            continue
        try:
            boundaries[name] = dt.date.fromisoformat(raw)
        except ValueError as exc:
            return _refuse(f"invalid {flag}: {exc}")
    if boundaries["valid_from"] and boundaries["valid_to"] and boundaries["valid_to"] < boundaries["valid_from"]:
        return _refuse("--to precedes --from")

    store = IndexMembershipStore(args.membership_root)
    try:
        tiles = _tiles(store, args.universe_id)
    except ContractError as exc:
        return _refuse(str(exc))

    publisher = UniverseSnapshotStore(args.universe_root)
    published: list[dict[str, Any]] = []
    already: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    selected = [
        tile for tile in tiles
        if (boundaries["valid_from"] is None or tile["visible_from"] >= boundaries["valid_from"])
        and (boundaries["valid_to"] is None or tile["visible_from"] <= boundaries["valid_to"])
    ]
    for tile in selected:
        manifest = tile["manifest"]
        snapshot_date = tile["snapshot_date"]
        try:
            members = store.read_members(args.universe_id, snapshot_date)
            if list(members.columns) != MEMBERS_COLUMNS:
                raise ContractError(
                    f"index membership members columns must be exactly {MEMBERS_COLUMNS}, got {list(members.columns)}"
                )
            document = {
                "universe_version": 1,
                "universe_id": args.universe_id,
                "source": manifest["source"],
                "snapshot_time": dt.datetime.combine(snapshot_date, dt.time(0), tzinfo=MARKET_ZONE).isoformat(),
                "visibility_time": dt.datetime.combine(tile["visible_from"], dt.time(0), tzinfo=MARKET_ZONE).isoformat(),
                "valid_from": tile["visible_from"].isoformat(),
                "valid_to": tile["valid_to"].isoformat() if tile["valid_to"] else None,
                # save() recomputes this from the bytes it writes; the schema only
                # requires the field to be present while validating the candidate.
                "members_artifact": {"path": "members.csv", "checksum_sha256": "0" * 64},
                "membership_evidence": " ".join((
                    f"source={manifest['source']}",
                    f"index_code={manifest['index_code']}",
                    f"pit_precision={manifest['pit_precision']}",
                    f"index_snapshot_date={snapshot_date.isoformat()}",
                    f"publish_lag_days={manifest['publish_lag_days']}",
                    f"membership_generation={manifest['generation_id']}",
                    "valid_to_is_open_ended" if tile["open_ended"] else "valid_to_is_next_visible_from",
                    LIMITATION,
                )),
            }
            directory = publisher.save(args.universe_root, document, members)
        except (ContractError, OSError, ValueError) as exc:
            recorded = {"valid_from": tile["visible_from"].isoformat(),
                        "index_snapshot_date": snapshot_date.isoformat()}
            if "already published" in str(exc):
                recorded["reason"] = "already_published"
                already.append(recorded)
            else:
                recorded["error"] = f"{type(exc).__name__}: {exc}"
                failed.append(recorded)
            continue
        saved = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        published.append({
            "valid_from": saved["valid_from"],
            "valid_to": saved["valid_to"],
            "generation_id": saved["generation_id"],
            "members_checksum_sha256": saved["members_artifact"]["checksum_sha256"],
            "instrument_count": int(len(members)),
            "open_ended": tile["open_ended"],
        })

    result: dict[str, Any] = {
        "status": ("failed" if failed else
                   "published" if published else
                   "already_published" if already else "empty"),
        "universe_id": args.universe_id,
        "snapshots_available": len(tiles),
        "snapshots_selected": len(selected),
        "published": published,
        "already_published": already,
        "failed": failed,
        "pit_precision": tiles[0]["manifest"]["pit_precision"],
        "limitation": LIMITATION,
    }
    if args.summary_out is not None:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.summary_out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
