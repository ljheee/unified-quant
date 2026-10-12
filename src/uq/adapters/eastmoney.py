from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import urllib.parse
import urllib.request

from ..errors import IndependentCalendarError


KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# f51 is the bar date, f53 the close; the rest of the payload is not needed to
# read a session list.
KLINE_FIELDS = "f51,f53"
CSI300_SECID = "1.000300"


def parse_index_kline_sessions(payload: object) -> list[date]:
    """Read the session dates out of an EastMoney index-kline payload."""
    if not isinstance(payload, dict):
        raise IndependentCalendarError("eastmoney calendar payload is not a JSON object")
    if payload.get("rc") not in (0, None):
        raise IndependentCalendarError(f"eastmoney calendar returned rc={payload.get('rc')!r}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise IndependentCalendarError("eastmoney calendar payload carries no data block")
    klines = data.get("klines")
    if not isinstance(klines, list):
        raise IndependentCalendarError("eastmoney calendar payload carries no klines")
    sessions: list[date] = []
    for row in klines:
        if not isinstance(row, str) or not row:
            continue
        stamp = row.split(",", 1)[0].strip()
        try:
            sessions.append(date.fromisoformat(stamp))
        except ValueError as exc:
            raise IndependentCalendarError(f"eastmoney calendar row is not dated: {row!r}") from exc
    return sorted(set(sessions))


class EastMoneySessionProvider:
    """Vendor-independent A-share session provider over EastMoney index klines.

    The v2 ingest link takes its bars from Tushare/TDX; this provider reads the
    trading sessions from a third vendor (EastMoney), so a session list that is
    wrong on either side cannot cancel itself out. Sessions are derived from an
    index series (default CSI 300, ``1.000300``), which is published on every
    session and never suspended.

    The result is a *cached, self-certifying* answer: the provider only
    certifies the range its payload actually covers, so a window that reaches
    past the newest published bar raises instead of being answered with an
    empty (and coverage-passing) session set.

    The provider is a plain callable, so it plugs into ``TradingCalendar``
    without the calendar module depending on this adapter.
    """

    provenance = "eastmoney_index_kline_v1"

    def __init__(
        self,
        secid: str = CSI300_SECID,
        *,
        timeout: float = 20.0,
        lookback_days: int = 400,
        opener: object | None = None,
        user_agent: str = "unified-quant/0.1 (session calendar)",
    ) -> None:
        if lookback_days < 0:
            raise ValueError("lookback_days must not be negative")
        self.secid = secid
        self.timeout = timeout
        self.lookback_days = lookback_days
        self.user_agent = user_agent
        self._opener = opener or self._open_url
        self._sessions: list[date] = []
        # The contiguous range the last payload certified: the fetch window's
        # start through the newest session it carried.
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
        fetch_start = start - timedelta(days=self.lookback_days)
        if self._certified is not None:
            fetch_start = min(fetch_start, self._certified[0])
        fetch_end = date.today()
        url = self._build_url(fetch_start, fetch_end)
        raw = self._read(url)
        sessions = parse_index_kline_sessions(self._decode(raw, url))
        if not sessions:
            raise IndependentCalendarError(
                f"eastmoney calendar returned no sessions for {fetch_start}..{fetch_end}; coverage cannot be verified"
            )
        newest = sessions[-1]
        if end > newest:
            # Either the window is in the future or the vendor has not
            # published it yet. Answering with an empty set would make the
            # router's expected-dates check pass vacuously.
            raise IndependentCalendarError(
                f"eastmoney calendar newest session {newest.isoformat()} does not cover requested window end {end.isoformat()}"
            )
        self._sessions = sessions
        self._certified = (fetch_start, newest)
        self.evidence = {
            "provenance": self.provenance,
            "secid": self.secid,
            "url": url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "payload_sha256": hashlib.sha256(raw).hexdigest(),
            "sessions": len(sessions),
            "certified_start": fetch_start.isoformat(),
            "certified_end": newest.isoformat(),
        }

    def _build_url(self, start: date, end: date) -> str:
        query = urllib.parse.urlencode(
            {
                "secid": self.secid,
                "fields1": "f1,f2,f3,f4,f5",
                "fields2": KLINE_FIELDS,
                "klt": "101",
                "fqt": "0",
                "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
            }
        )
        return f"{KLINE_URL}?{query}"

    def _read(self, url: str) -> bytes:
        try:
            return self._opener(url, self.timeout, self.user_agent)
        except IndependentCalendarError:
            raise
        except Exception as exc:
            raise IndependentCalendarError(f"eastmoney calendar is unreachable: {exc}") from exc

    @staticmethod
    def _decode(raw: bytes, url: str) -> object:
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise IndependentCalendarError(f"eastmoney calendar response is not JSON: {url}") from exc

    @staticmethod
    def _open_url(url: str, timeout: float, user_agent: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
