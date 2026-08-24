from __future__ import annotations

from datetime import date
from collections.abc import Callable
import pandas as pd


class TradingCalendar:
    """Trading-session predicate backed by an explicit session provider."""

    def __init__(
        self,
        sessions: set[date] | None = None,
        provider: Callable[[date, date], set[date]] | None = None,
        provenance: str = "unspecified",
    ) -> None:
        self.sessions = sessions or set()
        self.provider = provider
        self.provenance = provenance

    def between(self, start: date, end: date) -> set[date]:
        if start > end:
            return set()
        if self.provider is not None:
            return {value for value in self.provider(start, end) if start <= value <= end}
        return {value for value in self.sessions if start <= value <= end}

    def is_session(self, value: date) -> bool:
        return value in self.between(value, value)


def akshare_calendar(start: date, end: date) -> set[date]:
    """Load Sina-published A-share trading dates via AkShare."""
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("optional dependency akshare is not installed") from exc
    frame = ak.tool_trade_date_hist_sina()
    values = pd.to_datetime(frame["trade_date"]).dt.date
    return {value for value in values if start <= value <= end}


class IndexCalendarDeriver:
    """Derive historical trading sessions from an index bar frame."""

    def __init__(self, source: str = "mootdx_index_derived_v1") -> None:
        self.source = source

    def sessions(self, index_bars: pd.DataFrame, date_column: str = "datetime") -> list[date]:
        if index_bars.empty or date_column not in index_bars.columns:
            return []
        return sorted(set(pd.to_datetime(index_bars[date_column]).dt.date))

    def calendar(self, index_bars: pd.DataFrame, date_column: str = "datetime") -> TradingCalendar:
        sessions = set(self.sessions(index_bars, date_column))
        return TradingCalendar(sessions, provenance=self.source)

    def is_session(self, value: date, index_bars: pd.DataFrame) -> bool:
        return value in set(self.sessions(index_bars))
