"""Contract tests for the Sina independent session provider.

The EastMoney provider's tests are the shape to match: the point is not that a
vendor is reachable but that an unreachable, stale or short answer can never
narrow the expected session set.
"""

from datetime import date, datetime, timezone
import json

import pytest

from uq.adapters.sina import SinaSessionProvider, parse_index_kline_sessions
from uq.errors import IndependentCalendarError
from uq.market.calendar import TradingCalendar


def _bars(days: tuple[str, ...]) -> list[dict]:
    return [{"day": day, "close": "4000.00"} for day in days]


def _provider(days: tuple[str, ...], *, fail: bool = False) -> SinaSessionProvider:
    calls: list[str] = []

    def opener(url, timeout, user_agent):
        calls.append(url)
        if fail:
            raise OSError("connection refused")
        return json.dumps(_bars(days)).encode()

    provider = SinaSessionProvider(opener=opener)
    provider.calls = calls
    return provider


def test_parse_reads_unique_sorted_dates():
    sessions = parse_index_kline_sessions(_bars(("2025-09-02", "2025-09-01", "2025-09-02")))
    assert sessions == [date(2025, 9, 1), date(2025, 9, 2)]


def test_parse_rejects_malformed_payloads():
    with pytest.raises(IndependentCalendarError, match="not a JSON array"):
        parse_index_kline_sessions({"day": "2025-09-01"})
    with pytest.raises(IndependentCalendarError, match="no day"):
        parse_index_kline_sessions([{"close": "1.0"}])
    with pytest.raises(IndependentCalendarError, match="not dated"):
        parse_index_kline_sessions([{"day": "not-a-date"}])
    with pytest.raises(IndependentCalendarError, match="no bars"):
        parse_index_kline_sessions([])


def test_between_returns_sessions_inside_the_window():
    provider = _provider(("2025-09-01", "2025-09-02", "2025-09-05"))
    assert provider.between(date(2025, 9, 2), date(2025, 9, 4)) == {date(2025, 9, 2)}


def test_failed_transport_fails_closed():
    provider = _provider((), fail=True)
    with pytest.raises(IndependentCalendarError, match="unreachable"):
        provider.between(date(2025, 9, 1), date(2025, 9, 1))


def test_window_past_the_newest_published_session_fails_closed():
    provider = _provider(("2025-09-01", "2025-09-02"))
    with pytest.raises(IndependentCalendarError, match="does not cover"):
        provider.between(date(2025, 9, 3), date(2025, 9, 4))


def test_window_before_the_earliest_published_session_fails_closed():
    provider = _provider(("2025-09-01", "2025-09-02"))
    with pytest.raises(IndependentCalendarError, match="does not cover"):
        provider.between(date(2020, 1, 5), date(2020, 1, 9))


def test_cached_answer_is_certified_and_recorded_as_evidence():
    provider = _provider(("2025-09-01", "2025-09-02"))
    assert provider.between(date(2025, 9, 1), date(2025, 9, 2)) == {
        date(2025, 9, 1),
        date(2025, 9, 2),
    }
    first_calls = list(provider.calls)
    assert provider.between(date(2025, 9, 2), date(2025, 9, 2)) == {date(2025, 9, 2)}
    assert provider.calls == first_calls
    assert provider.evidence is not None
    assert provider.evidence["provenance"] == "sina_index_kline_v1"
    assert provider.evidence["symbol"] == "sh000300"
    assert provider.evidence["certified_start"] == "2025-09-01"
    assert provider.evidence["certified_end"] == "2025-09-02"
    assert len(provider.evidence["payload_sha256"]) == 64
    fetched_at = provider.evidence["fetched_at"]
    assert datetime.fromisoformat(str(fetched_at)).tzinfo == timezone.utc


def test_calendar_wiring_uses_the_provider_provenance():
    provider = _provider(("2025-09-01",))
    calendar = TradingCalendar(provider=provider, provenance=provider.provenance)
    assert calendar.provenance == "sina_index_kline_v1"
    assert calendar.between(date(2025, 9, 1), date(2025, 9, 1)) == {date(2025, 9, 1)}


def test_the_url_asks_for_the_daily_index_series():
    provider = _provider(("2025-09-01",))
    provider.between(date(2025, 9, 1), date(2025, 9, 1))
    url = provider.calls[0]
    assert url.startswith("https://vip.stock.finance.sina.com.cn/")
    assert "symbol=sh000300" in url
    assert "scale=240" in url
    assert "ma=no" in url
    assert "datalen=900" in url
