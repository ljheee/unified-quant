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

from uq.errors import ContractError

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


def test_tail_sessions_widen_the_universe_only_when_asked(monkeypatch, tmp_path):
    # 600000.XSHG is a member on the first session only; 000001.XSHE on the second
    # onwards.  With --tail-sessions 0 the departed name is never fetched; with
    # N=2 it is fetched on the sessions that follow its last member day.
    import pandas as pd

    sessions = [date(2025, 9, 1), date(2025, 9, 2), date(2025, 9, 3), date(2025, 9, 4)]
    membership = {
        sessions[0]: ["600000.XSHG"],
        sessions[1]: ["000001.XSHE"],
        sessions[2]: ["000001.XSHE"],
        sessions[3]: ["000001.XSHE"],
    }
    fetched: dict[date, list[str]] = {}

    def run(session, instruments):
        fetched[session] = sorted(instruments)
        return _FakeReport({"status": "published", "rows": 1, "warnings": []})

    class _Store:
        def as_of(self, universe, session):
            names = membership.get(session, [])
            return None, pd.DataFrame({"instrument": list(names)})

    monkeypatch.setattr(module, "EastMoneySessionProvider", lambda: _FakeProvider(sessions))
    monkeypatch.setattr(module, "build_tushare_v2_pipeline", lambda *a, **k: type("P", (), {"run": staticmethod(run)})())
    monkeypatch.setattr(module, "IndexMembershipStore", lambda root: _Store())
    monkeypatch.setattr(module, "TradingCalendar", lambda **kwargs: object())

    def invoke(tail, out):
        fetched.clear()
        code = module.main([
            "--data-root", str(tmp_path), "--membership-root", str(tmp_path),
            "--universe-id", "csi-all", "--start", "2025-09-01", "--end", "2025-09-04",
            "--summary-out", str(out), "--skip-health-probe",
            "--session-interval-seconds", "0", "--tail-sessions", str(tail),
        ])
        return code

    plain, tailed = tmp_path / "plain.json", tmp_path / "tailed.json"
    assert invoke(0, plain) == 0
    assert fetched[sessions[1]] == ["000001.XSHE"]
    assert fetched[sessions[2]] == ["000001.XSHE"]
    assert json.loads(plain.read_text())["tail_sessions"] == 0

    assert invoke(2, tailed) == 0
    # the departed name is fetched on the two sessions that follow its last
    # member day, and only there
    assert fetched[sessions[0]] == ["600000.XSHG"]
    assert fetched[sessions[1]] == ["000001.XSHE", "600000.XSHG"]
    assert fetched[sessions[2]] == ["000001.XSHE", "600000.XSHG"]
    assert fetched[sessions[3]] == ["000001.XSHE"]
    summary = json.loads(tailed.read_text())
    assert summary["tail_sessions"] == 2
    # the widening is recorded in the run summary, not silently applied
    assert [item.get("tail_members") for item in summary["published"]] == [
        None, ["600000.XSHG"], ["600000.XSHG"], None,
    ]


def test_an_unreadable_prior_membership_is_recorded_not_dropped(monkeypatch, tmp_path):
    # A prior session whose membership cannot be read would silently drop its
    # departed names from the tail, shrinking label coverage with no trace. The run
    # summary must name it (cr.md OBS-D).
    import pandas as pd

    sessions = [date(2025, 9, 1), date(2025, 9, 2), date(2025, 9, 3)]
    membership = {
        sessions[0]: ["600000.XSHG"],
        sessions[1]: ["000001.XSHE"],
        sessions[2]: ["000001.XSHE"],
    }
    # Session 0's membership is readable the first time (it decides that session's
    # own fetch set) but unreadable on every later read -- which is exactly what a
    # tail lookup is, so only the tail widening is affected.
    reads: dict[date, int] = {}

    def run(session, instruments):
        return _FakeReport({"status": "published", "rows": 1, "warnings": []})

    class _Store:
        def as_of(self, universe, session):
            reads[session] = reads.get(session, 0) + 1
            if session == sessions[0] and reads[session] > 1:
                raise ContractError("membership partition unreadable")
            names = membership.get(session, [])
            return None, pd.DataFrame({"instrument": list(names)})

    monkeypatch.setattr(module, "EastMoneySessionProvider", lambda: _FakeProvider(sessions))
    monkeypatch.setattr(module, "build_tushare_v2_pipeline", lambda *a, **k: type("P", (), {"run": staticmethod(run)})())
    monkeypatch.setattr(module, "IndexMembershipStore", lambda root: _Store())
    monkeypatch.setattr(module, "TradingCalendar", lambda **kwargs: object())

    out = tmp_path / "summary.json"
    assert module.main([
        "--data-root", str(tmp_path), "--membership-root", str(tmp_path),
        "--universe-id", "csi-all", "--start", "2025-09-01", "--end", "2025-09-03",
        "--summary-out", str(out), "--skip-health-probe",
        "--session-interval-seconds", "0", "--tail-sessions", "2",
    ]) == 0
    summary = json.loads(out.read_text())
    # 600000.XSHG cannot be added to the tail, and the session that failed is named
    assert [item.get("tail_members") for item in summary["published"]] == [
        None, None, None,
    ]
    failures = summary["tail_membership_read_failures"]
    assert [item["session"] for item in failures] == ["2025-09-01", "2025-09-01"]
    assert all("ContractError" in item["error"] for item in failures)
