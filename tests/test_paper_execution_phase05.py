from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.execution.market_dataset_publisher import MarketDatasetPublisher


def _publisher(tmp_path: Path) -> MarketDatasetPublisher:
    return MarketDatasetPublisher(tmp_path / "market-data-store")


def test_calendar_snapshot_publish_and_crosscheck(tmp_path: Path) -> None:
    store = _publisher(tmp_path)
    sessions = [date(2025, 6, day) for day in (2, 3, 4, 5, 6)]
    manifest = store.publish_calendar_snapshot(
        partition_date=date(2025, 6, 30),
        window_start=date(2025, 6, 1),
        window_end=date(2025, 6, 30),
        sessions=sessions,
        primary_source="akshare_sina",
        independent_source="eastmoney_index_kline",
    )
    assert manifest["family"] == "trading_calendar_snapshot_v1"
    assert manifest["row_count"] == 5
    assert manifest["cross_check_result"] == "agreement"
    read = store.read_manifest("trading_calendar_snapshot_v1", date(2025, 6, 30))
    assert read["generation_id"] == manifest["generation_id"]
    with pytest.raises(ContractError, match="already published"):
        store.publish_calendar_snapshot(
            partition_date=date(2025, 6, 30),
            window_start=date(2025, 6, 1),
            window_end=date(2025, 6, 30),
            sessions=sessions,
            primary_source="akshare_sina",
            independent_source="eastmoney_index_kline",
        )


def test_suspension_snapshot_primary_source_is_bars_status(tmp_path: Path) -> None:
    store = _publisher(tmp_path)
    frame = pd.DataFrame({
        "instrument": ["600000.XSHG", "000001.XSHE"],
        "session_date": ["2025-06-02", "2025-06-02"],
        "status": ["suspended", "normal"],
    })
    manifest = store.publish_suspension_snapshot(
        partition_date=date(2025, 6, 30),
        window_start=date(2025, 6, 1),
        window_end=date(2025, 6, 30),
        frame=frame,
    )
    assert manifest["family"] == "suspension_snapshot_v1"
    assert manifest["primary_source"] == "bars_daily_v2_status"
    assert manifest["reconciliation_source"] == "tushare_suspend_d"
    assert manifest["row_count"] == 2


