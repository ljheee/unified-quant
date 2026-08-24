import pandas as pd
import pytest

from uq.market.adjustment import XdxrAdjustmentDeriver


def test_cash_only_formula_matches_provider_factor_ratios():
    cases = [
        ("600000.XSHG", 9.31, 0.42, 16.5935, 17.3774),
        ("000001.XSHE", 11.30, 0.36, 134.5794, 139.008),
        ("300750.XSHE", 388.07, 1.411, 1.9495, 1.9566),
    ]
    for symbol, pre_close, cash, prior_factor, provider_factor in cases:
        event_date = pd.Timestamp("2026-01-01")
        events = pd.DataFrame([{
            "year": event_date.year,
            "month": event_date.month,
            "day": event_date.day,
            "category": 1,
            "fenhong": cash * 10.0,
            "songzhuangu": 0.0,
            "peigu": 0.0,
            "peigujia": 0.0,
        }])
        sessions = pd.to_datetime(["2025-12-31", event_date])
        derivation = XdxrAdjustmentDeriver().derive(
            symbol,
            events,
            sessions,
            pd.Series([pre_close], index=pd.to_datetime([event_date])),
        )
        derived = float(derivation.frame.iloc[0]["adj_factor"])
        expected = provider_factor / prior_factor
        assert derived == pytest.approx(expected, rel=2e-4)
