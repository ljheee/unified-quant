"""TDX source adapter: market routing and the 北交所 xdxr gap.

mootdx's own ``get_stock_market`` routes a leading ``9`` to Shanghai before the
Beijing branch exists, so ``920xxx`` codes were asked for on market 1 and TDX
returned no rows at all (measured: market 2 has 51 rows for 920000, market 1 has
0). The adapter therefore resolves the market itself.

These are offline: they lock the routing table, not the network.
"""

from __future__ import annotations

import pandas as pd
import pytest

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
