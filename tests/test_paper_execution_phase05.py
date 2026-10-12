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
