import pandas as pd
from pathlib import Path
import pytest

from uq.market.adjustment import XdxrAdjustmentDeriver


def _event(cash=0.0, bonus=0.0, rights=0.0, rights_price=0.0):
    return {
        "year": 2026, "month": 8, "day": 21, "category": 1,
        "fenhong": cash, "songzhuangu": bonus, "peigu": rights,
        "peigujia": rights_price,
    }


def test_cash_without_pre_close_is_rejected():
    with pytest.raises(ValueError, match="positive pre_close"):
        XdxrAdjustmentDeriver()._event_multiplier(pd.Series(_event(cash=2.0)), None)
