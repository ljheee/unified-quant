"""Regression tests for scripts/publish_adjusted_window.py.

The window driver publishes the reviewed adjusted-price line one session at a
time, each behind a quality report whose cross-check ran against TDX corporate
actions. These lock the properties that make the batch trustworthy: the jump
baseline is used rather than published, a rerun is idempotent, and the cross-check
compares the instruments two adjacent partitions share (a universe changes
between sessions, so requiring identical row sets would refuse every real window).
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import CanonicalV2Store, file_sha256_bytes
from uq.contracts.schema import load_schema
from uq.pipeline.adjustment_events import AdjustmentEvent

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "publish_adjusted_window", _ROOT / "scripts" / "publish_adjusted_window.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

DATES = pd.bdate_range("2026-01-05", periods=6)
INSTRUMENTS = ("600000.XSHG", "000001.XSHE")


def _publish_bars(root: Path, instruments=INSTRUMENTS):
    schema = load_schema("config/schemas/bars_daily.v2.yaml")
    rows = []
    for index, instrument in enumerate(instruments):
        for position, day in enumerate(DATES):
            close = 10.0 + position * 0.1 + index
            rows.append({
                "instrument": instrument, "session_date": day,
                "open": close - 0.1, "high": close + 0.2, "low": close - 0.2,
                "close": close, "volume": 10000.0, "amount": 100000.0,
                "status": "trading", "limit_up": close * 1.1, "limit_down": close * 0.9,
                "adj_factor": 1.0,
            })
    frame = pd.DataFrame(rows)
    store = CanonicalV2Store(root)
    for day in DATES:
        part = frame[frame["session_date"] == day]
        generation = store.prepare_generation(schema, day.date(), part, {}, {})
        report = {
            "report_version": 1, "binding_type": "canonical_v2",
            "bound_generation_id": generation, "policy": "reject_all", "status": "passed",
            "checks": [{"name": "coverage", "threshold": 0, "observed": 0,
                        "level": "error", "result": "passed"}],
            "errors": [], "warnings": [],
        }
        directory = QualityReportStore().save(root, report)
        store.publish(schema, day.date(), part, {}, {},
                      quality_checksum=file_sha256_bytes((directory / "report.json").read_bytes()))


def _fake_adapter(events):
    class _Adapter:
        connected_server = ("test", 7709)
        def fetch_xdxr_events(self, symbols):
            return {symbol: events.get(symbol, pd.DataFrame()) for symbol in symbols}
    return _Adapter()


def _no_events():
    # No ex-right event in the window: the event product is 1.0 and the vendor
    # factor is flat, so the jump agrees trivially.
    return {}


def _run(tmp_path, monkeypatch, *, start, end, events=None, setup=True):
    # The canonical bars partitions are immutable, so a rerun must reuse the ones
    # already on disk rather than republish them.
    if setup:
        _publish_bars(tmp_path / "bars")
    monkeypatch.setattr(module, "MootdxSourceAdapter", lambda: _fake_adapter(events or _no_events()))
    out = tmp_path / "report.json"
    code = module.main([
        "--bars-root", str(tmp_path / "bars"), "--adjusted-root", str(tmp_path / "adj"),
        "--start", start, "--end", end, "--out", str(out),
    ])
    return code, json.loads(out.read_text())


def test_the_baseline_session_is_used_but_not_published(tmp_path, monkeypatch):
    # The first session in the window has no predecessor, so the driver reaches one
    # session back for the jump baseline; that session must be reported as a
    # baseline, not published as if it were in the window.
    start, end = DATES[1].date().isoformat(), DATES[-1].date().isoformat()
    code, report = _run(tmp_path, monkeypatch, start=start, end=end)
    assert code == 0
    baseline = [item for item in report["skipped_sessions"] if item["reason"] == "jump_baseline"]
    assert [item["session"] for item in baseline] == [DATES[0].date().isoformat()]
    published = {item["session"] for item in report["records"]}
    assert DATES[0].date().isoformat() not in published
    assert published == {day.date().isoformat() for day in DATES[1:]}


def test_published_partitions_carry_a_verified_cross_check(tmp_path, monkeypatch):
    code, report = _run(
        tmp_path, monkeypatch,
        start=DATES[1].date().isoformat(), end=DATES[-1].date().isoformat(),
    )
    assert code == 0
    store = CanonicalV2Store(tmp_path / "adj")
    for record in report["records"]:
        assert record["cross_check_basis"] == "tdx_corporate_actions"
        manifest_path = (tmp_path / "adj" / "canonical" / "bars_adjusted" / "adjusted-v2"
                         / f"date={record['session']}" / "manifest.json")
        assert manifest_path.is_file()
        report_doc = QualityReportStore().read(
            tmp_path / "adj", record["generation_id"], binding_type="canonical_v2"
        )
        names = {check["name"] for check in report_doc["checks"]}
        assert "finite_values__restated_jump_matches_corporate_actions" in names


def test_a_rerun_publishes_nothing(tmp_path, monkeypatch):
    start, end = DATES[1].date().isoformat(), DATES[-1].date().isoformat()
    _run(tmp_path, monkeypatch, start=start, end=end)
    code, report = _run(tmp_path, monkeypatch, start=start, end=end, setup=False)
    assert code == 0
    assert report["published"] == 0
    assert all(item["reason"] in {"already_published", "jump_baseline"}
               for item in report["skipped_sessions"])


def test_the_cross_check_compares_the_instruments_both_sessions_share(tmp_path, monkeypatch):
    # The universe changes between sessions: a name that enters mid-window appears in
    # the later partition only. Requiring identical row sets would refuse the whole
    # session, so only the shared instruments are compared.
    import shutil

    bars_root = tmp_path / "bars"
    _publish_bars(bars_root, instruments=("600000.XSHG",))
    schema = load_schema("config/schemas/bars_daily.v2.yaml")
    fresh = tmp_path / "bars2"
    shutil.copytree(bars_root / "canonical", fresh / "canonical")
    last = DATES[-1]
    target = fresh / "canonical" / "bars_daily" / "v2" / f"date={last.date().isoformat()}"
    existing = pd.read_parquet(target / "data.parquet")
    extra = pd.DataFrame([{
        "instrument": "000001.XSHE", "session_date": last,
        "open": 9.9, "high": 10.2, "low": 9.8, "close": 10.0,
        "volume": 10000.0, "amount": 100000.0, "status": "trading",
        "limit_up": 11.0, "limit_down": 9.0, "adj_factor": 1.0,
    }])
    combined = pd.concat([existing, extra], ignore_index=True)
    shutil.rmtree(target)
    store = CanonicalV2Store(fresh)
    generation = store.prepare_generation(schema, last.date(), combined, {}, {})
    report = {
        "report_version": 1, "binding_type": "canonical_v2",
        "bound_generation_id": generation, "policy": "reject_all", "status": "passed",
        "checks": [{"name": "coverage", "threshold": 0, "observed": 0,
                    "level": "error", "result": "passed"}],
        "errors": [], "warnings": [],
    }
    directory = QualityReportStore().save(fresh, report)
    store.publish(schema, last.date(), combined, {}, {},
                  quality_checksum=file_sha256_bytes((directory / "report.json").read_bytes()))

    monkeypatch.setattr(module, "MootdxSourceAdapter", lambda: _fake_adapter({}))
    out = tmp_path / "report.json"
    code = module.main([
        "--bars-root", str(fresh), "--adjusted-root", str(tmp_path / "adj"),
        "--start", DATES[1].date().isoformat(), "--end", last.date().isoformat(),
        "--out", str(out),
    ])
    assert code == 0
    published = {item["session"] for item in json.loads(out.read_text())["records"]}
    assert last.date().isoformat() in published


def test_a_window_with_no_earlier_partition_refuses_its_first_session(tmp_path, monkeypatch):
    # When the requested window starts at the store's earliest partition there is
    # no jump baseline to inject, so the first session has no predecessor to compare
    # against and is refused (a gate failure, exit 1) rather than published without
    # a cross-check. The remaining sessions still publish.
    code, report = _run(
        tmp_path, monkeypatch,
        start=DATES[0].date().isoformat(), end=DATES[-1].date().isoformat(),
    )
    assert code == 1
    assert report["refused"] == 1
    assert report["refusals"][0]["session"] == DATES[0].date().isoformat()
    assert report["refusals"][0]["reason"] == "no earlier partition exists to use as the jump baseline"
    published = {item["session"] for item in report["records"]}
    assert published == {day.date().isoformat() for day in DATES[1:]}
    assert report["skipped_sessions"] == []


def test_an_interrupted_window_still_writes_its_audit_record(tmp_path, monkeypatch):
    # A multi-hour window must not lose its audit trail to an unexpected raise: the
    # partitions already published are durable and the run is resumable, so the
    # report naming which sessions landed is written before the exception leaves.
    start, end = DATES[1].date().isoformat(), DATES[-1].date().isoformat()
    _publish_bars(tmp_path / "bars")
    boom = RuntimeError("upstream died mid-window")

    def _exploding(store, session, *args, **kwargs):
        if session.isoformat() == DATES[3].date().isoformat():
            raise boom
        return _real_publish(store, session, *args, **kwargs)

    _real_publish = module.publish_adjusted_partition
    monkeypatch.setattr(module, "publish_adjusted_partition", _exploding)
    monkeypatch.setattr(module, "MootdxSourceAdapter", lambda: _fake_adapter({}))
    out = tmp_path / "report.json"
    with pytest.raises(RuntimeError, match="upstream died mid-window"):
        module.main([
            "--bars-root", str(tmp_path / "bars"), "--adjusted-root", str(tmp_path / "adj"),
            "--start", start, "--end", end, "--out", str(out),
        ])
    report = json.loads(out.read_text())
    assert report["status"] == "interrupted"
    # the sessions that landed before the raise are still recorded
    assert [item["session"] for item in report["records"]] == [
        day.date().isoformat() for day in DATES[1:3]
    ]


def test_the_report_status_distinguishes_published_from_already_published(tmp_path, monkeypatch):
    start, end = DATES[1].date().isoformat(), DATES[-1].date().isoformat()
    _run(tmp_path, monkeypatch, start=start, end=end)
    code, report = _run(tmp_path, monkeypatch, start=start, end=end, setup=False)
    assert code == 0
    assert report["status"] == "already_published"


def test_the_jump_denominator_comes_from_the_published_partition_when_it_exists(tmp_path, monkeypatch):
    # The store is the reviewed line of record, so a resumed run must compare
    # against the partition that is actually there rather than recomputing it from
    # bars -- the two can disagree once the published line has moved on
    # (cr.md OBS-C). The injected baseline is never published, so it stays on the
    # recomputed path and that difference is recorded.
    start, end = DATES[1].date().isoformat(), DATES[-1].date().isoformat()
    code, report = _run(tmp_path, monkeypatch, start=start, end=end)
    assert code == 0
    bases = {item["session"]: item["basis"] for item in report["previous_basis"]}
    assert bases[DATES[1].date().isoformat()] == "recomputed_from_bars"
    assert all(
        bases[day.date().isoformat()] == "published_partition"
        for day in DATES[2:]
    )
