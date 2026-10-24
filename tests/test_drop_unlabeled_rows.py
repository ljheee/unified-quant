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

from uq.errors import ContractError
from uq.research_chain.adapters import drop_unlabelled_factor_rows, unlabeled_keys


def _labels() -> pd.DataFrame:
    return pd.DataFrame({
        "instrument": ["A", "A", "A", "B", "B"],
        "decision_date": pd.to_datetime(
            ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-06", "2026-01-07"]
        ),
        "label": [0.01, None, 0.02, 0.03, None],
    })


WINDOW = ["2026-01-05", "2026-01-06", "2026-01-07",
         "2026-01-08", "2026-01-09", "2026-01-12"]


def _frame_for() -> pd.DataFrame:
    return pd.DataFrame({
        "instrument": ["A"] * len(WINDOW) + ["B"] * len(WINDOW),
        "datetime": pd.to_datetime(WINDOW * 2),
        "volume_ratio_20d": [1.0] * (2 * len(WINDOW)),
    })


def _labels_for(nulls: dict[str, list[str]]) -> pd.DataFrame:
    rows = []
    for instrument in ("A", "B"):
        for day in WINDOW:
            rows.append({
                "instrument": instrument,
                "decision_date": pd.Timestamp(day),
                "label": None if day in nulls.get(instrument, []) else 0.01,
            })
    return pd.DataFrame(rows)


class TestDropUnlabelledFactorRows:
    """The row-filtering half of `missing_policy: drop_unlabeled_rows`.

    `unlabeled_keys` decides *which* rows are unlabelled; this decides what happens
    to the factor frame. It is applied to the preprocessing *input* rather than the
    merged frame, because `DatasetWriter` requires the published frame and the
    preprocessing output to carry the same rows.
    """

    def test_removes_exactly_the_unlabelled_rows(self):
        frame = _frame_for()
        labels = _labels_for({"A": ["2026-01-08", "2026-01-09"]})
        kept, audit = drop_unlabelled_factor_rows(frame, labels)
        assert len(kept) == 10
        # A keeps its four labelled sessions, B keeps all six
        assert sorted(
            kept.loc[kept["instrument"] == "A", "datetime"].dt.strftime("%Y-%m-%d")
        ) == ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-12"]
        assert sorted(
            kept.loc[kept["instrument"] == "B", "datetime"].dt.strftime("%Y-%m-%d")
        ) == WINDOW
        assert audit["rows"] == 2
        assert audit["instruments"] == 1

    def test_an_instrument_that_loses_some_rows_keeps_the_rest(self):
        # The point of the policy: a halt removes the rows that cannot be trained on,
        # not the instrument. Dropping the whole name would reinstate the hindsight
        # bias a complete-case universe carries.
        kept, _ = drop_unlabelled_factor_rows(
            _frame_for(), _labels_for({"A": ["2026-01-09", "2026-01-12"]})
        )
        assert set(kept["instrument"]) == {"A", "B"}
        assert "A" in set(kept["instrument"])
        assert len(kept[kept["instrument"] == "A"]) == 4

    def test_a_fully_unlabelled_frame_is_refused(self):
        # Publishing a set with no trainable rows is worse than refusing: the dataset
        # would be empty and the caller would have to notice downstream.
        labels = _labels_for({})
        labels["label"] = None
        # The refusal now comes from the single implementation in
        # `models/sample_construction.py`, so the wording is that one's. It is the
        # same guard -- an all-null label set cannot yield a trainable frame.
        with pytest.raises(ContractError, match="no training rows"):
            drop_unlabelled_factor_rows(_frame_for(), labels)

    def test_nothing_to_drop_returns_the_frame_untouched(self):
        frame = _frame_for()
        kept, audit = drop_unlabelled_factor_rows(frame, _labels_for({}))
        # The frame is returned unchanged, and the audit still reports the two
        # shapes it inspected (both zero) rather than being empty -- an empty dict
        # would not distinguish "nothing to drop" from "nothing was checked".
        assert audit["null_labels"] == 0
        assert audit["absent_keys"] == 0
        assert audit["rows"] == 0
        pd.testing.assert_frame_equal(kept, frame)

    def test_the_result_carries_a_reset_index(self):
        # The frame is handed to the preprocessing transform, whose fingerprint is
        # order- and index-sensitive, so a dropped-row frame must not keep stale
        # indices.
        kept, _ = drop_unlabelled_factor_rows(
            _frame_for(), _labels_for({"A": ["2026-01-05"]})
        )
        assert kept.index.tolist() == list(range(len(kept)))

    def test_a_factor_row_absent_from_the_label_frame_is_dropped(self):
        # The shape a value test cannot see: `C` has factor rows but no key in the
        # label frame at all, so `label_frame["label"].isna()` names nothing. The
        # downstream left join would turn it into a NaN label and publish it. The
        # audit separates it from a present-but-null label so the report says which
        # shape was actually removed.
        labels = _labels_for({})
        labels = labels[labels["instrument"] != "A"]
        kept, audit = drop_unlabelled_factor_rows(_frame_for(), labels)
        assert "A" not in set(kept["instrument"])
        assert set(kept["instrument"]) == {"B"}
        assert audit["absent_keys"] == len(WINDOW)
        assert audit["null_labels"] == 0
        # and the surviving rows really do join to a label
        joined = kept.merge(
            labels, how="left", left_on=["instrument", "datetime"],
            right_on=["instrument", "decision_date"],
        )
        assert not joined["label"].isna().any()

    def test_both_shapes_are_counted_separately(self):
        # A produces present-but-null labels; C is absent entirely. Same policy, two
        # defects, one report that does not conflate them.
        labels = _labels_for({"A": [WINDOW[0]]})
        labels = labels[labels["instrument"] != "B"]
        kept, audit = drop_unlabelled_factor_rows(_frame_for(), labels)
        assert audit["null_labels"] == 1
        assert audit["absent_keys"] == len(WINDOW)
        assert audit["rows"] == 1 + len(WINDOW)
        assert set(kept["instrument"]) == {"A"}


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


