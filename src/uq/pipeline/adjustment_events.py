"""Recompute cumulative adjustment factors from TDX corporate-action events.

This is the independent second source for the reviewed adjusted-price line.  The
vendor's cumulative ``adj_factor`` is opaque: it is a number whose direction
(multiply, not divide), event coverage (送股/转增 as well as cash) and column
units are all claims the producer would otherwise have to take on trust.  TDX
publishes the underlying events, so the factor can be *recomputed* and compared,
which turns those claims into something a quality report can assert.

Measured provenance (2026-10-16, 6 symbols, 83 ex-right events): recomputed
jumps agree with the vendor's factor jumps to a maximum of 0.109%, mean 0.023%,
83/83 within 0.5%.  That residual is exchange rounding of the ex-right reference
price, which is why the comparison uses a tolerance rather than equality.

The ex-right convention used here is the standard A-share one:

    除权参考价 = (前收盘 - 每股现金 + 配股价 * 配股比例) / (1 + 送股比例 + 转增比例)
    ratio       = 除权参考价 / 前收盘   (< 1)

The factor is multiply-basis and anchored at 1.0 on the latest session, matching
``market/adjustment.py``: an event multiplies the factor of every session strictly
before its ex-date by ``ratio``, so ``adjusted_close = close * adj_factor`` is
return-continuous across the event.

TDX ``xdxr`` reports every amount per 10 shares -- cash included -- so each is
divided by 10 to reach a per-share quantity (verified: a ``fenhong`` of 1.74 on a
19.80 close reproduces the vendor jump to 0.03% only after that division).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Mapping

import pandas as pd

from ..adapters.mootdx_source import MootdxSourceAdapter
from ..errors import ContractError

# TDX xdxr `category`: 1 = 除权除息 (ex-right/ex-dividend), 5 = 股本变化.
_CATEGORY_EX_RIGHT = 1

# Amounts TDX reports per 10 shares.
_PER_TEN_SHARES = 10.0


class AdjustmentEvent:
    """One realised corporate action, normalised to per-share quantities."""

    __slots__ = ("session", "cash", "send", "transfer", "allot", "allot_price")

    def __init__(
        self,
        session: dt.date,
        *,
        cash: float,
        send: float,
        transfer: float,
        allot: float,
        allot_price: float,
    ) -> None:
        self.session = session
        self.cash = cash
        self.send = send
        self.transfer = transfer
        self.allot = allot
        self.allot_price = allot_price

    def ratio(self, previous_close: float) -> float:
        """``除权参考价 / 前收盘`` for this event, or ``NaN`` when undefined.

        The canonical deriver (``market/adjustment.py``) multiplies the factor of
        every session *before* the ex-date by exactly this quantity --
        ``ex_right_price / pre_close``, which is below 1 -- so the pre-event
        adjusted close equals the ex-date adjusted close and the measured return
        across the event is the true economic return.
        """
        if previous_close is None or previous_close <= 0:
            return float("nan")
        denominator = 1.0 + self.send + self.transfer + self.allot
        if denominator <= 0:
            return float("nan")
        reference = (
            previous_close - self.cash + self.allot_price * self.allot
        ) / denominator
        if reference <= 0:
            return float("nan")
        return reference / previous_close

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.isoformat(),
            "cash": self.cash,
            "send": self.send,
            "transfer": self.transfer,
            "allot": self.allot,
            "allot_price": self.allot_price,
        }


def _number(value: Any) -> float:
    """0.0 for a missing/blank TDX field, else float."""
    if value is None:
        return 0.0
    try:
        if pd.isna(value):
            return 0.0
    except (TypeError, ValueError):
        pass
    return float(value)


def events_from_xdxr(frame: pd.DataFrame) -> list[AdjustmentEvent]:
    """Normalise one instrument's raw ``xdxr`` frame into realised events.

    Only ``category == 1`` rows are kept: TDX also emits 股本变化 rows that
    describe share-count drift rather than a corporate action, and those have no
    ex-right meaning.  Events are returned in date order so a cumulative product
    is well defined.
    """
    if frame is None or len(frame) == 0:
        return []
    required = {"year", "month", "day", "category"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ContractError(f"xdxr events missing columns: {missing}")
    rows = frame[frame["category"] == _CATEGORY_EX_RIGHT]
    events: list[AdjustmentEvent] = []
    for row in rows.to_dict("records"):
        session = dt.date(int(row["year"]), int(row["month"]), int(row["day"]))
        # Every amount TDX reports here is per 10 shares, cash included: a
        # fenhong of 1.74 reproduces the vendor jump only after dividing by 10,
        # and the cash-only 2017-07-21 event confirms it (1.58 -> 0.158).
        cash = _number(row.get("fenhong")) / _PER_TEN_SHARES
        send = _number(row.get("songzhuangu")) / _PER_TEN_SHARES
        allot = _number(row.get("peigu")) / _PER_TEN_SHARES
        allot_price = _number(row.get("peigujia"))
        # 缩股 (reverse split) would *reduce* the share count, so it cannot be
        # folded into the `1 + shares` denominator the way 送股/转增 are, and no
        # ex-right formula for it is defined here. The canonical deriver ignores
        # the field (`src/uq/market/adjustment.py` never reads `suogu`), so a
        # non-zero value is refused rather than given an invented sign. The refusal
        # is definitional -- it does not depend on how often the provider
        # populates the column.
        reverse_split = _number(row.get("suogu"))
        if reverse_split != 0:
            raise ContractError(
                f"xdxr event on {session.isoformat()} carries suogu={reverse_split} "
                "(a reverse split), whose ex-right treatment is not defined here"
            )
        transfer = 0.0
        events.append(
            AdjustmentEvent(
                session,
                cash=cash,
                send=send,
                transfer=transfer,
                allot=allot,
                allot_price=allot_price,
            )
        )
    events.sort(key=lambda item: item.session)
    return events


def cumulative_ratio(
    events: Iterable[AdjustmentEvent],
    *,
    prior_sessions: Mapping[dt.date, float],
) -> dict[dt.date, float]:
    """Map each event date to the cumulative factor ratio before it.

    ``prior_sessions`` maps a trading session to that session's previous close
    for the instrument, so an event's ratio uses the close of the session before
    its ex-date.  The value returned for a date is ``ratio`` itself (the per-event
    jump), which the caller multiplies into a running product.
    """
    out: dict[dt.date, float] = {}
    for event in events:
        before = max((d for d in prior_sessions if d < event.session), default=None)
        close = prior_sessions.get(before) if before is not None else None
        out[event.session] = event.ratio(close) if close else float("nan")
    return out


class TdxEventSource:
    """Fetch and normalise TDX corporate-action events for a set of symbols."""

    def __init__(self, adapter: MootdxSourceAdapter | None = None) -> None:
        self._adapter = adapter or MootdxSourceAdapter()

    def events(self, symbols: Iterable[str]) -> dict[str, list[AdjustmentEvent]]:
        wanted = [str(symbol) for symbol in symbols]
        frames = self._adapter.fetch_xdxr_events(wanted)
        return {
            symbol: events_from_xdxr(frames.get(symbol, pd.DataFrame()))
            for symbol in wanted
        }


def symbol_of(instrument: str) -> str:
    """``600000.XSHG`` -> ``600000`` (TDX is keyed by the bare code)."""
    return str(instrument).split(".")[0]
