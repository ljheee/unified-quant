"""Derive governed adjustment snapshots from Tushare dividend disclosures.

This is the production caller ``XdxrAdjustmentDeriver`` was missing (pendding
item 6). The pipeline is:

    DividendEventStore (announcement-anchored records)
        -> implemented events mapped to the exchange XDXR shape
        -> XdxrAdjustmentDeriver (adj_factor.exchange_v2, multiply basis)
        -> AdjustmentSnapshotStore (the snapshot the factor side consumes)

Mapping rules, measured rather than assumed:

* only ``div_proc == '实施'`` records are applied: a plan or a shareholder
  meeting cannot move the reference price;
* ``ex_date`` must fall inside the derived window; an implemented record with
  no ex-date is dropped with an explicit reason, never guessed;
* the pre-tax per-share cash ``cash_div_tax`` is used because the exchange
  formula takes the pre-tax dividend; a null value fails closed instead of
  falling back to the after-tax field;
* Tushare reports per-share amounts (measured: CATL's 2022 cash 2.52 and bonus
  0.8 reproduce the official 2023-04-26 factor ratio 0.55214 under the per
  share formula), while the deriver's ``fenhong``/``songzhuangu`` are
  per-ten-share, so the mapped values are multiplied by ten;
* rights issues are not part of the Tushare ``dividend`` endpoint, so
  ``peigu``/``peigujia`` stay zero; the coverage is declared in the result's
  provenance instead of being implied;
* two distinct implementations on one ex-date with different amounts are an
  ambiguity and fail closed; byte-identical revisions already collapsed in the
  store because it keys rows by content;
* an implemented record whose ``stk_div`` disagrees with the
  ``stk_bo_rate + stk_co_rate`` breakdown, whose ``imp_ann_date`` follows its
  own ``ex_date``, or whose ``ex_date`` falls on a day that is not a bar
  session is dirty disclosure data and fails closed: the deriver can only
  apply an event on a trading session, so publishing such a record would
  write an ``events.csv`` its own effective-date table contradicts.

Every derivation is cross-checked against the official cumulative
``adj_factor`` carried by the canonical bars: ``derived[t]`` must equal
``official[t] / official[last]`` within the configured tolerance, otherwise the
snapshot is refused instead of published.

The tolerance is measured, not arbitrary. The exchange publishes the ex-right
reference price at 0.01 CNY resolution and the provider publishes
``adj_factor`` at four decimals, so the reproduced factor cannot match exactly.
On the A500 window 2025-08-29..09-30 the worst reconciliation was 8.2e-4
(601866.XSHG, whose whole divergence is 0.0021 CNY per share of implied cash),
and the documented default is 1e-3. Each result also reports the divergence
expressed as cash per share so a reviewer can see whether it stays inside the
published resolution rather than trusting a bare factor number.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from ..contracts.adjustment_snapshot import AdjustmentSnapshotStore
from ..errors import ContractError
from .adjustment import AdjustmentDerivation, XdxrAdjustmentDeriver

# The deriver's event table; the snapshot reader validates exactly these columns.
EVENT_COLUMNS = ["year", "month", "day", "category", "fenhong", "songzhuangu", "peigu", "peigujia"]
TEN_SHARE = 10.0
# Measured worst reconciliation on the A500 pilot window is 8.2e-4; see the
# module docstring for why the exchange/provider rounding forces a floor.
PROVIDER_TOLERANCE = 1e-3
PUBLIC_RESOLUTION_CNY = 0.01
_BLANKS = {"", "nan", "none", "null", "nat"}


@dataclass(frozen=True)
class AdjustmentChainResult:
    instrument: str
    events: pd.DataFrame
    derivation: AdjustmentDerivation
    sessions: tuple[dt.date, ...]
    window_start: dt.date
    window_end: dt.date
    dropped: tuple[dict[str, Any], ...]
    provider_check: dict[str, Any]


def _optional_date(value: Any) -> dt.date | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    text = str(value).strip()
    if text.lower() in _BLANKS:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ContractError(f"dividend date is not ISO: {value!r}") from exc


def _number(value: Any, field: str) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, bool):
        raise ContractError(f"dividend {field} must be numeric, got a boolean")
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in _BLANKS:
            return None
        try:
            value = float(text)
        except ValueError as exc:
            raise ContractError(f"dividend {field} is not numeric: {value!r}") from exc
    number = float(value)
    if not math.isfinite(number):
        raise ContractError(f"dividend {field} is not finite")
    return number


def implemented_events(
    records: pd.DataFrame,
    *,
    instrument: str,
    window_start: dt.date,
    window_end: dt.date,
) -> tuple[pd.DataFrame, tuple[dict[str, Any], ...]]:
    """Map implemented disclosures to the deriver's per-ten-share event table.

    Returns the event frame (carrying ``source_event_id`` for lineage) and the
    dropped records with explicit reasons, so a reviewer can see why an
    implemented disclosure did not enter the factors.
    """
    required = {
        "instrument",
        "div_proc",
        "ex_date",
        "cash_div_tax",
        "stk_div",
        "stk_bo_rate",
        "stk_co_rate",
        "imp_ann_date",
        "record_id",
    }
    missing = required - set(records.columns)
    if missing:
        raise ContractError(f"dividend records are missing columns: {sorted(missing)}")
    selected: dict[dt.date, list[dict[str, Any]]] = {}
    dropped: list[dict[str, Any]] = []
    for row in records[records["instrument"] == instrument].to_dict(orient="records"):
        record_id = str(row["record_id"])
        stage = "" if row["div_proc"] is None else str(row["div_proc"]).strip()
        if stage != "实施":
            continue
        ex_day = _optional_date(row["ex_date"])
        if ex_day is None:
            dropped.append({"record_id": record_id, "reason": "implementation_without_ex_date"})
            continue
        if not window_start <= ex_day <= window_end:
            continue
        imp_day = _optional_date(row["imp_ann_date"])
        if imp_day is not None and imp_day > ex_day:
            raise ContractError(
                f"implemented dividend {record_id} was announced after its own ex-date "
                f"({imp_day} > {ex_day}); refusing dirty disclosure data"
            )
        cash = _number(row["cash_div_tax"], "cash_div_tax")
        if cash is None:
            raise ContractError(
                f"implemented dividend {record_id} has no pre-tax cash field; "
                "refusing to fall back to the after-tax value"
            )
        bonus_rate = _number(row["stk_bo_rate"], "stk_bo_rate")
        capitalization_rate = _number(row["stk_co_rate"], "stk_co_rate")
        for field, value in (("cash_div_tax", cash), ("stk_bo_rate", bonus_rate), ("stk_co_rate", capitalization_rate)):
            if value is not None and value < 0:
                raise ContractError(f"implemented dividend {record_id} has a negative {field}")
        bonus = (bonus_rate or 0.0) + (capitalization_rate or 0.0)
        reported_total = _number(row["stk_div"], "stk_div")
        if reported_total is not None and abs(reported_total - bonus) > 1e-9:
            raise ContractError(
                f"implemented dividend {record_id} reports stk_div={reported_total} but "
                f"stk_bo_rate+stk_co_rate={bonus}; refusing to guess the breakdown"
            )
        selected.setdefault(ex_day, []).append(
            {
                "year": ex_day.year,
                "month": ex_day.month,
                "day": ex_day.day,
                "category": 1,
                "fenhong": cash * TEN_SHARE,
                "songzhuangu": bonus * TEN_SHARE,
                "peigu": 0.0,
                "peigujia": 0.0,
                "source_event_id": record_id,
            }
        )
    rows: list[dict[str, Any]] = []
    for ex_day, candidates in sorted(selected.items()):
        amounts = {(item["fenhong"], item["songzhuangu"]) for item in candidates}
        if len(amounts) > 1:
            raise ContractError(
                f"ambiguous implemented dividends on {ex_day} for {instrument}: {sorted(amounts)}; "
                "resolve the disclosure revision before deriving factors"
            )
        rows.append(sorted(candidates, key=lambda item: item["source_event_id"])[0])
    frame = pd.DataFrame(rows, columns=[*EVENT_COLUMNS, "source_event_id"])
    return frame, tuple(dropped)


def _pre_close_series(bars: pd.DataFrame, *, instrument: str, events: pd.DataFrame) -> pd.Series:
    """Raw close of the last session strictly before each ex-date."""
    ordered = bars.sort_values("session_date")
    sessions = list(ordered["session_date"])
    closes = list(ordered["close"])
    values: list[float] = []
    index: list[pd.Timestamp] = []
    unresolved: list[str] = []
    for event in events.to_dict(orient="records"):
        ex_day = dt.date(int(event["year"]), int(event["month"]), int(event["day"]))
        prior = [session for session in sessions if session < ex_day]
        if not prior:
            unresolved.append(ex_day.isoformat())
            continue
        close = closes[sessions.index(prior[-1])]
        if close is None or not math.isfinite(float(close)) or float(close) <= 0:
            unresolved.append(f"{ex_day.isoformat()} (non-positive prior close)")
            continue
        index.append(pd.Timestamp(ex_day))
        values.append(float(close))
    if unresolved:
        raise ContractError(
            f"cannot resolve the pre-ex close for {instrument}; extend the window to include "
            "the record session: " + ",".join(unresolved)
        )
    return pd.Series(values, index=pd.DatetimeIndex(index))


def _provider_check(derived: pd.DataFrame, bars: pd.DataFrame, *, tolerance: float) -> dict[str, Any]:
    official = bars[["session_date", "adj_factor"]].dropna()
    derived = derived.copy()
    derived["session_date"] = pd.to_datetime(derived["session_date"]).dt.date
    official = official.assign(session_date=pd.to_datetime(official["session_date"]).dt.date)
    if len(official) < 2:
        return {"status": "skipped", "reason": "fewer than two official adj_factor observations"}
    anchor = float(official.iloc[-1]["adj_factor"])
    if not math.isfinite(anchor) or anchor == 0:
        return {"status": "skipped", "reason": "official adj_factor anchor is unusable"}
    expected = official.assign(expected=official["adj_factor"].astype(float) / anchor)
    merged = derived.merge(expected[["session_date", "expected"]], on="session_date", how="inner")
    if merged.empty:
        return {"status": "skipped", "reason": "no overlapping sessions between derivation and bars"}
    merged = merged.assign(abs_diff=(merged["adj_factor"] - merged["expected"]).abs())
    worst = merged.loc[merged["abs_diff"].idxmax()]
    worst_session = worst["session_date"]
    pre_close = bars[bars["session_date"] == worst_session]["pre_close"] if "pre_close" in bars.columns else None
    if pre_close is None or len(pre_close) == 0:
        scale = float(bars.sort_values("session_date").loc[bars["session_date"] <= worst_session, "close"].iloc[-1])
    else:
        scale = float(pre_close.iloc[0])
    result: dict[str, Any] = {
        "status": "passed" if float(worst["abs_diff"]) <= tolerance else "failed",
        "tolerance": tolerance,
        "compared_sessions": int(len(merged)),
        "max_abs_diff": float(worst["abs_diff"]),
        "worst_session": worst_session.isoformat(),
        "derived_factor": float(worst["adj_factor"]),
        "provider_factor": float(worst["expected"]),
        # the same divergence expressed in CNY per share, comparable to the
        # exchange's 0.01-CNY published resolution
        "price_scale_gap_cny": float(worst["abs_diff"]) * scale,
        "within_public_resolution": float(worst["abs_diff"]) * scale <= PUBLIC_RESOLUTION_CNY,
    }
    if result["status"] == "failed":
        raise ContractError(
            "derived adjustment diverges from the official adj_factor: "
            f"max|diff|={result['max_abs_diff']:.6g} at {result['worst_session']} "
            f"(derived {result['derived_factor']:.6f} vs provider {result['provider_factor']:.6f}, "
            f"tolerance {tolerance})"
        )
    return result


def build_adjustment_chain(
    *,
    records: pd.DataFrame,
    bars: pd.DataFrame,
    instrument: str,
    window_start: dt.date,
    window_end: dt.date,
    provenance: str = "tushare:dividend",
    tolerance: float = PROVIDER_TOLERANCE,
) -> AdjustmentChainResult:
    """Derive one instrument's factors from its disclosures and verify them."""
    required = {"instrument", "session_date", "close", "adj_factor"}
    missing = required - set(bars.columns)
    if missing:
        raise ContractError(f"bars are missing columns: {sorted(missing)}")
    instrument_bars = bars[bars["instrument"] == instrument].copy()
    if instrument_bars.empty:
        raise ContractError(f"no bars for {instrument} in the requested window")
    instrument_bars["session_date"] = pd.to_datetime(instrument_bars["session_date"]).dt.date
    instrument_bars = instrument_bars.sort_values("session_date").reset_index(drop=True)
    sessions = sorted(set(instrument_bars["session_date"]))
    if sessions[0] != window_start or sessions[-1] != window_end:
        raise ContractError(
            f"bar sessions for {instrument} span {sessions[0]}..{sessions[-1]}, which does not match "
            f"the declared window {window_start}..{window_end}; pass the instrument's own session range"
        )
    events, dropped = implemented_events(
        records, instrument=instrument, window_start=window_start, window_end=window_end
    )
    # The deriver applies an event only when its ex-date is itself a session,
    # so a disclosure whose ex-date falls on a weekend/holiday (dirty data: the
    # exchange never schedules one) would be recorded in events.csv yet never
    # move a factor — a snapshot that contradicts its own effective-date table.
    # Refuse it instead of relying on the provider cross-check, which can only
    # catch divergences larger than the tolerance.
    off_session = sorted(
        {
            dt.date(int(event["year"]), int(event["month"]), int(event["day"]))
            for event in events.to_dict(orient="records")
        }
        - set(sessions)
    )
    if off_session:
        raise ContractError(
            f"implemented ex-dates for {instrument} fall outside the bar sessions: "
            f"{[day.isoformat() for day in off_session]}; the deriver can only apply an event "
            "on a trading session — verify the disclosure ex-date against the exchange calendar"
        )
    pre_close = None if events.empty else _pre_close_series(instrument_bars, instrument=instrument, events=events)
    derivation = XdxrAdjustmentDeriver(provenance=provenance).derive(
        instrument, events.copy(), pd.to_datetime(sessions), pre_close
    )
    check = _provider_check(derivation.frame, instrument_bars, tolerance=tolerance)
    return AdjustmentChainResult(
        instrument=instrument,
        events=events,
        derivation=derivation,
        sessions=tuple(sessions),
        window_start=window_start,
        window_end=window_end,
        dropped=dropped,
        provider_check=check,
    )