class TestTheTwoImplementationsAgree:
    """`drop_unlabelled_factor_rows` must stay an adapter, not a second rule.

    The v5 stage filters inside the stage; v6 publishes `sample_construction.v1`
    from `select_training_sample`. These used to be two independent copies of the
    label-side rule and had already drifted in strictness (the v5 copy accepted
    duplicate factor keys and a mismatched column set). The cross-check below is
    the lock that keeps them from drifting again: the same input must select the
    same rows and report the same counters through both entry points.
    """

    def test_both_entry_points_select_the_same_rows(self):
        from uq.models.sample_construction import select_training_sample

        frame = _frame_for()
        labels = _labels_for({"A": [WINDOW[0], WINDOW[2]]})
        ordered = [
            column for column in frame.columns
            if column not in {"instrument", "datetime"}
        ]

        stage_kept, stage_audit = drop_unlabelled_factor_rows(frame, labels)
        sample_kept, _, sample_labels = select_training_sample(
            frame, labels, ordered_features=ordered,
            missing_policy="drop_unlabeled_rows",
        )

        keys = lambda df: set(
            zip(
                df["instrument"].astype(str),
                pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d"),
            )
        )
        assert keys(stage_kept) == keys(sample_kept)
        # The stage's audit spells out both counters even at zero; the sample
        # artifact only records what it removed. They must agree on the removal.
        for counter in ("rows", "null_labels", "absent_keys"):
            assert stage_audit[counter] == sample_labels.get(counter, 0), counter
        # ...and the stage's filtered frame is the preprocessing *input*, so it
        # must not carry the label column the sample artifact publishes.
        assert list(stage_kept.columns) == ["instrument", "datetime", *ordered]
