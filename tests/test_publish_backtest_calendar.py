from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

from uq.research_chain.owning_contracts import BacktestMarketDatasetStore

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "publish_backtest_calendar", ROOT / "scripts" / "publish_backtest_calendar.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def _patch_calendars(monkeypatch, primary, independent):
    monkeypatch.setattr(
        module.Tushare2kAdapter, "trade_dates",
        lambda self, start, end, exchange="SSE": set(primary),
    )
    monkeypatch.setattr(
        module.SinaSessionProvider, "between",
        lambda self, start, end: set(independent),
    )


def test_publishes_calendar_only_after_exact_independent_agreement(tmp_path, monkeypatch, capsys):
    days = {date(2026, 1, 5), date(2026, 1, 6)}
    _patch_calendars(monkeypatch, days, days)
    assert module.main([
        "--market-dataset-root", str(tmp_path / "market"),
        "--start", "2026-01-05", "--end", "2026-01-06",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    store = BacktestMarketDatasetStore(tmp_path / "market")
    manifest = store.read_manifest_by(result["generation_id"], dataset_type="calendar")
    frame = store.read_frame(
        result["generation_id"], result["data_checksum_sha256"],
        dataset_type="calendar", required_columns={"date", "instrument"},
    )
    assert manifest["sources"]["cross_check"] == "exact_session_set_agreement"
    assert frame["date"].tolist() == ["2026-01-05", "2026-01-06"]
    assert frame["instrument"].tolist() == ["CALENDAR", "CALENDAR"]


def test_calendar_mismatch_refuses_without_publishing(tmp_path, monkeypatch, capsys):
    _patch_calendars(monkeypatch, {date(2026, 1, 5)}, {date(2026, 1, 6)})
    assert module.main([
        "--market-dataset-root", str(tmp_path / "market"),
        "--start", "2026-01-05", "--end", "2026-01-06",
    ]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "refused"
    assert not (tmp_path / "market" / "backtest_market_data").exists()
