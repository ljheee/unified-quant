"""Regression tests for scripts/build_tdx_adjustments.py.

The TDX entry point is the production way to publish the `adj_factor.derived_v1`
line. What matters is that it is bounded by the store rather than by the server,
that a symbol whose events are missing is refused rather than skipped, and that
the run is a gate (exit 1) instead of a log.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.canonical_v2 import CanonicalV2Store
from uq.contracts.schema import load_schema
from uq.market.adjustment_chain import TDX_DERIVATION_VERSION

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "build_tdx_adjustments", _ROOT / "scripts" / "build_tdx_adjustments.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

DATES = pd.bdate_range("2026-01-05", periods=8)


def _publish_bars(root: Path, instruments=("600000.XSHG", "000001.XSHE")):
    schema = load_schema("config/schemas/bars_daily.v2.yaml")
    rows = []
    for index, instrument in enumerate(instruments):
        for day in DATES:
            rows.append({
                "instrument": instrument, "session_date": day,
                "open": 10.0, "high": 11.0, "low": 9.8, "close": 10.0 + index,
                "volume": 10000.0, "amount": 100000.0,
                "status": "trading", "limit_up": 11.0, "limit_down": 9.0,
                "adj_factor": 1.0,
            })
    frame = pd.DataFrame(rows)
    store = CanonicalV2Store(root)
    for session in DATES:
        part = frame[frame["session_date"] == session]
        generation = store.prepare_generation(schema, session.date(), part, {}, {})
        from uq.contracts.artifacts import QualityReportStore
        from uq.contracts.canonical_v2 import file_sha256_bytes
        report = {
            "report_version": 1, "binding_type": "canonical_v2",
            "bound_generation_id": generation, "policy": "reject_all", "status": "passed",
            "checks": [{"name": "coverage", "threshold": 0, "observed": 0,
                        "level": "error", "result": "passed"}],
            "errors": [], "warnings": [],
        }
        directory = QualityReportStore().save(root, report)
        store.publish(schema, session.date(), part, {}, {},
                      quality_checksum=file_sha256_bytes((directory / "report.json").read_bytes()))
    return frame


def _fake_adapter(events_by_symbol):
    class _Adapter:
        connected_server = ("test", 7709)
        def fetch_xdxr_events(self, symbols):
            return {symbol: events_by_symbol.get(symbol, pd.DataFrame()) for symbol in symbols}
    return _Adapter()


def _xdxr(year=2026, month=1, day=7, fenhong=0.0, songzhuangu=0.0):
    return pd.DataFrame([{
        "year": year, "month": month, "day": day, "category": 1,
        "fenhong": fenhong, "songzhuangu": songzhuangu, "peigu": 0.0, "peigujia": 0.0,
    }])


def test_script_publishes_derived_v1_snapshots(tmp_path, monkeypatch):
    _publish_bars(tmp_path / "bars")
    events = {"600000": _xdxr(), "000001": _xdxr()}
    monkeypatch.setattr(module, "MootdxSourceAdapter", lambda: _fake_adapter(events))
    out = tmp_path / "report.json"
    code = module.main([
        "--bars-root", str(tmp_path / "bars"), "--adjustment-root", str(tmp_path / "adj"),
        "--start", str(DATES[0].date()), "--end", str(DATES[-1].date()), "--out", str(out),
    ])
    report = json.loads(out.read_text())
    assert code == 0
    assert report["lineage"] == TDX_DERIVATION_VERSION
    assert report["published"] == 2
    assert report["provider_reconciliation"]["verified"] == 2
    # the events are not enough on their own: the factor must reconcile with the
    # provider's own adj_factor, which the fixture set to 1.0 (no ex-date movement)
    assert all(item["snapshot_generation_id"] for item in report["records"])


def test_a_symbol_without_events_is_refused_not_skipped(tmp_path, monkeypatch):
    _publish_bars(tmp_path / "bars")
    events = {"600000": _xdxr()}   # 000001 missing
    monkeypatch.setattr(module, "MootdxSourceAdapter", lambda: _fake_adapter(events))
    out = tmp_path / "report.json"
    code = module.main([
        "--bars-root", str(tmp_path / "bars"), "--adjustment-root", str(tmp_path / "adj"),
        "--start", str(DATES[0].date()), "--end", str(DATES[-1].date()), "--out", str(out),
    ])
    report = json.loads(out.read_text())
    assert code == 1
    assert report["refused"] == 1
    assert report["refusals"][0]["instrument"] == "000001.XSHE"
    assert "no TDX xdxr events" in report["refusals"][0]["reason"]


def test_the_run_is_bounded_by_the_store_not_the_server(tmp_path, monkeypatch):
    # TDX answers for a symbol that has no bars in this window; it must not be
    # derived, because there is nothing to restate.
    _publish_bars(tmp_path / "bars", instruments=("600000.XSHG",))
    events = {"600000": _xdxr(), "999999": _xdxr()}
    monkeypatch.setattr(module, "MootdxSourceAdapter", lambda: _fake_adapter(events))
    out = tmp_path / "report.json"
    code = module.main([
        "--bars-root", str(tmp_path / "bars"), "--adjustment-root", str(tmp_path / "adj"),
        "--start", str(DATES[0].date()), "--end", str(DATES[-1].date()), "--out", str(out),
    ])
    report = json.loads(out.read_text())
    assert code == 0
    assert report["instruments_considered"] == 1
    assert [item["instrument"] for item in report["records"]] == ["600000.XSHG"]


def test_an_unknown_instrument_filter_is_a_configuration_failure(tmp_path, monkeypatch):
    _publish_bars(tmp_path / "bars", instruments=("600000.XSHG",))
    monkeypatch.setattr(module, "MootdxSourceAdapter", lambda: _fake_adapter({}))
    with pytest.raises(SystemExit, match="not present in the window's bars"):
        module.main([
            "--bars-root", str(tmp_path / "bars"), "--adjustment-root", str(tmp_path / "adj"),
            "--start", str(DATES[0].date()), "--end", str(DATES[-1].date()),
            "--instrument", "999999.XSHG", "--out", str(tmp_path / "report.json"),
        ])


def test_a_diverging_derivation_is_refused(tmp_path, monkeypatch):
    # The bar fixture's adj_factor is 1.0 everywhere, so an ex-date that moves the
    # factor cannot reconcile and the symbol must be refused rather than published.
    _publish_bars(tmp_path / "bars", instruments=("600000.XSHG",))
    events = {"600000": _xdxr(fenhong=5.0)}   # a real cash dividend
    monkeypatch.setattr(module, "MootdxSourceAdapter", lambda: _fake_adapter(events))
    out = tmp_path / "report.json"
    code = module.main([
        "--bars-root", str(tmp_path / "bars"), "--adjustment-root", str(tmp_path / "adj"),
        "--start", str(DATES[0].date()), "--end", str(DATES[-1].date()), "--out", str(out),
    ])
    report = json.loads(out.read_text())
    assert code == 1
    assert "diverges from the official adj_factor" in report["refusals"][0]["reason"]
