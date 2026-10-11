from __future__ import annotations

import os
from datetime import datetime
from datetime import date
from typing import Any
import pandas as pd

from ..errors import ContractError
from ..sources.fetch import FetchResult, FetchStatus

_EXCHANGE_SUFFIXES = {"SH": "XSHG", "SZ": "XSHE", "BJ": "XBSE"}
_INSTRUMENT_SUFFIXES = {value: key for key, value in _EXCHANGE_SUFFIXES.items()}


def _to_instrument(ts_code: str) -> str:
    code, _, exchange = ts_code.partition(".")
    if exchange not in _EXCHANGE_SUFFIXES:
        raise ValueError(f"unsupported Tushare code: {ts_code}")
    return f"{code}.{_EXCHANGE_SUFFIXES[exchange]}"


def _to_provider_code(instrument: str) -> str:
    code, _, suffix = instrument.partition(".")
    if suffix not in _INSTRUMENT_SUFFIXES:
        raise ValueError(f"unsupported instrument: {instrument}")
    return f"{code}.{_INSTRUMENT_SUFFIXES[suffix]}"


class TushareAdapter:
    """Shared Tushare access: token handling, rotation, and bars/index/calendar fetches."""

    BATCH_SIZE = 50

    def __init__(self, token_env: str = "TUSHARE_TOKENS") -> None:
        self.source_name = "tushare"
        self.token_env = token_env
        self._token_index = 0
        self._calendar_cache: dict[tuple[str, str, str], set[date]] = {}

    def _tokens(self) -> list[str]:
        tokens: list[str] = []
        if self.token_env == "TUSHARE_TOKENS":
            raw_tokens = os.environ.get("TUSHARE_TOKENS", "")
        else:
            raw_tokens = os.environ.get(self.token_env, "")
        if raw_tokens:
            tokens.extend(token.strip() for token in raw_tokens.split(",") if token.strip())
        if self.token_env == "TUSHARE_TOKENS":
            legacy_token = os.environ.get("TUSHARE_TOKEN", "")
            if legacy_token and not tokens:
                tokens.append(legacy_token.strip())
        unique: list[str] = []
        for token in tokens:
            if token not in unique:
                unique.append(token)
        return unique

    def _client(self):
        try:
            import tushare as ts
        except ImportError as exc:
            raise ContractError("optional dependency tushare is not installed") from exc
        tokens = self._tokens()
        if not tokens:
            raise ContractError(f"missing Tushare token environment variable: {self.token_env}")
        token = tokens[self._token_index % len(tokens)]
        self._token_index += 1
        ts.set_token(token)
        return ts.pro_api(token)

    def health_probe(self, exchange: str = "SSE") -> dict[str, object]:
        """Probe the real upstream path with one cheap calendar call.

        ``DailyIngestPipeline`` treats ``ok is not True`` as unhealthy, so a
        primary source without a probe can never publish. The probe must be an
        actual round trip — a token-presence check would report healthy while
        every data call still fails.
        """
        session = date.today().isoformat()
        report: dict[str, object] = {
            "provider": self.source_name,
            "token_env": self.token_env,
            "fetched_at": datetime.now().isoformat(),
        }
        try:
            frame = self._call("trade_cal", exchange=exchange, start_date=session, end_date=session)
        except Exception as exc:
            report["ok"] = False
            report["error"] = str(exc)
            return report
        columns = set(getattr(frame, "columns", ()))
        ok = frame is not None and not frame.empty and {"cal_date", "is_open"} <= columns
        report["ok"] = ok
        report["rows"] = int(len(frame)) if frame is not None else 0
        if not ok:
            report["error"] = "trade_cal returned an unusable envelope"
        return report

    def trade_dates(self, start: str, end: str, exchange: str = "SSE") -> set[date]:
        cache_key = (exchange, start, end)
        if cache_key in self._calendar_cache:
            return self._calendar_cache[cache_key]
        client = self._client()
        try:
            raw = client.trade_cal(
                exchange=exchange,
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
            )
        except Exception as exc:
            status, retryable, message = self._map_error(exc)
            error = ContractError(message)
            error.status = status.value
            error.retryable = retryable
            raise error from exc
        opened = raw["is_open"].astype(str).isin({"1", "1.0", "True", "true"})
        sessions = {
            value.date()
            for value in pd.to_datetime(raw.loc[opened, "cal_date"], format="%Y%m%d")
        }
        self._calendar_cache[cache_key] = sessions
        return sessions

    def _call(self, endpoint: str, **params: Any) -> pd.DataFrame:
        """Call one Tushare pro endpoint, mapping provider failures onto ContractError."""
        client = self._client()
        try:
            return getattr(client, endpoint)(
                **{
                    key: (value.replace("-", "") if isinstance(value, str) and key.endswith(("_start", "_end", "_date")) else value)
                    for key, value in params.items()
                }
            )
        except Exception as exc:
            status, retryable, message = self._map_error(exc)
            error = ContractError(f"{endpoint} failed: {message}")
            error.status = status.value
            error.retryable = retryable
            raise error from exc

    def index_weight(self, index_code: str, start: str, end: str) -> pd.DataFrame:
        return self._call("index_weight", index_code=index_code, start_date=start, end_date=end)

    def adj_factor(self, *, trade_date: str | None = None, ts_code: str | None = None,
                   start: str | None = None, end: str | None = None) -> pd.DataFrame:
        return self._call("adj_factor", trade_date=trade_date, ts_code=ts_code,
                          start_date=start, end_date=end)

    def stk_limit(self, *, trade_date: str) -> pd.DataFrame:
        return self._call("stk_limit", trade_date=trade_date)

    def suspension(self, *, trade_date: str, suspend_type: str = "S") -> pd.DataFrame:
        return self._call("suspend_d", trade_date=trade_date, suspend_type=suspend_type)

    def dividend(self, *, ann_date: str | None = None, ts_code: str | None = None) -> pd.DataFrame:
        return self._call("dividend", ann_date=ann_date, ts_code=ts_code)

    def stock_basic(self, *, exchange: str = "", list_status: str = "L") -> pd.DataFrame:
        return self._call("stock_basic", exchange=exchange, list_status=list_status)

    def delisted_instruments(self) -> set[str]:
        """Instruments the provider marks as delisted as of now (``list_status='D'``).

        This is a point-in-time truth, not an event date: at the measured account
        level ``stock_basic`` never returns ``out_date`` (it is dropped even when
        requested through ``fields``), ``namechange`` carried no delisting row for
        the one instrument that motivated this helper, and ``stk_premarket`` is not
        permitted. Callers may therefore only use it to excuse an absence that
        already looks total, never to date-bound one.
        """
        frame = self.stock_basic(list_status="D")
        if frame.empty:
            return set()
        delisted: set[str] = set()
        for code in frame["ts_code"]:
            try:
                delisted.add(_to_instrument(str(code)))
            except ValueError:
                # An unmappable code simply cannot excuse its own absence, which
                # leaves the gate flagging it: the fail-safe direction.
                continue
        return delisted
    @staticmethod
    def _schema_version_for(fields: list[str]) -> str:
        """bars_daily v2 is keyed by session_date; anything else keeps the research-v1 key.

        Comparator fetches ask for a field subset without any key field, so the
        version may not be inferable; those keep the historical research-v1 path.
        """
        return "v2" if "session_date" in set(fields) else "research-v1"

    def _daily_batch(
        self,
        client,
        codes: str,
        batch: list[str],
        start: str,
        end: str,
    ) -> pd.DataFrame:
        """Fetch one ``daily`` batch, refusing to accept a silent empty answer.

        Tushare raises on most transient failures, so an empty frame for a
        non-empty batch is indistinguishable from a dropped request. Measured in
        production: one 50-code batch of a 500-name session answered empty, the
        run published 451 of 500 instruments as ``published``, and only the
        training readiness gate noticed 22 sessions later. Retry once, then raise
        so the caller reports a retryable upstream error instead of publishing a
        silently short panel.
        """
        for _ in range(2):
            response = client.daily(
                ts_code=codes or None,
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
            )
            if not batch or response is None or not response.empty:
                return pd.DataFrame() if response is None else response
        raise RuntimeError(
            f"daily returned no rows for batch {batch[0]}..{batch[-1]} ({len(batch)} codes)"
        )

    def _reference_frames(self, start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame, set[tuple[str, object]]]:
        """Whole-market adjustment factors, price limits and suspensions for the window days."""
        days = sorted(self.trade_dates(start, end))
        stamp = lambda value: value.strftime("%Y%m%d")
        adj_parts: list[pd.DataFrame] = []
        limit_parts: list[pd.DataFrame] = []
        suspended: set[tuple[str, object]] = set()
        for day in days:
            for label, frame in (
                ("adj_factor", self.adj_factor(trade_date=stamp(day))),
                ("stk_limit", self.stk_limit(trade_date=stamp(day))),
                ("suspend_d", self.suspension(trade_date=stamp(day))),
            ):
                if frame.empty:
                    continue
                frame = frame.assign(instrument=frame["ts_code"].map(_to_instrument))
                if label == "adj_factor":
                    adj_parts.append(frame[["instrument", "trade_date", "adj_factor"]])
                elif label == "stk_limit":
                    limit_parts.append(frame[["instrument", "trade_date", "up_limit", "down_limit"]])
                else:
                    suspended.update(
                        (row.instrument, day) for row in frame[["instrument"]].itertuples()
                    )
        adj = (
            pd.concat(adj_parts, ignore_index=True)
            .assign(session_date=lambda frame: pd.to_datetime(frame["trade_date"], format="%Y%m%d").dt.normalize())
            [["instrument", "session_date", "adj_factor"]]
            if adj_parts else pd.DataFrame(columns=["instrument", "session_date", "adj_factor"])
        )
        limits = (
            pd.concat(limit_parts, ignore_index=True)
            .assign(session_date=lambda frame: pd.to_datetime(frame["trade_date"], format="%Y%m%d").dt.normalize())
            .rename(columns={"up_limit": "limit_up", "down_limit": "limit_down"})
            [["instrument", "session_date", "limit_up", "limit_down"]]
            if limit_parts else pd.DataFrame(columns=["instrument", "session_date", "limit_up", "limit_down"])
        )
        return adj, limits, suspended

    def fetch(self, dataset: str, instruments: list[str], start: str, end: str, fields: list[str]) -> FetchResult:
        fetched_at = datetime.now()
        if dataset != "bars_daily":
            return self._unsupported(dataset, fields, fetched_at)
        schema_version = self._schema_version_for(fields)
        try:
            client = self._client()
        except ContractError:
            raise
        except Exception as exc:
            status, retryable, message = self._map_error(exc)
            return FetchResult(pd.DataFrame(), status, self.source_name, dataset, schema_version, frozenset(fields), frozenset(), fetched_at, retryable=retryable, errors=(message,))
        raw_parts: list[pd.DataFrame] = []
        errors: list[str] = []
        provider_codes = [_to_provider_code(instrument) for instrument in instruments]
        batches = [provider_codes[index:index + self.BATCH_SIZE] for index in range(0, len(provider_codes), self.BATCH_SIZE)] or [[]]
        for batch in batches:
            codes = ",".join(batch)
            try:
                response = self._daily_batch(client, codes, batch, start, end)
                raw_parts.append(response)
            except Exception as exc:
                status, retryable, message = self._map_error(exc)
                return FetchResult(pd.DataFrame(), status, self.source_name, dataset, schema_version, frozenset(fields), frozenset(), fetched_at, retryable=retryable, errors=(message,))
        raw = pd.concat(raw_parts, ignore_index=True) if raw_parts else pd.DataFrame()
        if schema_version == "v2":
            try:
                adj, limits, suspended = self._reference_frames(start, end)
            except ContractError as exc:
                status, retryable, message = self._map_error(exc)
                return FetchResult(pd.DataFrame(), status, self.source_name, dataset, schema_version, frozenset(fields), frozenset(), fetched_at, retryable=retryable, errors=(message,))
            normalized = self._normalize_v2(raw, adj, limits, suspended)
        else:
            normalized = self._normalize(raw)
        unexplained: list[str] = []
        if schema_version == "v2" and instruments:
            # Report, never block: this call sees one session and only suspend_d,
            # and a multi-week halt loses its suspend_d rows partway through
            # (measured: 601989 stopped appearing in suspend_d on 2025-09-04 while
            # it stayed halted until its merger), so blocking here rejected 18 of
            # 23 legitimate sessions. The training readiness gate owns the
            # explained/unexplained verdict because it evaluates the whole window.
            halted: dict[str, set[object]] = {}
            for instrument, day in suspended:
                halted.setdefault(instrument, set()).add(day)
            window_days = set(self.trade_dates(start, end))
            present = set(normalized["instrument"]) if not normalized.empty else set()
            unexplained = [
                instrument
                for instrument in instruments
                if instrument not in present and not window_days <= halted.get(instrument, set())
            ]
        missing_fields = set(fields) - set(normalized.columns)
        status = FetchStatus.PARTIAL_FIELDS if missing_fields else FetchStatus.SUCCESS
        warnings = list(f"missing fields: {sorted(missing_fields)}") if missing_fields else []
        if unexplained:
            warnings.append(
                f"daily omitted {len(unexplained)} requested instruments that suspend_d does not "
                f"cover for the whole window: {sorted(unexplained)[:10]}"
            )
        delivered = frozenset(set(fields) & set(normalized.columns))
        key_field = "session_date" if schema_version == "v2" else "datetime"
        observed_start = min(normalized[key_field]) if not normalized.empty else None
        observed_end = max(normalized[key_field]) if not normalized.empty else None
        empty = normalized.empty
        return FetchResult(
            normalized,
            FetchStatus.EMPTY if empty else status,
            self.source_name,
            dataset,
            schema_version,
            frozenset(fields),
            delivered,
            fetched_at,
            observed_start.date() if observed_start is not None else None,
            observed_end.date() if observed_end is not None else None,
            warnings=tuple(warnings),
            errors=tuple(errors),
            metadata={
                "provider": "tushare.pro",
                "endpoint": "daily",
                "reference_endpoints": ["adj_factor", "stk_limit", "suspend_d"],
                "raw_payload": raw.to_csv(index=False).encode("utf-8"),
                "request": {"dataset": dataset, "instruments": instruments, "start": start, "end": end, "fields": fields},
            },
        )

    @staticmethod
    def _map_error(exc: Exception) -> tuple[FetchStatus, bool, str]:
        if isinstance(exc, (ImportError, ModuleNotFoundError)):
            return FetchStatus.UNSUPPORTED_REQUEST, False, str(exc)
        if isinstance(exc, ContractError):
            return FetchStatus.AUTH_FAILED, False, str(exc)
        text = str(exc).lower()
        if "token" in text or "auth" in text or "权限" in str(exc):
            return FetchStatus.AUTH_FAILED, False, str(exc)
        if "freq" in text or "每分钟" in str(exc) or "limit" in text:
            return FetchStatus.RATE_LIMITED, True, str(exc)
        return FetchStatus.UPSTREAM_ERROR, True, str(exc)

    @staticmethod
    def _unsupported(dataset: str, fields: list[str], fetched_at: datetime) -> FetchResult:
        return FetchResult(pd.DataFrame(), FetchStatus.UNSUPPORTED_REQUEST, "tushare", dataset, "research-v1", frozenset(fields), frozenset(), fetched_at, errors=("dataset unsupported",))

    @staticmethod
    def _normalize_v2(
        raw: pd.DataFrame,
        adj: pd.DataFrame,
        limits: pd.DataFrame,
        suspended: set[tuple[str, object]],
    ) -> pd.DataFrame:
        base = TushareAdapter._normalize(raw).rename(columns={"datetime": "session_date"})
        if base.empty:
            return base.assign(
                status=pd.Series(dtype=str),
                limit_up=pd.Series(dtype="float64"),
                limit_down=pd.Series(dtype="float64"),
                adj_factor=pd.Series(dtype="float64"),
            )
        frame = base.merge(adj, on=["instrument", "session_date"], how="left")
        frame = frame.merge(limits, on=["instrument", "session_date"], how="left")
        # A suspension row or a zero-volume bar both mean the instrument did not trade
        # that session; bars_daily.v2 only allows volume == 0 for non-trading status.
        keys = list(zip(frame["instrument"], frame["session_date"].dt.date))
        frame["status"] = [
            "suspended" if key in suspended or volume == 0 else "trading"
            for key, volume in zip(keys, frame["volume"])
        ]
        return frame[
            ["instrument", "session_date", "open", "high", "low", "close", "volume", "amount",
             "status", "limit_up", "limit_down", "adj_factor"]
        ]

    @staticmethod
    def _normalize(raw: pd.DataFrame) -> pd.DataFrame:
        columns = ["instrument", "datetime", "open", "high", "low", "close", "volume", "amount"]
        if raw.empty:
            return pd.DataFrame(columns=columns)
        frame = raw.copy()
        frame["instrument"] = frame["ts_code"].map(_to_instrument)
        frame["datetime"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d").dt.normalize()
        result = pd.DataFrame({
            "instrument": frame["instrument"],
            "datetime": frame["datetime"],
            "open": pd.to_numeric(frame["open"]),
            "high": pd.to_numeric(frame["high"]),
            "low": pd.to_numeric(frame["low"]),
            "close": pd.to_numeric(frame["close"]),
            "volume": pd.to_numeric(frame["vol"]) * 100.0,
            "amount": pd.to_numeric(frame["amount"]) * 1000.0,
        })
        return result.sort_values(["instrument", "datetime"]).reset_index(drop=True)