def test_corporate_action_set_digest_recomputes_from_dividend_store(tmp_path: Path) -> None:
    store = _publisher(tmp_path)
    for day in (10, 20):
        frame = pd.DataFrame({"session_date": [f"2025-06-{day}"]})
        store.publish_suspension_snapshot(
            partition_date=date(2025, 6, day),
            window_start=date(2025, 6, 1),
            window_end=date(2025, 6, 30),
            frame=frame,
        )
    binding = store.build_set_binding(
        "suspension_snapshot_v1", window_start=date(2025, 6, 1), window_end=date(2025, 6, 30)
    )
    assert binding["member_count"] == 2
    assert binding["family"] == "suspension_snapshot_v1"
    store.verify_set_binding(binding)
    tampered = dict(binding)
    tampered["set_digest_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="partition set digest mismatch"):
        store.verify_set_binding(tampered)


def test_set_digest_changes_when_member_is_tampered(tmp_path: Path) -> None:
    store = _publisher(tmp_path)
    frame = pd.DataFrame({"session_date": ["2025-06-10"]})
    store.publish_suspension_snapshot(
        partition_date=date(2025, 6, 10),
        window_start=date(2025, 6, 1),
        window_end=date(2025, 6, 30),
        frame=frame,
    )
    binding = store.build_set_binding(
        "suspension_snapshot_v1", window_start=date(2025, 6, 1), window_end=date(2025, 6, 30)
    )
    data_path = tmp_path / "market-data-store" / "suspension_snapshot_v1" / "2025-06-10" / "data.parquet"
    frame2 = pd.DataFrame({"session_date": ["2025-06-11"]})
    frame2.to_parquet(data_path, index=False)
    new_triples = store.read_partition_triples("suspension_snapshot_v1", date(2025, 6, 1), date(2025, 6, 30))
    assert new_triples[0]["data_checksum_sha256"] != binding["set_digest_sha256"]


def test_window_before_first_visible_snapshot_fails_closed(tmp_path: Path) -> None:
    store = _publisher(tmp_path)
    with pytest.raises(ContractError, match="no published"):
        store.build_set_binding(
            "trading_calendar_snapshot_v1", window_start=date(2024, 1, 1), window_end=date(2024, 12, 31)
        )


def _canonical_bars(tmp_path: Path):
    """Write a canonical bars_daily/v2 partition and return (path, manifest-ish)."""
    import hashlib

    directory = tmp_path / "canonical" / "bars_daily" / "v2" / "date=2025-09-22"
    directory.mkdir(parents=True)
    frame = pd.DataFrame({
        "instrument": ["600000.XSHG", "000001.XSHE"],
        "session_date": pd.to_datetime(["2025-09-22", "2025-09-22"]),
        "open": [10.0, 11.0],
        "close": [10.5, 11.5],
        "volume": [1000.0, 2000.0],
        "status": ["trading", "trading"],
        "limit_up": [11.0, 12.1],
        "limit_down": [9.0, 9.9],
        "adj_factor": [1.0, 2.0],
    })
    data_path = directory / "data.parquet"
    frame.to_parquet(data_path, index=False)
    return data_path, hashlib.sha256(data_path.read_bytes()).hexdigest(), list(frame.columns), len(frame)


def test_bars_partition_publishes_a_byte_identical_canonical_copy(tmp_path: Path) -> None:
    store = _publisher(tmp_path)
    data_path, checksum, columns, row_count = _canonical_bars(tmp_path)
    manifest = store.publish_bars_partition(
        partition_date=date(2025, 9, 22),
        window_start=date(2025, 9, 1),
        window_end=date(2025, 9, 30),
        source_data_path=data_path,
        source_generation_id="a" * 64,
        source_data_checksum_sha256=checksum,
        columns=columns,
        row_count=row_count,
    )
    assert manifest["family"] == "bars_daily_v2"
    assert manifest["data_checksum_sha256"] == checksum
    published = tmp_path / "market-data-store" / "bars_daily_v2" / "2025-09-22" / "data.parquet"
    assert published.read_bytes() == data_path.read_bytes()

    binding = store.build_set_binding(
        "bars_daily_v2", window_start=date(2025, 9, 1), window_end=date(2025, 9, 30)
    )
    assert binding["member_count"] == 1
    store.verify_set_binding(binding)

    read_manifest, frame = store.read_partition_frame("bars_daily_v2", date(2025, 9, 22))
    assert read_manifest["generation_id"] == manifest["generation_id"]
    assert len(frame) == row_count

    with pytest.raises(ContractError, match="already published"):
        store.publish_bars_partition(
            partition_date=date(2025, 9, 22),
            window_start=date(2025, 9, 1),
            window_end=date(2025, 9, 30),
            source_data_path=data_path,
            source_generation_id="a" * 64,
            source_data_checksum_sha256=checksum,
            columns=columns,
            row_count=row_count,
        )


def test_bars_partition_rejects_a_canonical_checksum_mismatch(tmp_path: Path) -> None:
    store = _publisher(tmp_path)
    data_path, _checksum, columns, row_count = _canonical_bars(tmp_path)
    with pytest.raises(ContractError, match="do not match their manifest checksum"):
        store.publish_bars_partition(
            partition_date=date(2025, 9, 22),
            window_start=date(2025, 9, 1),
            window_end=date(2025, 9, 30),
            source_data_path=data_path,
            source_generation_id="a" * 64,
            source_data_checksum_sha256="b" * 64,
            columns=columns,
            row_count=row_count,
        )


def test_corporate_action_partition_derives_the_exclusion_set(tmp_path: Path) -> None:
    store = _publisher(tmp_path)
    frame = pd.DataFrame({
        "date": ["2025-09-22", "2025-09-22"],
        "instrument": ["600000.XSHG", "000001.XSHE"],
    })
    manifest = store.publish_corporate_action_snapshot(
        partition_date=date(2025, 9, 22),
        window_start=date(2025, 9, 1),
        window_end=date(2025, 9, 30),
        frame=frame,
        source_generation_ids=["c" * 64],
        event_count=2,
    )
    assert manifest["family"] == "dividend_event_v1"
    assert manifest["columns"] == ["date", "instrument"]
    assert manifest["row_count"] == 2
    binding = store.build_set_binding(
        "dividend_event_v1", window_start=date(2025, 9, 1), window_end=date(2025, 9, 30)
    )
    store.verify_set_binding(binding)

    with pytest.raises(ContractError, match="must carry the partition ex-date"):
        store.publish_corporate_action_snapshot(
            partition_date=date(2025, 9, 23),
            window_start=date(2025, 9, 1),
            window_end=date(2025, 9, 30),
            frame=frame,
            source_generation_ids=["c" * 64],
            event_count=2,
        )
    with pytest.raises(ContractError, match="source generation ids"):
        store.publish_corporate_action_snapshot(
            partition_date=date(2025, 9, 24),
            window_start=date(2025, 9, 1),
            window_end=date(2025, 9, 30),
            frame=frame.assign(date="2025-09-24"),
            source_generation_ids=[],
            event_count=2,
        )


def test_read_partition_frame_rejects_tampered_bytes(tmp_path: Path) -> None:
    store = _publisher(tmp_path)
    frame = pd.DataFrame({"date": ["2025-09-22"], "instrument": ["600000.XSHG"]})
    store.publish_corporate_action_snapshot(
        partition_date=date(2025, 9, 22),
        window_start=date(2025, 9, 1),
        window_end=date(2025, 9, 30),
        frame=frame,
        source_generation_ids=["c" * 64],
        event_count=1,
    )
    data_path = tmp_path / "market-data-store" / "dividend_event_v1" / "2025-09-22" / "data.parquet"
    tampered = pd.DataFrame({"date": ["2025-09-22"], "instrument": ["000001.XSHE"]})
    tampered.to_parquet(data_path, index=False)
    with pytest.raises(ContractError, match="tampered dividend_event_v1 partition"):
        store.read_partition_frame("dividend_event_v1", date(2025, 9, 22))


def test_calendar_snapshot_without_crosscheck_cannot_record_agreement(tmp_path: Path) -> None:
    """m12: an unchecked independent source must not record a false agreement."""
    store = _publisher(tmp_path)
    sessions = [date(2025, 6, 2)]
    with pytest.raises(ContractError, match="cross-check was not performed"):
        store.publish_calendar_snapshot(
            partition_date=date(2025, 6, 30),
            window_start=date(2025, 6, 1),
            window_end=date(2025, 6, 30),
            sessions=sessions,
            primary_source="akshare_sina",
            independent_source="not_checked",
        )


def test_calendar_snapshot_without_crosscheck_records_skipped(tmp_path: Path) -> None:
    """m12: the honest skipped outcome is recorded and validates against the contract."""
    store = _publisher(tmp_path)
    sessions = [date(2025, 6, 2)]
    manifest = store.publish_calendar_snapshot(
        partition_date=date(2025, 6, 30),
        window_start=date(2025, 6, 1),
        window_end=date(2025, 6, 30),
        sessions=sessions,
        primary_source="akshare_sina",
        independent_source="not_checked",
        cross_check_result="skipped",
    )
    assert manifest["cross_check_result"] == "skipped"
    read = store.read_manifest("trading_calendar_snapshot_v1", date(2025, 6, 30))
    assert read["cross_check_result"] == "skipped"
