"""Regression tests for the window-level universe union publisher.

The v6 request can bind only one `universe_snapshot.v1`, while the PIT membership
store publishes monthly tiles.  The union artifact is an identity anchor, not a
replacement for the per-day `index_membership_binding`, so these tests pin both
the member set and the evidence wording that keeps that distinction visible.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from uq.contracts.index_membership import IndexMembershipStore, visible_from
from uq.execution.market_dataset_publisher import MarketDatasetPublisher

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "publish_universe_union", _ROOT / "scripts" / "publish_universe_union.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

CALENDAR = [date(2026, 1, 1) + timedelta(days=step) for step in range(120)]
TRADING_DAYS = [day for day in CALENDAR if day.weekday() < 5]
SNAPSHOTS = [date(2026, 1, 30), date(2026, 2, 27)]
LAG = 2
UNIVERSE = "csi-union-test"


def _membership(root: Path) -> None:
    store = IndexMembershipStore(root)
    for position, snapshot_date in enumerate(SNAPSHOTS):
        store.publish(
            universe_id=UNIVERSE,
            index_code="000985.CSI",
            source="tushare:index_weight:000985.CSI",
            source_fetched_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            snapshot_date=snapshot_date,
            publish_lag_days=LAG,
            trading_days=TRADING_DAYS,
            members=pd.DataFrame({"instrument": [
                "600000.XSHG",
                f"{600001 + position:06d}.XSHG",
            ]}),
        )


def _market(root: Path, start: date, end: date) -> None:
    sessions = [day for day in TRADING_DAYS if start <= day <= end]
    MarketDatasetPublisher(root).publish_calendar_snapshot(
        partition_date=end,
        window_start=start,
        window_end=end,
        sessions=sessions,
        primary_source="tushare:trade_cal",
        independent_source="not_checked",
        cross_check_result="skipped",
    )


def test_union_contains_every_visible_member_and_declares_its_semantics(tmp_path):
    _membership(tmp_path / "membership")
    first_visible = visible_from(SNAPSHOTS[0], LAG, TRADING_DAYS)
    last_visible = visible_from(SNAPSHOTS[1], LAG, TRADING_DAYS)
    _market(tmp_path / "market", first_visible, last_visible)
    out = tmp_path / "report.json"
    code = module.main([
        "--membership-root", str(tmp_path / "membership"),
        "--universe-root", str(tmp_path / "research"),
        "--universe-id", UNIVERSE,
        "--market-dataset-root", str(tmp_path / "market"),
        "--start", first_visible.isoformat(), "--end", last_visible.isoformat(),
        "--out", str(out),
    ])
    assert code == 0
    report = json.loads(out.read_text())
    assert report["instrument_count"] == 3
    manifest = json.loads(
        (tmp_path / "research" / "universes" / UNIVERSE / report["generation_id"] / "manifest.json")
        .read_text()
    )
    assert manifest["valid_from"] == first_visible.isoformat()
    assert manifest["valid_to"] is None
    assert "member_rule=union_of_visible_monthly_snapshots" in manifest["membership_evidence"]
    assert "window_level_union_not_daily_pit" in manifest["membership_evidence"]
    assert "index_membership_binding" in manifest["membership_evidence"]
    assert "not strict daily PIT" in manifest["membership_evidence"]


def test_union_rerun_names_the_same_content_identity(tmp_path):
    _membership(tmp_path / "membership")
    first_visible = visible_from(SNAPSHOTS[0], LAG, TRADING_DAYS)
    last_visible = visible_from(SNAPSHOTS[1], LAG, TRADING_DAYS)
    _market(tmp_path / "market", first_visible, last_visible)
    argv = [
        "--membership-root", str(tmp_path / "membership"),
        "--universe-root", str(tmp_path / "research"),
        "--universe-id", UNIVERSE,
        "--market-dataset-root", str(tmp_path / "market"),
        "--start", first_visible.isoformat(), "--end", last_visible.isoformat(),
    ]
    assert module.main(argv) == 0
    assert module.main(argv) == 0


def test_union_rerun_reports_already_published(tmp_path, capsys):
    _membership(tmp_path / "membership")
    first_visible = visible_from(SNAPSHOTS[0], LAG, TRADING_DAYS)
    last_visible = visible_from(SNAPSHOTS[1], LAG, TRADING_DAYS)
    _market(tmp_path / "market", first_visible, last_visible)
    argv = [
        "--membership-root", str(tmp_path / "membership"),
        "--universe-root", str(tmp_path / "research"),
        "--universe-id", UNIVERSE,
        "--market-dataset-root", str(tmp_path / "market"),
        "--start", first_visible.isoformat(), "--end", last_visible.isoformat(),
    ]
    assert module.main(argv) == 0
    capsys.readouterr()
    assert module.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_published"
