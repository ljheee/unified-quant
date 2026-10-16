"""The reviewed adjusted-price line producer (cr.md: bars_adjusted/adjusted-v2).

The v1 line was named by the factor store's reviewed rule but never shipped a
schema, so no canonical adjusted partition could be published at all.  This locks
the new producer's three enforced properties:

* a partition whose restatement does not match ``price * adj_factor`` is never
  published (and never gets a "passed" report);
* an adj_factor that is missing or non-positive is refused;
* the published partition reads back through the canonical-v2 reader with the
  same identities the report binds.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import typing

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import CanonicalV2Store, read_canonical_v2
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.pipeline.adjusted_price import (
    ADJUSTED_V2_SCHEMA,
    publish_adjusted_partition,
    restate,
    verify_positive,
    verify_restatement,
)

ROOT = Path(__file__).resolve().parents[1]
SESSION = date(2026, 8, 21)


_SHAPE = {
    "open": [10.0, 20.0],
    "high": [10.5, 21.0],
    "low": [9.5, 19.0],
    "close": [10.2, 20.4],
    "volume": [1000.0, 2000.0],
    "amount": [10200.0, 40800.0],
    "limit_up": [11.22, 22.44],
    "limit_down": [9.18, 18.36],
    "adj_factor": [2.0, 0.5],
}


def _bars(instruments=("600000.XSHG", "000001.XSHE"), **overrides):
    names = [str(value) for value in instruments]
    frame = pd.DataFrame({
        "instrument": names,
        "session_date": pd.to_datetime([SESSION] * len(names)),
        "status": ["trading"] * len(names),
    })
    for column, values in _SHAPE.items():
        frame[column] = list(values)[: len(names)] if len(names) <= len(values) else (
            [values[0]] * len(names)
        )
    for column, value in overrides.items():
        frame[column] = value
    return frame


def _independent(raw: pd.DataFrame):
    """A second, independently derived restatement (here: a different code path
    computing the same product) that the producer must be compared against."""
    out = raw.copy()
    for column in ("open", "high", "low", "close"):
        out[column] = raw[column].astype(float) * raw["adj_factor"].astype(float)
    return out


def _published(tmp_path: Path, frame=None):
    store = CanonicalV2Store(tmp_path)
    frame = _bars() if frame is None else frame
    return publish_adjusted_partition(
        store, SESSION, frame, independent_restated=_independent(frame),
        lineage={"upstream": ["bars_daily/v2"]},
        source_versions={"tushare": "test"},
    ), store


def test_restatement_multiplies_every_price_column():
    restated = restate(_bars())
    for column in ("open", "high", "low", "close"):
        expected = _bars()[column] * _bars()["adj_factor"]
        pd.testing.assert_series_equal(
            restated[column].astype(float), expected.astype(float), check_names=False
        )
    # identity columns are untouched
    pd.testing.assert_frame_equal(restated[["instrument", "session_date"]], _bars()[["instrument", "session_date"]])


def test_missing_adj_factor_is_refused():
    frame = _bars(adj_factor=[None, 0.5])
    with pytest.raises(ContractError, match="adj_factor is missing"):
        restate(frame)


def test_non_positive_adj_factor_is_refused():
    frame = _bars(adj_factor=[0.0, 0.5])
    with pytest.raises(ContractError, match="adj_factor is non-positive"):
        restate(frame)


def test_wrong_restatement_is_refused_before_publication(tmp_path):
    # The frame is schema-legal (invariants hold on its own numbers) but the
    # producer must still refuse it because the prices it carries are not the
    # restatement of the vendor prices by adj_factor.  Nothing is published and
    # no quality report claims "passed".
    raw = _bars()
    drifted = raw.copy()
    # a 1% price drift, far outside the 1e-3 tolerance, while every invariant
    # (high >= close, low <= close, adj_factor > 0) still holds
    drifted["close"] = raw["close"] * 1.01
    drifted["high"] = raw["high"] * 1.01
    drifted["low"] = raw["low"] * 1.01
    drifted["open"] = raw["open"] * 1.01
    drifted["amount"] = raw["amount"] * 1.01
    store = CanonicalV2Store(tmp_path)
    # Either of the two checks may fire first (the producer verifies its own
    # restatement, then the independent one); what matters is that the partition
    # is refused before publication and no report claims "passed".
    with pytest.raises(ContractError) as refusal:
        publish_adjusted_partition(
            store, SESSION, drifted, independent_restated=_independent(raw), source_versions={"tushare": "test"}
        )
    assert "does not equal" in str(refusal.value) or "independent restatement" in str(refusal.value)
    partition = tmp_path / "canonical" / "bars_adjusted" / "adjusted-v2" / f"date={SESSION.isoformat()}"
    assert not partition.exists()
    assert not (tmp_path / "reports").exists()


def test_non_positive_restated_price_is_refused():
    # Driven on an already-restated frame: `restate` itself refuses a non-positive
    # factor, so this isolates the sign rule the review asked for (a restated
    # price of zero would silently divide-by-zero into the factor layer).
    restated = _bars().copy()
    restated["close"] = [0.0, 20.4]
    with pytest.raises(ContractError, match="non-positive"):
        verify_positive(restated)


def test_published_partition_reads_back_with_the_bound_generation(tmp_path):
    payload, store = _published(tmp_path)
    manifest = json.loads(
        (tmp_path / "canonical" / "bars_adjusted" / "adjusted-v2" / f"date={SESSION.isoformat()}" / "manifest.json").read_text()
    )
    assert manifest["generation_id"] == payload["generation_id"]
    # the report the store read back is the one bound to that generation
    report = QualityReportStore().read(
        tmp_path, payload["generation_id"], binding_type="canonical_v2"
    )
    assert report["policy"] == "reject_all"
    assert report["status"] == "passed"
    names = {check["name"] for check in report["checks"]}
    # `_published` uses the independent_restated path, so the cross-check check
    # that ran is the independent-source one. The report must name the check that
    # actually ran, not a fixed corporate-action name (cr.md 8.34).
    assert {
        "coverage",
        "semantic_version",
        "finite_values__restated_price_recomputes",
        "finite_values__restated_price_matches_independent_source",
        "finite_values__restated_price_positive",
    } <= names
    assert "finite_values__restated_jump_matches_corporate_actions" not in names
    # and the data itself is the restated frame
    restored = read_canonical_v2(
        tmp_path, load_schema(ADJUSTED_V2_SCHEMA), SESSION,
        expected_anchor=manifest["trust_anchor_sha256"],
    )
    assert sorted(restored["close"]) == sorted((_bars()["close"] * _bars()["adj_factor"]).tolist())


def test_schema_marks_the_price_columns_as_back_adjusted():
    schema = load_schema(ADJUSTED_V2_SCHEMA)
    assert schema.version == "adjusted-v2"
    for column in ("open", "high", "low", "close"):
        assert schema.fields[column]["adjustment"] == "back_adjusted"
    # units and non-price fields are inherited from the reviewed v2 input
    bars = load_schema("config/schemas/bars_daily.v2.yaml")
    assert schema.fields["volume"]["unit"] == bars.fields["volume"]["unit"]
    assert schema.fields["adj_factor"]["semantics"] == bars.fields["adj_factor"]["semantics"]
    assert schema.invariants == bars.invariants


def test_republishing_the_same_partition_is_immutable(tmp_path):
    _published(tmp_path)
    store = CanonicalV2Store(tmp_path)
    with pytest.raises(ContractError, match="already published"):
        publish_adjusted_partition(
            store, SESSION, _bars(), independent_restated=_independent(_bars()), source_versions={"tushare": "test"}
        )


def test_publication_without_an_independent_restatement_is_refused(tmp_path):
    # Restating and then verifying one's own output is a tautology that would
    # stamp reject_all/passed on anything, so the cross-check is mandatory.
    store = CanonicalV2Store(tmp_path)
    with pytest.raises(ContractError, match="requires an independent restatement"):
        publish_adjusted_partition(
            store, SESSION, _bars(), independent_restated=None, source_versions={"tushare": "test"}
        )


def test_a_disagreeing_independent_restatement_blocks_publication(tmp_path):
    store = CanonicalV2Store(tmp_path)
    with pytest.raises(ContractError, match="disagrees with the independent restatement"):
        publish_adjusted_partition(
            store, SESSION, _bars(), independent_restated=_independent(_bars()).assign(close=1.0),
            source_versions={"tushare": "test"},
        )
    assert not (tmp_path / "canonical" / "bars_adjusted" / "adjusted-v2" / f"date={SESSION.isoformat()}").exists()


# --- the independent source: TDX corporate-action events (cr.md 2026-10-16) ---

from uq.pipeline.adjustment_events import AdjustmentEvent, events_from_xdxr
from uq.pipeline.adjusted_price import restate_from_events


def _events(**overrides):
    kwargs = dict(
        session=date(2026, 8, 21),
        cash=0.362, send=0.0, transfer=0.0, allot=0.0, allot_price=0.0,
    )
    kwargs.update(overrides)
    return AdjustmentEvent(**kwargs)


def test_restatement_from_events_never_reads_adj_factor():
    # The independent path must not consult the vendor factor at all: give the
    # frame an unrelated adj_factor and confirm the recomputation depends only on
    # the event and its prior close.
    raw = _bars()
    raw["adj_factor"] = 7.5
    # An event dated AFTER this session scales it down by ex_right/pre_close: the
    # session is still on the pre-event basis. An event on or before this session is
    # already reflected in the quoted price and is left alone.
    closes = {"600000.XSHG": {date(2026, 8, 21): 10.0},
              "000001.XSHE": {date(2026, 8, 21): 20.0}}
    events = {symbol: [_events(session=date(2026, 8, 24), cash=0.5)] for symbol in ("600000.XSHG", "000001.XSHE")}
    out = restate_from_events(raw, events, closes, SESSION)
    # (10.0 - 0.5) / 10.0 = 0.95
    assert float(out.loc[0, "close"]) / float(raw.loc[0, "close"]) == pytest.approx(0.95, rel=1e-9)


def test_an_instrument_without_events_is_trivially_comparable():
    # A name with no corporate action in history has an event product of exactly
    # 1.0, so its restatement is unchanged and still comparable against a flat
    # vendor factor. Refusing it would let one never-restructured symbol block a
    # whole session (cr.md 8.36).
    raw = _bars(["600000.XSHG"])
    out = restate_from_events(raw, {}, {"600000.XSHG": {}}, SESSION)
    pd.testing.assert_series_equal(out["close"], raw["close"])


def test_an_event_exactly_on_the_session_is_not_applied_to_it():
    # The boundary that separates "already quoted post-event" from "still
    # pre-event": an event whose ex-date IS this session leaves the session alone
    # (product 1.0), while the same event one session later scales it. A `<=` that
    # became `<` would apply the event to its own ex-date session and silently
    # rescale it, so this locks the direction of the comparison with a non-trivial
    # ratio (the existing jump test uses a ratio of exactly 1, which hides it).
    raw = _bars(["600000.XSHG"])
    closes = {"600000.XSHG": {date(2026, 8, 20): 10.0}}
    on_session = restate_from_events(
        raw, {"600000.XSHG": [_events(session=SESSION, cash=0.5)]}, closes, SESSION
    )
    pd.testing.assert_series_equal(on_session["close"], raw["close"])
    after_session = restate_from_events(
        raw, {"600000.XSHG": [_events(session=date(2026, 8, 24), cash=0.5)]}, closes, SESSION
    )
    assert float(after_session.loc[0, "close"]) < float(raw.loc[0, "close"])


def test_production_requires_some_independent_restatement(tmp_path):
    store = CanonicalV2Store(tmp_path)
    with pytest.raises(ContractError, match="requires an independent restatement"):
        publish_adjusted_partition(
            store, SESSION, _bars(),
            independent_restated=None, events=None, closes=None,
            source_versions={"tushare": "test"},
        )
    assert not (tmp_path / "canonical").exists()


def _restated_pair(session, instruments):
    """The produced restatement of a one-session frame, for jump comparison."""
    return restate(_bars(instruments))


def test_event_based_cross_check_compares_the_jump_not_the_level(tmp_path):
    # Levels differ by the anchor (the vendor's factor is anchored at listing, the
    # event product at the earliest reachable event -- measured 9x..712x apart on
    # a real session), so a level comparison would reject everything. The jump is
    # what must agree, and here it agrees exactly because the frame is unchanged.
    instruments = ["600000.XSHG", "000001.XSHE"]
    # a ratio of exactly 1 (no cash, no share change) so the event product matches
    # the vendor's jump, which is 1 because the previous frame restates the same
    # raw rows
    closes = {"600000.XSHG": {date(2026, 8, 20): 10.0},
              "000001.XSHE": {date(2026, 8, 20): 20.0}}
    events = {symbol: [_events(session=date(2026, 8, 21), cash=0.0)] for symbol in instruments}
    frame = _bars(instruments, adj_factor=1.0)
    store = CanonicalV2Store(tmp_path)
    payload = publish_adjusted_partition(
        store, SESSION, frame,
        events=events, closes=closes,
        previous_restated=restate(frame),
        previous_raw=frame.copy(),
        lineage={"upstream": ["bars_daily/v2"]},
        source_versions={"tushare": "tushare", "tdx": "mootdx xdxr"},
    )
    report = QualityReportStore().read(
        tmp_path, payload["generation_id"], binding_type="canonical_v2"
    )
    assert any(check["name"] == "finite_values__restated_jump_matches_corporate_actions"
               for check in report["checks"])


def test_event_based_cross_check_needs_the_previous_partition(tmp_path):
    instruments = ["600000.XSHG", "000001.XSHE"]
    store = CanonicalV2Store(tmp_path)
    with pytest.raises(ContractError, match="requires the previous session"):
        publish_adjusted_partition(
            store, SESSION, _bars(instruments),
            events={symbol: [_events(session=date(2026, 8, 21), cash=0.0)] for symbol in instruments},
            closes={"600000.XSHG": {date(2026, 8, 20): 10.0},
                    "000001.XSHE": {date(2026, 8, 20): 20.0}},
            source_versions={"tushare": "test"},
        )


def _disagreeing_previous(instruments):
    """A previous partition whose restated prices imply a different jump."""
    frame = _bars(instruments, adj_factor=1.0)
    previous = restate(frame)
    previous["close"] = previous["close"] * 1.05
    previous["open"] = previous["open"] * 1.05
    previous["high"] = previous["high"] * 1.05
    previous["low"] = previous["low"] * 1.05
    return previous


def test_a_disagreeing_event_recomputation_blocks_publication(tmp_path):
    # Same frame, but the previous partition is moved: the jump the vendor implies
    # no longer matches the jump the events imply, so nothing may be published.
    instruments = ["600000.XSHG", "000001.XSHE"]
    previous = _disagreeing_previous(instruments)
    store = CanonicalV2Store(tmp_path)
    with pytest.raises(ContractError, match="jump disagrees"):
        publish_adjusted_partition(
            store, SESSION, _bars(instruments, adj_factor=1.0),
            events={symbol: [_events(session=date(2026, 8, 21), cash=0.0)] for symbol in instruments},
            closes={"600000.XSHG": {date(2026, 8, 20): 10.0},
                    "000001.XSHE": {date(2026, 8, 20): 20.0}},
            previous_restated=previous,
            previous_raw=_bars(instruments, adj_factor=1.0),
            source_versions={"tushare": "test"},
        )
    partition = tmp_path / "canonical" / "bars_adjusted" / "adjusted-v2" / f"date={SESSION.isoformat()}"
    assert not partition.exists()


def test_jump_cross_check_compares_partitions_from_different_sessions(tmp_path):
    # The real case: this session's partition and the previous session's partition
    # carry *different* session_date values. Keying the comparison on
    # (instrument, session_date) would never match and would reject every real
    # publication, so this locks the instrument-only alignment.
    instruments = ["600000.XSHG", "000001.XSHE"]
    previous_session = date(2026, 8, 20)
    current = _bars(instruments, adj_factor=1.0)
    previous_raw = _bars(instruments, adj_factor=1.0)
    previous_raw["session_date"] = pd.to_datetime([previous_session] * len(instruments))
    closes = {"600000.XSHG": {previous_session: 10.0},
              "000001.XSHE": {previous_session: 20.0}}
    events = {symbol: [_events(session=date(2026, 8, 21), cash=0.0)] for symbol in instruments}
    store = CanonicalV2Store(tmp_path)
    payload = publish_adjusted_partition(
        store, SESSION, current,
        events=events, closes=closes,
        previous_restated=restate(previous_raw),
        previous_raw=previous_raw,
        source_versions={"tushare": "test"},
    )
    assert payload["rows"] == len(instruments)


def test_jump_cross_check_refuses_a_previous_frame_spanning_two_sessions(tmp_path):
    # A previous "partition" that carries two session dates cannot be identified,
    # so the jump has no well-defined denominator and must be refused.
    instruments = ["600000.XSHG", "000001.XSHE"]
    previous_raw = _bars(instruments, adj_factor=1.0)
    previous_raw["session_date"] = pd.to_datetime([date(2026, 8, 19), date(2026, 8, 20)])
    store = CanonicalV2Store(tmp_path)
    with pytest.raises(ContractError, match="spans multiple sessions"):
        publish_adjusted_partition(
            store, SESSION, _bars(instruments, adj_factor=1.0),
            events={symbol: [_events(session=date(2026, 8, 21), cash=0.0)] for symbol in instruments},
            closes={"600000.XSHG": {date(2026, 8, 20): 10.0},
                    "000001.XSHE": {date(2026, 8, 20): 20.0}},
            previous_restated=restate(previous_raw),
            previous_raw=previous_raw,
            source_versions={"tushare": "test"},
        )


def test_the_report_names_only_the_cross_check_that_ran(tmp_path):
    # cr.md 8.34: the report used to emit a fixed check name, so the
    # independent_restated path claimed finite_values__restated_jump_matches_
    # corporate_actions -- a check that never ran. A quality report that asserts
    # an unperformed check is the failure this gate exists to prevent, so each
    # path must name its own check and not the other's.
    import datetime as dt

    from uq.contracts.artifacts import QualityReportStore

    # path A: independent_restated
    root_a = tmp_path / "a"
    raw = _bars()
    payload_a = publish_adjusted_partition(
        CanonicalV2Store(root_a), SESSION, raw,
        independent_restated=_independent(raw), source_versions={"t": "t"},
    )
    names_a = {c["name"] for c in QualityReportStore().read(
        root_a, payload_a["generation_id"], binding_type="canonical_v2")["checks"]}
    assert "finite_values__restated_price_matches_independent_source" in names_a
    assert "finite_values__restated_jump_matches_corporate_actions" not in names_a

    # path B: TDX corporate actions
    instruments = ["600000.XSHG", "000001.XSHE"]
    frame = _bars(instruments, adj_factor=1.0)
    closes = {"600000.XSHG": {date(2026, 8, 20): 10.0},
              "000001.XSHE": {date(2026, 8, 20): 20.0}}
    events = {s: [_events(session=date(2026, 8, 21), cash=0.0)] for s in instruments}
    root_b = tmp_path / "b"
    payload_b = publish_adjusted_partition(
        CanonicalV2Store(root_b), SESSION, frame,
        events=events, closes=closes,
        previous_restated=restate(frame), previous_raw=frame.copy(),
        source_versions={"t": "t"},
    )
    names_b = {c["name"] for c in QualityReportStore().read(
        root_b, payload_b["generation_id"], binding_type="canonical_v2")["checks"]}
    assert "finite_values__restated_jump_matches_corporate_actions" in names_b
    assert "finite_values__restated_price_matches_independent_source" not in names_b


def _one_name_frame(close, session):
    return pd.DataFrame({
        "instrument": ["600000.XSHG"], "session_date": [pd.Timestamp(session)],
        "open": [close], "high": [close], "low": [close], "close": [close],
    })


def test_the_tick_floor_absorbs_exchange_rounding_without_masking_a_drift():
    # The exchange rounds the ex-right reference price to the tick, so on a
    # low-priced name the two jump ratios differ by ~tick/pre_close -- already
    # ~0.09% on a 5.56 name (89% of the 1e-3 budget) and over the budget on a
    # 3-yuan name. A pure relative tolerance refuses the whole session over that
    # legitimate residual; the tick floor absorbs it, while a real 1% drift on the
    # same name is still refused.
    from uq.pipeline.adjusted_price import verify_jump_cross_check

    previous, current = date(2026, 8, 20), date(2026, 8, 21)
    pre_close = 3.00
    prev_restated = _one_name_frame(pre_close, previous)
    prev_expected = _one_name_frame(pre_close, previous)
    # vendor built from the rounded reference price (2.97); events from the exact
    # formula (2.9655): the rounding residual alone exceeds 1e-3 relative
    rounded = _one_name_frame(pre_close * 2.97 / pre_close, current)
    exact = _one_name_frame(pre_close * 2.9655 / pre_close, current)
    floor = {"600000.XSHG": pre_close}
    with pytest.raises(ContractError, match="jump disagrees"):
        verify_jump_cross_check(rounded, exact, prev_restated, prev_expected, tolerance=1e-3)
    verify_jump_cross_check(
        rounded, exact, prev_restated, prev_expected, tolerance=1e-3, previous_close=floor
    )
    drifted = _one_name_frame(pre_close * 0.99, current)
    with pytest.raises(ContractError, match="jump disagrees"):
        verify_jump_cross_check(
            drifted, _one_name_frame(pre_close, current),
            prev_restated, prev_expected, tolerance=1e-3, previous_close=floor,
        )


def test_every_public_annotation_resolves():
    # `from __future__ import annotations` makes every annotation a lazy string, so
    # a name used in a signature but never imported is invisible until something
    # evaluates the hints -- `Mapping` was used in six signatures while only `Any`
    # and `Sequence` were imported (cr.md CR38-2). Nothing in the library evaluates
    # them today, so this lock is the only thing standing between that and a
    # NameError in a future caller that does.
    import inspect

    import uq.pipeline.adjusted_price as module

    names = [
        name for name, obj in vars(module).items()
        if inspect.isfunction(obj) and obj.__module__ == module.__name__
    ]
    assert names
    for name in names:
        typing.get_type_hints(getattr(module, name))


def test_a_key_column_missing_from_only_one_side_is_a_typed_rejection():
    # The check used to subtract both frames' columns, so it only caught a key
    # absent from *both*; a key present in raw but missing in restated reached
    # `set_index` and surfaced as a bare KeyError instead of a typed rejection
    # naming the side at fault (cr.md OBS-A).
    raw = _bars()
    restated = restate(raw).drop(columns=["instrument"])
    with pytest.raises(ContractError, match="on the restated frame"):
        verify_restatement(raw, restated)
    with pytest.raises(ContractError, match="on the raw frame"):
        verify_restatement(raw.drop(columns=["session_date"]), restate(raw))
