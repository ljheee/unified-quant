from __future__ import annotations

from datetime import datetime
from datetime import date, timedelta
import time
import pandas as pd

from ..sources.fetch import FetchResult, FetchStatus


class MootdxSourceAdapter:
    """Research adapter for TDX standard quote servers via mootdx."""

    DEFAULT_SERVERS: tuple[tuple[str, int], ...] = (
        ("115.238.90.165", 7709),
        ("180.153.18.170", 7709),
        ("119.147.212.81", 7709),
    )

    def __init__(
        self,
        server: tuple[str, int] | None = None,
        timeout: int = 8,
        page_size: int = 800,
        servers: tuple[tuple[str, int], ...] | None = None,
        retries: int = 1,
        retry_backoff_seconds: float = 0.25,
    ):
        self.source_name = "tdx"
        self.servers = servers or ((server,) if server else self.DEFAULT_SERVERS)
        if not self.servers:
            raise ValueError("at least one TDX server is required")
        self.timeout = timeout
        if not 1 <= page_size <= 800:
            raise ValueError("page_size must be between 1 and 800")
        self.page_size = page_size
        if retries < 0:
            raise ValueError("retries must not be negative")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        self.retries = retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.server = self.servers[0]

    def _client(self):
        try:
            from mootdx.quotes import Quotes
        except ImportError as exc:
            raise RuntimeError("optional dependency mootdx is not installed") from exc
        failures: list[str] = []
        for candidate in self.servers:
            try:
                client = Quotes.factory(market="std", server=candidate, timeout=self.timeout)
                probe = client.bars(symbol="600000", frequency=9, start=0, offset=1)
                if probe is not None and not probe.empty:
                    self.server = candidate
                    return client
                failures.append(f"{candidate}: empty probe")
            except Exception as exc:
                failures.append(f"{candidate}: {exc}")
        raise RuntimeError("all TDX servers failed: " + "; ".join(failures))

    @staticmethod
    def market_for(symbol: str) -> int:
        if symbol.startswith(("60", "68")):
            return 1
        if symbol.startswith(("00", "30")):
            return 0
        raise ValueError(f"unsupported A-share symbol prefix: {symbol}")

    def health_probe(self, symbols: tuple[str, ...] = ("600000", "000001")) -> dict[str, object]:
        fetched_at = datetime.now().isoformat()
        report: dict[str, object] = {"server": self.server, "fetched_at": fetched_at, "checks": {}}
        try:
            client = self._client()
        except Exception as exc:
            report["ok"] = False
            report["error"] = f"client unavailable: {exc}"
            return report
        ok_checks: list[bool] = []
        for symbol in symbols:
            try:
                bars = client.bars(symbol=symbol, frequency=9, start=0, offset=10)
                ok = bars is not None and not bars.empty
                ok_checks.append(ok)
                report["checks"][symbol] = {"ok": ok, "shape": None if bars is None else bars.shape}
            except Exception as exc:
                ok_checks.append(False)
                report["checks"][symbol] = {"ok": False, "error": repr(exc)}
        try:
            xdxr = client.xdxr(symbol=symbols[0])
            xdxr_ok = xdxr is not None and not xdxr.empty
            report["xdxr"] = {"ok": xdxr_ok, "shape": None if xdxr is None else xdxr.shape}
            ok_checks.append(xdxr_ok)
        except Exception as exc:
            report["xdxr"] = {"ok": False, "error": repr(exc)}
            ok_checks.append(False)
        report["ok"] = all(ok_checks)
        try:
            client.close()
        except Exception:
            pass
        return report

    def _requested_bar_estimate(self, start: str, end: str) -> int | None:
        if not start or not end:
            return None
        try:
            days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        except ValueError as exc:
            raise ValueError("start/end must be ISO dates") from exc
        if days <= 0:
            raise ValueError("end must not precede start")
        # TDX start/offset counts backward from the latest session, not from
        # ``end``. A full page makes recent historical requests deterministic;
        # longer ranges remain covered by bounded pagination.
        return self.page_size
    def fetch(self, dataset: str, instruments: list[str], start: str, end: str, fields: list[str]) -> FetchResult:
        fetched_at = datetime.now()
        if dataset != "bars_daily":
            return FetchResult(pd.DataFrame(), FetchStatus.UNSUPPORTED_REQUEST, self.source_name, dataset, "research-v1", frozenset(fields), frozenset(), fetched_at, errors=("dataset unsupported",))
        frames: list[pd.DataFrame] = []
        warnings: list[str] = []
        errors: list[str] = []
        try:
            client = self._client()
        except Exception as exc:
            return FetchResult(
                pd.DataFrame(),
                FetchStatus.UPSTREAM_ERROR,
                self.source_name,
                dataset,
                "research-v1",
                frozenset(fields),
                frozenset(),
                fetched_at,
                retryable=True,
                errors=(f"client unavailable: {exc}",),
                metadata={"server": getattr(self, "server", self.servers[0])},
            )
        try:
            for instrument in instruments:
                code = instrument.split(".", 1)[0]
                try:
                    estimate = self._requested_bar_estimate(start, end) or self.page_size
                    remaining = estimate
                    offset = 0
                    instrument_frames: list[pd.DataFrame] = []
                    while remaining > 0:
                        size = min(self.page_size, remaining)
                        attempts = 0
                        while True:
                            try:
                                page = client.bars(symbol=code, frequency=9, start=offset, offset=size)
                                break
                            except Exception as exc:
                                attempts += 1
                                if attempts > self.retries:
                                    raise RuntimeError(
                                        f"{instrument} offset={offset} failed after {attempts} attempts: {exc}"
                                    ) from exc
                                warnings.append(f"retrying {instrument} offset={offset} after {exc}")
                                time.sleep(self.retry_backoff_seconds * (2 ** (attempts - 1)))
                        if getattr(page, "empty", True) or len(page) == 0:
                            warnings.append(f"no more history at offset={offset}: {instrument}")
                            break
                        local = page.copy()
                        if "datetime" not in local.columns:
                            local.index.name = "datetime"
                            local = local.reset_index()
                        local["instrument"] = instrument
                        instrument_frames.append(local)
                        if len(page) < size:
                            break
                        offset += size
                        remaining -= size
                    if not instrument_frames:
                        warnings.append(f"empty response: {instrument}")
                        continue
                    frames.extend(instrument_frames)
                except Exception as exc:
                    errors.append(f"fetch failed: {instrument}: {exc}")
        finally:
            try:
                client.close()
            except Exception:
                pass
        if not frames:
            if errors:
                return FetchResult(pd.DataFrame(), FetchStatus.UPSTREAM_ERROR, self.source_name, dataset, "research-v1", frozenset(fields), frozenset(), fetched_at, retryable=True, errors=tuple(errors), metadata={"server": getattr(self, "server", self.servers[0])})
            return FetchResult(pd.DataFrame(), FetchStatus.EMPTY, self.source_name, dataset, "research-v1", frozenset(fields), frozenset(), fetched_at, retryable=True, warnings=tuple(warnings), metadata={"server": getattr(self, "server", self.servers[0])})
        raw = pd.concat(frames, ignore_index=False)
        raw_payload = raw.to_csv(index=True, index_label="__source_index").encode("utf-8")
        try:
            normalized = self._normalize(raw, start, end)
        except Exception as exc:
            return FetchResult(pd.DataFrame(), FetchStatus.UPSTREAM_ERROR, self.source_name, dataset, "research-v1", frozenset(fields), frozenset(), fetched_at, retryable=False, errors=(f"normalization failed: {exc}",), metadata={"server": getattr(self, "server", self.servers[0])})
        delivered = frozenset(set(fields) & set(normalized.columns))
        status = FetchStatus.PARTIAL_ROWS if len(frames) < len(instruments) else FetchStatus.SUCCESS
        observed_start = min(normalized["datetime"]) if not normalized.empty else None
        observed_end = max(normalized["datetime"]) if not normalized.empty else None
        return FetchResult(
            normalized,
            FetchStatus.EMPTY if normalized.empty else status,
            self.source_name,
            dataset,
            "research-v1",
            frozenset(fields),
            delivered,
            fetched_at,
            observed_start.date() if observed_start is not None else None,
            observed_end.date() if observed_end is not None else None,
            retryable=True,
            warnings=tuple(warnings),
            errors=tuple(errors),
            metadata={
                "server": getattr(self, "server", self.servers[0]),
                "raw_payload": raw_payload,
                "request": {"dataset": dataset, "instruments": instruments, "start": start, "end": end, "fields": fields},
            },
        )

    @staticmethod
    def _normalize(raw: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
        if "instrument" not in raw.columns:
            raise ValueError("mootdx response has no instrument column")
        if "datetime" in raw.columns:
            normalized = raw.reset_index(drop=True).copy()
            normalized["datetime"] = pd.to_datetime(normalized["datetime"], errors="raise").dt.normalize()
        else:
            index_name = raw.index.name or "datetime"
            normalized = raw.reset_index().copy()
            if "datetime" not in normalized.columns:
                normalized = normalized.rename(columns={index_name: "datetime"})
            if all(isinstance(value, str) and value.isdigit() and len(value) == 8 for value in normalized["datetime"]):
                normalized["datetime"] = pd.to_datetime(normalized["datetime"], format="%Y%m%d")
            else:
                normalized["datetime"] = pd.to_datetime(normalized["datetime"], errors="raise")
            normalized["datetime"] = normalized["datetime"].dt.normalize()
        required = ("open", "high", "low", "close", "vol", "amount")
        missing = [column for column in required if column not in normalized.columns]
        if missing:
            raise ValueError(f"mootdx response has missing columns: {missing}")
        result = pd.DataFrame({
            "instrument": normalized["instrument"],
            "datetime": normalized["datetime"],
            "open": pd.to_numeric(normalized["open"]),
            "high": pd.to_numeric(normalized["high"]),
            "low": pd.to_numeric(normalized["low"]),
            "close": pd.to_numeric(normalized["close"]),
            "volume": pd.to_numeric(normalized["vol"]) * 100.0,
            "amount": pd.to_numeric(normalized["amount"]),
        })
        if start:
            result = result[result["datetime"] >= pd.Timestamp(start)]
        if end:
            result = result[result["datetime"] <= pd.Timestamp(end)]
        return result.sort_values(["instrument", "datetime"]).reset_index(drop=True)
