"""Dividend disclosures -> adjustment snapshot production chain (pendding item 6)."""

import dataclasses
import datetime as dt
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.adjustment_snapshot import AdjustmentSnapshotReader
from uq.contracts.dividend_events import DividendEventStore
from uq.errors import ContractError
from uq.market.adjustment_chain import (
    EVENT_COLUMNS,
    build_adjustment_chain,
    build_tdx_adjustment_chain,
    implemented_events,
    publish_adjustment_snapshot,
)

INSTRUMENT = "600000.XSHG"
DATES = [dt.date(2026, 8, 19), dt.date(2026, 8, 20), dt.date(2026, 8, 21)]
_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "build_dividend_adjustments", _ROOT / "scripts" / "build_dividend_adjustments.py"
)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)


def _bars(closes=(10.0, 10.0, 9.5), factors=(0.95, 1.0, 1.0), instrument=INSTRUMENT):
    return pd.DataFrame(
        {
            "instrument": [instrument] * len(DATES),
            "session_date": DATES,
            "close": list(closes),
            "adj_factor": list(factors),
        }
    )


def _record(**overrides):
    record = {
        "instrument": INSTRUMENT,
        "div_proc": "实施",
        "ann_date": "2026-08-14",
        "imp_ann_date": "2026-08-14",
        "ex_date": "2026-08-20",
        "cash_div": 0.5,
        "cash_div_tax": 0.5,
        "stk_div": None,
        "stk_bo_rate": None,
        "stk_co_rate": None,
        "record_id": "a" * 64,
    }
    record.update(overrides)
    return record


def _records(*rows):
    return pd.DataFrame(list(rows))


def test_cash_event_maps_per_ten_shares_and_derives_the_official_ratio():
    records = _records(_record())
    events, dropped = implemented_events(
        records, instrument=INSTRUMENT, window_start=DATES[0], window_end=DATES[-1]
    )
    assert dropped == ()
    assert list(events.columns) == [*EVENT_COLUMNS, "source_event_id"]
    row = events.iloc[0]
    assert row["fenhong"] == pytest.approx(5.0)  # 0.5/share -> per ten shares
    assert row["songzhuangu"] == 0.0
    assert row["peigu"] == 0.0 and row["peigujia"] == 0.0
    assert row["source_event_id"] == "a" * 64

    result = build_adjustment_chain(
        records=records, bars=_bars(), instrument=INSTRUMENT, window_start=DATES[0], window_end=DATES[-1]
    )
    factors = result.derivation.frame["adj_factor"].tolist()
    assert factors == pytest.approx([0.95, 1.0, 1.0], abs=1e-12)
    assert result.provider_check["status"] == "passed"
    assert result.provider_check["compared_sessions"] == 3


def test_bonus_and_cash_use_per_share_rates_times_ten():
    # CATL 2022: 2.52 cash + 0.8 transfer per share against a 385.9 pre-close
    # reproduces the official 2023-04-26 ratio 0.55214.
    rows = [dt.date(2026, 8, 19), dt.date(2026, 8, 20)]
    bars = pd.DataFrame(
        {
            "instrument": [INSTRUMENT] * 2,
            "session_date": rows,
            "close": [385.9, 212.99],
            "adj_factor": [0.552141, 1.0],
        }
    )
    records = _records(
        _record(cash_div=2.52, cash_div_tax=2.52, stk_div=0.8, stk_co_rate=0.8, ex_date="2026-08-20")
    )
    result = build_adjustment_chain(
        records=records, bars=bars, instrument=INSTRUMENT, window_start=rows[0], window_end=rows[1], tolerance=5e-4
    )
    assert result.events.iloc[0]["fenhong"] == pytest.approx(25.2)
    assert result.events.iloc[0]["songzhuangu"] == pytest.approx(8.0)
    assert result.provider_check["status"] == "passed"
    assert result.provider_check["max_abs_diff"] < 5e-4


