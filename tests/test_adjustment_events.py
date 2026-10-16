"""TDX xdxr as the independent source for the adjusted-price line.

The measured provenance these locks encode (2026-10-16, TDX ``xdxr`` vs the
vendor's ``adj_factor`` jumps over 83 ex-right events on 6 symbols): agreement
to a maximum of 0.109%, mean 0.023%, 83/83 inside 0.5%.  The unit conventions
below are the part that is easy to get wrong and silent, so each is pinned.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.pipeline.adjustment_events import (
    AdjustmentEvent,
    events_from_xdxr,
    symbol_of,
)


def _xdxr_row(**overrides):
    row = {
        "year": 2015, "month": 4, "day": 13,
        "category": 1, "name": "除权除息",
        "fenhong": 1.74,          # per share, cash
        "peigujia": 0.0,
        "songzhuangu": 2.0,       # per 10 shares -> 0.20 send ratio
        "peigu": 0.0,
        "suogu": 0.0,
    }
    row.update(overrides)
    return row


def test_events_from_xdxr_normalises_units_to_per_share():
    (event,) = events_from_xdxr(pd.DataFrame([_xdxr_row()]))
    assert event.session == dt.date(2015, 4, 13)
    # every amount is per 10 shares in TDX, cash included
    assert event.cash == pytest.approx(0.174)
    assert event.send == pytest.approx(0.20)
    assert event.transfer == pytest.approx(0.0)
    assert event.allot == pytest.approx(0.0)


def test_only_ex_right_events_survive():
    frame = pd.DataFrame([
        _xdxr_row(year=2015, month=4, day=13),
        # TDX emits share-count drift rows too; they carry no ex-right meaning
        _xdxr_row(category=5, name="股本变化", year=2025, month=10, day=17,
                  fenhong=None, songzhuangu=None, peigu=None, peigujia=None),
    ])
    events = events_from_xdxr(frame)
    assert [event.session for event in events] == [dt.date(2015, 4, 13)]


def test_events_are_returned_in_date_order():
    frame = pd.DataFrame([
        _xdxr_row(year=2020, month=7, day=23),
        _xdxr_row(year=2015, month=4, day=13),
        _xdxr_row(year=2018, month=7, day=12),
    ])
    assert [event.session for event in events_from_xdxr(frame)] == [
        dt.date(2015, 4, 13), dt.date(2018, 7, 12), dt.date(2020, 7, 23),
    ]


def test_ex_right_ratio_reproduces_the_measured_vendor_jump():
    # 000001.SZ, ex 2015-04-13: prev close 19.80, fenhong 1.74, send 2.0/10.
    # Vendor adj_factor jump measured 1.210263; the recomputation must land
    # within the 0.5% band the 83-event run stayed inside.
    (event,) = events_from_xdxr(pd.DataFrame([_xdxr_row()]))
    ratio = event.ratio(19.80)
    assert ratio == pytest.approx(1.210639, rel=1e-3)
    assert abs(ratio - 1.210263) / 1.210263 < 0.005


def test_cash_only_event_matches_the_vendor_to_within_0_05_percent():
    # 000001.SZ, ex 2017-07-21: no share changes, so this isolates the cash term.
    (event,) = events_from_xdxr(pd.DataFrame([_xdxr_row(year=2017, month=7, day=21,
                                                       fenhong=1.58, songzhuangu=0.0)]))
    ratio = event.ratio(10.97)
    assert abs(ratio - 1.014806) / 1.014806 < 0.0005


def test_send_ratio_belongs_in_the_denominator_not_the_numerator():
    # The bug this pins: adding send*price as cash instead of dividing by
    # (1+send) yields the reciprocal artifact and a ~30% "gap".
    (event,) = events_from_xdxr(pd.DataFrame([_xdxr_row()]))
    correct = event.ratio(19.80)
    # the wrong form: treating the send ratio as if it were a cash term
    naive = 19.80 / (19.80 - 0.174 + 0.20 * 19.80)
    assert correct > 1.0
    assert naive < 1.0
    assert abs(correct - naive) > 0.1


def test_ratio_is_nan_rather_than_fabricated_without_a_prior_close():
    (event,) = events_from_xdxr(pd.DataFrame([_xdxr_row()]))
    assert event.ratio(0.0) != event.ratio(0.0)  # NaN
    assert event.ratio(-1.0) != event.ratio(-1.0)


def test_missing_required_columns_are_refused():
    with pytest.raises(ContractError, match="xdxr events missing columns"):
        events_from_xdxr(pd.DataFrame([{"year": 2015, "month": 4}]))


def test_empty_frame_yields_no_events():
    assert events_from_xdxr(pd.DataFrame()) == []
    assert events_from_xdxr(None) == []


def test_instrument_is_reduced_to_the_bare_tdx_code():
    assert symbol_of("600000.XSHG") == "600000"
    assert symbol_of("000001.XSHE") == "000001"
    assert symbol_of("920961.XBSE") == "920961"
