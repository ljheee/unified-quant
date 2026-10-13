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


class _DroppingClient(_Client):
    """Answers a batch with nothing, the failure mode that published a short panel."""

    def __init__(self, *, recover_after: int | None = None):
        super().__init__()
        self.recover_after = recover_after
        self.daily_calls = 0

    def daily(self, ts_code, start_date, end_date):
        self.daily_calls += 1
        if self.recover_after is not None and self.daily_calls > self.recover_after:
            return super().daily(ts_code, start_date, end_date)
        return pd.DataFrame()


class _PartialClient(_Client):
    """Answers two of three requested instruments, as a dropped batch looks."""

    def daily(self, ts_code, start_date, end_date):
        self.calls.append("daily")
        frame = super().daily(ts_code, start_date, end_date)
        return frame[frame["ts_code"] != "000001.SZ"].reset_index(drop=True)


def test_silently_empty_batch_is_retried_then_fails_closed():
    client = _DroppingClient()
    result = _adapter(client).fetch("bars_daily", ["600000.XSHG", "000001.XSHE"], "2026-03-02", "2026-03-02", V2_FIELDS)
    assert result.status is FetchStatus.UPSTREAM_ERROR
    assert result.retryable is True
    assert result.rows.empty
    assert client.daily_calls == 2
    assert "no rows for batch" in result.errors[0]


def test_empty_batch_that_recovers_on_retry_publishes():
    client = _DroppingClient(recover_after=1)
    result = _adapter(client).fetch("bars_daily", ["600000.XSHG", "000001.XSHE"], "2026-03-02", "2026-03-02", V2_FIELDS)
    assert result.status is FetchStatus.SUCCESS
    assert client.daily_calls == 2


class _NoneDroppingClient(_Client):
    """Answers a batch with None, the other shape a dropped request can take."""

    def __init__(self, *, recover_after: int | None = None, none_when_at_most: int | None = None):
        super().__init__()
        self.recover_after = recover_after
        self.none_when_at_most = none_when_at_most
        self.daily_calls = 0

    def daily(self, ts_code, start_date, end_date):
        self.daily_calls += 1
        if self.recover_after is not None and self.daily_calls > self.recover_after:
            return super().daily(ts_code, start_date, end_date)
        if self.none_when_at_most is not None and ts_code is not None and len(ts_code.split(",")) > self.none_when_at_most:
            return super().daily(ts_code, start_date, end_date)
        return None


def test_none_batch_is_retried_then_fails_closed():
    client = _NoneDroppingClient()
    result = _adapter(client).fetch("bars_daily", ["600000.XSHG", "000001.XSHE"], "2026-03-02", "2026-03-02", V2_FIELDS)
    assert result.status is FetchStatus.UPSTREAM_ERROR
    assert result.retryable is True
    assert result.rows.empty
    assert client.daily_calls == 2
    assert "no rows for batch" in result.errors[0]


def test_none_batch_that_recovers_on_retry_publishes():
    client = _NoneDroppingClient(recover_after=1)
    result = _adapter(client).fetch("bars_daily", ["600000.XSHG", "000001.XSHE"], "2026-03-02", "2026-03-02", V2_FIELDS)
    assert result.status is FetchStatus.SUCCESS
    assert client.daily_calls == 2


def test_a_none_batch_in_a_multi_batch_fetch_fails_instead_of_publishing_short():
    # 60 instruments are two daily batches (50 + 10). Before the fix a None
    # from the second batch was appended as nothing and the run published the
    # first batch alone as SUCCESS with only a warning - the same silently
    # short panel the empty-frame guard was written to stop.
    instruments = [f"6000{i:02d}.XSHG" for i in range(60)]
    client = _NoneDroppingClient(none_when_at_most=10)
    result = _adapter(client).fetch("bars_daily", instruments, "2026-03-02", "2026-03-02", V2_FIELDS)
    assert result.status is FetchStatus.UPSTREAM_ERROR
    assert result.retryable is True
    assert result.rows.empty


def test_an_empty_instrument_request_issues_no_provider_call():
    # batches falls back to one empty batch; calling daily with ts_code=None
    # there would fetch the whole market and hand back unrequested rows.
    client = _Client()
    result = _adapter(client).fetch("bars_daily", [], "2026-03-02", "2026-03-02", V2_FIELDS)
    assert "daily" not in client.calls
    assert result.rows.empty


def test_a_rate_limited_reference_endpoint_keeps_its_retryable_status():
    # _call classifies the failure and stamps the ContractError; the fetch
    # error path used to re-map every ContractError to a non-retryable
    # AUTH_FAILED, hiding a transient rate limit from retry automation.
    class _RateLimitedClient(_Client):
        def adj_factor(self, trade_date=None, ts_code=None, start_date=None, end_date=None):
            raise Exception("抱歉，每分钟最多访问该接口200次")

    result = _adapter(_RateLimitedClient()).fetch(
        "bars_daily", ["600000.XSHG"], "2026-03-02", "2026-03-02", V2_FIELDS
    )
    assert result.status is FetchStatus.RATE_LIMITED
    assert result.retryable is True
    assert result.rows.empty


def test_an_unsupported_dataset_carries_the_request_derived_version():
    result = _adapter(_Client()).fetch(
        "bars_weekly", ["600000.XSHG"], "2026-03-02", "2026-03-02", V2_FIELDS
    )
    assert result.status is FetchStatus.UNSUPPORTED_REQUEST
    assert result.schema_version == "v2"


def test_requested_instrument_missing_without_a_suspension_is_reported_not_blocked():
    client = _PartialClient()
    result = _adapter(client).fetch(
        "bars_daily", ["600000.XSHG", "000001.XSHE", "000651.XSHE"], "2026-03-02", "2026-03-02", V2_FIELDS
    )
    assert result.status is FetchStatus.SUCCESS
    # 000001 is halted for the whole window per suspend_d, so only the name the
    # provider simply did not answer for is reported. Blocking here would be
    # wrong: a long halt loses its suspend_d rows, which only the whole-window
    # readiness gate can weigh.
    report = " ".join(result.warnings)
    assert "['000651.XSHE']" in report
    assert "000001.XSHE" not in report


def test_instrument_halted_for_the_whole_window_is_not_reported_as_missing():
    class _HaltedClient(_PartialClient):
        def suspend_d(self, trade_date, suspend_type):
            self.calls.append("suspend_d")
            return pd.DataFrame({
                "ts_code": ["000001.SZ", "000651.SZ"],
                "trade_date": [trade_date, trade_date],
                "suspend_timing": [None, None],
                "suspend_type": ["S", "S"],
            })

    halted = _adapter(_HaltedClient()).fetch(
        "bars_daily", ["600000.XSHG", "000001.XSHE", "000651.XSHE"], "2026-03-02", "2026-03-02", V2_FIELDS
    )
    assert halted.status is FetchStatus.SUCCESS
    assert halted.rows["instrument"].tolist() == ["600000.XSHG"]
    assert not [warning for warning in halted.warnings if "omitted" in warning]
