from __future__ import annotations

from datetime import date

import pandas as pd


class AkShareLifecycleProvider:
    """Conservative listing metadata backed by current exchange lists."""

    source_name = "akshare"
    # Beijing listings are absent from the AkShare Shanghai/Shenzhen rosters,
    # so the vendor that carries them is Tushare (measured: stock_basic with
    # exchange="BSE" answers 349 names with list_date at the 2000-point tier,
    # while the free tier does not). The roster is *current* listing truth: it
    # can excuse an absence before a known listing date, never one inside the
    # listing window, and it carries no delisting date.
    bse_source_name = "tushare_bse"
    BSE_SYMBOL_PREFIXES = ("43", "83", "87", "92")
    _bj_cache: dict[str, date | None] | None = None

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
        if not instruments:
            return {}
        wanted = tuple(sources)
        want_akshare = "akshare" in wanted
        want_bse = self.bse_source_name in wanted
        if not want_akshare and not want_bse:
            return {instrument: "unknown_requires_review" for instrument in instruments}
        listed_codes: set[str] = set()
        delisted_codes: dict[str, tuple[date | None, date]] = {}
        akshare_ok = False
        if want_akshare:
            try:
                listed_codes = self._listed_codes()
                delisted_codes = self._delisted_codes()
                akshare_ok = True
            except Exception:
                akshare_ok = False
        bj_roster: dict[str, date | None] = {}
        if want_bse and any(instrument.endswith(".XBSE") for instrument in instruments):
            try:
                bj_roster = self._bj_codes()
            except Exception:
                bj_roster = {}
        result: dict[str, str] = {}
        for instrument in instruments:
            symbol, _, suffix = instrument.partition(".")
            if suffix == "XBSE":
                result[instrument] = self._classify_bse(trade_date, symbol, bj_roster)
                continue
            if not akshare_ok:
                result[instrument] = "unknown_requires_review"
                continue
            exchange_ok = suffix in {"XSHG", "XSHE"}
            prefix_ok = (suffix == "XSHG" and symbol.startswith(("60", "68"))) or (
                suffix == "XSHE" and symbol.startswith(("00", "30"))
            )
            # cr.md H1: delisted instruments are absent from the *current*
            # listed roster, so membership in delisted_codes must be checked
            # first. A gap before the delist date is inside the listing window
            # and must stay unexplained (fail closed), not be excused as
            # "not listed yet".
            if symbol in delisted_codes:
                listed_on, delisted_on = delisted_codes[symbol]
                if trade_date >= delisted_on:
                    result[instrument] = "delisted_expected_missing"
                elif listed_on is not None and trade_date < listed_on:
                    # cr.md U5: a gap before the listing date of a
                    # since-delisted stock is a legitimate not-yet-listed
                    # absence (historical backfill spans pre-IPO dates);
                    # only the in-window gap stays fail closed (H1).
                    result[instrument] = "not_listed_expected_missing"
                else:
                    result[instrument] = "unknown_requires_review"
            elif exchange_ok and prefix_ok and symbol not in listed_codes:
                result[instrument] = "not_listed_expected_missing"
            else:
                result[instrument] = "unknown_requires_review"
        return result

    def _classify_bse(self, trade_date: date, symbol: str, roster: dict[str, date | None]) -> str:
        """Classify a Beijing listing from its current roster entry.

        A symbol whose prefix is not a Beijing prefix cannot be excused, and a
        symbol absent from the current roster is indistinguishable between
        "delisted" and "never listed" because no Beijing delisting feed exists
        at this data tier — both stay fail closed.
        """
        if symbol[:2] not in self.BSE_SYMBOL_PREFIXES:
            return "unknown_requires_review"
        listed_on = roster.get(symbol)
        if listed_on is not None and trade_date < listed_on:
            return "not_listed_expected_missing"
        return "unknown_requires_review"

    def _bj_codes(self) -> dict[str, date | None]:
        """Beijing listing roster: symbol -> listing date when published."""
        if self._bj_cache is not None:
            return self._bj_cache
        from ..adapters.tushare_2k import Tushare2kAdapter

        frame = Tushare2kAdapter().stock_basic(exchange="BSE")
        roster: dict[str, date | None] = {}
        listed_column = frame["list_date"] if "list_date" in frame.columns else None
        for index, code in enumerate(frame["ts_code"]):
            symbol = str(code).partition(".")[0]
            listed_on = None
            if listed_column is not None and pd.notna(listed_column.iloc[index]):
                listed_on = self._parse_list_date(listed_column.iloc[index])
            roster[symbol] = listed_on
        self._bj_cache = roster
        return roster

    @staticmethod
    def _parse_list_date(value: object) -> date | None:
        text = str(value).strip()
        if len(text) == 8 and text.isdigit():
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
        try:
            return pd.Timestamp(text).date()
        except (TypeError, ValueError):
            return None

    def _listed_codes(self) -> set[str]:
        import akshare as ak

        codes: set[str] = set()
        for board in ("主板A股", "科创板"):
            frame = ak.stock_info_sh_name_code(symbol=board)
            codes.update(frame["证券代码"].astype(str).str.zfill(6))
        frame = ak.stock_info_sz_name_code(symbol="A股列表")
        codes.update(frame["A股代码"].astype(str).str.zfill(6))
        return codes

    def _delisted_codes(self) -> dict[str, tuple[date | None, date]]:
        """Map delisted symbol -> (listing date when known, delist date)."""
        import akshare as ak

        events: dict[str, tuple[date | None, date]] = {}
        sh = ak.stock_info_sh_delist(symbol="全部")
        sh_listed = sh["上市日期"] if "上市日期" in sh.columns else None
        for index, (code, value) in enumerate(zip(sh["公司代码"], sh["暂停上市日期"])):
            if pd.notna(value):
                listed_on = None
                if sh_listed is not None and pd.notna(sh_listed.iloc[index]):
                    listed_on = pd.Timestamp(sh_listed.iloc[index]).date()
                events[str(code).zfill(6)] = (listed_on, pd.Timestamp(value).date())
        sz = ak.stock_info_sz_delist(symbol="终止上市公司")
        sz_listed = sz["上市日期"] if "上市日期" in sz.columns else None
        for index, (code, value) in enumerate(zip(sz["证券代码"], sz["终止上市日期"])):
            if pd.notna(value):
                listed_on = None
                if sz_listed is not None and pd.notna(sz_listed.iloc[index]):
                    listed_on = pd.Timestamp(sz_listed.iloc[index]).date()
                events[str(code).zfill(6)] = (listed_on, pd.Timestamp(value).date())
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