def test_unimplemented_stages_are_ignored():
    records = _records(
        _record(div_proc="预案", ex_date=None),
        _record(div_proc="股东大会通过", ex_date=None, record_id="b" * 64),
        _record(div_proc="预披露", ex_date=None, record_id="c" * 64),
    )
    events, dropped = implemented_events(
        records, instrument=INSTRUMENT, window_start=DATES[0], window_end=DATES[-1]
    )
    assert events.empty
    assert dropped == ()


def test_implementation_without_ex_date_is_dropped_with_a_reason():
    records = _records(_record(ex_date=None))
    events, dropped = implemented_events(
        records, instrument=INSTRUMENT, window_start=DATES[0], window_end=DATES[-1]
    )
    assert events.empty
    assert dropped[0]["reason"] == "implementation_without_ex_date"


def test_implementation_without_pre_tax_cash_fails_closed():
    with pytest.raises(ContractError, match="no pre-tax cash field"):
        implemented_events(
            _records(_record(cash_div_tax=None)),
            instrument=INSTRUMENT,
            window_start=DATES[0],
            window_end=DATES[-1],
        )


def test_ambiguous_revisions_on_one_ex_date_fail_closed():
    with pytest.raises(ContractError, match="ambiguous implemented dividends"):
        implemented_events(
            _records(_record(cash_div_tax=0.5), _record(cash_div_tax=0.9, record_id="d" * 64)),
            instrument=INSTRUMENT,
            window_start=DATES[0],
            window_end=DATES[-1],
        )


def test_identical_revisions_collapse_to_one_event():
    events, _ = implemented_events(
        _records(_record(), _record(cash_div=0.5, cash_div_tax=0.5, record_id="e" * 64)),
        instrument=INSTRUMENT,
        window_start=DATES[0],
        window_end=DATES[-1],
    )
    assert len(events) == 1
    assert events.iloc[0]["source_event_id"] == "a" * 64


def test_events_outside_the_window_are_not_applied():
    events, _ = implemented_events(
        _records(_record(ex_date="2026-09-20")),
        instrument=INSTRUMENT,
        window_start=DATES[0],
        window_end=DATES[-1],
    )
    assert events.empty


def test_ex_date_on_the_first_session_cannot_resolve_pre_close():
    records = _records(_record(ex_date="2026-08-19"))
    with pytest.raises(ContractError, match="cannot resolve the pre-ex close"):
        build_adjustment_chain(
            records=records, bars=_bars(), instrument=INSTRUMENT, window_start=DATES[0], window_end=DATES[-1]
        )


def test_ex_date_on_a_non_session_day_is_refused():
    # The deriver applies an event only when its ex-date is itself a session:
    # a Saturday ex-date would be recorded in events.csv yet never move a
    # factor, and the provider cross-check can only catch divergences larger
    # than the tolerance. The chain must refuse instead of publishing a
    # snapshot that contradicts its own effective-date table.
    sessions = [dt.date(2026, 8, 17) + dt.timedelta(days=offset) for offset in range(8)]
    sessions = [day for day in sessions if day.weekday() < 5]  # 08-17..08-21 + 08-24
    bars = pd.DataFrame(
        {
            "instrument": [INSTRUMENT] * len(sessions),
            "session_date": sessions,
            "close": [10.0] * len(sessions),
            "adj_factor": [1.0] * len(sessions),
        }
    )
    records = _records(_record(ex_date="2026-08-22"))  # a Saturday
    with pytest.raises(ContractError, match="fall outside the bar sessions"):
        build_adjustment_chain(
            records=records, bars=bars, instrument=INSTRUMENT, window_start=sessions[0], window_end=sessions[-1]
        )


def test_stk_div_must_match_the_bonus_breakdown():
    # stk_div without a matching bo/co breakdown must not silently map to a
    # zero bonus; consistent rows derive exactly the breakdown amounts.
    events, _ = implemented_events(
        _records(_record(stk_div=0.8, stk_co_rate=0.8)),
        instrument=INSTRUMENT,
        window_start=DATES[0],
        window_end=DATES[-1],
    )
    assert events.iloc[0]["songzhuangu"] == pytest.approx(8.0)
    with pytest.raises(ContractError, match="refusing to guess the breakdown"):
        implemented_events(
            _records(_record(stk_div=1.0, stk_co_rate=0.8)),
            instrument=INSTRUMENT,
            window_start=DATES[0],
            window_end=DATES[-1],
        )


