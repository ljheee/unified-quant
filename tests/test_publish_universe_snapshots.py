"""Regression tests for scripts/publish_universe_snapshots.py.

The research chain binds ``universe_snapshot`` artifacts but only test helpers
produced them.  These tests pin what the production entry point must guarantee:
coverage tiled exactly over ``[visible_from, next_visible_from)``, the newest
snapshot open-ended, the monthly-approximation limitation carried into every
artifact, refusal when the membership source cannot be tiled, and idempotent
re-runs against an immutable store.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import UniverseSnapshotStore
from uq.contracts.index_membership import IndexMembershipStore, visible_from

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "publish_universe_snapshots", _ROOT / "scripts" / "publish_universe_snapshots.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

UNIVERSE = "a500-test"
CALENDAR = [date(2026, 1, 1) + timedelta(days=step) for step in range(200)]
TRADING_DAYS = [day for day in CALENDAR if day.weekday() < 5]
SNAPSHOTS = [date(2026, 1, 30), date(2026, 2, 27), date(2026, 3, 31)]
LAG = 2


def build_membership(root: Path, snapshots: list[date] | None = None, *, lag: int = LAG) -> None:
    store = IndexMembershipStore(root)
    for position, snapshot_date in enumerate(snapshots or SNAPSHOTS):
        store.publish(
            universe_id=UNIVERSE,
            index_code="000510.SH",
            source="tushare:index_weight:000510.SH",
            source_fetched_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            snapshot_date=snapshot_date,
            publish_lag_days=lag,
            trading_days=TRADING_DAYS,
            members=pd.DataFrame({"instrument": [f"{600000 + position + row:06d}.XSHG" for row in range(3)]}),
        )


def run(monkeypatch, capsys, *, membership_root: Path, universe_root: Path,
        extra: list[str] | None = None) -> tuple[int, dict]:
    argv = [
        "publish_universe_snapshots.py",
        "--membership-root", str(membership_root),
        "--universe-root", str(universe_root),
        "--universe-id", UNIVERSE,
        *(extra or []),
    ]
    monkeypatch.setattr("sys.argv", argv)
    code = module.main()
    return code, json.loads(capsys.readouterr().out)


def test_tiles_snapshots_over_visibility(tmp_path, monkeypatch, capsys):
    build_membership(tmp_path / "membership")
    universe_root = tmp_path / "research"
    code, result = run(monkeypatch, capsys, membership_root=tmp_path / "membership",
                       universe_root=universe_root)
    assert code == 0 and result["status"] == "published", result
    assert result["pit_precision"] == "monthly_approximation"
    published = sorted(result["published"], key=lambda row: row["valid_from"])
    assert len(published) == len(SNAPSHOTS)
    expected_visible = [visible_from(day, LAG, TRADING_DAYS) for day in SNAPSHOTS]
    assert [row["valid_from"] for row in published] == [day.isoformat() for day in expected_visible]
    for current, following in zip(published, published[1:]):
        assert current["valid_to"] == (
            date.fromisoformat(following["valid_from"]) - timedelta(days=1)).isoformat()
        assert current["open_ended"] is False
    assert published[-1]["valid_to"] is None and published[-1]["open_ended"] is True

    store = UniverseSnapshotStore(universe_root)
    for row in published:
        manifest = store.read_manifest(row["generation_id"])
        assert manifest["universe_id"] == UNIVERSE
        assert manifest["valid_from"] == row["valid_from"]
        assert manifest["members_artifact"]["checksum_sha256"] == row["members_checksum_sha256"]
        assert "monthly_approximation" in manifest["membership_evidence"]
        assert "membership_generation=" in manifest["membership_evidence"]
        assert "not strict daily PIT" in manifest["membership_evidence"]


def test_rerun_is_idempotent(tmp_path, monkeypatch, capsys):
    build_membership(tmp_path / "membership")
    universe_root = tmp_path / "research"
    first_code, first = run(monkeypatch, capsys, membership_root=tmp_path / "membership",
                            universe_root=universe_root)
    generations = sorted(directory.name for directory in (universe_root / "universes" / UNIVERSE).iterdir())
    second_code, second = run(monkeypatch, capsys, membership_root=tmp_path / "membership",
                              universe_root=universe_root)
    assert first_code == 0 and second_code == 0
    assert first["status"] == "published" and second["status"] == "already_published"
    assert second["published"] == []
    assert len(second["already_published"]) == len(SNAPSHOTS)
    assert sorted(directory.name for directory in (universe_root / "universes" / UNIVERSE).iterdir()) == generations


def test_visibility_window_selects_a_subset(tmp_path, monkeypatch, capsys):
    build_membership(tmp_path / "membership")
    _, all_result = run(monkeypatch, capsys, membership_root=tmp_path / "membership",
                        universe_root=tmp_path / "all")
    start = date.fromisoformat(sorted(r["valid_from"] for r in all_result["published"])[1])
    code, result = run(monkeypatch, capsys, membership_root=tmp_path / "membership",
                       universe_root=tmp_path / "subset", extra=["--from", start.isoformat()])
    assert code == 0
    assert result["snapshots_selected"] == 2
    assert {row["valid_from"] for row in result["published"]} == {
        row["valid_from"] for row in all_result["published"] if date.fromisoformat(row["valid_from"]) >= start
    }


def test_unknown_universe_refuses_without_writing(tmp_path, monkeypatch, capsys):
    universe_root = tmp_path / "research"
    code, result = run(monkeypatch, capsys, membership_root=tmp_path / "membership",
                       universe_root=universe_root)
    assert code == 2 and result["status"] == "configuration_failure"
    assert "no index membership snapshots" in result["error"]
    assert not (universe_root / "universes").exists()


def test_snapshots_that_cannot_be_tiled_are_refused(tmp_path, monkeypatch, capsys):
    # Lag 0 makes the two adjacent snapshot dates visible on the same trading day,
    # which would produce two artifacts claiming the same coverage start.
    build_membership(tmp_path / "membership", [date(2026, 1, 30), date(2026, 2, 1)], lag=0)
    universe_root = tmp_path / "research"
    code, result = run(monkeypatch, capsys, membership_root=tmp_path / "membership",
                       universe_root=universe_root)
    assert code == 2 and result["status"] == "configuration_failure"
    assert "share a visible_from date" in result["error"]
    assert not (universe_root / "universes").exists()


def test_malformed_window_boundaries_are_refused(tmp_path, monkeypatch, capsys):
    build_membership(tmp_path / "membership")
    code, result = run(monkeypatch, capsys, membership_root=tmp_path / "membership",
                       universe_root=tmp_path / "research", extra=["--from", "2026/01/30"])
    assert code == 2 and "invalid --from" in result["error"]
    code, result = run(monkeypatch, capsys, membership_root=tmp_path / "membership",
                       universe_root=tmp_path / "research",
                       extra=["--from", "2026-03-01", "--to", "2026-02-01"])
    assert code == 2 and result["error"] == "--to precedes --from"
