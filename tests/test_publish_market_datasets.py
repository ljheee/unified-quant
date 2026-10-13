"""Regression tests for scripts/publish_market_datasets.py.

cr.md P1/P2: this script is the only production entry point that turns already
ingested governed artifacts into the four ``paper_execution_template.v2`` input
families, and it had no test at all.  Two behaviours matter most:

* a malformed window is a *configuration* failure, so it must leave the store
  untouched and report the same structured shape as the other governed entry
  points instead of raising ``ValueError``;
* ``cross_check_result`` must follow the independent-calendar flag, because a
  snapshot that claims ``agreement`` without a second vendor is a false
  governance claim (and the publisher itself refuses that combination).
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "publish_market_datasets", _ROOT / "scripts" / "publish_market_datasets.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

CALENDAR = "trading_calendar_snapshot_v1"
START, END = date(2025, 8, 29), date(2025, 9, 2)
SESSIONS = [date(2025, 8, 29), date(2025, 9, 1), date(2025, 9, 2)]


class _FakeTushare:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def trade_dates(self, start: str, end: str) -> list[date]:
        return list(SESSIONS)


def _patch_adapters(monkeypatch, independent: set[date] | None) -> None:
    class _FakeEastMoney:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def between(self, start: date, end: date) -> set[date]:
            assert independent is not None, "provider built despite --skip-independent-calendar"
            return set(independent)

    monkeypatch.setattr(module, "Tushare2kAdapter", _FakeTushare)
    monkeypatch.setattr(module, "EastMoneySessionProvider", _FakeEastMoney)


def _run(monkeypatch, tmp_path, *, argv: list[str], independent: set[date] | None) -> tuple[int, dict]:
    _patch_adapters(monkeypatch, independent)
    market_root = tmp_path / "market-datasets"
    base = [
        "--data-root", str(tmp_path / "bars-store"),
        "--market-dataset-root", str(market_root),
        "--start", START.isoformat(), "--end", END.isoformat(),
        "--family", "calendar",
    ]
    monkeypatch.setattr("sys.argv", ["publish_market_datasets.py", *base, *argv])
    code = module.main()
    return code, {"market_root": market_root}


def _calendar_manifest(market_root: Path) -> dict:
    return json.loads((market_root / CALENDAR / END.isoformat() / "manifest.json").read_text())


def test_malformed_window_is_a_structured_refusal(monkeypatch, tmp_path, capsys):
    code, paths = _run(monkeypatch, tmp_path, argv=["--start", "2025-08-29x"], independent=set(SESSIONS))
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "configuration_failure"
    assert "invalid --start/--end" in payload["error"]
    assert not (paths["market_root"] / CALENDAR).exists()


def test_reversed_window_is_a_structured_refusal(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("sys.argv", [
        "publish_market_datasets.py",
        "--data-root", str(tmp_path / "bars-store"),
        "--market-dataset-root", str(tmp_path / "market-datasets"),
        "--start", END.isoformat(), "--end", START.isoformat(), "--family", "calendar",
    ])
    _patch_adapters(monkeypatch, set(SESSIONS))
    assert module.main() == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "configuration_failure", "error": "--end precedes --start"}


def test_skipped_independent_calendar_records_skipped(monkeypatch, tmp_path, capsys):
    code, paths = _run(
        monkeypatch, tmp_path, argv=["--skip-independent-calendar"], independent=None
    )
    assert code == 0
    capsys.readouterr()
    manifest = _calendar_manifest(paths["market_root"])
    assert manifest["cross_check_result"] == "skipped"
    assert manifest["independent_source"] == "not_checked"
    assert manifest["primary_source"] == "tushare:trade_cal"
    assert manifest["session_dates"] == [session.isoformat() for session in SESSIONS]


def test_independent_calendar_agreement_is_recorded(monkeypatch, tmp_path, capsys):
    code, paths = _run(monkeypatch, tmp_path, argv=[], independent=set(SESSIONS))
    assert code == 0
    capsys.readouterr()
    manifest = _calendar_manifest(paths["market_root"])
    assert manifest["cross_check_result"] == "agreement"
    assert manifest["independent_source"] == "eastmoney_index_kline_v1"


def test_divergent_independent_sessions_fail_closed(monkeypatch, tmp_path, capsys):
    with pytest.raises(module.ContractError, match="session lists diverge"):
        _run(monkeypatch, tmp_path, argv=[], independent=set(SESSIONS) - {SESSIONS[-1]})
    capsys.readouterr()


def test_rerun_reports_already_present_without_republishing(monkeypatch, tmp_path, capsys):
    code, paths = _run(monkeypatch, tmp_path, argv=[], independent=set(SESSIONS))
    assert code == 0
    first = json.loads(capsys.readouterr().out)
    code, _ = _run(monkeypatch, tmp_path, argv=[], independent=set(SESSIONS))
    assert code == 0
    second = json.loads(capsys.readouterr().out)
    capsys.readouterr()
    assert first["published"] == [CALENDAR]
    assert second["published"] == []
    assert second["already_present"] == [CALENDAR]
    assert second["families"]["calendar"] is None
    assert first["bindings"][CALENDAR] == second["bindings"][CALENDAR]
