from datetime import date
import pandas as pd
import pytest

from uq.errors import ContractError
from uq.factors.adjusted_price import assert_not_raw_close, calculate_adjusted_factors


def frame(days=22, factor=1.0, close=10.0):
    return pd.DataFrame({
        "instrument": ["600000.XSHG"] * days,
        "datetime": pd.to_datetime([date(2026,7,1+day) for day in range(days)]),
        "close": [close] * days,
        "adj_factor": [factor] * days,
    })


SNAPSHOT="a"*64; TABLE="b"*64


def test_adjusted_factors_and_history_nulls():
    result=calculate_adjusted_factors(frame(), adjustment_snapshot_ids=[SNAPSHOT]*2,effective_date_table_checksums=[TABLE]*2)
    assert len(result)==22
    for column in ("return_1d","return_5d","return_20d","volatility_20d"):
        assert result[column].isna().sum() >= 1


def test_missing_dependency_rejected():
    bad=frame().drop(columns=["adj_factor"])
    with pytest.raises(ContractError,match="adjusted dependency absent"):
        calculate_adjusted_factors(bad,adjustment_snapshot_ids=[SNAPSHOT],effective_date_table_checksums=[TABLE])


@pytest.mark.parametrize("field,value,message", [
    ("snapshot","c"*64,"two snapshots"),
])
def test_mixed_snapshot_rejected(field,value,message):
    with pytest.raises(ContractError,match=message):
        calculate_adjusted_factors(frame(),adjustment_snapshot_ids=[SNAPSHOT,value],effective_date_table_checksums=[TABLE,TABLE])


def test_effective_checksum_mismatch():
    with pytest.raises(ContractError,match="inside one window"):
        calculate_adjusted_factors(frame(),adjustment_snapshot_ids=[SNAPSHOT,SNAPSHOT],effective_date_table_checksums=[TABLE,"d"*64])


def test_raw_close_substitution_detected():
    bars=frame()
    raw=bars.copy(); raw["adj_factor"]=1.0
    adjusted=calculate_adjusted_factors(bars,adjustment_snapshot_ids=[SNAPSHOT,SNAPSHOT],effective_date_table_checksums=[TABLE,TABLE])
    with pytest.raises(ContractError,match="raw close substituted"):
        assert_not_raw_close(raw,adjusted)
