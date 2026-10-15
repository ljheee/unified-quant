"""Regression tests for scripts/ingest_window.py.

The window driver is the only command that reproduces a multi-session ingest
from the membership store (``uq-ingest daily`` publishes one session at a
time).  The behaviours that matter are not the happy path -- the pipeline's own
tests cover that -- but the two audit properties a multi-hour run depends on:

* an interrupted run still writes the summary naming which sessions landed
  (re-running the same command resumes because published partitions are
  skipped);
* a calendar that cannot certify the window fails loudly instead of silently
  narrowing the session set.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ingest_window", _ROOT / "scripts" / "ingest_window.py")
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)


class _FakeProvider:
    provenance = "test:independent_calendar"
    evidence = {"provenance": "test"}

    def __init__(self, sessions):
        self._sessions = list(sessions)

    def between(self, start, end):
        return {value for value in self._sessions if start <= value <= end}


class _FakeReport:
    def __init__(self, payload):
        self._payload = payload

    def to_json(self):
        return json.dumps(self._payload)


def _patch(monkeypatch, provider, run):
    class _Pipeline:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, session, instruments):
            return run(session, instruments)

    monkeypatch.setattr(module, "EastMoneySessionProvider", lambda: provider)
    monkeypatch.setattr(module, "build_tushare_v2_pipeline", lambda *a, **k: _Pipeline())
    monkeypatch.setattr(
        module, "IndexMembershipStore",
        lambda root: type("M", (), {
            "as_of": lambda self, universe, session: (None, pd.DataFrame({"instrument": ["600000.XSHG"]})),
        })(),
    )
    monkeypatch.setattr(module, "TradingCalendar", lambda **kwargs: object())


def test_interrupted_run_still_writes_the_summary(monkeypatch, tmp_path):
    sessions = [date(2025, 9, 1), date(2025, 9, 2)]
    seen: list[date] = []

    def run(session, instruments):
        seen.append(session)
        if len(seen) == 2:
            raise KeyboardInterrupt("simulated ctrl-c")
        return _FakeReport({"status": "published", "rows": 10, "warnings": []})

    _patch(monkeypatch, _FakeProvider(sessions), run)
    out = tmp_path / "summary.json"
    with pytest.raises(KeyboardInterrupt):
        module.main([
            "--data-root", str(tmp_path), "--membership-root", str(tmp_path),
            "--universe-id", "csi-all", "--start", "2025-09-01", "--end", "2025-09-02",
            "--summary-out", str(out), "--skip-health-probe", "--session-interval-seconds", "0",
        ])
    summary = json.loads(out.read_text())
    assert summary["status"] == "interrupted"
    assert summary["interrupted"] is True
    # the session that did land is recorded, so the partial run is auditable
    assert [item["session"] for item in summary["published"]] == ["2025-09-01"]


def test_published_partitions_are_skipped_on_rerun(monkeypatch, tmp_path):
    sessions = [date(2025, 9, 1), date(2025, 9, 2)]
    partition = tmp_path / "canonical" / "bars_daily" / "v2" / "date=2025-09-01"
    partition.mkdir(parents=True)
    (partition / "manifest.json").write_text("{}")
    (partition / "data.parquet").write_bytes(b"")

    def run(session, instruments):
        return _FakeReport({"status": "published", "rows": 1, "warnings": []})

    _patch(monkeypatch, _FakeProvider(sessions), run)
    out = tmp_path / "summary.json"
    code = module.main([
        "--data-root", str(tmp_path), "--membership-root", str(tmp_path),
        "--universe-id", "csi-all", "--start", "2025-09-01", "--end", "2025-09-02",
        "--summary-out", str(out), "--skip-health-probe", "--session-interval-seconds", "0",
    ])
    summary = json.loads(out.read_text())
    assert code == 0
    assert [item["session"] for item in summary["skipped"]] == ["2025-09-01"]
    assert [item["session"] for item in summary["published"]] == ["2025-09-02"]


def test_calendar_that_cannot_certify_the_window_fails(monkeypatch, tmp_path):
    class _Broken(_FakeProvider):
        def between(self, start, end):
            raise module.IndependentCalendarError("endpoint unreachable")

    _patch(monkeypatch, _Broken([]), lambda s, i: _FakeReport({"status": "published"}))
    with pytest.raises(SystemExit):
        module.main([
            "--data-root", str(tmp_path), "--membership-root", str(tmp_path),
            "--universe-id", "csi-all", "--start", "2025-09-01", "--end", "2025-09-02",
            "--skip-health-probe", "--calendar-attempts", "1", "--calendar-retry-seconds", "0",
        ])
