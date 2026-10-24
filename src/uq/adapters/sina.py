"""Vendor-independent A-share session provider over Sina index klines.

The v2 ingest link takes its bars from Tushare/TDX; a *third* vendor has to
supply the trading sessions so that a wrong session list on either side cannot
cancel itself out.  This provider reads them from Sina's CSI300 index series,
which is published on every session and never suspended.

Why a second provider exists: ``push2his.eastmoney.com`` -- the host
``EastMoneySessionProvider`` reads -- was measured 2026-10-23 answering every
request with an empty TCP reply after a successful TLS handshake, while
``quote.eastmoney.com`` and ``push2.eastmoney.com`` on the same network returned
200.  The failure is host-specific and was confirmed from a browser, so it is
not a client defect.  Keeping the two providers side by side means an outage on
one vendor does not stop governed ingest.

The answer is *cached and self-certifying* exactly as the EastMoney one is: the
provider only certifies the range its payload actually covers, so a window that
reaches past the newest published bar raises instead of being answered with an
empty (and coverage-passing) session set.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import urllib.parse
import urllib.request

from ..errors import IndependentCalendarError


KLINE_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
# `day` is the bar date.  scale=240 is the daily bar; `ma=no` keeps the payload
# to plain bars so parsing stays trivial.
CSI300_SYMBOL = "sh000300"


def parse_index_kline_sessions(payload: object) -> list[date]:
    """Read the session dates out of a Sina index-kline payload."""
    if not isinstance(payload, list):
        raise IndependentCalendarError("sina calendar payload is not a JSON array")
    sessions: list[date] = []
    for row in payload:
        if not isinstance(row, dict):
            raise IndependentCalendarError("sina calendar row is not an object")
        stamp = row.get("day")
        if not isinstance(stamp, str) or not stamp:
            raise IndependentCalendarError("sina calendar row carries no day")
        try:
            sessions.append(date.fromisoformat(stamp.strip()))
        except ValueError as exc:
            raise IndependentCalendarError(f"sina calendar row is not dated: {stamp!r}") from exc
    if not sessions:
        raise IndependentCalendarError("sina calendar payload carries no bars")
    return sorted(set(sessions))


class SinaSessionProvider:
    """Vendor-independent A-share session provider over Sina index klines."""

    provenance = "sina_index_kline_v1"

    def __init__(
        self,
        symbol: str = CSI300_SYMBOL,
        *,
        timeout: float = 20.0,
        bars: int = 900,
        opener: object | None = None,
        user_agent: str = "unified-quant/0.1 (session calendar)",
    ) -> None:
        if bars <= 0:
            raise ValueError("bars must be positive")
        self.symbol = symbol
        self.timeout = timeout
        self.bars = bars
        self.user_agent = user_agent
        self._opener = opener or self._open_url
        self._sessions: list[date] = []
        # The contiguous range the last payload certified: its earliest
        # through its newest session.
        self._certified: tuple[date, date] | None = None
        self.evidence: dict[str, object] | None = None

    def between(self, start: date, end: date) -> set[date]:
        if start > end:
            return set()
        self._ensure(start, end)
        return {value for value in self._sessions if start <= value <= end}

    __call__ = between

    def _ensure(self, start: date, end: date) -> None:
        if self._certified is not None and self._certified[0] <= start and end <= self._certified[1]:
            return
        url = self._build_url()
        raw = self._read(url)
        sessions = parse_index_kline_sessions(self._decode(raw, url))
        newest = sessions[-1]
        if end > newest:
            # Either the window is in the future or the vendor has not published
            # it yet.  Answering with an empty set would make the router's
            # expected-dates check pass vacuously.
            raise IndependentCalendarError(
                f"sina calendar newest session {newest.isoformat()} does not cover requested window end {end.isoformat()}"
            )
        oldest = sessions[0]
        if start < oldest:
            # Symmetric to the newest-end guard: the payload is the vendor's
            # answer up to `bars` bars back, so an earliest bar after the
            # requested start means the vendor's history does not reach the
            # window.
            raise IndependentCalendarError(
                f"sina calendar earliest session {oldest.isoformat()} does not cover requested window start {start.isoformat()}"
            )
        self._sessions = sessions
        # The certified range is what the payload proves: its earliest through
        # its newest session, not the wider fetch window.
        self._certified = (oldest, newest)
        self.evidence = {
            "provenance": self.provenance,
            "symbol": self.symbol,
            "url": url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "payload_sha256": hashlib.sha256(raw).hexdigest(),
            "sessions": len(sessions),
            "certified_start": oldest.isoformat(),
            "certified_end": newest.isoformat(),
        }

    def _build_url(self) -> str:
        query = urllib.parse.urlencode({
            "symbol": self.symbol,
            "scale": "240",
            "ma": "no",
            "datalen": str(self.bars),
        })
        return f"{KLINE_URL}?{query}"

    def _read(self, url: str) -> bytes:
        try:
            return self._opener(url, self.timeout, self.user_agent)
        except IndependentCalendarError:
            raise
        except Exception as exc:
            raise IndependentCalendarError(f"sina calendar is unreachable: {exc}") from exc

    @staticmethod
    def _decode(raw: bytes, url: str) -> object:
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise IndependentCalendarError(f"sina calendar response is not JSON: {url}") from exc

    @staticmethod
    def _open_url(url: str, timeout: float, user_agent: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
