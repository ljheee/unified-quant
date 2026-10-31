"""Validation and settlement scheduling for v4 dividend receivables.

The backtest consumes a reviewed event panel rather than deriving economic
events from adjusted prices. Rights issues remain unsupported and their
instruments must be explicitly excluded by a complete, reviewed exclusion
list bound into the same panel.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from collections import defaultdict
from typing import Any

import pandas as pd

from ..contracts.gate_contracts import sha256_json
from ..errors import ContractError

ACTION_EVENT_COLUMNS = [
    "event_type", "event_id", "instrument", "record_date", "ex_date",
    "pay_date", "div_listdate", "cash_div_after_tax", "stock_div_per_share",
    "source_announcement_date", "implementation_announcement_date",
    "source_record_id", "source_approval_ref", "reason", "window_start",
    "window_end", "coverage_complete",
]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _date(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if nullable and (value is None or pd.isna(value) or value == ""):
        return None
    if not isinstance(value, str):
        raise ContractError(f"corporate-action {label} must be an ISO date")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ContractError(f"corporate-action {label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ContractError(f"corporate-action {label} must be a canonical ISO date")
    return value


def _amount(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"corporate-action {label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ContractError(f"corporate-action {label} must be finite and nonnegative")
    return result


def normalize_action_events(
    frame: pd.DataFrame,
    *,
    start_date: str,
    end_date: str,
    trading_dates: list[str],
) -> dict[str, Any]:
    """Validate the v4 panel and return date-indexed event schedules.

    Record and ex dates must be present in the price calendar. Payment and
    listing dates may fall after the backtest window; those receivables remain
    visible in terminal NAV and are not made available early.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty or frame.columns.tolist() != ACTION_EVENT_COLUMNS:
        raise ContractError("v4 corporate-action panel has an invalid or empty schema")
    if not trading_dates or trading_dates != sorted(set(trading_dates)):
        raise ContractError("v4 corporate-action calendar must be ordered and unique")
    if trading_dates[0] != start_date or trading_dates[-1] != end_date:
        raise ContractError("v4 corporate-action calendar does not cover the configured window")
    if frame["event_id"].duplicated().any():
        raise ContractError("duplicate v4 corporate-action event id")

    coverage = frame.loc[frame["event_type"] == "rights_issue_coverage"]
    if len(coverage) != 1:
        raise ContractError("v4 requires exactly one rights-issue exclusion coverage record")
    coverage_row = coverage.iloc[0]
    coverage_complete = coverage_row["coverage_complete"]
    if (coverage_row["instrument"] != "__RIGHTS_COVERAGE__"
            or coverage_row["window_start"] != start_date
            or coverage_row["window_end"] != end_date
            or (not isinstance(coverage_complete, bool)
                and type(coverage_complete).__name__ != "bool_")
            or not bool(coverage_complete)
            or not isinstance(coverage_row["source_approval_ref"], str)
            or not coverage_row["source_approval_ref"].strip()):
        raise ContractError("rights-issue exclusion coverage is incomplete or misbound")
    if (not isinstance(coverage_row["event_id"], str)
            or not _SHA256.fullmatch(coverage_row["event_id"])
            or coverage_row["event_id"] != sha256_json({
                "event_type": "rights_issue_coverage",
                "window_start": start_date,
                "window_end": end_date,
                "source_approval_ref": coverage_row["source_approval_ref"],
            })):
        raise ContractError("rights-issue coverage identity is invalid")

    dividend_coverage = frame.loc[frame["event_type"] == "dividend_coverage"]
    if len(dividend_coverage) != 1:
        raise ContractError("v4 requires exactly one complete dividend-source coverage record")
    dividend_coverage_row = dividend_coverage.iloc[0]
    dividend_complete = dividend_coverage_row["coverage_complete"]
    approval_ref = dividend_coverage_row["source_approval_ref"]
    if (dividend_coverage_row["instrument"] != "__DIVIDEND_COVERAGE__"
            or dividend_coverage_row["window_start"] != start_date
            or dividend_coverage_row["window_end"] != end_date
            or (not isinstance(dividend_complete, bool)
                and type(dividend_complete).__name__ != "bool_")
            or not bool(dividend_complete)
            or not isinstance(approval_ref, str) or not approval_ref.strip()
            or not isinstance(dividend_coverage_row["event_id"], str)
            or not _SHA256.fullmatch(dividend_coverage_row["event_id"])
            or dividend_coverage_row["event_id"] != sha256_json({
                "event_type": "dividend_coverage", "window_start": start_date,
                "window_end": end_date, "source_approval_ref": approval_ref,
            })):
        raise ContractError("dividend-source coverage is incomplete or misbound")

    known_types = {"dividend", "rights_issue_exclusion", "rights_issue_coverage", "dividend_coverage"}
    if not set(frame["event_type"]).issubset(known_types):
        raise ContractError("v4 corporate-action panel contains an unsupported event type")
    dividend_rows = frame.loc[frame["event_type"] == "dividend"]
    rights_rows = frame.loc[frame["event_type"] == "rights_issue_exclusion"]
    if rights_rows["instrument"].duplicated().any():
        raise ContractError("duplicate instrument in rights-issue exclusion list")

    record_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ex_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pay_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    listing_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    normalized_dividends = []
    date_set = set(trading_dates)
    for row in dividend_rows.to_dict(orient="records"):
        instrument = row["instrument"]
        if not isinstance(instrument, str) or not instrument or instrument.startswith("__"):
            raise ContractError("dividend event has an invalid instrument")
        event_id = row["event_id"]
        source_record_id = row["source_record_id"]
        if (not isinstance(event_id, str) or not _SHA256.fullmatch(event_id)
                or event_id != source_record_id or not isinstance(source_record_id, str)
                or not isinstance(row["source_approval_ref"], str)
                or not row["source_approval_ref"].strip()):
            raise ContractError("dividend event must bind its source record id")
        record_date = _date(row["record_date"], "record_date")
        ex_date = _date(row["ex_date"], "ex_date")
        pay_date = _date(row["pay_date"], "pay_date", nullable=True)
        list_date = _date(row["div_listdate"], "div_listdate", nullable=True)
        announcement = _date(row["source_announcement_date"], "source_announcement_date")
        implementation = _date(
            row["implementation_announcement_date"], "implementation_announcement_date"
        )
        cash_per_share = _amount(row["cash_div_after_tax"], "cash_div_after_tax")
        stock_per_share = _amount(row["stock_div_per_share"], "stock_div_per_share")
        if record_date not in date_set or ex_date not in date_set or not record_date < ex_date:
            raise ContractError("dividend record/ex dates must be ordered sessions inside the backtest window")
        if announcement > ex_date or implementation > ex_date:
            raise ContractError("dividend implementation was not visible by ex date")
        if cash_per_share > 0 and (pay_date is None or pay_date < ex_date):
            raise ContractError("cash dividend requires a pay date on or after ex date")
        if stock_per_share > 0 and (list_date is None or list_date < ex_date):
            raise ContractError("stock dividend requires a listing date on or after ex date")
        if cash_per_share == 0 and stock_per_share == 0:
            raise ContractError("implemented dividend event has no cash or stock distribution")
        event = {
            "event_id": event_id,
            "instrument": instrument,
            "record_date": record_date,
            "ex_date": ex_date,
            "pay_date": pay_date,
            "div_listdate": list_date,
            "cash_div_after_tax": cash_per_share,
            "stock_div_per_share": stock_per_share,
            "record_shares": None,
            "cash_receivable": 0.0,
            "stock_receivable_shares": 0,
        }
        normalized_dividends.append(event)
        record_events[record_date].append(event)
        ex_events[ex_date].append(event)
        if pay_date is not None:
            settle_on = next((session for session in trading_dates if session >= pay_date), None)
            if settle_on is not None:
                pay_events[settle_on].append(event)
        if list_date is not None:
            settle_on = next((session for session in trading_dates if session >= list_date), None)
            if settle_on is not None:
                listing_events[settle_on].append(event)

    rights_instruments = set()
    approval_ref = coverage_row["source_approval_ref"]
    for row in rights_rows.to_dict(orient="records"):
        instrument = row["instrument"]
        if not isinstance(instrument, str) or not instrument or instrument.startswith("__"):
            raise ContractError("rights-issue exclusion has an invalid instrument")
        if (row["source_approval_ref"] != approval_ref or not isinstance(row["reason"], str)
                or not row["reason"].strip()):
            raise ContractError("rights-issue exclusion does not match its coverage attestation")
        expected_id = sha256_json({
            "event_type": "rights_issue_exclusion", "instrument": instrument,
            "source_approval_ref": approval_ref, "reason": row["reason"],
            "window_start": start_date, "window_end": end_date,
        })
        if (not isinstance(row["event_id"], str) or not _SHA256.fullmatch(row["event_id"])
                or row["event_id"] != expected_id):
            raise ContractError("rights-issue exclusion does not match its coverage attestation")
        rights_instruments.add(instrument)

    return {
        "record_events": dict(record_events),
        "ex_events": dict(ex_events),
        "pay_events": dict(pay_events),
        "listing_events": dict(listing_events),
        "dividend_events": normalized_dividends,
        "rights_issue_coverage": coverage_row.to_dict(),
        "dividend_coverage": dividend_coverage_row.to_dict(),
        "rights_issue_instruments": rights_instruments,
    }


