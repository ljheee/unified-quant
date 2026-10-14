from datetime import date
from pathlib import Path
import pandas as pd
import pytest
from uq.adapters.mootdx_source import MootdxSourceAdapter
from uq.adapters.tushare_free import TushareFreeAdapter
from uq.market.calendar import IndexCalendarDeriver
from uq.market.universe import StaticUniverseLoader
from uq.sources.fetch import FetchStatus
from uq.pipeline.daily import DailyIngestPipeline
from uq.quality.gate import CrossSourceGate
from uq.routing.router import SourceRouter
from tests.test_unified_data import canonical_rows

ROOT = Path(__file__).resolve().parents[1]

class FakeMootdxAdapter(MootdxSourceAdapter):
    def __init__(self, page): super().__init__(server=("127.0.0.1", 7709)); self.page = page
    def _client(self):
        class Client:
            def __init__(self, page): self.page = page
            def bars(self, symbol, frequency, start, offset):
                frame = self.page.copy(); frame.index.name = "datetime"; return frame
            def close(self): pass
        return Client(self.page)

def mootdx_page():
    frame = pd.DataFrame({
        "open": [10.0], "close": [10.2], "high": [10.3], "low": [9.9],
        "vol": [100.0], "amount": [102000.0],
    }, index=pd.to_datetime(["2026-08-21"]))
    frame["instrument"] = "600000.XSHG"
    return frame

def test_fetch_result_statuses_are_typed():
    from uq.adapters.demo import DeterministicDemoAdapter
    adapter = DeterministicDemoAdapter("demo", canonical_rows())
    assert adapter.source_name == "demo"

def test_tushare_normalizes_units_and_symbols():
    raw = pd.DataFrame([{
        "ts_code": "600000.SH", "trade_date": "20260821", "open": 10.0,
        "high": 10.3, "low": 9.9, "close": 10.2, "vol": 100.0, "amount": 102.0,
    }])
    out = TushareFreeAdapter._normalize(raw)
    assert out.iloc[0]["instrument"] == "600000.XSHG"
    assert out.iloc[0]["volume"] == 10000.0
    assert out.iloc[0]["amount"] == 102000.0

def test_mootdx_normalizes_lot_volume_and_date_range():
    adapter = FakeMootdxAdapter(mootdx_page())
    result = adapter.fetch("bars_daily", ["600000.XSHG"], "2026-08-20", "2026-08-21", ["instrument", "datetime", "open", "high", "low", "close", "volume", "amount"])
    assert result.status == FetchStatus.SUCCESS
    assert result.rows.iloc[0]["volume"] == 10000.0
    assert result.observed_end == date(2026, 8, 21)

def test_static_universe_validates_canonical_ids(tmp_path):
    path = tmp_path / "u.txt"; path.write_text("600000.XSHG\n# comment\n000001.XSHE\n")
    assert StaticUniverseLoader(path).load() == ["600000.XSHG", "000001.XSHE"]
    path.write_text("600000.SH\n")
    with pytest.raises(ValueError, match="invalid canonical"):
        StaticUniverseLoader(path).load()

def test_index_calendar_derives_unique_sessions():
    bars = pd.DataFrame({"datetime": pd.to_datetime(["2026-08-21", "2026-08-21", "2026-08-22"])})
    deriver = IndexCalendarDeriver()
    assert deriver.sessions(bars) == [date(2026,8,21), date(2026,8,22)]

def test_requested_bar_estimate_is_the_requested_span_not_the_page_size():
    """cr.md latent B13: the estimate used to answer "one page" whatever was asked,
    which hid the truncation from every caller.  It is now the requested span, which
    is what the refusal in ``fetch`` needs.  A full page is still what gets requested
    -- that is locked by the fetch tests."""
    adapter = MootdxSourceAdapter(page_size=100)
    assert adapter._requested_bar_estimate("2026-06-11", "2026-06-12") == 2
    assert adapter._requested_bar_estimate("2026-06-11", "2026-06-11") == 1
    assert adapter._requested_bar_estimate("", "") is None


class _FixedPageMootdxAdapter(MootdxSourceAdapter):
    """Returns one fixed page for every ``bars`` call, ignoring the offset."""

    def __init__(self, page: pd.DataFrame, page_size: int = 5) -> None:
        super().__init__(server=("127.0.0.1", 7709), page_size=page_size)
        self._page = page

    def _client(self):
        page = self._page

        class Client:
            def bars(self, symbol, frequency, start, offset):
                frame = page.copy()
                frame.index.name = "datetime"
                return frame

            def close(self):
                pass

        return Client()


def _mootdx_page(dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [10.0] * len(dates),
            "close": [10.2] * len(dates),
            "high": [10.3] * len(dates),
            "low": [9.9] * len(dates),
            "vol": [100.0] * len(dates),
            "amount": [102000.0] * len(dates),
        },
        index=pd.to_datetime(dates),
    )


_BARS_FIELDS = ["instrument", "datetime", "open", "high", "low", "close", "volume", "amount"]


def test_mootdx_refuses_a_span_wider_than_one_page():
    """cr.md latent B13: a TDX request addresses at most one page, so a requested
    span that cannot fit must be refused, never served as the most recent page."""
    adapter = _FixedPageMootdxAdapter(_mootdx_page(["2026-08-21"]), page_size=5)
    result = adapter.fetch("bars_daily", ["600000.XSHG"], "2026-06-01", "2026-08-21", _BARS_FIELDS)
    assert result.status == FetchStatus.UNSUPPORTED_REQUEST
    assert "exceeds the 5-bar page" in result.errors[0]
    assert result.rows.empty


def test_mootdx_malformed_dates_return_an_envelope_not_an_exception():
    """The span estimate is evaluated before the per-instrument loop, so its
    ``ValueError`` for a malformed date must still surface as a typed FetchResult
    envelope -- ``fetch`` never raises for a request-level error."""
    adapter = _FixedPageMootdxAdapter(_mootdx_page(["2026-08-21"]), page_size=5)
    result = adapter.fetch("bars_daily", ["600000.XSHG"], "not-a-date", "2026-08-21", _BARS_FIELDS)
    assert result.status == FetchStatus.UNSUPPORTED_REQUEST
    assert "ISO dates" in result.errors[0]
    assert result.rows.empty


def test_mootdx_refuses_a_full_page_that_does_not_reach_the_requested_start():
    """A full page proves more history exists behind the window.  If the oldest bar
    it returned is newer than the requested start, the window was never delivered
    and the partial tail must not flow on as a successful fetch."""
    page = _mootdx_page(["2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21", "2026-08-24"])
    adapter = _FixedPageMootdxAdapter(page, page_size=5)
    # The requested window fits one page (5 calendar days) but is historical: the
    # source can only serve the most recent page, whose oldest bar (08-18) is still
    # newer than the requested start (06-01), so the coverage check must refuse.
    result = adapter.fetch("bars_daily", ["600000.XSHG"], "2026-06-01", "2026-06-05", _BARS_FIELDS)
    assert result.status == FetchStatus.UNSUPPORTED_REQUEST
    assert "does not cover the requested start" in result.errors[0]
    assert result.rows.empty


def test_mootdx_serves_a_window_a_full_page_actually_covers():
    """A full page whose oldest bar reaches the requested start is a complete answer."""
    page = _mootdx_page(["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"])
    adapter = _FixedPageMootdxAdapter(page, page_size=5)
    result = adapter.fetch("bars_daily", ["600000.XSHG"], "2026-06-01", "2026-06-05", _BARS_FIELDS)
    assert result.status == FetchStatus.SUCCESS
    assert len(result.rows) == 5
