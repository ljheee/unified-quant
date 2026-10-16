"""Reconcile the two corporate-action event sources against each other.

The adjusted-price chain can derive factors from either of two event sources:
Tushare ``dividend`` disclosures mapped into the exchange event shape, and TDX
``xdxr`` events as published.  Each is separately reconciled against the vendor's
own cumulative ``adj_factor``, but that leaves a real gap: if the *vendor's* factor
is wrong, both derivations agree with it and the error passes.  Comparing the two
event sources directly is what catches that, and it is what the 2026-10-16
investigation had to be done by hand.

Both sources land in the same eight-column exchange shape, so the comparison is
field by field on the ex-date:

* a date present in one source and absent in the other is a *coverage* difference
  and is always reported, because a missing event silently under-adjusts;
* amounts are compared within a tolerance expressed per 10 shares (TDX publishes
  them that way), so a rounding difference does not read as a disagreement.

The result never picks a winner.  It reports both values and the verdict
``agree`` / ``diverge`` / ``coverage_only``, because which side is right has to be
settled by the vendor's factor jump, which is a separate check.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from ..errors import ContractError

_EXCHANGE_COLUMNS = ("year", "month", "day", "category", "fenhong", "songzhuangu", "peigu", "peigujia")
# TDX publishes amounts per 10 shares; the disclosure mapping multiplies per-share
# values by 10, so both sides are already per 10 shares here.
_DEFAULT_TOLERANCE_PER_TEN = 1e-6


@dataclass(frozen=True)
class EventDivergence:
    session: dt.date
    field: str
    disclosure: float | None
    tdx: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.isoformat(),
            "field": self.field,
            "disclosure": self.disclosure,
            "tdx": self.tdx,
        }


@dataclass(frozen=True)
class ReconciliationResult:
    instrument: str
    compared: int
    agreements: int
    divergences: tuple[EventDivergence, ...]
    disclosure_only: tuple[dt.date, ...]
    tdx_only: tuple[dt.date, ...]

    @property
    def status(self) -> str:
        """One of ``agreed`` / ``amount_diverged`` / ``coverage_diverged`` / ``no_overlap``.

        Amount and coverage differences are kept apart on purpose: a disagreeing
        amount is a data-quality question about a shared event, while a date only
        one source knows about is a missing event that silently under-adjusts the
        derived factor. Folding them into one "diverged" would make the run summary
        unable to say which kind of problem it found.
        """
        if self.compared == 0:
            return "no_overlap"
        if self.divergences:
            return "amount_diverged"
        if self.disclosure_only or self.tdx_only:
            return "coverage_diverged"
        return "agreed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "status": self.status,
            "compared": self.compared,
            "agreements": self.agreements,
            "divergences": [item.as_dict() for item in self.divergences],
            "divergence_count": len(self.divergences),
            "disclosure_only_dates": [day.isoformat() for day in self.disclosure_only],
            "tdx_only_dates": [day.isoformat() for day in self.tdx_only],
        }


def _session_of(row: Mapping[str, Any]) -> dt.date:
    return dt.date(int(row["year"]), int(row["month"]), int(row["day"]))


def _index_by_session(frame: pd.DataFrame) -> dict[dt.date, Mapping[str, Any]]:
    if frame is None or frame.empty:
        return {}
    missing = sorted({"year", "month", "day"} - set(frame.columns))
    if missing:
        raise ContractError(f"event frame missing date columns: {missing}")
    out: dict[dt.date, Mapping[str, Any]] = {}
    for row in frame.to_dict(orient="records"):
        session = _session_of(row)
        if session in out:
            raise ContractError(
                f"duplicate event date {session.isoformat()}; resolve the source's own "
                "revisions before reconciling the two sources"
            )
        out[session] = row
    return out


def _amount(row: Mapping[str, Any], field: str) -> float | None:
    if field not in row:
        return None
    value = row[field]
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    return float(value)


def reconcile_instrument(
    instrument: str,
    disclosure_events: pd.DataFrame,
    tdx_events: pd.DataFrame,
    *,
    tolerance: float = _DEFAULT_TOLERANCE_PER_TEN,
) -> ReconciliationResult:
    """Compare one instrument's events across the two sources."""
    left = _index_by_session(disclosure_events)
    right = _index_by_session(tdx_events)
    compared = agreements = 0
    divergences: list[EventDivergence] = []
    for session in sorted(set(left) & set(right)):
        compared += 1
        row_divergences: list[EventDivergence] = []
        for field in ("fenhong", "songzhuangu", "peigu", "peigujia"):
            disclosure_value = _amount(left[session], field)
            tdx_value = _amount(right[session], field)
            if disclosure_value is None or tdx_value is None:
                if disclosure_value is not tdx_value:
                    row_divergences.append(EventDivergence(session, field, disclosure_value, tdx_value))
                continue
            if abs(disclosure_value - tdx_value) > tolerance:
                row_divergences.append(EventDivergence(session, field, disclosure_value, tdx_value))
        if row_divergences:
            divergences.extend(row_divergences)
        else:
            agreements += 1
    return ReconciliationResult(
        instrument=instrument,
        compared=compared,
        agreements=agreements,
        divergences=tuple(divergences),
        disclosure_only=tuple(sorted(set(left) - set(right))),
        tdx_only=tuple(sorted(set(right) - set(left))),
    )


def reconcile_instruments(
    instruments: Sequence[str],
    disclosure_events: Mapping[str, pd.DataFrame],
    tdx_events: Mapping[str, pd.DataFrame],
    *,
    tolerance: float = _DEFAULT_TOLERANCE_PER_TEN,
) -> tuple[ReconciliationResult, ...]:
    """Reconcile several instruments, in the order given."""
    return tuple(
        reconcile_instrument(
            instrument,
            disclosure_events.get(instrument, pd.DataFrame()),
            tdx_events.get(instrument, pd.DataFrame()),
            tolerance=tolerance,
        )
        for instrument in instruments
    )


def summarize(results: Iterable[ReconciliationResult]) -> dict[str, Any]:
    """A run-level view: which instruments diverged and by how much."""
    collected = list(results)
    amount_diverged = [item for item in collected if item.status == "amount_diverged"]
    coverage_diverged = [item for item in collected if item.status == "coverage_diverged"]
    return {
        "instruments_compared": len(collected),
        "agreed": len([item for item in collected if item.status == "agreed"]),
        "no_overlap": len([item for item in collected if item.status == "no_overlap"]),
        "amount_diverged": len(amount_diverged),
        "coverage_diverged": len(coverage_diverged),
        "amount_diverged_instruments": [item.instrument for item in amount_diverged],
        "coverage_diverged_instruments": [item.instrument for item in coverage_diverged],
        "records": [item.as_dict() for item in collected],
    }
