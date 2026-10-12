#!/usr/bin/env python3
"""Rebuild the digest inventories that stand in for gitignored governed stores.

Canonical bars partitions and index membership snapshots never enter the
repository, so the durable review evidence is the per-partition identity set:
manifest digest, data digest, generation id and row count. Every record is
re-derived from the bytes on disk *and* read back through the governed
manifest-first reader, so an inventory cannot be committed over a store that no
longer verifies.

Usage:
  scripts/build_store_evidence.py --kind bars --store data/bars-a500-store \
      --universe-id a500 --start 2025-08-29 --end 2025-09-30 \
      --out evidence/a500-pilot/2025-08-29-to-2025-09-30/bars-inventory.json
  scripts/build_store_evidence.py --kind membership --store data/a500-store \
      --universe-id a500 --out evidence/a500-membership/evidence-index.json
  scripts/build_store_evidence.py --kind index --layer a500_pilot_ingest \
      --out evidence/a500-pilot/2025-08-29-to-2025-09-30/evidence-index.json \
      --record evidence/.../pilot-record.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from uq.contracts.index_membership import IndexMembershipStore
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.store.reader import ManifestFirstReader

REPO_ROOT = Path(__file__).resolve().parents[1]
BARS_DATA_FILE = "data.parquet"


def _governed(action: str, path: Path, call):
    """A rejected governed read is a refusal to archive, not a traceback."""
    try:
        return call()
    except ContractError as exc:
        raise SystemExit(f"inventory refused: cannot {action} {path}: {exc}") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_relative(path: Path, repo_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise SystemExit(f"evidence paths must live inside the repository: {path}") from exc


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"unreadable partition manifest {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemExit(f"partition manifest must be an object: {path}")
    return document


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _partition_dates(directory: Path) -> list[dt.date]:
    """Discovery only: every manifest still goes through the governed reader."""
    dates: list[dt.date] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        try:
            dates.append(dt.date.fromisoformat(child.name.split("=", 1)[-1]))
        except ValueError as exc:
            raise SystemExit(f"unexpected partition directory: {child}") from exc
    if not dates:
        raise SystemExit(f"no partitions under {directory}")
    return dates


def bars_inventory(
    store: Path,
    universe_id: str,
    start: dt.date,
    end: dt.date,
    schema_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    schema = load_schema(schema_path)
    reader = ManifestFirstReader(store)
    base = store / "canonical" / "bars_daily" / "v2"
    if not base.is_dir():
        raise SystemExit(f"missing bars partition root: {base}")
    records: list[dict[str, Any]] = []
    for session in _partition_dates(base):
        directory = base / f"date={session.isoformat()}"
        if not start <= session <= end:
            continue
        manifest_path = directory / "manifest.json"
        manifest = _read_manifest(manifest_path)
        data_path = directory / BARS_DATA_FILE
        data_sha256 = _sha256(data_path)
        if data_sha256 != manifest.get("data_checksum_sha256"):
            raise SystemExit(f"data digest drifted from manifest: {data_path}")
        frame = _governed("read back partition", directory, lambda: reader.read(schema, session))
        if len(frame) != manifest["row_count"]:
            raise SystemExit(
                f"readback row count {len(frame)} != manifest row_count "
                f"{manifest['row_count']}: {directory}"
            )
        records.append(
            {
                "session_date": session.isoformat(),
                "manifest_path": _repo_relative(manifest_path, repo_root),
                "manifest_sha256": _sha256(manifest_path),
                "data_path": _repo_relative(data_path, repo_root),
                "data_sha256": data_sha256,
                "generation_id": manifest["generation_id"],
                "row_count": manifest["row_count"],
                "run_id": manifest["run_id"],
                "code_fingerprint": manifest["code_fingerprint"],
                "schema_checksum_sha256": manifest["schema_checksum_sha256"],
                "quality_report_checksum": manifest["quality_report_checksum"],
                "trust_anchor_sha256": manifest["trust_anchor_sha256"],
                "source_versions": manifest.get("source_versions", {}),
                "sources": sorted(
                    {
                        str(lineage.get("source"))
                        for lineage in manifest.get("lineage", {}).values()
                        if isinstance(lineage, dict) and lineage.get("source")
                    }
                ),
            }
        )
    if not records:
        raise SystemExit(f"no bars partitions in {base} for {start}..{end}")
    return {
        "record_version": 1,
        "kind": "bars_daily_inventory",
        "universe_id": universe_id,
        "dataset": "bars_daily",
        "schema_version": schema.version,
        "store_root": _repo_relative(store, repo_root),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "partition_count": len(records),
        "verification": (
            "manifest digest + data digest recomputed from bytes, partition re-read "
            "through ManifestFirstReader with checksum verification"
        ),
        "note": (
            "the store root is gitignored; these digests are re-derivable by "
            "re-ingesting the same window and re-running this script"
        ),
        "records": records,
    }


def membership_inventory(store: Path, universe_id: str, repo_root: Path) -> dict[str, Any]:
    store_reader = IndexMembershipStore(store)
    base = store / "index_membership" / universe_id
    if not base.is_dir():
        raise SystemExit(f"missing membership snapshot root: {base}")
    snapshot_dates = _partition_dates(base)
    records: list[dict[str, Any]] = []
    index_code = ""
    pit_precision = ""
    for snapshot_date in snapshot_dates:
        manifest_path = base / snapshot_date.isoformat() / "manifest.json"
        manifest = _governed(
            "validate snapshot manifest",
            manifest_path,
            lambda: store_reader.read_manifest(universe_id, snapshot_date),
        )
        artifact = manifest["members_artifact"]
        artifact_path = base / snapshot_date.isoformat() / artifact["path"]
        artifact_sha256 = _sha256(artifact_path)
        if artifact_sha256 != artifact["checksum_sha256"]:
            raise SystemExit(f"members digest drifted from manifest: {artifact_path}")
        members = _governed("read back members", artifact_path, lambda: store_reader.read_members(universe_id, snapshot_date))
        if len(members) != artifact["row_count"]:
            raise SystemExit(f"members row count drift: {artifact_path}")
        index_code = str(manifest["index_code"])
        pit_precision = str(manifest["pit_precision"])
        records.append(
            {
                "snapshot_date": snapshot_date.isoformat(),
                "visible_from": str(manifest["visible_from"]),
                "publish_lag_days": manifest["publish_lag_days"],
                "manifest_path": _repo_relative(manifest_path, repo_root),
                "manifest_sha256": _sha256(manifest_path),
                "generation_id": manifest["generation_id"],
                "members_sha256": artifact_sha256,
                "row_count": artifact["row_count"],
            }
        )
    return {
        "universe_id": universe_id,
        "index_code": index_code,
        "store_root": _repo_relative(store, repo_root),
        "pit_precision": pit_precision,
        "snapshot_count": len(records),
        "earliest_visible_date": store_reader.earliest_visible_date(universe_id).isoformat(),
        "verification": (
            "manifest validated against index_membership.v1, members digest recomputed "
            "from bytes and members re-read through IndexMembershipStore"
        ),
        "note": (
            "the store root is gitignored; these digests are re-derivable by "
            "re-running scripts/fetch_index_membership.py for the same window"
        ),
        "records": records,
    }


def evidence_index(
    layer: str,
    records: list[str],
    status: str,
    note: str,
    index_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    """Pin tracked files. An index that records itself can never verify (writing
    the record changes the file), so the producer refuses it instead of leaving
    the ban to a test."""
    relative_index = _repo_relative(index_path, repo_root)
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in records:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"evidence records must be repo-relative: {raw}")
        relative = path.as_posix()
        if relative == relative_index:
            raise SystemExit(f"an evidence index cannot record itself: {relative}")
        if relative in seen:
            raise SystemExit(f"duplicate evidence record: {relative}")
        target = (repo_root / path).resolve()
        if not target.is_file():
            raise SystemExit(f"missing evidence record: {raw}")
        seen.add(relative)
        entries.append({"path": relative, "sha256": _sha256(target)})
    entries.sort(key=lambda entry: entry["path"])
    return {
        "layer": layer,
        "record_version": 1,
        "status": status,
        "note": note,
        "records": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("bars", "membership", "index"), required=True)
    parser.add_argument("--store", type=Path, default=None)
    parser.add_argument("--universe-id", default=None)
    parser.add_argument("--start", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--schema", type=Path, default=Path("config/schemas/bars_daily.v2.yaml"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--layer", default="a500_pilot_ingest")
    parser.add_argument("--status", default="archived")
    parser.add_argument(
        "--note",
        default="digests recomputed by scripts/build_store_evidence.py at the recorded commit",
    )
    parser.add_argument("--record", action="append", default=[], help="repo-relative tracked file to pin")
    args = parser.parse_args(argv)

    if args.kind == "bars":
        missing = [name for name in ("store", "universe_id", "start", "end") if getattr(args, name) is None]
        if missing:
            raise SystemExit(f"--kind bars also requires: {', '.join('--' + item.replace('_', '-') for item in missing)}")
        payload = bars_inventory(args.store, args.universe_id, args.start, args.end, args.schema, REPO_ROOT)
    elif args.kind == "membership":
        if args.store is None or args.universe_id is None:
            raise SystemExit("--kind membership also requires --store and --universe-id")
        payload = membership_inventory(args.store, args.universe_id, REPO_ROOT)
    else:
        if not args.record:
            raise SystemExit("--kind index requires at least one --record")
        payload = evidence_index(args.layer, args.record, args.status, args.note, args.out, REPO_ROOT)

    _write(args.out, payload)
    counted = payload.get("partition_count", payload.get("snapshot_count", len(payload["records"])))
    print(json.dumps({"kind": args.kind, "out": str(args.out), "record_count": counted}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
