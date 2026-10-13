"""Resumable, immutable publication of Tushare dividend disclosures.

The fetch script is the producer of the governed DividendEventStore: a re-run
must skip already-published partitions without republishing them, must warn
when the provider has revised the rows behind a frozen partition, and must
stamp each partition with its own fetch time.
"""

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "fetch_dividend_events", _ROOT / "scripts" / "fetch_dividend_events.py"
)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

DAY = dt.date(2026, 8, 29)
NEXT = dt.date(2026, 8, 30)


def _frame(cash_div_tax=0.12, ann_date=DAY):
    return pd.DataFrame(
        [
            {
                "ts_code": "600000.SH",
                "end_date": "20260630",
                "ann_date": ann_date.strftime("%Y%m%d"),
                "div_proc": "预案",
                "stk_div": 0.0,
                "stk_bo_rate": None,
                "stk_co_rate": None,
                "cash_div": None,
                "cash_div_tax": cash_div_tax,
                "record_date": None,
                "ex_date": None,
                "pay_date": None,
                "div_listdate": None,
                "imp_ann_date": None,
            }
        ]
    )


class _Adapter:
    def __init__(self, frames):
        self._frames = frames

    def dividend(self, *, ann_date):
        return self._frames.get(ann_date, pd.DataFrame())


def _run(tmp_path, monkeypatch, frames, capsys):
    monkeypatch.setattr(script, "Tushare2kAdapter", lambda: _Adapter(frames))
    code = script.main()
    out = capsys.readouterr()
    summary = json.loads(out.out)
    return code, summary, out.err


@pytest.fixture(autouse=True)
def _argv(monkeypatch, tmp_path):
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fetch_dividend_events.py",
            "--start", DAY.isoformat(),
            "--end", NEXT.isoformat(),
            "--store", str(tmp_path / "store"),
        ],
    )


def _manifest(tmp_path, day):
    return json.loads(
        (tmp_path / "store" / "dividend_events" / day.isoformat() / "manifest.json").read_text(encoding="utf-8")
    )


def test_publish_then_resume_skips_without_drift(tmp_path, monkeypatch, capsys):
    frames = {DAY.isoformat(): _frame(), NEXT.isoformat(): _frame(ann_date=NEXT)}
    code, summary, _ = _run(tmp_path, monkeypatch, frames, capsys)
    assert code == 0
    assert summary["published"] == [DAY.isoformat(), NEXT.isoformat()]
    assert summary["content_drift"] == []
    code, summary, err = _run(tmp_path, monkeypatch, frames, capsys)
    assert code == 0
    assert summary["published"] == []
    assert summary["already_present"] == [DAY.isoformat(), NEXT.isoformat()]
    assert summary["content_drift"] == []
    assert err == ""


def test_resume_warns_when_the_provider_revises_a_frozen_partition(tmp_path, monkeypatch, capsys):
    frames = {DAY.isoformat(): _frame(), NEXT.isoformat(): _frame(ann_date=NEXT)}
    _run(tmp_path, monkeypatch, frames, capsys)
    frozen = _manifest(tmp_path, DAY)
    revised = {DAY.isoformat(): _frame(cash_div_tax=0.99), NEXT.isoformat(): _frame(ann_date=NEXT)}
    code, summary, err = _run(tmp_path, monkeypatch, revised, capsys)
    assert code == 0
    # the frozen partition keeps its original bytes; the drift is only reported
    assert summary["already_present"] == [DAY.isoformat(), NEXT.isoformat()]
    assert summary["content_drift"] == [DAY.isoformat()]
    assert "provider revised already-published partition" in err
    assert _manifest(tmp_path, DAY) == frozen


def test_each_partition_is_stamped_with_its_own_fetch_time(tmp_path, monkeypatch, capsys):
    frames = {DAY.isoformat(): _frame(), NEXT.isoformat(): _frame(ann_date=NEXT)}
    ticks = iter(
        dt.datetime(2026, 9, 1, 9, 0, second, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        for second in range(10)
    )
    real_datetime = dt.datetime

    class _Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return next(ticks)

    monkeypatch.setattr(script.dt, "datetime", _Clock)
    code, summary, _ = _run(tmp_path, monkeypatch, frames, capsys)
    assert code == 0
    first = _manifest(tmp_path, DAY)["source_fetched_at"]
    second = _manifest(tmp_path, NEXT)["source_fetched_at"]
    assert first == "2026-09-01T09:00:00+08:00"
    assert second == "2026-09-01T09:00:01+08:00"
