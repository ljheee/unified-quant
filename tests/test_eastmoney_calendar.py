from datetime import date, datetime, timezone
import json

import pytest

from uq.adapters.eastmoney import EastMoneySessionProvider, parse_index_kline_sessions
from uq.errors import IndependentCalendarError
from uq.market.calendar import TradingCalendar


def _payload(days: tuple[str, ...]) -> dict:
    return {
        "rc": 0,
        "data": {"code": "000300", "market": 1, "klines": [f"{day},4000.00" for day in days]},
    }


def _provider(days: tuple[str, ...], *, fail: bool = False) -> EastMoneySessionProvider:
    calls: list[str] = []

    def opener(url, timeout, user_agent):
        calls.append(url)
        if fail:
            raise OSError("connection refused")
        return json.dumps(_payload(days)).encode()

    provider = EastMoneySessionProvider(opener=opener)
    provider.calls = calls
    return provider


def test_parse_index_kline_sessions_reads_unique_sorted_dates():
    sessions = parse_index_kline_sessions(_payload(("2025-09-02", "2025-09-01", "2025-09-02")))
    assert sessions == [date(2025, 9, 1), date(2025, 9, 2)]


def test_parse_rejects_malformed_payloads():
    with pytest.raises(IndependentCalendarError, match="not a JSON object"):
        parse_index_kline_sessions("nope")
    with pytest.raises(IndependentCalendarError, match="no klines"):
        parse_index_kline_sessions({"rc": 0, "data": {}})
    with pytest.raises(IndependentCalendarError, match="not dated"):
        parse_index_kline_sessions({"rc": 0, "data": {"klines": ["not-a-date,1.0"]}})


def test_between_returns_sessions_inside_the_window():
    provider = _provider(("2025-09-01", "2025-09-02", "2025-09-05"))
    assert provider.between(date(2025, 9, 2), date(2025, 9, 4)) == {date(2025, 9, 2)}


def test_failed_transport_fails_closed():
    provider = _provider((), fail=True)
    with pytest.raises(IndependentCalendarError, match="unreachable"):
        provider.between(date(2025, 9, 1), date(2025, 9, 1))


def test_empty_payload_never_empties_the_expected_sessions():
    provider = _provider(())
    with pytest.raises(IndependentCalendarError, match="cannot be verified"):
        provider.between(date(2025, 9, 1), date(2025, 9, 1))


def test_window_past_the_newest_published_session_fails_closed():
    provider = _provider(("2025-09-01", "2025-09-02"))
    with pytest.raises(IndependentCalendarError, match="does not cover"):
        provider.between(date(2025, 9, 3), date(2025, 9, 4))


def test_window_before_the_earliest_published_session_fails_closed():
    # Symmetric to the newest-end guard: the vendor's history does not reach
    # this window, and an empty answer would certify "no trading days" for
    # sessions the provider simply cannot see.
    provider = _provider(("2025-09-01", "2025-09-02"))
    with pytest.raises(IndependentCalendarError, match="does not cover"):
        provider.between(date(2020, 1, 5), date(2020, 1, 9))


def test_window_starting_at_the_earliest_session_is_served():
    provider = _provider(("2025-09-01", "2025-09-02"))
    assert provider.between(date(2025, 9, 1), date(2025, 9, 2)) == {
        date(2025, 9, 1),
        date(2025, 9, 2),
    }
    # the certified range is what the payload proves, not the wider fetch window
    assert provider.evidence["certified_start"] == "2025-09-01"
    assert provider.evidence["certified_end"] == "2025-09-02"


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
    assert provider.evidence["provenance"] == "eastmoney_index_kline_v1"
    assert provider.evidence["certified_end"] == "2025-09-02"
    assert len(provider.evidence["payload_sha256"]) == 64
    fetched_at = provider.evidence["fetched_at"]
    assert datetime.fromisoformat(str(fetched_at)).tzinfo == timezone.utc


def test_calendar_wiring_uses_the_provider_provenance():
    provider = _provider(("2025-09-01",))
    calendar = TradingCalendar(provider=provider, provenance=provider.provenance)
    assert calendar.provenance == "eastmoney_index_kline_v1"
    assert calendar.between(date(2025, 9, 1), date(2025, 9, 1)) == {date(2025, 9, 1)}
