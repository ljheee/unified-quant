from __future__ import annotations

from datetime import date

import pandas as pd


class AkShareLifecycleProvider:
    """Conservative listing metadata backed by current exchange lists."""

    source_name = "akshare"

    STATUS_EXPECTED_MISSING = {
        "not_listed_expected_missing",
        "delisted_expected_missing",
        "suspended_expected_missing",
    }

    def classify_missing(
        self,
        trade_date: date,
        instruments: list[str],
        sources: tuple[str, ...] | list[str] = ("akshare",),
    ) -> dict[str, str]:
        if not instruments or "akshare" not in sources:
            return {instrument: "unknown_requires_review" for instrument in instruments}
        listed_codes: set[str]
        delisted_codes: dict[str, date]
        try:
            listed_codes = self._listed_codes()
            delisted_codes = self._delisted_codes()
        except Exception:
            return {instrument: "unknown_requires_review" for instrument in instruments}
        result: dict[str, str] = {}
        for instrument in instruments:
            symbol, _, suffix = instrument.partition(".")
            exchange_ok = suffix in {"XSHG", "XSHE"}
            prefix_ok = (suffix == "XSHG" and symbol.startswith(("60", "68"))) or (
                suffix == "XSHE" and symbol.startswith(("00", "30"))
            )
            result[instrument] = (
                "delisted_expected_missing"
                if symbol in delisted_codes and trade_date >= delisted_codes[symbol]
                else
                "not_listed_expected_missing"
                if exchange_ok and prefix_ok and symbol not in listed_codes
                else "unknown_requires_review"
            )
        return result

    def _listed_codes(self) -> set[str]:
        import akshare as ak

        codes: set[str] = set()
        for board in ("主板A股", "科创板"):
            frame = ak.stock_info_sh_name_code(symbol=board)
            codes.update(frame["证券代码"].astype(str).str.zfill(6))
        frame = ak.stock_info_sz_name_code(symbol="A股列表")
        codes.update(frame["A股代码"].astype(str).str.zfill(6))
        return codes

    def _delisted_codes(self) -> dict[str, date]:
        import akshare as ak

        events: dict[str, date] = {}
        sh = ak.stock_info_sh_delist(symbol="全部")
        for code, value in zip(sh["公司代码"], sh["暂停上市日期"]):
            if pd.notna(value):
                events[str(code).zfill(6)] = pd.Timestamp(value).date()
        sz = ak.stock_info_sz_delist(symbol="终止上市公司")
        for code, value in zip(sz["证券代码"], sz["终止上市日期"]):
            if pd.notna(value):
                events[str(code).zfill(6)] = pd.Timestamp(value).date()
        return events

    def suspension_window(
        self,
        trade_date: date,
        instruments: list[str],
        sources: tuple[str, ...] | list[str] = ("akshare",),
    ) -> dict[str, str]:
        """Classify same-day A-share suspensions from a daily announcement feed."""
        if not instruments or "akshare" not in sources:
            return {}
        wanted = {instrument.split(".", 1)[0]: instrument for instrument in instruments}
        try:
            import akshare as ak

            frame = ak.news_trade_notify_suspend_baidu(trade_date.strftime("%Y%m%d"))
        except Exception:
            return {}
        if frame.empty:
            return {}
        result: dict[str, str] = {}
        for code, exchange in zip(frame["股票代码"], frame["交易所代码"]):
            instrument = wanted.get(str(code).zfill(6))
            if instrument and str(exchange).upper() in {"SH", "SZ"}:
                result[instrument] = "suspended_expected_missing"
        return result
