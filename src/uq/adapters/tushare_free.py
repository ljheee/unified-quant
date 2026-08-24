from __future__ import annotations

import os
from datetime import datetime
from datetime import date
import pandas as pd

from ..errors import ContractError
from ..sources.fetch import FetchResult, FetchStatus


class TushareFreeAdapter:
    """Adapter limited to endpoints available to low-point Tushare accounts."""

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

    def fetch(self, dataset: str, instruments: list[str], start: str, end: str, fields: list[str]) -> FetchResult:
        fetched_at = datetime.now()
        if dataset != "bars_daily":
            return self._unsupported(dataset, fields, fetched_at)
        try:
            client = self._client()
        except ContractError:
            raise
        except Exception as exc:
            status, retryable, message = self._map_error(exc)
            return FetchResult(pd.DataFrame(), status, self.source_name, dataset, "research-v1", frozenset(fields), frozenset(), fetched_at, retryable=retryable, errors=(message,))
        raw_parts: list[pd.DataFrame] = []
        errors: list[str] = []
        provider_codes = [
            instrument.replace(".XSHG", ".SH").replace(".XSHE", ".SZ")
            for instrument in instruments
        ]
        batches = [provider_codes[index:index + self.BATCH_SIZE] for index in range(0, len(provider_codes), self.BATCH_SIZE)] or [[]]
        for batch in batches:
            codes = ",".join(batch)
            try:
                raw_parts.append(client.daily(
                    ts_code=codes or None,
                    start_date=start.replace("-", ""),
                    end_date=end.replace("-", ""),
                ))
            except Exception as exc:
                status, retryable, message = self._map_error(exc)
                return FetchResult(pd.DataFrame(), status, self.source_name, dataset, "research-v1", frozenset(fields), frozenset(), fetched_at, retryable=retryable, errors=(message,))
        raw = pd.concat(raw_parts, ignore_index=True) if raw_parts else pd.DataFrame()
        normalized = self._normalize(raw)
        missing_fields = set(fields) - set(normalized.columns)
        status = FetchStatus.PARTIAL_FIELDS if missing_fields else FetchStatus.SUCCESS
        warnings = tuple(f"missing fields: {sorted(missing_fields)}") if missing_fields else ()
        delivered = frozenset(set(fields) & set(normalized.columns))
        observed_start = min(normalized["datetime"]) if not normalized.empty else None
        observed_end = max(normalized["datetime"]) if not normalized.empty else None
        empty = normalized.empty
        return FetchResult(
            normalized,
            FetchStatus.EMPTY if empty else status,
            self.source_name,
            dataset,
            "research-v1",
            frozenset(fields),
            delivered,
            fetched_at,
            observed_start.date() if observed_start is not None else None,
            observed_end.date() if observed_end is not None else None,
            warnings=warnings,
            errors=tuple(errors),
            metadata={
                "provider": "tushare.pro",
                "endpoint": "daily",
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
    def _normalize(raw: pd.DataFrame) -> pd.DataFrame:
        columns = ["instrument", "datetime", "open", "high", "low", "close", "volume", "amount"]
        if raw.empty:
            return pd.DataFrame(columns=columns)
        frame = raw.copy()
        frame["instrument"] = frame["ts_code"].str.replace(".SH", ".XSHG", regex=False).str.replace(".SZ", ".XSHE", regex=False)
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
