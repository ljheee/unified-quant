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

def test_requested_bar_estimate_covers_recent_historical_dates():
    adapter = MootdxSourceAdapter(page_size=100)
    assert adapter._requested_bar_estimate("2026-06-11", "2026-06-12") == 100
