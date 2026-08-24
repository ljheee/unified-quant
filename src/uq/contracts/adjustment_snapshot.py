from __future__ import annotations

import csv
import io
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .gate_contracts import adjustment_snapshot_generation, validate_contract
from .canonical_v2 import file_sha256_bytes
from ..errors import ContractError


_EVENT_COLUMNS = ["year", "month", "day", "category", "fenhong", "songzhuangu", "peigu", "peigujia"]
_EFFECTIVE_COLUMNS = ["session_date", "effective_date", "source_event_id"]
_SESSION_COLUMNS = ["session_date"]


def _csv_checksum(frame: pd.DataFrame) -> tuple[bytes, str]:
    content = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return content, file_sha256_bytes(content)


@dataclass(frozen=True)
class AdjustmentSnapshot:
    generation_id: str
    manifest: dict[str, Any]
    events: pd.DataFrame
    effective_dates: pd.DataFrame
    sessions: pd.DataFrame


class AdjustmentSnapshotStore:
    def save(
        self,
        root: Path,
        *,
        instrument: str,
        visibility_time: datetime,
        window_start: date,
        window_end: date,
        events: pd.DataFrame,
        effective_dates: pd.DataFrame,
        sessions: list[date],
    ) -> Path:
        try:
            date.fromisoformat(window_start.isoformat())
            date.fromisoformat(window_end.isoformat())
        except ValueError as exc:
            raise ContractError("adjustment snapshot window contains invalid calendar dates") from exc
        if window_start > window_end:
            raise ContractError("adjustment snapshot window start exceeds end")
        session_dates = sorted(set(sessions))
        if not session_dates or session_dates[0] != window_start or session_dates[-1] != window_end:
            raise ContractError("sessions do not cover adjustment snapshot window")
        events = events.sort_values(["year", "month", "day"], kind="stable").reset_index(drop=True)
        effective_dates = effective_dates.sort_values("session_date", kind="stable").reset_index(drop=True)
        sessions_frame = pd.DataFrame({"session_date": [value.isoformat() for value in session_dates]})
        event_content, event_checksum = _csv_checksum(events)
        effective_content, effective_checksum = _csv_checksum(effective_dates)
        _, sessions_checksum = _csv_checksum(sessions_frame)

        unsigned = {
            "snapshot_version": 1,
            "instrument": instrument,
            "formula_version": "adj_factor.exchange_v1",
            "visibility_time": visibility_time.isoformat(),
            "event_artifact": {"path": "events.csv", "checksum_sha256": event_checksum},
            "effective_date_table": {"path": "effective_dates.csv", "checksum_sha256": effective_checksum},
            "sessions": {"start_date": window_start.isoformat(), "end_date": window_end.isoformat(), "checksum_sha256": sessions_checksum},
        }
        generation_id = adjustment_snapshot_generation(unsigned)
        manifest = {**unsigned, "generation_id": generation_id}
        validate_contract("adjustment_snapshot.v1.json", manifest)
        directory = root / "adjustments" / "snapshots" / instrument / generation_id
        staging = directory.with_name(f"{directory.name}.staging.{uuid.uuid4().hex}")
        staging.mkdir(parents=True)
        try:
            (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            (staging / "events.csv").write_bytes(event_content)
            (staging / "effective_dates.csv").write_bytes(effective_content)
            (staging / "sessions.csv").write_bytes(sessions_frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))
            for path in staging.iterdir():
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            descriptor = os.open(staging, os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
            directory.parent.mkdir(parents=True, exist_ok=True)
            if directory.exists():
                raise ContractError(f"immutable adjustment snapshot already published: {directory}")
            os.replace(staging, directory)
            parent_descriptor = os.open(directory.parent, os.O_RDONLY)
            try: os.fsync(parent_descriptor)
            finally: os.close(parent_descriptor)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return directory


def _validate_calendar_dates(*values: str) -> None:
    for value in values:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ContractError(f"invalid adjustment snapshot calendar date: {value}") from exc


class AdjustmentSnapshotReader:
    def __init__(self, root: Path) -> None:
        self.root = root

    def read_manifest(self, instrument: str, generation_id: str) -> dict[str, Any]:
        path = self._manifest_path(instrument, generation_id)
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ContractError(f"missing or malformed adjustment snapshot: {instrument}/{generation_id}") from exc
        validate_contract("adjustment_snapshot.v1.json", manifest)
        _validate_calendar_dates(manifest["sessions"]["start_date"], manifest["sessions"]["end_date"])
        expected_generation = adjustment_snapshot_generation({key: value for key, value in manifest.items() if key != "generation_id"})
        if manifest["generation_id"] != expected_generation:
            raise ContractError("adjustment snapshot generation mismatch")
        return manifest

    def read_events(self, instrument: str, generation_id: str) -> pd.DataFrame:
        return self._read_csv(instrument, generation_id, "event_artifact", _EVENT_COLUMNS)

    def read_effective_dates(self, instrument: str, generation_id: str) -> pd.DataFrame:
        return self._read_csv(instrument, generation_id, "effective_date_table", _EFFECTIVE_COLUMNS)

    def select_visible_generation(
        self,
        *,
        instrument: str,
        window_start: date,
        window_end: date,
        as_of_time: datetime,
        candidates: list[str],
    ) -> str:
        visible: list[str] = []
        for generation_id in candidates:
            manifest = self.read_manifest(instrument, generation_id)
            if datetime.fromisoformat(manifest["visibility_time"]) > as_of_time:
                continue
            sessions = self._read_csv(instrument, generation_id, "sessions", _SESSION_COLUMNS)["session_date"].tolist()
            session_manifest = manifest["sessions"]
            expected_dates = pd.date_range(session_manifest["start_date"], session_manifest["end_date"], freq="D").strftime("%Y-%m-%d").tolist()
            if (
                session_manifest["start_date"] != window_start.isoformat()
                or session_manifest["end_date"] != window_end.isoformat()
            ):
                continue
            if sessions != expected_dates:
                raise ContractError("invalid adjustment snapshot session coverage")
            visible.append(generation_id)
        if len(visible) != 1:
            raise ContractError(f"ambiguous or absent visible adjustment snapshot: {len(visible)}")
        return visible[0]

    def load(self, instrument: str, generation_id: str) -> AdjustmentSnapshot:
        directory = self._manifest_path(instrument, generation_id).parent
        allowed = {"manifest.json", "events.csv", "effective_dates.csv", "sessions.csv"}
        actual = {path.name for path in directory.iterdir()}
        if not actual <= allowed:
            raise ContractError(f"unexpected adjustment snapshot files: {sorted(actual - allowed)}")
        return AdjustmentSnapshot(
            generation_id=generation_id,
            manifest=self.read_manifest(instrument, generation_id),
            events=self.read_events(instrument, generation_id),
            effective_dates=self.read_effective_dates(instrument, generation_id),
            sessions=self._read_csv(instrument, generation_id, "sessions", _SESSION_COLUMNS),
        )

    def _manifest_path(self, instrument: str, generation_id: str) -> Path:
        return self.root / "adjustments" / "snapshots" / instrument / generation_id / "manifest.json"

    def _read_csv(self, instrument: str, generation_id: str, artifact_field: str, columns: list[str]) -> pd.DataFrame:
        manifest = self.read_manifest(instrument, generation_id)
        artifact = manifest[artifact_field]
        artifact_path = artifact.get("path", "sessions.csv")
        path = self._manifest_path(instrument, generation_id).parent / artifact_path
        if Path(path.name) != Path(artifact_path):
            raise ContractError(f"unsafe adjustment snapshot artifact path: {artifact_path}")
        try: content = path.read_bytes()
        except FileNotFoundError as exc:
            raise ContractError(f"missing adjustment snapshot artifact: {artifact_path}") from exc
        if file_sha256_bytes(content) != artifact["checksum_sha256"]:
            raise ContractError(f"adjustment snapshot checksum mismatch: {artifact_path}")
        frame = pd.read_csv(io.BytesIO(content))
        if list(frame.columns) != columns:
            raise ContractError(f"adjustment snapshot artifact column mismatch: {artifact_path}")
        return frame
