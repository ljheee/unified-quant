"""M6: unified governed-calendar selection and coverage semantics.

``PublishedPaperMarketData`` selects calendar partitions by partition date
inside the window (the set-binding convention) and requires their content
windows to chain across the whole request window.  Sessions are only ever
served from the selected set, so a store that cannot account for the window
fails closed with one clear refusal instead of serving a partial session
list whose bindings would die later.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from uq.errors import ContractError
from uq.execution.market_dataset_publisher import MarketDatasetPublisher
from uq.execution.market_source import PublishedPaperMarketData

WINDOW_START = date(2026, 1, 5)
WINDOW_END = date(2026, 2, 10)


def _publisher(tmp_path: Path) -> MarketDatasetPublisher:
    return MarketDatasetPublisher(tmp_path / "market-store")


def _publish_calendar(
    publisher: MarketDatasetPublisher,
    *,
    partition_date: date,
    window_start: date,
    window_end: date,
    sessions: list[date],
) -> None:
    publisher.publish_calendar_snapshot(
        partition_date=partition_date,
        window_start=window_start,
        window_end=window_end,
        sessions=sessions,
        primary_source="tushare:trade_cal",
        independent_source="eastmoney_index_kline_v1",
    )


def _market(publisher: MarketDatasetPublisher) -> PublishedPaperMarketData:
    return PublishedPaperMarketData(publisher, window_start=WINDOW_START, window_end=WINDOW_END)


def test_sessions_and_bindings_come_from_chained_selected_partitions(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path)
    january = [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19), date(2026, 1, 26)]
    february = [date(2026, 2, 2), date(2026, 2, 9), date(2026, 2, 10)]
    _publish_calendar(
        publisher, partition_date=date(2026, 1, 31),
        window_start=date(2026, 1, 1), window_end=date(2026, 1, 31), sessions=january,
    )
    _publish_calendar(
        publisher, partition_date=WINDOW_END,
        window_start=date(2026, 2, 1), window_end=date(2026, 2, 28), sessions=february,
    )
    market = _market(publisher)
    assert market.sessions() == january + february
    jan_binding = market.calendar_binding(date(2026, 1, 12))
    feb_binding = market.calendar_binding(date(2026, 2, 9))
    assert jan_binding["family"] == "trading_calendar_snapshot_v1"
    assert feb_binding["family"] == "trading_calendar_snapshot_v1"
    assert jan_binding["generation_id"] != feb_binding["generation_id"]
    jan_manifest = publisher.read_manifest("trading_calendar_snapshot_v1", date(2026, 1, 31))
    assert jan_binding["generation_id"] == jan_manifest["generation_id"]
    assert jan_binding["manifest_digest_sha256"] == jan_manifest["manifest_digest_sha256"]


def test_straddling_partition_is_a_single_clear_refusal(tmp_path: Path) -> None:
    """A partition whose content covers the window tail but whose partition
    date falls outside the window is not selected: the run refuses instead of
    serving sessions whose set binding would exclude the very partition they
    came from."""
    publisher = _publisher(tmp_path)
    _publish_calendar(
        publisher, partition_date=date(2026, 1, 31),
        window_start=date(2026, 1, 1), window_end=date(2026, 1, 31),
        sessions=[date(2026, 1, 5)],
    )
    _publish_calendar(
        publisher, partition_date=date(2026, 2, 28),
        window_start=date(2026, 2, 1), window_end=date(2026, 2, 28),
        sessions=[date(2026, 2, 2), date(2026, 2, 10)],
    )
    market = _market(publisher)
    with pytest.raises(ContractError, match="do not cover the paper window"):
        market.sessions()
    with pytest.raises(ContractError, match="do not cover the paper window"):
        market.calendar_binding(date(2026, 2, 10))


def test_a_coverage_gap_between_partitions_fails_closed(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path)
    _publish_calendar(
        publisher, partition_date=date(2026, 1, 31),
        window_start=date(2026, 1, 1), window_end=date(2026, 1, 31),
        sessions=[date(2026, 1, 5)],
    )
    _publish_calendar(
        publisher, partition_date=WINDOW_END,
        window_start=date(2026, 2, 5), window_end=date(2026, 2, 28),
        sessions=[date(2026, 2, 10)],
    )
    market = _market(publisher)
    with pytest.raises(ContractError, match="do not cover the paper window"):
        market.sessions()


def test_an_empty_store_fails_closed(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path)
    market = _market(publisher)
    with pytest.raises(ContractError, match="no governed trading calendar snapshot covers"):
        market.sessions()


def test_calendar_binding_rejects_a_session_outside_every_content_window(tmp_path: Path) -> None:
    publisher = _publisher(tmp_path)
    _publish_calendar(
        publisher, partition_date=WINDOW_END,
        window_start=WINDOW_START, window_end=WINDOW_END,
        sessions=[WINDOW_START, WINDOW_END],
    )
    market = _market(publisher)
    binding = market.calendar_binding(WINDOW_START)
    assert binding["family"] == "trading_calendar_snapshot_v1"
    with pytest.raises(ContractError, match="no governed calendar partition covers session"):
        market.calendar_binding(date(2025, 12, 31))