def rights_issue_coverage_row(
    *, start_date: str, end_date: str, source_approval_ref: str,
) -> dict[str, Any]:
    """Create the required signed-scope marker for an exclusion list."""
    if not isinstance(source_approval_ref, str) or not source_approval_ref.strip():
        raise ContractError("rights-issue exclusion requires a source approval reference")
    identity = sha256_json({
        "event_type": "rights_issue_coverage", "window_start": start_date,
        "window_end": end_date, "source_approval_ref": source_approval_ref,
    })
    return {
        "event_type": "rights_issue_coverage", "event_id": identity,
        "instrument": "__RIGHTS_COVERAGE__", "record_date": None, "ex_date": None,
        "pay_date": None, "div_listdate": None, "cash_div_after_tax": 0.0,
        "stock_div_per_share": 0.0, "source_announcement_date": None,
        "implementation_announcement_date": None, "source_record_id": None,
        "source_approval_ref": source_approval_ref, "reason": "coverage_attestation",
        "window_start": start_date, "window_end": end_date, "coverage_complete": True,
    }


def dividend_coverage_row(
    *, start_date: str, end_date: str, source_approval_ref: str,
) -> dict[str, Any]:
    """Create the required reviewed completeness marker for dividend disclosures."""
    if not isinstance(source_approval_ref, str) or not source_approval_ref.strip():
        raise ContractError("dividend-event coverage requires a source approval reference")
    identity = sha256_json({
        "event_type": "dividend_coverage", "window_start": start_date,
        "window_end": end_date, "source_approval_ref": source_approval_ref,
    })
    return {
        "event_type": "dividend_coverage", "event_id": identity,
        "instrument": "__DIVIDEND_COVERAGE__", "record_date": None, "ex_date": None,
        "pay_date": None, "div_listdate": None, "cash_div_after_tax": 0.0,
        "stock_div_per_share": 0.0, "source_announcement_date": None,
        "implementation_announcement_date": None, "source_record_id": None,
        "source_approval_ref": source_approval_ref, "reason": "complete_dividend_source_coverage",
        "window_start": start_date, "window_end": end_date, "coverage_complete": True,
    }

