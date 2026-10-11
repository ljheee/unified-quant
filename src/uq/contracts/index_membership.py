from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from ..errors import ContractError
from .gate_contracts import adjustment_snapshot_generation, sha256_json, validate_contract

_MEMBER_COLUMNS = ["instrument"]
_UNIVERSE_ID = re.compile(r"[a-z][a-z0-9_-]{1,63}")
_SNAPSHOT_DIR = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _validate_calendar_fields(document: dict[str, Any]) -> None:
    for field in ("snapshot_date", "visible_from"):
        try:
            date.fromisoformat(document[field])
        except (TypeError, ValueError) as exc:
            raise ContractError(f"invalid calendar date in index membership manifest: {field}") from exc


def visible_from(snapshot_date: date, publish_lag_days: int, trading_days: list[date]) -> date:
    if publish_lag_days < 0:
        raise ContractError("publish_lag_days must be non-negative")
    ordered = sorted(day for day in trading_days if day > snapshot_date)
    if len(ordered) <= publish_lag_days:
        raise ContractError("insufficient trading calendar to derive visible_from")
    return ordered[publish_lag_days]


def build_members_bytes(members: pd.DataFrame) -> bytes:
    if list(members.columns) != _MEMBER_COLUMNS:
        raise ContractError("index membership columns must be exactly instrument")
    if members["instrument"].duplicated().any():
        raise ContractError("duplicate instrument in index membership snapshot")
    for value in members["instrument"]:
        validate_contract("index_membership_row.v1.json", {"instrument": value})
    ordered = members.sort_values("instrument").reset_index(drop=True)
    return ordered.to_csv(index=False, lineterminator="\n").encode("utf-8")


class IndexMembershipStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def publish(
        self,
        *,
        universe_id: str,
        index_code: str,
        source: str,
        source_fetched_at: datetime,
        snapshot_date: date,
        publish_lag_days: int,
        trading_days: list[date],
        members: pd.DataFrame,
    ) -> Path:
        if not _UNIVERSE_ID.fullmatch(universe_id):
            raise ContractError("unsafe universe identifier")
        if source_fetched_at.tzinfo is None:
            raise ContractError("source_fetched_at must be timezone-aware")
        content = build_members_bytes(members)
        members_sha = hashlib.sha256(content).hexdigest()
        document = {
            "snapshot_version": 1,
            "universe_id": universe_id,
            "index_code": index_code,
            "source": source,
            "source_fetched_at": source_fetched_at.isoformat(),
            "snapshot_date": snapshot_date.isoformat(),
            "visible_from": visible_from(snapshot_date, publish_lag_days, trading_days).isoformat(),
            "publish_lag_days": publish_lag_days,
            "pit_precision": "monthly_approximation",
            "members_artifact": {
                "path": "members.csv",
                "row_count": int(len(members)),
                "checksum_sha256": members_sha,
            },
        }
        document["generation_id"] = adjustment_snapshot_generation(document)
        validate_contract("index_membership_snapshot.v1.json", document)
        _validate_calendar_fields(document)
        directory = self.root / "index_membership" / universe_id / snapshot_date.isoformat()
        if directory.exists():
            raise ContractError(f"immutable index membership snapshot already published: {directory}")
        staging = directory.with_name(f"{directory.name}.staging.{uuid.uuid4().hex}")
        staging.mkdir(parents=True)
        try:
            (staging / "members.csv").write_bytes(content)
            (staging / "manifest.json").write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            directory.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(directory)
        except Exception:
            for path in staging.glob("*"):
                path.unlink()
            staging.rmdir()
            raise
        return directory

    def read_manifest(self, universe_id: str, snapshot_date: date) -> dict[str, Any]:
        if not _UNIVERSE_ID.fullmatch(universe_id):
            raise ContractError("unsafe universe identifier")
        manifest_path = self.root / "index_membership" / universe_id / snapshot_date.isoformat() / "manifest.json"
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ContractError(f"missing or malformed index membership manifest: {snapshot_date}") from exc
        validate_contract("index_membership_snapshot.v1.json", document)
        _validate_calendar_fields(document)
        if document["generation_id"] != adjustment_snapshot_generation(
            {key: value for key, value in document.items() if key != "generation_id"}
        ):
            raise ContractError("index membership generation mismatch")
        return document

    def read_members(self, universe_id: str, snapshot_date: date) -> pd.DataFrame:
        document = self.read_manifest(universe_id, snapshot_date)
        member_name = Path(document["members_artifact"]["path"])
        if member_name.name != document["members_artifact"]["path"] or member_name.name in {".", ".."}:
            raise ContractError("unsafe index membership artifact path")
        path = self.root / "index_membership" / universe_id / snapshot_date.isoformat() / member_name.name
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != document["members_artifact"]["checksum_sha256"]:
            raise ContractError("tampered index membership bytes")
        members = pd.read_csv(path, dtype={"instrument": str})
        if len(members) != document["members_artifact"]["row_count"]:
            raise ContractError("index membership row count mismatch")
        return members

    def _snapshot_dates(self, universe_id: str) -> list[date]:
        directory = self.root / "index_membership" / universe_id
        if not directory.is_dir():
            raise ContractError(f"no index membership snapshots for {universe_id}")
        return sorted(
            date.fromisoformat(child.name)
            for child in directory.iterdir()
            if child.is_dir() and _SNAPSHOT_DIR.fullmatch(child.name)
        )

    def earliest_visible_date(self, universe_id: str) -> date:
        visible = [
            date.fromisoformat(self.read_manifest(universe_id, snapshot_date)["visible_from"])
            for snapshot_date in self._snapshot_dates(universe_id)
        ]
        if not visible:
            raise ContractError(f"no index membership snapshots for {universe_id}")
        return min(visible)

    def as_of(self, universe_id: str, as_of: date) -> tuple[date, pd.DataFrame]:
        candidates = [
            snapshot_date
            for snapshot_date in self._snapshot_dates(universe_id)
            if date.fromisoformat(self.read_manifest(universe_id, snapshot_date)["visible_from"]) <= as_of
        ]
        if not candidates:
            earliest = self.earliest_visible_date(universe_id)
            raise ContractError(
                f"no index membership visible at {as_of}; earliest visible date is {earliest}"
            )
        selected = max(candidates)
        return selected, self.read_members(universe_id, selected)


