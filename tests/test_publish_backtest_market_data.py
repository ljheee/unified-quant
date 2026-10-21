"""The three window-level market inputs `backtest_config.v1` binds.

`BacktestMarketDatasetStore.read_frame` had a reader without a writer until
`scripts/publish_backtest_market_data.py`, so a real run could not supply the
calendar, suspension or corporate-action input.  These tests drive the real
store publisher and the real script against governed-shaped fixtures, then prove
the published frames are readable through `read_frame` with the very binding the
backtest stage uses.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.research_chain.owning_contracts import BacktestMarketDatasetStore

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "publish_backtest_market_data.py"
SESSIONS = [dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 1, 7)]


def _canonical_bars(root: Path, sessions: list[dt.date]) -> None:
    """Publish canonical v2-style partitions with the columns the script reads."""
    import hashlib

    from uq.contracts.canonical_v2 import file_sha256_bytes
    from uq.contracts.gate_contracts import validate_contract

    for index, session in enumerate(sessions):
        directory = root / "canonical" / "bars_daily" / "v2" / f"date={session.isoformat()}"
        directory.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(
            [
                {"instrument": "600000.XSHG", "session_date": session,
                 "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
                 "volume": 1000.0, "amount": 10000.0, "status": "trading"},
                # one suspended and one delisted name, so the suspension frame has
                # real rows rather than only the all-trading fallback
                {"instrument": "000001.XSHE", "session_date": session,
                 "open": 20.0, "high": 20.5, "low": 19.5, "close": 20.0,
                 "volume": 2000.0, "amount": 40000.0, "status": "suspended"},
                {"instrument": "600519.XSHG", "session_date": session,
                 "open": 30.0, "high": 30.5, "low": 29.5, "close": 30.0,
                 "volume": 3000.0, "amount": 90000.0, "status": "delisted"},
            ]
        )
        data_path = directory / "data.parquet"
        frame.to_parquet(data_path, index=False)
        checksum = file_sha256_bytes(data_path.read_bytes())
        manifest = {
            "manifest_version": 1,
            "dataset": "bars_daily",
            "schema_version": "v2",
            "partition_date": session.isoformat(),
            "generation_id": hashlib.sha256(
                json.dumps({"d": "bars_daily", "v": "v2", "p": session.isoformat(),
                            "i": index}, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "row_count": len(frame),
            "data_checksum_sha256": checksum,
            "columns": list(frame.columns),
            "quality_report_checksum": "0" * 64,
        }
        (directory / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))


def _dividend_store(root: Path, announcement: dt.date) -> None:
    """Publish one dividend disclosure partition through its own store publisher.

    Hand-writing the manifest would fork the contract; `DividendEventStore.publish`
    owns `dividend_event_partition.v1` and derives the generation id, so the fixture
    stays the shape the production publisher really reads.
    """
    from uq.contracts.dividend_events import EVENT_COLUMNS, DividendEventStore

    frame = pd.DataFrame(
        [
            {"ts_code": "600000.SH", "end_date": "2025-12-31",
             "div_proc": "实施", "ex_date": "2026-01-06", "cash_div_tax": 0.02,
             "stk_div": 0.0, "record_date": "2026-01-07", "cash_div": 0.0,
             "stk_bo_rate": 0.0, "stk_co_rate": 0.0,
             "ann_date": announcement.isoformat(),
             "imp_ann_date": announcement.isoformat(), "pay_date": None,
             "div_listdate": None},
            {"ts_code": "600519.SH", "end_date": "2025-12-31",
             "div_proc": "实施", "ex_date": "2026-01-09", "cash_div_tax": 0.05,
             "stk_div": 0.0, "record_date": "2026-01-12", "cash_div": 0.0,
             "stk_bo_rate": 0.0, "stk_co_rate": 0.0,
             "ann_date": announcement.isoformat(),
             "imp_ann_date": announcement.isoformat(), "pay_date": None,
             "div_listdate": None},
            # announced but not yet implemented: the window must not widen to it
            {"ts_code": "000001.SZ", "end_date": "2025-12-31",
             "div_proc": "预案", "ex_date": "2026-01-08", "cash_div_tax": 0.01,
             "stk_div": 0.0, "record_date": "2026-01-11", "cash_div": 0.0,
             "stk_bo_rate": 0.0, "stk_co_rate": 0.0,
             "ann_date": announcement.isoformat(),
             "imp_ann_date": announcement.isoformat(), "pay_date": None,
             "div_listdate": None},
        ]
    )
    # The fixture must carry exactly the contract's raw columns, so a drift in
    # this file cannot silently teach the publisher a different shape.
    expected = set(EVENT_COLUMNS) - {"record_id", "instrument"} | {"ts_code"}
    assert set(frame.columns) == expected
    DividendEventStore(root).publish(
        ann_date=announcement,
        source="tushare:dividend",
        source_fetched_at=dt.datetime(2026, 1, 3, 9, tzinfo=dt.timezone(dt.timedelta(hours=8))),
        frame=frame,
    )


def _run(data_root: Path, dividend_root: Path, *, start: dt.date, end: dt.date) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--data-root", str(data_root),
         "--dividend-root", str(dividend_root),
         "--start", start.isoformat(), "--end", end.isoformat()],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def test_publishes_all_three_families_readable_through_the_store(tmp_path: Path) -> None:
    _canonical_bars(tmp_path, SESSIONS)
    _dividend_store(tmp_path / "dividend-store", dt.date(2026, 1, 2))
    report = _run(tmp_path, tmp_path / "dividend-store", start=SESSIONS[0], end=SESSIONS[-1])

    store = BacktestMarketDatasetStore(tmp_path)
    calendar = store.read_frame(
        report["bindings"]["calendar_binding"]["generation_id"],
        report["bindings"]["calendar_binding"]["checksum_sha256"],
        dataset_type="calendar", required_columns={"date"},
    )
    assert sorted(calendar["date"].astype(str)) == [d.isoformat() for d in SESSIONS]

    suspension = store.read_frame(
        report["bindings"]["suspension_binding"]["dataset_generation_id"],
        report["bindings"]["suspension_binding"]["data_checksum_sha256"],
        dataset_type="suspension", required_columns={"date", "instrument", "suspended"},
    )
    suspended = suspension.loc[suspension["suspended"].astype(bool)]
    # every non-trading status, on every session, is recorded
    assert len(suspended) == 6
    assert set(suspended["instrument"]) == {"000001.XSHE", "600519.XSHG"}

    corporate = store.read_frame(
        report["bindings"]["corporate_action_binding"]["dataset_generation_id"],
        report["bindings"]["corporate_action_binding"]["data_checksum_sha256"],
        dataset_type="corporate_action", required_columns={"date", "instrument"},
    )
    # Only ex-dates inside the window survive: 600519's 2026-01-09 ex-date is after
    # the window, and the 预案 row is not implemented at all.
    assert set(corporate["date"]) == {"2026-01-06"}
    assert set(corporate["instrument"]) == {"600000.XSHG"}
    assert "000001.XSHE" not in set(corporate["instrument"])
    assert "2026-01-09" not in set(corporate["date"])


def test_provenance_records_the_primary_source_and_the_reconciliation_role(tmp_path: Path) -> None:
    _canonical_bars(tmp_path, SESSIONS)
    _dividend_store(tmp_path / "dividend-store", dt.date(2026, 1, 2))
    report = _run(tmp_path, tmp_path / "dividend-store", start=SESSIONS[0], end=SESSIONS[-1])

    store = BacktestMarketDatasetStore(tmp_path)
    manifest = store.read_manifest_by(
        report["bindings"]["suspension_binding"]["dataset_generation_id"], dataset_type="suspension"
    )
    assert manifest["sources"]["primary"] == "canonical_bars_daily_v2_status"
    assert manifest["sources"]["reconciliation"] == "tushare_suspend_d"
    assert manifest["producer"] == "scripts/publish_backtest_market_data.py"
    assert manifest["window_start"] == "2026-01-05"
    assert manifest["window_end"] == "2026-01-07"


def test_republishing_the_same_window_is_a_skip_not_a_refusal(tmp_path: Path) -> None:
    _canonical_bars(tmp_path, SESSIONS)
    _dividend_store(tmp_path / "dividend-store", dt.date(2026, 1, 2))
    first = _run(tmp_path, tmp_path / "dividend-store", start=SESSIONS[0], end=SESSIONS[-1])
    second = _run(tmp_path, tmp_path / "dividend-store", start=SESSIONS[0], end=SESSIONS[-1])
    # every writer is immutable, so a partially completed run must resume rather
    # than abort -- the same tolerance `publish_market_datasets.py` documents
    assert first["bindings"] == second["bindings"]


def test_a_different_window_is_a_different_generation(tmp_path: Path) -> None:
    _canonical_bars(tmp_path, SESSIONS)
    _dividend_store(tmp_path / "dividend-store", dt.date(2026, 1, 2))
    full = _run(tmp_path, tmp_path / "dividend-store", start=SESSIONS[0], end=SESSIONS[-1])
    short = _run(tmp_path, tmp_path / "dividend-store", start=SESSIONS[0], end=SESSIONS[1])
    assert full["bindings"]["calendar_binding"]["generation_id"] != short["bindings"]["calendar_binding"]["generation_id"]
    assert short["row_counts"]["calendar"] == 2


def test_a_window_with_no_canonical_partitions_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "canonical" / "bars_daily" / "v2").mkdir(parents=True)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _run(tmp_path, tmp_path / "dividend-store", start=SESSIONS[0], end=SESSIONS[-1])
    assert "no canonical bars partitions" in exc.value.stderr


def test_the_store_refuses_an_empty_frame(tmp_path: Path) -> None:
    store = BacktestMarketDatasetStore(tmp_path)
    with pytest.raises(ContractError, match="non-empty frame"):
        store.publish(
            pd.DataFrame(columns=["date", "instrument"]),
            dataset_type="calendar", window_start=SESSIONS[0], window_end=SESSIONS[-1],
            sources={"primary": "x"}, producer="test",
        )


def test_the_store_refuses_a_frame_without_the_key_columns(tmp_path: Path) -> None:
    store = BacktestMarketDatasetStore(tmp_path)
    with pytest.raises(ContractError, match="requires a date column"):
        store.publish(
            pd.DataFrame([{"instrument": "600000.XSHG"}]),
            dataset_type="calendar", window_start=SESSIONS[0], window_end=SESSIONS[-1],
            sources={"primary": "x"}, producer="test",
        )


def test_a_published_frame_whose_bytes_drift_is_refused_on_read(tmp_path: Path) -> None:
    _canonical_bars(tmp_path, SESSIONS)
    _dividend_store(tmp_path / "dividend-store", dt.date(2026, 1, 2))
    report = _run(tmp_path, tmp_path / "dividend-store", start=SESSIONS[0], end=SESSIONS[-1])
    store = BacktestMarketDatasetStore(tmp_path)
    binding = report["bindings"]["calendar_binding"]
    data_path = tmp_path / "backtest_market_data" / f"dataset={binding['generation_id']}" / "data.parquet"
    pd.DataFrame([{"date": "1999-01-01", "instrument": "TAMPERED"}]).to_parquet(data_path, index=False)
    with pytest.raises(ContractError, match="tampered"):
        store.read_frame(
            binding["generation_id"], binding["checksum_sha256"],
            dataset_type="calendar", required_columns={"date"},
        )


def test_the_ex_date_not_the_announcement_date_decides_window_membership(tmp_path: Path) -> None:
    """The window filter is on `ex_date`, and an announcement may precede it.

    The disclosure store is announcement-anchored, so a partition dated before the
    window is ordinary; an ex-date inside the window must still be included, and an
    announcement inside the window whose ex-date lands after it must still be
    excluded.  Getting this backwards either widens the exclusion set past the
    window or silently drops this window's event.
    """
    _canonical_bars(tmp_path, SESSIONS)
    # Announced before the window, ex-date inside it.
    _dividend_store(tmp_path / "dividend-store", dt.date(2026, 1, 2))
    report = _run(tmp_path, tmp_path / "dividend-store", start=SESSIONS[0], end=SESSIONS[-1])
    store = BacktestMarketDatasetStore(tmp_path)
    binding = report["bindings"]["corporate_action_binding"]
    corporate = store.read_frame(
        binding["dataset_generation_id"], binding["data_checksum_sha256"],
        dataset_type="corporate_action", required_columns={"date", "instrument"},
    )
    assert set(corporate["date"]) == {"2026-01-06"}

    # Extend the window past the second ex-date: it now joins.
    wider = _run(tmp_path, tmp_path / "dividend-store", start=SESSIONS[0], end=dt.date(2026, 1, 12))
    wider_corporate = store.read_frame(
        wider["bindings"]["corporate_action_binding"]["dataset_generation_id"],
        wider["bindings"]["corporate_action_binding"]["data_checksum_sha256"],
        dataset_type="corporate_action", required_columns={"date", "instrument"},
    )
    assert set(wider_corporate["date"]) == {"2026-01-06", "2026-01-09"}
    # still no 预案 row, and still no ex-date from any other announcement
    assert set(wider_corporate["instrument"]) == {"600000.XSHG", "600519.XSHG"}


def test_an_all_trading_window_publishes_an_explicit_negative_row(tmp_path: Path) -> None:
    """A window with no suspensions is a state, not a missing input.

    The store refuses an empty frame, so an all-trading window must publish an
    explicit "nothing suspended" marker rather than failing: a genuinely
    event-free window has to remain publishable and readable.
    """
    for index, session in enumerate(SESSIONS):
        directory = tmp_path / "canonical" / "bars_daily" / "v2" / f"date={session.isoformat()}"
        directory.mkdir(parents=True)
        import hashlib

        from uq.contracts.canonical_v2 import file_sha256_bytes

        frame = pd.DataFrame(
            [{"instrument": "600000.XSHG", "session_date": session, "open": 10.0,
              "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1000.0,
              "amount": 10000.0, "status": "trading"}]
        )
        data_path = directory / "data.parquet"
        frame.to_parquet(data_path, index=False)
        (directory / "manifest.json").write_text(json.dumps({
            "manifest_version": 1, "dataset": "bars_daily", "schema_version": "v2",
            "partition_date": session.isoformat(),
            "generation_id": hashlib.sha256(str(index).encode()).hexdigest(),
            "row_count": 1, "data_checksum_sha256": file_sha256_bytes(data_path.read_bytes()),
            "columns": list(frame.columns), "quality_report_checksum": "0" * 64,
        }))
    _dividend_store(tmp_path / "dividend-store", dt.date(2026, 1, 2))
    report = _run(tmp_path, tmp_path / "dividend-store", start=SESSIONS[0], end=SESSIONS[-1])
    store = BacktestMarketDatasetStore(tmp_path)
    suspension = store.read_frame(
        report["bindings"]["suspension_binding"]["dataset_generation_id"],
        report["bindings"]["suspension_binding"]["data_checksum_sha256"],
        dataset_type="suspension", required_columns={"date", "instrument", "suspended"},
    )
    assert len(suspension) == 1
    assert not suspension["suspended"].astype(bool).any()
    # and the backtest's own derivation of suspended names yields the empty set
    suspended = suspension.loc[suspension["suspended"].astype(bool)]
    assert set(suspended["instrument"]) == set()
