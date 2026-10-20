from datetime import date
import pandas as pd
import pytest

from uq.errors import ContractError
from uq.factors.adjusted_price import assert_not_raw_close, calculate_adjusted_factors


def frame(days=22, factor=1.0, close=10.0):
    # A real instrument never has a constant close across a long window, and a
    # constant close plus a constant 1.0 factor is exactly the degenerate shape the
    # production guard rejects (cr.md FC-M3). Drift the close so the fixture models
    # a flat-factor-but-trading instrument (the 62-of-5059 real case).
    return pd.DataFrame({
        "instrument": ["600000.XSHG"] * days,
        "datetime": pd.to_datetime([date(2026,7,1+day) for day in range(days)]),
        "close": [close * (1.0 + 0.001 * day) for day in range(days)],
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


def _event_frame(*rows):
    return pd.DataFrame(
        list(rows),
        columns=["year", "month", "day", "category", "fenhong", "songzhuangu", "peigu", "peigujia"],
    )


def test_a_degenerate_flat_factor_with_a_flat_close_is_rejected():
    # The narrow production guard: a constant 1.0 factor AND a constant close means
    # the adjustment join produced nothing. It must NOT reject a flat factor alone,
    # because 62 of 5059 real instruments are legitimately flat (never had a
    # corporate action) -- that case is covered by the test below.
    bars = frame()
    bars["close"] = 10.0
    with pytest.raises(ContractError, match="adjustment join produced nothing"):
        calculate_adjusted_factors(
            bars, adjustment_snapshot_ids=[SNAPSHOT], effective_date_table_checksums=[TABLE]
        )


def test_a_legitimately_flat_factor_still_passes():
    # Flat factor, but the close moves: a real instrument with no corporate action.
    calculate_adjusted_factors(
        frame(factor=1.0), adjustment_snapshot_ids=[SNAPSHOT], effective_date_table_checksums=[TABLE]
    )


def test_event_replay_accepts_a_factor_jump_that_matches_the_event():
    # A cash dividend on 2026-07-05: the factor must jump by pre_close/(pre_close-cash).
    bars = frame(factor=1.0)
    bars["close"] = [10.0, 10.0, 10.0, 10.0, 9.8, 9.8, 9.8] + [9.8] * 15
    pre_close = 10.0
    cash_per_share = 0.2
    jump = pre_close / (pre_close - cash_per_share)
    bars.loc[4:, "adj_factor"] = jump
    events = _event_frame((2026, 7, 5, 1, 2.0, 0.0, 0.0, 0.0))  # fenhong is per 10 shares
    report = calculate_adjusted_factors(
        bars, adjustment_snapshot_ids=[SNAPSHOT], effective_date_table_checksums=[TABLE],
        events=events,
    )
    assert len(report) == len(bars)


def test_event_replay_rejects_a_missing_factor_jump():
    # Same event, but the factor never moves: the adjustment was not applied.
    bars = frame(factor=1.0)
    events = _event_frame((2026, 7, 5, 1, 2.0, 0.0, 0.0, 0.0))
    with pytest.raises(ContractError, match="disagree with the corporate-action event table"):
        calculate_adjusted_factors(
            bars, adjustment_snapshot_ids=[SNAPSHOT], effective_date_table_checksums=[TABLE],
            events=events,
        )


def test_event_replay_skips_an_event_outside_the_window():
    # An event whose ex-date is not in the frame is reported as skipped, not failed:
    # a window that starts after the event legitimately does not contain it.
    bars = frame(factor=1.0)
    events = _event_frame((2020, 1, 2, 1, 2.0, 0.0, 0.0, 0.0))
    calculate_adjusted_factors(
        bars, adjustment_snapshot_ids=[SNAPSHOT], effective_date_table_checksums=[TABLE],
        events=events,
    )


def test_a_multi_instrument_event_table_only_replays_each_instruments_own_events():
    # A flat table replayed against every instrument is a false-failure machine:
    # 600000 has a dividend and its factor jumps, 000001 has no corporate action so
    # its factor is flat *by right*. Demanding the same jump from both rejects a
    # correct adjustment. With an `instrument` column each frame is checked against
    # its own events only.
    bars = frame(factor=1.0)
    flat = pd.DataFrame({
        "instrument": ["000001.XSHE"] * len(bars),
        "datetime": bars["datetime"],
        "close": bars["close"],
        "adj_factor": [1.0] * len(bars),
    })
    jumping = bars.copy()
    pre_close, cash = 10.0, 0.2
    jumping["adj_factor"] = 1.0
    jumping.loc[4:, "adj_factor"] = pre_close / (pre_close - cash)
    combined = pd.concat([jumping, flat], ignore_index=True)
    events = _event_frame((2026, 7, 5, 1, 2.0, 0.0, 0.0, 0.0))
    events["instrument"] = ["600000.XSHG"]
    report = calculate_adjusted_factors(
        combined, adjustment_snapshot_ids=[SNAPSHOT], effective_date_table_checksums=[TABLE],
        events=events,
    )
    assert len(report) == len(combined)
    # the same flat table without the column keeps the previous behaviour, so
    # existing single-instrument callers are unaffected
    with pytest.raises(ContractError, match="disagree with the corporate-action event table"):
        calculate_adjusted_factors(
            combined, adjustment_snapshot_ids=[SNAPSHOT], effective_date_table_checksums=[TABLE],
            events=_event_frame((2026, 7, 5, 1, 2.0, 0.0, 0.0, 0.0)),
        )