def build_membership_set(
    store: IndexMembershipStore,
    *,
    universe_id: str,
    window_start: date,
    window_end: date,
    trading_days: list[date],
) -> dict[str, Any]:
    if window_end < window_start:
        raise ContractError("membership window end precedes start")
    window_days = [day for day in trading_days if window_start <= day <= window_end]
    if not window_days:
        raise ContractError("membership window has no trading days")
    snapshots = []
    index_codes: set[str] = set()
    for day in window_days:
        selected, _ = store.as_of(universe_id, day)
        manifest = store.read_manifest(universe_id, selected)
        index_codes.add(manifest["index_code"])
        if not snapshots or snapshots[-1]["snapshot_date"] != selected.isoformat():
            snapshots.append({
                "snapshot_date": selected.isoformat(),
                "visible_from": manifest["visible_from"],
                "generation_id": manifest["generation_id"],
                "manifest_digest_sha256": sha256_json(manifest),
                "members_checksum_sha256": manifest["members_artifact"]["checksum_sha256"],
            })
    if len(index_codes) != 1:
        raise ContractError("membership window mixes index codes")
    document = {
        "binding_type": "index_membership_set",
        "universe_id": universe_id,
        "index_code": index_codes.pop(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "snapshots": snapshots,
    }
    document["set_digest_sha256"] = sha256_json(
        {key: value for key, value in document.items() if key != "set_digest_sha256"}
    )
    return document


def verify_membership_set(
    store: IndexMembershipStore,
    document: dict[str, Any],
    trading_days: list[date],
) -> None:
    expected = sha256_json({key: value for key, value in document.items() if key != "set_digest_sha256"})
    if document.get("set_digest_sha256") != expected:
        raise ContractError("index membership set digest mismatch")
    if any(
        later["snapshot_date"] <= earlier["snapshot_date"]
        for earlier, later in zip(document["snapshots"], document["snapshots"][1:])
    ):
        raise ContractError("index membership set snapshots must be strictly ascending")
    for entry in document["snapshots"]:
        manifest = store.read_manifest(document["universe_id"], date.fromisoformat(entry["snapshot_date"]))
        if manifest["generation_id"] != entry["generation_id"]:
            raise ContractError("index membership set generation mismatch")
        if sha256_json(manifest) != entry["manifest_digest_sha256"]:
            raise ContractError("index membership set manifest digest mismatch")
        if manifest["members_artifact"]["checksum_sha256"] != entry["members_checksum_sha256"]:
            raise ContractError("index membership set members checksum mismatch")
    window_start = date.fromisoformat(document["window_start"])
    window_end = date.fromisoformat(document["window_end"])
    for day in (d for d in trading_days if window_start <= d <= window_end):
        visible = [
            entry for entry in document["snapshots"]
            if date.fromisoformat(entry["visible_from"]) <= day
        ]
        if not visible:
            raise ContractError(f"no visible index membership snapshot for {day}")
        latest = max(visible, key=lambda entry: entry["snapshot_date"])["snapshot_date"]
        selected, _ = store.as_of(document["universe_id"], day)
        if selected.isoformat() != latest:
            raise ContractError(f"index membership set selection is not latest visible for {day}")
