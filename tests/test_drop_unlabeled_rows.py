"""Locks for `missing_policy: drop_unlabeled_rows`.

A null *label* is a different defect from a null *feature*, so it gets its own policy
rather than being folded into `drop_incomplete_rows`:

* a null feature means the row cannot be computed (`close_location_1d` when
  `high == low` on a limit-locked day; a 20-session warm-up);
* a null label means the target is unknowable (the instrument stopped trading, so
  there is no forward price to compute a return against).

Qlib separates the two the same way: `DropnaLabel` is a distinct processor from
`DropnaProcessor`, and `is_for_infer()` is False for labels but True for features
-- measured -- so a row can be untrainable and still be perfectly scoreable. sklearn
draws the same line inside one call: `ensure_all_finite` governs X and its docstring
says it "does not influence whether y can have np.inf, np.nan, pd.NA values".

Why this file does not reuse `tests/test_research_chain_phase2.py`'s fixture: that
fixture publishes a factor partition over 20 sessions while its label set spans 25
(the extra five are the label horizon's tail), so every unlabelled row falls on a
date the factor frame does not contain and the policy has nothing to act on. These
locks exercise the rule directly instead.
"""

from __future__ import annotations

import pandas as pd
import pytest

from uq.research_chain.adapters import unlabeled_keys


def _labels() -> pd.DataFrame:
    return pd.DataFrame({
        "instrument": ["A", "A", "A", "B", "B"],
        "decision_date": pd.to_datetime(
            ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-06", "2026-01-07"]
        ),
        "label": [0.01, None, 0.02, 0.03, None],
    })


def test_names_exactly_the_rows_whose_label_is_null():
    keys = unlabeled_keys(_labels())
    assert list(keys.columns) == ["instrument", "decision_date"]
    assert len(keys) == 2
    assert list(keys["instrument"]) == ["A", "B"]
    assert [value.date().isoformat() for value in keys["decision_date"]] == [
        "2026-01-06", "2026-01-07",
    ]


def test_an_all_labelled_frame_yields_no_keys():
    frame = _labels()
    frame["label"] = frame["label"].fillna(0.0)
    assert unlabeled_keys(frame).empty


def test_dropping_keeps_every_other_row_of_that_instrument():
    # The point of the policy: a halt removes the rows that cannot be trained on,
    # not the instrument. A row with no target is not a reason to drop the rows that
    # still have one -- that would reintroduce the hindsight bias of a
    # future-conditioned complete-case universe.
    frame = pd.DataFrame({
        "instrument": ["A", "A", "B"],
        "datetime": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-06"]),
        "f1": [1.0, 2.0, 3.0],
    })
    keys = unlabeled_keys(_labels())
    merged = frame.merge(
        keys.drop_duplicates(), how="left",
        left_on=["instrument", pd.to_datetime(frame["datetime"])],
        right_on=["instrument", "decision_date"], indicator=True,
    )
    kept = frame.loc[(merged["_merge"] == "left_only").to_numpy()].reset_index(drop=True)
    assert kept["instrument"].tolist() == ["A", "B"]   # A survives with its other row
    assert kept["datetime"].tolist() == [
        pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")
    ]


def test_an_all_labelled_frame_returns_no_keys_and_a_single_null_returns_one():
    # The audit trail names every dropped row, so an off-by-one here silently
    # mis-reports how much data the policy removed. `A` contributes one null (01-06),
    # `B` one (01-07) -- if the selection dropped more than that, `drop_unlabeled_rows`
    # would be discarding rows the policy says it keeps.
    keys = unlabeled_keys(_labels())
    assert len(keys) == 2
    frame = _labels()
    frame.loc[frame["label"] == 0.02, "label"] = None
    assert len(unlabeled_keys(frame)) == 3
    frame["label"] = frame["label"].fillna(0.0)
    assert unlabeled_keys(frame).empty
