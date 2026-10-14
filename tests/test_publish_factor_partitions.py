"""Regression tests for scripts/publish_factor_partitions.py and the projection library.

This entry point is what turns the A500 / full-A ``bars_daily/v2`` store into
governed factor partitions, so the cases that matter are the governance ones:
the input line is taken from the store and refused unless the reviewed definition
binds it; the visibility basis is an explicit operator choice and the honest
end-of-day timestamp really does fail closed; history that is too short is skipped
with a reason instead of being silently guessed; and non-trading rows never reach
the calculator.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.schema import load_schema
from uq.store.pit_store import CanonicalStore
from uq.errors import ContractError
from uq.factors.inputs import factor_input_frame
from uq.factors.store import read_factor_partition

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "publish_factor_partitions", _ROOT / "scripts" / "publish_factor_partitions.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

V2_SCHEMA = _ROOT / "config/schemas/bars_daily.v2.yaml"
INSTRUMENTS = ["600000.XSHG", "000001.XSHE", "300750.XSHE"]
REVIEWED = ("basic", "2.0.0")
# Deterministic prices: range_ratio_1d = 0.2, close_location_1d = 0.5,
# amount_20d = 1000.0, volume_ratio_20d = 1.0.
BAR = {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "volume": 100.0, "amount": 1000.0}


def sessions(count: int) -> list[date]:
    day = date(2026, 1, 5)
    out: list[date] = []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    return out


def bars(day: date, *, suspended: str | None = None) -> pd.DataFrame:
    rows = []
    for instrument in INSTRUMENTS:
        halted = instrument == suspended
        rows.append({
            "instrument": instrument,
            "session_date": pd.Timestamp(day),
            "open": BAR["open"], "high": BAR["high"], "low": BAR["low"], "close": BAR["close"],
            "volume": 0.0 if halted else BAR["volume"],
            "amount": 0.0 if halted else BAR["amount"],
            "status": "suspended" if halted else "trading",
            "limit_up": 11.0, "limit_down": 9.0, "adj_factor": 1.0,
        })
    return pd.DataFrame(rows)


def build_store(root: Path, days: list[date], *, suspended: dict[date, str] | None = None) -> None:
    """Write a canonical bars_daily/v2 store the same way the ingest pipeline does.

    ``CanonicalStore`` (used by ``DailyIngestPipeline``) writes the manifest shape the
    real ``data/bars-a500-store`` carries: ``manifest_version: 1`` with
    ``schema_version: "v2"``, which is what ``ManifestFirstReader`` validates.
    """
    schema = load_schema(V2_SCHEMA)
    store = CanonicalStore(root)
    for day in days:
        store.publish(schema, day, bars(day, suspended=(suspended or {}).get(day)), {}, {})


def run(monkeypatch, capsys, *, bars_root: Path, factor_root: Path, start: date, end: date,
        factor_version: str = REVIEWED[1], visibility: str = "session_close",
        partition_set_out: Path | None = None) -> tuple[int, dict]:
    argv = [
        "publish_factor_partitions.py",
        "--bars-root", str(bars_root), "--factor-root", str(factor_root),
        "--factor-set", REVIEWED[0], "--factor-version", factor_version,
        "--start", start.isoformat(), "--end", end.isoformat(),
        "--input-visibility", visibility,
    ]
    if partition_set_out is not None:
        argv += ["--partition-set-out", str(partition_set_out)]
    monkeypatch.setattr("sys.argv", argv)
    code = module.main()
    return code, json.loads(capsys.readouterr().out)


def test_publishes_over_the_reviewed_v2_line_with_exact_values(tmp_path, monkeypatch, capsys):
    days = sessions(21)
    build_store(tmp_path, days)
    set_path = tmp_path / "set.json"
    code, result = run(monkeypatch, capsys, bars_root=tmp_path, factor_root=tmp_path / "factors",
                       start=days[0], end=days[-1], partition_set_out=set_path)
    assert code == 0 and result["status"] == "published", result
    assert result["input_line"] == "bars_daily/v2"
    # minimum_history is 20 sessions, so only the 20th and 21st are publishable.
    assert [item["session"] for item in result["published"]] == [days[19].isoformat(), days[20].isoformat()]
    assert {item["reason"] for item in result["skipped"]} == {"insufficient_history"}

    partition = Path(result["published"][0]["partition"])
    assert "schema_version=v2" in str(partition) and "factor_version=2.0.0" in str(partition)
    frame = read_factor_partition(partition)
    assert len(frame) == len(INSTRUMENTS)
    assert frame["range_ratio_1d"].unique().tolist() == [pytest.approx(0.2)]
    assert frame["close_location_1d"].unique().tolist() == [pytest.approx(0.5)]
    assert frame["amount_20d"].unique().tolist() == [pytest.approx(1000.0)]
    assert frame["volume_ratio_20d"].unique().tolist() == [pytest.approx(1.0)]

    document = json.loads(set_path.read_text())
    assert document["input_schema_version"] == "v2"
    assert document["window_start"] == days[19].isoformat()
    assert document["window_end"] == days[20].isoformat()
    assert len(document["partitions"]) == 2


def test_history_shorter_than_minimum_history_is_skipped_not_guessed(tmp_path, monkeypatch, capsys):
    days = sessions(19)
    build_store(tmp_path, days)
    code, result = run(monkeypatch, capsys, bars_root=tmp_path, factor_root=tmp_path / "factors",
                       start=days[0], end=days[-1])
    assert code == 0
    assert result["status"] == "empty" and result["published"] == []
    assert {item["reason"] for item in result["skipped"]} == {"insufficient_history"}
    assert result["skipped"][0]["detail"].startswith("1 of 20")


def test_unreviewed_input_line_refuses_before_writing_anything(tmp_path, monkeypatch, capsys):
    days = sessions(21)
    build_store(tmp_path, days)
    factor_root = tmp_path / "factors"
    code, result = run(monkeypatch, capsys, bars_root=tmp_path, factor_root=factor_root,
                       start=days[0], end=days[-1], factor_version="1.0.0")
    assert code == 2
    assert result["status"] == "configuration_failure"
    assert "reviewed only for research-v1" in result["error"]
    assert not (factor_root / "factors").exists()


def test_ingest_time_visibility_fails_closed_for_end_of_day_bars(tmp_path, monkeypatch, capsys):
    days = sessions(21)
    build_store(tmp_path, days)
    code, result = run(monkeypatch, capsys, bars_root=tmp_path, factor_root=tmp_path / "factors",
                       start=days[19], end=days[19], visibility="ingest_time")
    assert code == 1 and result["status"] == "failed"
    assert "upstream partition visibility violates factor run cutoff" in result["failed"][0]["error"]


def test_rerun_is_idempotent_and_keeps_the_set_digest(tmp_path, monkeypatch, capsys):
    days = sessions(21)
    build_store(tmp_path, days)
    factor_root = tmp_path / "factors"
    set_path = tmp_path / "set.json"
    first_code, first = run(monkeypatch, capsys, bars_root=tmp_path, factor_root=factor_root,
                            start=days[19], end=days[20], partition_set_out=set_path)
    assert first_code == 0 and first["status"] == "published"
    first_digest = first["partition_set"]["set_digest_sha256"]
    second_code, second = run(monkeypatch, capsys, bars_root=tmp_path, factor_root=factor_root,
                              start=days[19], end=days[20], partition_set_out=set_path)
    assert second_code == 0 and second["status"] == "already_published"
    assert {item["reason"] for item in second["skipped"]} == {"already_published"}
    assert second["partition_set"]["set_digest_sha256"] == first_digest


def test_non_trading_rows_never_reach_the_calculator(tmp_path, monkeypatch, capsys):
    days = sessions(21)
    suspended_instrument = INSTRUMENTS[1]
    build_store(tmp_path, days, suspended={days[19]: suspended_instrument})
    code, result = run(monkeypatch, capsys, bars_root=tmp_path, factor_root=tmp_path / "factors",
                       start=days[20], end=days[20])
    assert code == 0 and result["status"] == "published"
    published = result["published"][0]
    # The 20-session window still holds the suspended row, which must be projected
    # out; if it were kept, the suspended instrument's rolling means would absorb a
    # zero and report a finite ratio instead of an honest null.
    assert published["non_trading_rows_excluded"] == 1
    frame = read_factor_partition(Path(published["partition"]))
    halted = frame[frame["instrument"] == suspended_instrument]
    assert halted["volume_ratio_20d"].isna().all()
    others = frame[frame["instrument"] != suspended_instrument]
    assert others["volume_ratio_20d"].unique().tolist() == [pytest.approx(1.0)]


def test_orphaned_quality_report_is_completed_not_mislabeled(tmp_path, monkeypatch, capsys):
    """cr.md feat MAJOR-1: an interrupted run can leave the immutable quality
    report on disk without the partition.  A re-run must finish that
    publication -- the old classifier read the report's "already published"
    refusal as a published partition and covered a day with nothing on disk."""
    days = sessions(21)
    build_store(tmp_path, days)
    factor_root = tmp_path / "factors"
    first_code, first = run(monkeypatch, capsys, bars_root=tmp_path, factor_root=factor_root,
                            start=days[19], end=days[19])
    assert first_code == 0 and first["status"] == "published"
    partition = Path(first["published"][0]["partition"])
    shutil.rmtree(partition)  # the interruption: report survives, partition is gone

    second_code, second = run(monkeypatch, capsys, bars_root=tmp_path, factor_root=factor_root,
                              start=days[19], end=days[19])
    assert second_code == 0 and second["status"] == "published", second
    assert partition.is_dir()
    assert len(read_factor_partition(partition)) == len(INSTRUMENTS)


def test_divergent_orphaned_quality_report_fails_loudly(tmp_path, monkeypatch, capsys):
    """The orphaned report may only be reused byte-identically; a rewritten
    report is tampering and must fail the run, never read as already published."""
    days = sessions(21)
    build_store(tmp_path, days)
    factor_root = tmp_path / "factors"
    first_code, first = run(monkeypatch, capsys, bars_root=tmp_path, factor_root=factor_root,
                            start=days[19], end=days[19])
    assert first_code == 0 and first["status"] == "published"
    partition = Path(first["published"][0]["partition"])
    generation = first["published"][0]["generation_id"]
    report_path = factor_root / "reports" / "factor_v1" / generation / "report.json"
    document = json.loads(report_path.read_text(encoding="utf-8"))
    document["warnings"] = ["operator note smuggled into the immutable report"]
    content = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    report_path.write_bytes(content)
    (report_path.parent / "report.sha256").write_text(hashlib.sha256(content).hexdigest() + "\n")
    shutil.rmtree(partition)

    code, result = run(monkeypatch, capsys, bars_root=tmp_path, factor_root=factor_root,
                       start=days[19], end=days[19])
    assert code == 1 and result["status"] == "failed", result
    assert "disagrees with the recomputed decision" in result["failed"][0]["error"]
    assert not partition.exists()


def test_expression_defined_set_is_refused_up_front(tmp_path, monkeypatch, capsys):
    """cr.md feat OBS-5: an expression-defined set used to pass every
    config-stage check and then fail identically on every session inside the
    governed engine; the refusal now happens before anything is read or written."""
    days = sessions(21)
    build_store(tmp_path, days)
    factor_root = tmp_path / "factors"
    monkeypatch.setattr("sys.argv", [
        "publish_factor_partitions.py",
        "--bars-root", str(tmp_path), "--factor-root", str(factor_root),
        "--factor-set", "alpha158", "--factor-version", "1.0.0",
        "--start", days[0].isoformat(), "--end", days[-1].isoformat(),
        "--input-visibility", "session_close",
    ])
    code = module.main()
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result["status"] == "configuration_failure", result
    assert "expression-defined factors" in result["error"]
    assert not factor_root.exists()


def test_projection_refuses_unknown_lines_and_missing_reviewed_columns():
    required = ["instrument", "datetime", "open", "high", "low", "close", "volume", "amount"]
    frame = bars(date(2026, 1, 5)).rename(columns={"session_date": "datetime"})
    with pytest.raises(ContractError, match="no governed factor projection for bars_daily/v3"):
        factor_input_frame(frame, schema_version="v3", required_columns=required)
    with pytest.raises(ContractError, match="missing reviewed columns"):
        factor_input_frame(frame.drop(columns=["amount"]), schema_version="research-v1",
                           required_columns=required)
    projected, statistics = factor_input_frame(frame, schema_version="research-v1",
                                               required_columns=required)
    assert statistics["non_trading_rows_excluded"] == 0
    assert list(projected.columns) == required
