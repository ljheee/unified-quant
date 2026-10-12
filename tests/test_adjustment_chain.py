"""Dividend disclosures -> adjustment snapshot production chain (pendding item 6)."""

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
    records = _records(_record(cash_div=2.52, cash_div_tax=2.52, stk_co_rate=0.8, ex_date="2026-08-20"))
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


def test_script_reports_and_exit_code_follow_refusals(tmp_path, monkeypatch):
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
            return _bars()
    monkeypatch.setattr(script, "ManifestFirstReader", lambda *_: _Reader())
    assert script.main(
        [
            "--bars-root", str(bars_root),
            "--dividend-root", str(records),
            "--adjustment-root", str(tmp_path / "adj"),
            "--start", DATES[0].isoformat(),
            "--end", DATES[-1].isoformat(),
            "--out", str(tmp_path / "report.json"),
        ]
    ) == 0
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["published"] == 1
    assert report["refused"] == 0
    assert report["records"][0]["events"][0]["source_record_id"]
    assert report["provider_reconciliation"]["all_within_public_resolution"] is True