def test_implementation_announced_after_its_ex_date_is_refused():
    with pytest.raises(ContractError, match="announced after its own ex-date"):
        implemented_events(
            _records(_record(imp_ann_date="2026-08-21", ex_date="2026-08-20")),
            instrument=INSTRUMENT,
            window_start=DATES[0],
            window_end=DATES[-1],
        )


def test_a_negative_component_is_refused_before_summing():
    # stk_bo_rate -0.5 + stk_co_rate 1.0 would sum to a positive bonus and
    # slip past a post-sum check.
    with pytest.raises(ContractError, match="negative stk_bo_rate"):
        implemented_events(
            _records(_record(stk_bo_rate=-0.5, stk_co_rate=1.0)),
            instrument=INSTRUMENT,
            window_start=DATES[0],
            window_end=DATES[-1],
        )


def test_publish_refuses_a_drifted_effective_date_checksum(tmp_path):
    result = build_adjustment_chain(
        records=_records(_record()), bars=_bars(), instrument=INSTRUMENT, window_start=DATES[0], window_end=DATES[-1]
    )
    drifted = dataclasses.replace(
        result,
        derivation=dataclasses.replace(result.derivation, effective_date_table_checksum="0" * 64),
    )
    with pytest.raises(ContractError, match="declared effective-date checksum"):
        publish_adjustment_snapshot(
            tmp_path,
            drifted,
            visibility_time=dt.datetime(2026, 8, 21, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
        )


def test_provider_divergence_refuses_to_derive():
    bars = _bars(closes=(10.0, 10.0, 9.5), factors=(0.50, 1.0, 1.0))
    with pytest.raises(ContractError, match="diverges from the official adj_factor"):
        build_adjustment_chain(
            records=_records(_record()),
            bars=bars,
            instrument=INSTRUMENT,
            window_start=DATES[0],
            window_end=DATES[-1],
            tolerance=5e-4,
        )


def test_provider_check_is_skipped_without_official_factors():
    bars = _bars(factors=(None, None, None))
    result = build_adjustment_chain(
        records=_records(_record()), bars=bars, instrument=INSTRUMENT, window_start=DATES[0], window_end=DATES[-1]
    )
    assert result.provider_check["status"] == "skipped"


def test_bars_window_must_match_the_declared_window():
    with pytest.raises(ContractError, match="does not match the declared window"):
        build_adjustment_chain(
            records=_records(_record()),
            bars=_bars(),
            instrument=INSTRUMENT,
            window_start=dt.date(2026, 8, 18),
            window_end=DATES[-1],
        )


def test_snapshot_publishes_and_is_selectable_across_a_weekend(tmp_path):
    # Sessions 08-19..08-21 only; the window is the instrument's own session
    # range, so the reader must not demand calendar-day coverage.
    result = build_adjustment_chain(
        records=_records(_record()), bars=_bars(), instrument=INSTRUMENT, window_start=DATES[0], window_end=DATES[-1]
    )
    visibility = dt.datetime(2026, 8, 21, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    generation_id = publish_adjustment_snapshot(tmp_path, result, visibility_time=visibility)
    reader = AdjustmentSnapshotReader(tmp_path)
    manifest = reader.read_manifest(INSTRUMENT, generation_id)
    assert manifest["formula_version"] == "adj_factor.exchange_v2"
    events = reader.read_events(INSTRUMENT, generation_id)
    assert list(events.columns) == EVENT_COLUMNS
    effective = reader.read_effective_dates(INSTRUMENT, generation_id)
    assert len(effective) == len(DATES)
    # the event applies from its ex-date onward, and its lineage is the
    # announcement record id, not an opaque fingerprint
    assert effective["source_event_id"].fillna("").tolist() == ["", "a" * 64, "a" * 64]
    assert reader.select_visible_generation(
        instrument=INSTRUMENT,
        window_start=DATES[0],
        window_end=DATES[-1],
        as_of_time=dt.datetime(2026, 8, 21, 16, 0, tzinfo=dt.timezone.utc),
        candidates=[generation_id],
    ) == generation_id


def test_snapshot_is_not_visible_before_its_visibility_time(tmp_path):
    result = build_adjustment_chain(
        records=_records(_record()), bars=_bars(), instrument=INSTRUMENT, window_start=DATES[0], window_end=DATES[-1]
    )
    visibility = dt.datetime(2026, 8, 21, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    generation_id = publish_adjustment_snapshot(tmp_path, result, visibility_time=visibility)
    with pytest.raises(ContractError, match="ambiguous or absent visible adjustment snapshot"):
        AdjustmentSnapshotReader(tmp_path).select_visible_generation(
            instrument=INSTRUMENT,
            window_start=DATES[0],
            window_end=DATES[-1],
            as_of_time=dt.datetime(2026, 8, 21, 8, 0, tzinfo=dt.timezone.utc),
            candidates=[generation_id],
        )


def test_script_refuses_a_window_without_bars(tmp_path):
    with pytest.raises(SystemExit, match="missing canonical bars root"):
        script.main(
            [
                "--bars-root", str(tmp_path),
                "--dividend-root", str(tmp_path),
                "--adjustment-root", str(tmp_path),
                "--start", DATES[0].isoformat(),
                "--end", DATES[-1].isoformat(),
                "--out", str(tmp_path / "report.json"),
            ]
        )


def _run_script(tmp_path, monkeypatch, bars):
    bars_root = tmp_path / "bars"
    canonical = bars_root / "canonical" / "bars_daily" / "v2" / f"date={DATES[0].isoformat()}"
    canonical.mkdir(parents=True)
    (canonical / "manifest.json").write_text("{}")
    records = tmp_path / "div"
    dividend = DividendEventStore(records)
    dividend.publish(
        ann_date=dt.date(2026, 8, 14),
        source="tushare:dividend",
        source_fetched_at=dt.datetime(2026, 8, 15, 9, tzinfo=dt.timezone.utc),
        frame=pd.DataFrame(
            [
                {
                    "ts_code": "600000.SH",
                    "end_date": "20260630",
                    "ann_date": "20260814",
                    "div_proc": "实施",
                    "stk_div": 0.0,
                    "stk_bo_rate": None,
                    "stk_co_rate": None,
                    "cash_div": 0.5,
                    "cash_div_tax": 0.5,
                    "record_date": None,
                    "ex_date": "20260820",
                    "pay_date": None,
                    "div_listdate": None,
                    "imp_ann_date": None,
                }
            ]
        ),
    )
    monkeypatch.setattr(script, "_window_sessions", lambda *_: DATES)
    monkeypatch.setattr(script, "load_schema", lambda *a, **k: None)
    class _Reader:
        def read(self, schema, session):
            return bars
    monkeypatch.setattr(script, "ManifestFirstReader", lambda *_: _Reader())
    exit_code = script.main(
        [
            "--bars-root", str(bars_root),
            "--dividend-root", str(records),
            "--adjustment-root", str(tmp_path / "adj"),
            "--start", DATES[0].isoformat(),
            "--end", DATES[-1].isoformat(),
            "--out", str(tmp_path / "report.json"),
        ]
    )
    return exit_code, json.loads((tmp_path / "report.json").read_text())


def test_script_reports_and_exit_code_follow_refusals(tmp_path, monkeypatch):
    exit_code, report = _run_script(tmp_path, monkeypatch, _bars())
    assert exit_code == 0
    assert report["published"] == 1
    assert report["refused"] == 0
    assert report["records"][0]["events"][0]["source_record_id"]
    reconciliation = report["provider_reconciliation"]
    assert reconciliation["verified"] == 1
    assert reconciliation["skipped"] == 0
    assert reconciliation["skipped_instruments"] == []
    assert reconciliation["all_within_public_resolution"] is True


def test_script_report_surfaces_skipped_provider_checks_separately(tmp_path, monkeypatch):
    # A snapshot published without official factors was never reconciled; the
    # summary must surface it as skipped, not fold it into max_abs_diff as 0.0.
    exit_code, report = _run_script(tmp_path, monkeypatch, _bars(factors=(None, None, None)))
    assert exit_code == 0
    assert report["published"] == 1
    reconciliation = report["provider_reconciliation"]
    assert reconciliation["verified"] == 0
    assert reconciliation["skipped"] == 1
    assert reconciliation["skipped_instruments"] == [
        {"instrument": INSTRUMENT, "reason": "fewer than two official adj_factor observations"}
    ]
    assert reconciliation["max_abs_diff"] is None
    assert reconciliation["worst_instrument"] is None
    assert reconciliation["all_within_public_resolution"] is None


# --- TDX xdxr as the event source (lineage adj_factor.derived_v4) ------------


def _xdxr(**overrides):
    """One realised ex-right event in TDX's own (per-ten-share) shape."""
    event = {
        "year": 2026, "month": 8, "day": 20,
        "category": 1,
        "fenhong": 5.0,        # per 10 shares -> 0.5 per share
        "peigujia": 0.0,
        "songzhuangu": 0.0,
        "peigu": 0.0,
        "suogu": None,
    }
    event.update(overrides)
    return event


def _events(*rows):
    return pd.DataFrame(list(rows) or [_xdxr()])


def test_tdx_chain_declares_the_derived_v4_lineage(tmp_path):
    result = build_tdx_adjustment_chain(
        events=_events(), bars=_bars(), instrument=INSTRUMENT,
        window_start=DATES[0], window_end=DATES[-1],
    )
    assert result.derivation.version == "adj_factor.derived_v4"
    assert result.derivation.provenance == "tdx:xdxr"


def test_tdx_chain_reconciles_against_the_provider_factor(tmp_path):
    # 10.0 -> ex-right (10.0-0.5)/1.0 = 9.5, matching the fixture's 0.95 factor
    result = build_tdx_adjustment_chain(
        events=_events(), bars=_bars(), instrument=INSTRUMENT,
        window_start=DATES[0], window_end=DATES[-1],
    )
    assert result.provider_check["status"] == "passed"


def test_tdx_chain_refuses_a_diverging_derivation(tmp_path):
    bars = _bars(factors=(0.5, 1.0, 1.0))
    with pytest.raises(ContractError, match="diverges from the official adj_factor"):
        build_tdx_adjustment_chain(
            events=_events(), bars=bars, instrument=INSTRUMENT,
            window_start=DATES[0], window_end=DATES[-1],
        )


def test_tdx_chain_ignores_events_outside_the_window(tmp_path):
    # TDX returns a name's whole history; an ex-date before the window must not be
    # applied (its pre-ex close is not in the window) nor rejected.
    events = pd.DataFrame([
        _xdxr(year=1999, month=5, day=10),
        _xdxr(),
    ])
    result = build_tdx_adjustment_chain(
        events=events, bars=_bars(), instrument=INSTRUMENT,
        window_start=DATES[0], window_end=DATES[-1],
    )
    assert len(result.events) == 1
    assert result.provider_check["status"] == "passed"


def test_tdx_chain_ignores_non_ex_right_categories(tmp_path):
    events = pd.DataFrame([
        _xdxr(category=5),   # 股本变化: share-count drift, not an action
        _xdxr(),
    ])
    result = build_tdx_adjustment_chain(
        events=events, bars=_bars(), instrument=INSTRUMENT,
        window_start=DATES[0], window_end=DATES[-1],
    )
    assert len(result.events) == 1


def test_tdx_chain_refuses_an_ex_date_that_is_not_a_session(tmp_path):
    # 2026-08-19 is inside the declared window but 2026-08-22 (a Saturday) is not,
    # so the ex-date is in-window yet never applied by the deriver -- exactly the
    # self-contradicting snapshot this refuses. (An out-of-window date is filtered
    # instead; see test_tdx_chain_ignores_events_outside_the_window.)
    window = [dt.date(2026, 8, 19), dt.date(2026, 8, 21)]   # 08-20 is not a session
    bars = pd.DataFrame({
        "instrument": [INSTRUMENT] * len(window),
        "session_date": window,
        "close": [10.0, 9.5],
        "adj_factor": [1.0, 1.0],
    })
    events = pd.DataFrame([_xdxr(day=20)])   # in-window, but never applied
    with pytest.raises(ContractError, match="fall outside the bar sessions"):
        build_tdx_adjustment_chain(
            events=events, bars=bars, instrument=INSTRUMENT,
            window_start=window[0], window_end=window[-1],
        )


def test_tdx_chain_refuses_without_events(tmp_path):
    with pytest.raises(ContractError, match="no TDX xdxr events"):
        build_tdx_adjustment_chain(
            events=pd.DataFrame(), bars=_bars(), instrument=INSTRUMENT,
            window_start=DATES[0], window_end=DATES[-1],
        )


def test_tdx_snapshot_publishes_as_v4_and_reads_back(tmp_path):
    result = build_tdx_adjustment_chain(
        events=_events(), bars=_bars(), instrument=INSTRUMENT,
        window_start=DATES[0], window_end=DATES[-1],
    )
    generation_id = publish_adjustment_snapshot(
        tmp_path, result, visibility_time=dt.datetime(2026, 8, 22, tzinfo=dt.timezone.utc)
    )
    manifest = AdjustmentSnapshotReader(tmp_path).read_manifest(INSTRUMENT, generation_id)
    assert manifest["snapshot_version"] == 4
    assert manifest["formula_version"] == "adj_factor.derived_v4"


def test_a_derived_v4_snapshot_is_consumable(tmp_path):
    # The point of listing two consumable lineages: a snapshot derived from TDX
    # events feeds the same consumption seam as the disclosure-mapped one.
    result = build_tdx_adjustment_chain(
        events=_events(), bars=_bars(), instrument=INSTRUMENT,
        window_start=DATES[0], window_end=DATES[-1],
    )
    generation_id = publish_adjustment_snapshot(
        tmp_path, result, visibility_time=dt.datetime(2026, 8, 22, tzinfo=dt.timezone.utc)
    )
    selected = AdjustmentSnapshotReader(tmp_path).select_visible_generation(
        instrument=INSTRUMENT,
        window_start=DATES[0], window_end=DATES[-1],
        as_of_time=dt.datetime(2026, 8, 23, tzinfo=dt.timezone.utc),
        candidates=[generation_id],
    )
    assert selected == generation_id


def test_the_v3_contract_still_rejects_the_derived_v4_lineage(tmp_path):
    # v3 pins its lineage by const and stays byte-identical, so a snapshot that
    # claims derived_v4 under snapshot_version 3 is a tampered document.
    result = build_tdx_adjustment_chain(
        events=_events(), bars=_bars(), instrument=INSTRUMENT,
        window_start=DATES[0], window_end=DATES[-1],
    )
    generation_id = publish_adjustment_snapshot(
        tmp_path, result, visibility_time=dt.datetime(2026, 8, 22, tzinfo=dt.timezone.utc)
    )
    path = tmp_path / "adjustments" / "snapshots" / INSTRUMENT / generation_id / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["snapshot_version"] = 3
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ContractError, match="adjustment_snapshot.v3.json validation failed"):
        AdjustmentSnapshotReader(tmp_path).read_manifest(INSTRUMENT, generation_id)


def test_the_v4_contract_rejects_the_exchange_lineage(tmp_path):
    # ...and the reverse: v4 is not a widening of v3, it is a different lineage.
    result = build_tdx_adjustment_chain(
        events=_events(), bars=_bars(), instrument=INSTRUMENT,
        window_start=DATES[0], window_end=DATES[-1],
    )
    generation_id = publish_adjustment_snapshot(
        tmp_path, result, visibility_time=dt.datetime(2026, 8, 22, tzinfo=dt.timezone.utc)
    )
    path = tmp_path / "adjustments" / "snapshots" / INSTRUMENT / generation_id / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["formula_version"] = "adj_factor.exchange_v2"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ContractError, match="adjustment_snapshot.v4.json validation failed"):
        AdjustmentSnapshotReader(tmp_path).read_manifest(INSTRUMENT, generation_id)