def publish_adjustment_snapshot(
    root: str | Path,
    result: AdjustmentChainResult,
    *,
    visibility_time: dt.datetime,
) -> str:
    """Persist a derived chain through the governed snapshot store."""
    if result.derivation.effective_dates is None or result.derivation.effective_date_table_checksum is None:
        raise ContractError("derivation does not carry its effective-date table")
    directory = AdjustmentSnapshotStore().save(
        root,
        instrument=result.instrument,
        visibility_time=visibility_time,
        window_start=result.window_start,
        window_end=result.window_end,
        events=result.events[EVENT_COLUMNS].reset_index(drop=True),
        effective_dates=result.derivation.effective_dates,
        sessions=list(result.sessions),
        # persist the lineage the derivation declared, not the store's default,
        # so a reader can tell the two event sources apart
        formula_version=result.derivation.version,
        # the store refuses the write if the table it persists is not byte-
        # identical to the one the factor side declared
        expected_effective_checksum=result.derivation.effective_date_table_checksum,
    )
    return directory.name


# --- TDX xdxr as the event source (lineage adj_factor.derived_v1) -------------
#
# `build_adjustment_chain` above takes Tushare *disclosures* and maps them into the
# exchange event shape.  TDX publishes the exchange shape directly, so this path
# skips the mapping entirely: that is the point of having a second derivation --
# a disclosure row that is mis-staged (a plan or a shareholder-meeting record
# carrying cash_div=0) or mis-mapped cannot distort the events, because nothing
# here interprets a disclosure.
TDX_DERIVATION_VERSION = "adj_factor.derived_v1"


