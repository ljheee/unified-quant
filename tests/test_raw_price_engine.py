from datetime import date
import hashlib

import numpy as np
import pandas as pd
import pytest

from uq.errors import ContractError
from uq.factors.raw_price import calculate_raw_price_factors, logical_fingerprint


def bars(rows):
    return pd.DataFrame(rows)


def test_raw_price_factor_values_and_insufficient_history_nulls():
    rows=[]
    for day in range(1, 23):
        rows.append({
            "instrument":"600000.XSHG", "datetime":pd.Timestamp(2026,8,day), "high":11.0,
            "low":9.0, "close":10.0, "volume":float(day), "amount":float(day*10),
        })
    out=calculate_raw_price_factors(bars(rows))
    assert len(out)==22
    assert out["amount_20d"].iloc[:19].isna().all()
    assert out["amount_20d"].iloc[19] == pytest.approx(sum(range(1,21))*10/20)
    assert out["volume_ratio_20d"].iloc[19] == pytest.approx(20 / (sum(range(1,21))/20))
    assert out["range_ratio_1d"].iloc[0] == pytest.approx(0.2)
    assert out["close_location_1d"].iloc[0] == pytest.approx(0.5)


def test_flat_high_low_emits_null_not_sentinel():
    frame=bars([{
        "instrument":"000001.XSHE","datetime":pd.Timestamp(2026,8,21),"high":5.0,"low":5.0,
        "close":5.0,"volume":1.0,"amount":1.0,
    }])
    out=calculate_raw_price_factors(frame)
    assert pd.isna(out.iloc[0]["close_location_1d"])


def test_duplicate_and_missing_keys_rejected():
    base={"instrument":"A","datetime":pd.Timestamp(2026,8,21),"high":2.0,"low":1.0,"close":1.5,"volume":1.0,"amount":1.0}
    with pytest.raises(ContractError,match="duplicate canonical keys"):
        calculate_raw_price_factors(bars([base,base]))
    missing=dict(base); missing.pop("volume")
    with pytest.raises(ContractError,match="missing raw-price input columns"):
        calculate_raw_price_factors(bars([missing]))


def test_future_partition_cannot_affect_historical_compute():
    historical=bars([
        {"instrument":"A","datetime":pd.Timestamp(2026,8,20),"high":3.0,"low":1.0,"close":2.0,"volume":1.0,"amount":1.0},
        {"instrument":"A","datetime":pd.Timestamp(2026,8,21),"high":4.0,"low":2.0,"close":3.0,"volume":2.0,"amount":2.0},
    ])
    polluted=bars(historical.to_dict("records") + [
        {"instrument":"A","datetime":pd.Timestamp(2026,8,25),"high":9.0,"low":1.0,"close":8.0,"volume":99.0,"amount":99.0},
    ])
    left=calculate_raw_price_factors(historical)
    right=calculate_raw_price_factors(polluted).iloc[:len(left)]
    pd.testing.assert_frame_equal(left,right)


def test_logical_fingerprint_is_deterministic():
    frame=bars([
        {"instrument":"A","datetime":pd.Timestamp(2026,8,21),"high":2.0,"low":1.0,"close":1.5,"volume":3.0,"amount":7.0},
    ])
    left=calculate_raw_price_factors(frame)
    right=calculate_raw_price_factors(frame.copy())
    assert logical_fingerprint(left)==logical_fingerprint(right)


def test_raw_price_factors_do_not_consume_adjustment_data():
    base = bars([
        {"instrument":"A","datetime":pd.Timestamp(2026,8,21),"high":2.0,"low":1.0,"close":1.5,"volume":3.0,"amount":7.0},
    ])
    polluted = base.copy()
    polluted["adj_factor"] = [2.0]
    left = calculate_raw_price_factors(base)
    right = calculate_raw_price_factors(polluted)
    pd.testing.assert_frame_equal(left, right)
    assert "adj_factor" not in right.columns
