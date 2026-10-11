from datetime import date

import pandas as pd
import pytest

from uq.adapters.tushare import TushareAdapter
from uq.sources.fetch import FetchStatus

V2_FIELDS = [
    "instrument", "session_date", "open", "high", "low", "close", "volume", "amount",
    "status", "adj_factor",
]
V1_FIELDS = [
    "instrument", "datetime", "open", "high", "low", "close", "volume", "amount",
]


class _Client:
    def __init__(self, *, fail_adj_factor: bool = False, suspend: bool = True):
        self.fail_adj_factor = fail_adj_factor
        self.suspend = suspend
        self.calls: list[str] = []

    def trade_cal(self, exchange, start_date, end_date):
        return pd.DataFrame({"cal_date": ["20260302"], "is_open": ["1"]})

    def daily(self, ts_code, start_date, end_date):
        self.calls.append("daily")
        return pd.DataFrame({
            "ts_code": ["600000.SH", "000001.SZ"],
            "trade_date": ["20260302", "20260302"],
            "open": [10.0, 10.0],
            "high": [11.0, 11.0],
            "low": [9.0, 9.0],
            "close": [10.5, 10.0],
            "pre_close": [10.0, 10.0],
            "vol": [100.0, 0.0],
            "amount": [1050.0, 0.0],
        })

    def adj_factor(self, trade_date=None, ts_code=None, start_date=None, end_date=None):
        self.calls.append("adj_factor")
        if self.fail_adj_factor:
            raise Exception("抱歉，您没有接口(adj_factor)访问权限")
        return pd.DataFrame({
            "ts_code": ["600000.SH", "000001.SZ"],
            "trade_date": ["20260302", "20260302"],
            "adj_factor": [2.0, 4.0],
        })

    def stk_limit(self, trade_date):
        self.calls.append("stk_limit")
        return pd.DataFrame({
            "ts_code": ["600000.SH", "000001.SZ"],
            "trade_date": ["20260302", "20260302"],
            "up_limit": [11.0, 11.0],
            "down_limit": [9.0, 9.0],
        })

    def suspend_d(self, trade_date, suspend_type):
        self.calls.append("suspend_d")
        if not self.suspend:
            return pd.DataFrame(columns=["ts_code", "trade_date", "suspend_timing", "suspend_type"])
        return pd.DataFrame({
            "ts_code": ["000001.SZ"],
            "trade_date": ["20260302"],
            "suspend_timing": ["上午"],
            "suspend_type": ["S"],
        })


def _adapter(client: _Client) -> TushareAdapter:
    adapter = TushareAdapter(token_env="MISSING_UQ_TOKEN")
    adapter._client = lambda: client
    return adapter


def test_schema_version_is_derived_from_the_key_field():
    assert TushareAdapter._schema_version_for(V2_FIELDS) == "v2"
    assert TushareAdapter._schema_version_for(V1_FIELDS) == "research-v1"
    assert TushareAdapter._schema_version_for(["close", "volume"]) == "research-v1"


def test_v2_fetch_merges_reference_data_and_reports_v2_schema():
    client = _Client()
    result = _adapter(client).fetch(
        "bars_daily", ["600000.XSHG", "000001.XSHE"], "2026-03-02", "2026-03-02", V2_FIELDS
    )
    assert result.schema_version == "v2"
    assert result.status is FetchStatus.SUCCESS
    assert set(V2_FIELDS) <= set(result.delivered_fields)
    rows = result.rows.set_index("instrument")
    assert rows.loc["600000.XSHG", "adj_factor"] == 2.0
    assert rows.loc["600000.XSHG", "status"] == "trading"
    assert rows.loc["600000.XSHG", "limit_up"] == 11.0
    assert rows.loc["000001.XSHE", "status"] == "suspended"
    assert {"daily", "adj_factor", "stk_limit", "suspend_d"} <= set(client.calls)


def test_zero_volume_bar_without_suspension_is_not_marked_trading():
    client = _Client(suspend=False)
    result = _adapter(client).fetch(
        "bars_daily", ["600000.XSHG", "000001.XSHE"], "2026-03-02", "2026-03-02", V2_FIELDS
    )
    rows = result.rows.set_index("instrument")
    assert rows.loc["000001.XSHE", "volume"] == 0.0
    assert rows.loc["000001.XSHE", "status"] == "suspended"


def test_v1_fetch_does_not_touch_the_reference_endpoints():
    client = _Client()
    result = _adapter(client).fetch(
        "bars_daily", ["600000.XSHG"], "2026-03-02", "2026-03-02", V1_FIELDS
    )
    assert result.schema_version == "research-v1"
    assert "datetime" in result.rows.columns
    assert client.calls == ["daily"]


def test_missing_reference_permission_fails_closed_instead_of_publishing_bars():
    client = _Client(fail_adj_factor=True)
    result = _adapter(client).fetch(
        "bars_daily", ["600000.XSHG"], "2026-03-02", "2026-03-02", V2_FIELDS
    )
    assert result.status is FetchStatus.AUTH_FAILED
    assert result.rows.empty
    assert result.errors


def test_comparator_subset_fetch_keeps_the_historical_path():
    client = _Client()
    result = _adapter(client).fetch("bars_daily", ["600000.XSHG"], "2026-03-02", "2026-03-02", ["close"])
    assert result.schema_version == "research-v1"
    assert client.calls == ["daily"]
