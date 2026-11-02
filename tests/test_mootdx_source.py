"""TDX source adapter: market routing and probe request-shape regressions.

mootdx's own ``get_stock_market`` routes a leading ``9`` to Shanghai before the
Beijing branch exists, so ``920xxx`` codes were asked for on market 1 and TDX
returned no rows at all (measured: market 2 has 51 rows for 920000, market 1 has
0). The adapter therefore resolves the market itself.

These are offline: they lock the routing table, not the network.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pandas as pd
import pytest
import mootdx.quotes as mootdx_quotes

from uq.adapters.mootdx_source import MootdxSourceAdapter


def test_the_modern_bse_range_routes_to_the_beijing_market():
    assert MootdxSourceAdapter.market_for("920000") == 2
    assert MootdxSourceAdapter.market_for("920405") == 2


def test_legacy_neeq_era_bse_codes_also_route_to_beijing():
    for code in ("430047", "830799", "870204", "881234"):
        assert MootdxSourceAdapter.market_for(code) == 2


def test_shanghai_and_shenzhen_routing_is_unchanged():
    assert MootdxSourceAdapter.market_for("600000") == 1
    assert MootdxSourceAdapter.market_for("688981") == 1
    assert MootdxSourceAdapter.market_for("000001") == 0
    assert MootdxSourceAdapter.market_for("300750") == 0


def test_an_unknown_prefix_is_refused_rather_than_defaulted():
    # Defaulting an unrecognised prefix to a market would silently query the wrong
    # exchange and report "no events" for an instrument that has some.
    with pytest.raises(ValueError, match="unsupported"):
        MootdxSourceAdapter.market_for("999999")


def test_mootdx_itself_still_gets_a_9_prefix_wrong():
    # Documents why the adapter cannot just delegate: the upstream prefix table
    # catches a leading 9 in the Shanghai branch. If mootdx fixes this, this test
    # should be updated and the local override can go.
    from mootdx.utils import get_stock_market

    assert get_stock_market("920000") == 1


def test_health_probe_uses_full_configured_page_for_bars(monkeypatch):
    counts = []

    class FakeMacClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def connect(self):
            pass

        def close(self):
            pass

        def get_stock_kline(self, *, market, code, period, start, count):
            counts.append(count)
            # Short counts are known to be a weaker coverage probe; the adapter
            # must use the configured full page in both preflight and health.
            if count != 37:
                return pd.DataFrame()
            return pd.DataFrame(
                {
                    "datetime": [pd.Timestamp("2026-08-21")],
                    "open": [10.0],
                    "high": [10.3],
                    "low": [9.9],
                    "close": [10.2],
                    "vol": [10000.0],
                    "amount": [102000.0],
                }
            )

    fake_module = ModuleType("easy_tdx")
    fake_module.MacClient = FakeMacClient
    monkeypatch.setitem(sys.modules, "easy_tdx", fake_module)

    class FakeXdxrClient:
        def xdxr(self, *, symbol):
            return pd.DataFrame({"date": [20260821]})

        def close(self):
            pass

    monkeypatch.setattr(
        mootdx_quotes.Quotes,
        "factory",
        staticmethod(lambda **kwargs: FakeXdxrClient()),
    )
    adapter = MootdxSourceAdapter(servers=(("127.0.0.1", 7709),), page_size=37)

    report = adapter.health_probe(symbols=("600000", "000001"))

    assert report["ok"] is True
    assert counts == [37, 37, 37]
    assert report["bars_server"] == ("127.0.0.1", 7709)
    assert report["xdxr_server"] == ("127.0.0.1", 7709)


def test_mac_bars_probe_fails_over_until_a_candidate_returns_real_rows(monkeypatch):
    calls = []
    clients = []

    class FakeMacClient:
        def __init__(self, *, host, **kwargs):
            self.host = host
            self.closed = False
            clients.append(self)

        def connect(self):
            pass

        def get_stock_kline(self, *, market, code, period, start, count):
            calls.append((self.host, market, code, period, start, count))
            if self.host == "empty":
                return pd.DataFrame()
            return pd.DataFrame(
                {
                    "datetime": [pd.Timestamp("2026-08-21")],
                    "open": [10.0],
                    "high": [10.3],
                    "low": [9.9],
                    "close": [10.2],
                    "vol": [10000.0],
                    "amount": [102000.0],
                }
            )

        def close(self):
            self.closed = True

    fake_module = ModuleType("easy_tdx")
    fake_module.MacClient = FakeMacClient
    monkeypatch.setitem(sys.modules, "easy_tdx", fake_module)
    adapter = MootdxSourceAdapter(
        bar_servers=(("empty", 7709), ("working", 7709)),
        page_size=37,
    )

    client = adapter._client()

    assert client.host == "working"
    assert adapter.connected_server == ("working", 7709)
    assert clients[0].closed is True
    assert clients[1].closed is False
    assert calls == [
        ("empty", 1, "600000", 4, 0, 37),
        ("working", 1, "600000", 4, 0, 37),
    ]
    client.close()


def test_fetch_uses_mac_market_enum_values_and_share_volume():
    calls = []

    class FakeClient:
        def get_stock_kline(self, *, market, code, period, start, count):
            calls.append((market, code, period, start, count))
            return pd.DataFrame(
                {
                    "datetime": [pd.Timestamp("2026-08-21")],
                    "open": [10.0],
                    "high": [10.3],
                    "low": [9.9],
                    "close": [10.2],
                    "vol": [10000.0],
                    "amount": [102000.0],
                }
            )

        def close(self):
            pass

    class Adapter(MootdxSourceAdapter):
        def _client(self):
            return FakeClient()

    adapter = Adapter(page_size=37)
    result = adapter.fetch(
        "bars_daily",
        ["920000.XBSE"],
        "2026-08-21",
        "2026-08-21",
        ["instrument", "datetime", "volume", "amount"],
    )

    assert calls == [(2, "920000", 4, 0, 37)]
    assert result.status.value == "success"
    assert result.rows.iloc[0]["volume"] == 10000.0
