"""Announcement-anchored dividend/disclosure events (pendding item 6).

Tushare ``dividend`` is not an event table with a stable primary key: the same
``(ts_code, end_date, div_proc)`` disclosure is revised in place (a plan's
``cash_div_tax`` moved from 0 to 0.02 in one measured group) and exact duplicate
rows are returned as well. This store keeps the raw announcement record instead
of collapsing it to an inferred corporate-action event:

* the partition key is the disclosure date ``ann_date``, so a record is readable
  exactly when the market could have seen it (no ``ex_date`` lookahead);
* the row key is ``record_id`` = SHA-256 of the canonical normalized row, so
  byte-identical duplicates collapse to one row while each revised content
  revision survives as its own auditable record;
* ``ex_date``/``pay_date``/``imp_ann_date`` stay as fields, so a consumer that
  needs a confirmed implementation must filter on ``div_proc='实施'`` *and*
  re-check ``imp_ann_date`` against its own as-of date.

The store never infers an ex-date partition; ``XdxrAdjustmentDeriver`` remains
the only place that turns these records into an adjustment schedule.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from ..errors import ContractError
from .gate_contracts import canonical_json, sha256_json, validate_contract

EVENT_COLUMNS = [
    "record_id",
    "instrument",
    "end_date",
    "ann_date",
    "div_proc",
    "stk_div",
    "stk_bo_rate",
    "stk_co_rate",
    "cash_div",
    "cash_div_tax",
    "record_date",
    "ex_date",
    "pay_date",
    "div_listdate",
    "imp_ann_date",
]
NUMERIC_COLUMNS = ("stk_div", "stk_bo_rate", "stk_co_rate", "cash_div", "cash_div_tax")
DATE_COLUMNS = ("end_date", "ann_date", "record_date", "ex_date", "pay_date", "div_listdate", "imp_ann_date")
EVENT_STAGES = ("预披露", "预案", "股东大会通过", "实施")
FIELD_UNITS = {
    "stk_div": "shares per share, bonus plus capitalization (股/股)",
    "stk_bo_rate": "bonus shares per share (股/股)",
    "stk_co_rate": "capitalization shares per share (股/股)",
    "cash_div": "cash dividend per share after tax (元/股)",
    "cash_div_tax": "cash dividend per share before tax (元/股)",
}

_PROVIDER_SUFFIXES = {"SH": "XSHG", "SZ": "XSHE", "BJ": "XBSE"}
_PARTITION_DIR = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_BLANKS = {"", "none", "nan", "null", "nat", "-"}


def _to_instrument(ts_code: Any) -> str:
    code, _, exchange = str(ts_code).partition(".")
    if len(code) != 6 or not code.isdigit() or exchange not in _PROVIDER_SUFFIXES:
        raise ContractError(f"unsupported Tushare instrument code in dividend row: {ts_code!r}")
    return f"{code}.{_PROVIDER_SUFFIXES[exchange]}"


def _optional_float(value: Any, column: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, bool):
        raise ContractError(f"dividend {column} must be numeric, got a boolean")
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in _BLANKS:
            return None
        try:
            value = float(text)
        except ValueError as exc:
            raise ContractError(f"dividend {column} is not numeric: {value!r}") from exc
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        raise ContractError(f"dividend {column} is not numeric: {value!r}")
    if not math.isfinite(number):
        raise ContractError(f"dividend {column} is not finite: {value!r}")
    if number < 0:
        raise ContractError(f"dividend {column} is negative: {number}")
    return number


def _optional_date(value: Any, column: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.date().isoformat() if isinstance(value, dt.datetime) else value.isoformat()
    text = str(value).strip()
    if text.lower() in _BLANKS:
        return None
    text = text.replace("/", "-")
    try:
        if len(text) == 8 and text.isdigit():
            return dt.date(int(text[:4]), int(text[4:6]), int(text[6:])).isoformat()
        return dt.date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ContractError(f"dividend {column} is not a date: {value!r}") from exc


def normalize_dividend_event_rows(frame: pd.DataFrame, *, ann_date: dt.date) -> list[dict[str, Any]]:
    """Normalize one disclosure day into validated, content-keyed records."""
    required = set(EVENT_COLUMNS) - {"record_id", "instrument"} | {"ts_code"}
    missing = required - set(frame.columns)
    if missing:
        raise ContractError(f"dividend frame is missing columns: {sorted(missing)}")
    if frame.empty:
        raise ContractError("cannot publish an empty dividend disclosure partition")
    partition = ann_date.isoformat()
    records: dict[str, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        record: dict[str, Any] = {"instrument": _to_instrument(row["ts_code"])}
        for column in DATE_COLUMNS:
            record[column] = _optional_date(row[column], column)
        for column in NUMERIC_COLUMNS:
            record[column] = _optional_float(row[column], column)
        stage = row["div_proc"]
        if stage is None or (isinstance(stage, float) and math.isnan(stage)) or str(stage).strip() not in EVENT_STAGES:
            raise ContractError(f"unknown dividend disclosure stage: {stage!r}")
        record["div_proc"] = str(stage).strip()
        if record["ann_date"] != partition:
            raise ContractError(
                f"dividend row ann_date {record['ann_date']!r} does not match partition {partition}"
            )
        if record["end_date"] is None:
            raise ContractError("dividend row has no report period (end_date)")
        for column in ("ex_date", "imp_ann_date"):
            if record[column] is not None and record[column] < partition:
                raise ContractError(f"dividend {column} precedes ann_date for {record['instrument']}")
        record_id = hashlib.sha256(canonical_json(record)).hexdigest()
        records[record_id] = {"record_id": record_id, **record}
    ordered = sorted(
        records.values(),
        key=lambda item: (item["instrument"], item["end_date"], item["div_proc"], item["record_id"]),
    )
    return ordered


def render_events_csv(records: Iterable[dict[str, Any]]) -> bytes:
    """Serialize records with a stable, locale-independent text encoding."""
    lines = [",".join(EVENT_COLUMNS)]
    for record in records:
        cells: list[str] = []
        for column in EVENT_COLUMNS:
            value = record[column]
            if value is None:
                cell = ""
            elif isinstance(value, float):
                cell = repr(value)
            else:
                cell = str(value)
            if "," in cell or "\n" in cell or '"' in cell:
                raise ContractError(f"dividend cell needs escaping, refusing to render: {column}={cell!r}")
            cells.append(cell)
        lines.append(",".join(cells))
    return ("\n".join(lines) + "\n").encode("utf-8")


class DividendEventStore:
    """Immutable, announcement-anchored dividend disclosure store."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _partition(self, ann_date: dt.date) -> Path:
        return self.root / "dividend_events" / ann_date.isoformat()

    def publish(
        self,
        *,
        ann_date: dt.date,
        source: str,
        source_fetched_at: dt.datetime,
        frame: pd.DataFrame,
    ) -> Path:
        if source_fetched_at.tzinfo is None:
            raise ContractError("source_fetched_at must be timezone-aware")
        records = normalize_dividend_event_rows(frame, ann_date=ann_date)
        content = render_events_csv(records)
        document: dict[str, Any] = {
            "partition_version": 1,
            "ann_date": ann_date.isoformat(),
            "source": source,
            "source_fetched_at": source_fetched_at.isoformat(),
            "event_count": len(records),
            "instrument_count": len({record["instrument"] for record in records}),
            "field_units": FIELD_UNITS,
            "events_artifact": {
                "path": "events.csv",
                "row_count": len(records),
                "checksum_sha256": hashlib.sha256(content).hexdigest(),
                "columns": list(EVENT_COLUMNS),
            },
        }
        document["generation_id"] = sha256_json(document)
        validate_contract("dividend_event_partition.v1.json", document)
        directory = self._partition(ann_date)
        if directory.exists():
            raise ContractError(f"immutable dividend partition already published: {directory}")
        staging = directory.with_name(f"{directory.name}.staging.{uuid.uuid4().hex}")
        staging.mkdir(parents=True)
        try:
            (staging / "events.csv").write_bytes(content)
            (staging / "manifest.json").write_text(
                json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
            directory.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(directory)
        except Exception:
            for path in staging.glob("*"):
                path.unlink()
            staging.rmdir()
            raise
        return directory

    def read_manifest(self, ann_date: dt.date) -> dict[str, Any]:
        manifest_path = self._partition(ann_date) / "manifest.json"
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ContractError(f"missing or malformed dividend partition manifest: {ann_date}") from exc
        validate_contract("dividend_event_partition.v1.json", document)
        if document["ann_date"] != ann_date.isoformat():
            raise ContractError("dividend partition directory and manifest date disagree")
        if document["generation_id"] != sha256_json(
            {key: value for key, value in document.items() if key != "generation_id"}
        ):
            raise ContractError("dividend partition generation mismatch")
        artifact = document["events_artifact"]
        name = Path(artifact["path"])
        if name.name != artifact["path"] or name.name in {".", ".."}:
            raise ContractError("unsafe dividend events artifact path")
        return document

    def read_events(self, ann_date: dt.date) -> pd.DataFrame:
        document = self.read_manifest(ann_date)
        artifact = document["events_artifact"]
        path = self._partition(ann_date) / artifact["path"]
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise ContractError(f"missing dividend events artifact: {path}") from exc
        if hashlib.sha256(content).hexdigest() != artifact["checksum_sha256"]:
            raise ContractError("tampered dividend events bytes")
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        if list(frame.columns) != artifact["columns"]:
            raise ContractError("dividend events columns do not match the manifest")
        if len(frame) != artifact["row_count"]:
            raise ContractError("dividend events row count mismatch")
        records = frame.to_dict(orient="records")
        for record in records:
            normalized = {column: (record[column] or None) for column in EVENT_COLUMNS}
            for column in NUMERIC_COLUMNS:
                normalized[column] = None if normalized[column] is None else float(normalized[column])
            expected = hashlib.sha256(
                canonical_json({key: value for key, value in normalized.items() if key != "record_id"})
            ).hexdigest()
            if expected != normalized["record_id"]:
                raise ContractError("dividend record_id does not match its content")
        for column in DATE_COLUMNS:
            frame[column] = frame[column].where(frame[column] != "", None)
        for column in NUMERIC_COLUMNS:
            frame[column] = pd.to_numeric(frame[column].where(frame[column] != "", None))
        return frame

    def list_partitions(self) -> list[dt.date]:
        directory = self.root / "dividend_events"
        if not directory.is_dir():
            return []
        return sorted(
            dt.date.fromisoformat(child.name)
            for child in directory.iterdir()
            if child.is_dir() and _PARTITION_DIR.fullmatch(child.name)
        )

    def as_of(self, as_of_date: dt.date) -> pd.DataFrame:
        """Every disclosure announced on or before ``as_of_date`` (PIT read)."""
        frames = [self.read_events(ann_date) for ann_date in self.list_partitions() if ann_date <= as_of_date]
        if not frames:
            return pd.DataFrame(columns=EVENT_COLUMNS)
        combined = pd.concat(frames, ignore_index=True)
        return combined.sort_values(
            ["ann_date", "instrument", "end_date", "div_proc", "record_id"], kind="stable"
        ).reset_index(drop=True)
