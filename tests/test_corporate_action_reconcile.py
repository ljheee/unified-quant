"""The two corporate-action event sources, compared against each other.

Each derivation is separately reconciled against the vendor's cumulative
adj_factor, which leaves a real hole: if the vendor's factor itself is wrong, both
derivations agree with it. These locks cover the direct event-to-event comparison,
using the divergence the 2026-10-16 investigation found by hand.
"""

from __future__ import annotations

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.market.corporate_action_reconcile import (
    reconcile_instrument,
    summarize,
)

COLS = ("year", "month", "day", "category", "fenhong", "songzhuangu", "peigu", "peigujia")


def _events(rows):
    return pd.DataFrame(rows, columns=COLS)


def test_identical_event_sources_agree():
    left = _events([(2025, 7, 16, 1, 41.0, 0.0, 0.0, 0.0)])
    right = left.copy()
    result = reconcile_instrument("600000.XSHG", left, right)
    assert result.status == "agreed"
    assert result.compared == 1
    assert result.agreements == 1
    assert not result.divergences


def test_an_amount_difference_is_reported_with_both_values():
    # the 000858 1999-06-08 case: TDX 10.0 per 10 shares vs the disclosure's 12.5
    left = _events([(1999, 6, 8, 1, 12.5, 0.0, 0.0, 0.0)])
    right = _events([(1999, 6, 8, 1, 10.0, 0.0, 0.0, 0.0)])
    result = reconcile_instrument("000858.XSHE", left, right)
    assert result.status == "amount_diverged"
    (divergence,) = result.divergences
    assert divergence.field == "fenhong"
    assert divergence.disclosure == 12.5
    assert divergence.tdx == 10.0


def test_a_send_vs_transfer_split_that_preserves_shares_is_still_reported():
    # A classification difference (送股 vs 转增) changes two fields even when the
    # share-count total is the same, so it must surface rather than be averaged away.
    left = _events([(2015, 4, 13, 1, 1.74, 2.0, 0.0, 0.0)])
    right = _events([(2015, 4, 13, 1, 1.74, 0.0, 0.0, 0.0)])
    result = reconcile_instrument("000001.XSHE", left, right)
    assert result.status == "amount_diverged"
    fields = {item.field for item in result.divergences}
    assert fields == {"songzhuangu"}


def test_a_date_only_one_source_knows_about_is_a_coverage_difference():
    left = _events([(2025, 7, 16, 1, 41.0, 0.0, 0.0, 0.0)])
    right = _events([
        (2025, 7, 16, 1, 41.0, 0.0, 0.0, 0.0),
        (2007, 6, 18, 1, 0.9, 1.0, 0.0, 0.0),
    ])
    result = reconcile_instrument("000001.XSHE", left, right)
    assert result.status == "coverage_diverged"
    assert len(result.tdx_only) == 1
    assert result.agreements == 1


def test_a_null_amount_is_treated_as_zero_not_as_absent():
    left = _events([(2025, 7, 16, 1, None, 0.0, 0.0, 0.0)])
    right = _events([(2025, 7, 16, 1, 0.0, 0.0, 0.0, 0.0)])
    assert reconcile_instrument("600000.XSHG", left, right).status == "agreed"


def test_a_source_that_omits_a_field_is_reported_not_silently_zeroed():
    # One side does not carry `peigujia` at all: that is a schema difference, not a
    # zero, and must be reported instead of compared as zero.
    left = _events([(2025, 7, 16, 1, 41.0, 0.0, 0.0, 0.0)])
    right = left.drop(columns=["peigujia"])
    result = reconcile_instrument("600000.XSHG", left, right)
    assert result.status == "amount_diverged"
    assert any(item.field == "peigujia" for item in result.divergences)


def test_a_duplicate_date_within_one_source_is_refused():
    left = _events([
        (2025, 7, 16, 1, 41.0, 0.0, 0.0, 0.0),
        (2025, 7, 16, 1, 42.0, 0.0, 0.0, 0.0),
    ])
    with pytest.raises(ContractError, match="duplicate event date"):
        reconcile_instrument("600000.XSHG", left, left.copy())


def test_no_overlap_is_reported_as_its_own_status():
    result = reconcile_instrument(
        "600000.XSHG",
        _events([(2025, 7, 16, 1, 41.0, 0.0, 0.0, 0.0)]),
        _events([(2007, 6, 18, 1, 0.9, 1.0, 0.0, 0.0)]),
    )
    assert result.status == "no_overlap"


def test_a_frame_missing_date_columns_is_refused():
    with pytest.raises(ContractError, match="missing date columns"):
        reconcile_instrument("600000.XSHG", pd.DataFrame({"fenhong": [1.0]}), pd.DataFrame())


def test_summarize_counts_each_status():
    agreed = reconcile_instrument(
        "600000.XSHG", _events([(2025, 7, 16, 1, 41.0, 0.0, 0.0, 0.0)]),
        _events([(2025, 7, 16, 1, 41.0, 0.0, 0.0, 0.0)]),
    )
    amount_diverged = reconcile_instrument(
        "000858.XSHE", _events([(1999, 6, 8, 1, 12.5, 0.0, 0.0, 0.0)]),
        _events([(1999, 6, 8, 1, 10.0, 0.0, 0.0, 0.0)]),
    )
    summary = summarize([agreed, amount_diverged])
    assert summary["instruments_compared"] == 2
    assert summary["agreed"] == 1
    assert summary["amount_diverged"] == 1
    assert summary["amount_diverged_instruments"] == ["000858.XSHE"]
