"""Regression tests for scripts/probe_index_weight_visibility.py.

The probe is the forward-only observation tool behind the measured
``index_weight`` publication lag bound (pendding item 1): its append-only log
is committed evidence, so its revision detection must not silently drop
changes.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "probe_index_weight_visibility", _ROOT / "scripts" / "probe_index_weight_visibility.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

_SESSIONS = {date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)}


class _FakeAdapter:
    """Serves a mutable index_weight payload so revisions can be simulated."""

    def __init__(self) -> None:
        self.members: list[tuple[str, float]] = []

    def index_weight(self, index_code: str, start: str, end: str) -> pd.DataFrame:
        return pd.DataFrame([
            {"trade_date": "20251231", "con_code": code, "weight": weight}
            for code, weight in self.members
        ])

    def trade_dates(self, start: str, end: str) -> set[date]:
        return set(_SESSIONS)


def _run_probe(tmp_path: Path, monkeypatch, adapter: _FakeAdapter, observed_on: str) -> list[dict]:
    log = tmp_path / "visibility.jsonl"
    monkeypatch.setattr(module, "Tushare2kAdapter", lambda: adapter)
    monkeypatch.setattr("sys.argv", [
        "probe_index_weight_visibility.py",
        "--log", str(log),
        "--env-file", str(tmp_path / "no-such.env"),
        "--start", "20251201",
        "--end", observed_on.replace("-", ""),
        "--observed-on", observed_on,
    ])
    assert module.main() == 0
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_revision_reverting_to_first_seen_content_is_recorded(tmp_path, monkeypatch, capsys):
    # cr.md round-4 finding: the log replay pinned the FIRST-seen fingerprint
    # per snapshot (setdefault), so an A→B→A revision chain recorded the
    # first_seen and the A→B change but silently dropped the reverting B→A
    # change.  The replay must compare against the LATEST fingerprint.
    adapter = _FakeAdapter()
    adapter.members = [("600000.SH", 1.0), ("000001.SZ", 2.0)]
    records = _run_probe(tmp_path, monkeypatch, adapter, "2026-01-05")
    assert [record["event"] for record in records] == ["first_seen"]
    first_fingerprint = records[0]["fingerprint"]

    adapter.members = [("600000.SH", 9.0), ("000001.SZ", 2.0)]
    records = _run_probe(tmp_path, monkeypatch, adapter, "2026-01-06")
    assert [record["event"] for record in records] == ["first_seen", "content_changed"]
    assert records[1]["fingerprint"] != first_fingerprint

    adapter.members = [("600000.SH", 1.0), ("000001.SZ", 2.0)]
    records = _run_probe(tmp_path, monkeypatch, adapter, "2026-01-07")
    assert [record["event"] for record in records] == [
        "first_seen", "content_changed", "content_changed",
    ]
    # The reverting change is recorded and points back at the original content.
    assert records[2]["fingerprint"] == first_fingerprint


def test_unchanged_content_stays_idempotent_across_reruns(tmp_path, monkeypatch, capsys):
    adapter = _FakeAdapter()
    adapter.members = [("600000.SH", 1.0)]
    records = _run_probe(tmp_path, monkeypatch, adapter, "2026-01-05")
    assert [record["event"] for record in records] == ["first_seen"]
    # Same day, same content: no new records.
    records = _run_probe(tmp_path, monkeypatch, adapter, "2026-01-05")
    assert len(records) == 1
    # Later day, same content: still no new records.
    records = _run_probe(tmp_path, monkeypatch, adapter, "2026-01-06")
    assert len(records) == 1
    # And after a revision, re-observing the revised content adds nothing.
    adapter.members = [("600000.SH", 2.0)]
    records = _run_probe(tmp_path, monkeypatch, adapter, "2026-01-07")
    assert [record["event"] for record in records] == ["first_seen", "content_changed"]
    records = _run_probe(tmp_path, monkeypatch, adapter, "2026-01-07")
    assert len(records) == 2