def build_tdx_adjustment_chain(
    *,
    events: pd.DataFrame,
    bars: pd.DataFrame,
    instrument: str,
    window_start: dt.date,
    window_end: dt.date,
    provenance: str = "tdx:xdxr",
    tolerance: float = PROVIDER_TOLERANCE,
) -> AdjustmentChainResult:
    """Derive one instrument's factors from raw TDX ``xdxr`` events.

    The event frame is used exactly as TDX publishes it (filtered to realised
    ex-right rows and ordered), so this derivation is lineage
    ``adj_factor.derived_v1`` rather than the disclosure-mapped
    ``adj_factor.exchange_v2``.  Both are multiply-basis and anchored at 1.0 on
    the latest session, so a consumer may read either.

    Everything the disclosure path enforces is still enforced here, because the
    failure modes are the derivation's, not the source's:

    * an ex-date that is not a trading session would be recorded in the snapshot's
      event table yet never move a factor, so the window is refused rather than
      published as a self-contradicting snapshot;
    * the derived factors are reconciled against the provider's own ``adj_factor``
      column, so a divergence beyond ``tolerance`` blocks publication.
    """
    required = {"instrument", "session_date", "close", "adj_factor"}
    missing = required - set(bars.columns)
    if missing:
        raise ContractError(f"bars are missing columns: {sorted(missing)}")
    if events is None or events.empty:
        raise ContractError(f"no TDX xdxr events supplied for {instrument}")

    instrument_bars = bars[bars["instrument"] == instrument].copy()
    if instrument_bars.empty:
        raise ContractError(f"no bars for {instrument} in the requested window")
    instrument_bars["session_date"] = pd.to_datetime(instrument_bars["session_date"]).dt.date
    instrument_bars = instrument_bars.sort_values("session_date").reset_index(drop=True)
    sessions = sorted(set(instrument_bars["session_date"]))
    if sessions[0] != window_start or sessions[-1] != window_end:
        raise ContractError(
            f"bar sessions for {instrument} span {sessions[0]}..{sessions[-1]}, which does not match "
            f"the declared window {window_start}..{window_end}; pass the instrument's own session range"
        )

    if "category" not in events.columns:
        raise ContractError("TDX xdxr events require a category column")
    selected = events[events["category"] == 1].copy()
    # TDX returns the instrument's whole history; only the ex-dates inside the
    # declared window can move this window's factors, so the off-session check
    # below must consider exactly those. Rejecting an out-of-window ex-date would
    # make a short window unusable forever, since no window covers a listing's
    # entire history.
    in_window = [
        dt.date(int(row["year"]), int(row["month"]), int(row["day"]))
        for row in selected.to_dict(orient="records")
        if window_start <= dt.date(int(row["year"]), int(row["month"]), int(row["day"])) <= window_end
    ]
    off_session = [day for day in in_window if day not in set(sessions)]
    if off_session:
        raise ContractError(
            f"TDX ex-dates for {instrument} fall outside the bar sessions: "
            f"{[day.isoformat() for day in off_session[:10]]}; the deriver can only apply an event "
            "on a trading session — verify the event date against the exchange calendar"
        )

    # Keep only the in-window events: the deriver applies an event on its ex-date,
    # so an out-of-window event would need a pre-ex close this window does not
    # contain and would be unresolvable by construction.
    ordered = selected[[
        window_start <= dt.date(int(row["year"]), int(row["month"]), int(row["day"])) <= window_end
        for _, row in selected.iterrows()
    ]].sort_values(["year", "month", "day"], kind="stable").reset_index(drop=True)
    # No second off-session check over `ordered`: it is the same in-window set the
    # check above already covers, so a repeat would be dead code whose better
    # comment makes it look like the one that actually fires (cr.md OBS-E).
    ordered["source_event_id"] = [
        f"tdx_xdxr:{instrument}:{int(r['year']):04d}{int(r['month']):02d}{int(r['day']):02d}"
        for _, r in ordered.iterrows()
    ]
    pre_close = _pre_close_series(instrument_bars, instrument=instrument, events=ordered)
    derivation = XdxrAdjustmentDeriver(provenance=provenance).derive(
        instrument, ordered.copy(), pd.to_datetime(sessions), pre_close
    )
    # Publish the lineage this derivation actually declares, so a reader that
    # refuses unknown lineages cannot mistake it for the disclosure-mapped one.
    derivation = AdjustmentDerivation(
        frame=derivation.frame,
        version=TDX_DERIVATION_VERSION,
        provenance=derivation.provenance,
        snapshot_id=derivation.snapshot_id,
        effective_date_table_checksum=derivation.effective_date_table_checksum,
        effective_dates=derivation.effective_dates,
    )
    check = _provider_check(derivation.frame, instrument_bars, tolerance=tolerance)
    return AdjustmentChainResult(
        instrument=instrument,
        # keep source_event_id: publish_adjustment_snapshot selects EVENT_COLUMNS
        # itself, and a caller reporting the lineage needs the per-event id
        events=ordered.reset_index(drop=True),
        derivation=derivation,
        sessions=tuple(sessions),
        window_start=window_start,
        window_end=window_end,
        dropped=(),
        provider_check=check,
    )
