from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from datetime import date

import pandas as pd


DERIVATION_VERSION = "adj_factor.exchange_v1"


@dataclass(frozen=True)
class AdjustmentDerivation:
    frame: pd.DataFrame
    version: str
    provenance: str
    snapshot_id: str | None = None
    effective_date_table_checksum: str | None = None


class XdxrAdjustmentDeriver:
    """Derive backward cumulative adjustment factors from exchange XDXR events.

    The multiplier uses the exchange-style ex-right reference price:

    ``(pre_close - cash_per_share + rights_price * rights_ratio)
       / (1 + bonus_ratio + rights_ratio)``.

    Provider amount fields use the ten-share convention.
    """

    def __init__(self, provenance: str = "mootdx.xdxr") -> None:
        self.provenance = provenance

    def derive(
        self,
        instrument: str,
        events: pd.DataFrame,
        sessions: pd.Series | list[date],
        pre_close: pd.Series | None = None,
    ) -> AdjustmentDerivation:
        if "category" not in events.columns:
            raise ValueError("xdxr events require category")
        event_dates = self._event_dates(events)
        pre_close_by_date = self._pre_close_by_date(pre_close)
        factors: list[float] = []
        cumulative = 1.0
        session_values = pd.DatetimeIndex(sessions).unique().sort_values(ascending=False)
        for session in session_values:
            factors.append(cumulative)
            on_session = (event_dates == pd.Timestamp(session)).to_numpy()
            for _, event in events[on_session].iterrows():
                cumulative *= self._event_multiplier(event, pre_close_by_date.get(pd.Timestamp(session)))
        factors.reverse()
        if len(factors) != len(session_values):
            raise ValueError("derived factor count does not match sessions")
        session_dates = sorted(set(sessions))
        result = pd.DataFrame({
            "instrument": instrument,
            "session_date": session_dates,
            "adj_factor": factors,
        })

        effective_rows = []
        for session in session_dates:
            applicable = [
                event for event in events.to_dict(orient="records")
                if date(int(event["year"]), int(event["month"]), int(event["day"])) <= session.date()
            ]
            latest = max(applicable, key=lambda event: (int(event["year"]), int(event["month"]), int(event["day"]))) if applicable else None
            effective_rows.append({
                "session_date": session,
                "effective_date": None if latest is None else date(int(latest["year"]), int(latest["month"]), int(latest["day"])),
                "source_event_id": "" if latest is None else str(latest.get("source_event_id") or self._event_fingerprint(latest)),
            })
        effective_table = pd.DataFrame(effective_rows)
        snapshot_payload = {
            "instrument": instrument,
            "derivation_version": DERIVATION_VERSION,
            "provenance": self.provenance,
            "event_table_sha256": hashlib.sha256(
                events.sort_values(["year", "month", "day"]).to_csv(index=False).encode("utf-8")
            ).hexdigest(),
            "sessions_sha256": hashlib.sha256(
                pd.Series(session_dates).astype(str).to_csv(index=False).encode("utf-8")
            ).hexdigest(),
        }
        snapshot_id = hashlib.sha256(json.dumps(snapshot_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        table_checksum = hashlib.sha256(effective_table.to_csv(index=False).encode("utf-8")).hexdigest()
        return AdjustmentDerivation(result, DERIVATION_VERSION, self.provenance, snapshot_id, table_checksum)

    @staticmethod
    def _event_fingerprint(event: dict[str, object]) -> str:
        payload = json.dumps(event, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _event_dates(events: pd.DataFrame) -> pd.Series:
        values = [
            date(int(row.year), int(row.month), int(row.day))
            for _, row in events.iterrows()
        ]
        return pd.Series(pd.to_datetime(values), index=events.index)

    @staticmethod
    def _event_multiplier(event: pd.Series, pre_close: float | None = None) -> float:
        if int(event.get("category", -1)) != 1:
            return 1.0
        cash = float(event.get("fenhong") or 0.0) / 10.0
        bonus = float(event.get("songzhuangu") or 0.0) / 10.0
        rights = float(event.get("peigu") or 0.0) / 10.0
        rights_price = float(event.get("peigujia") or 0.0)
        denominator = 1.0 + bonus + rights
        if pre_close is None or pre_close <= 0:
            raise ValueError("exchange formula requires positive pre_close")
        if denominator <= 0:
            raise ValueError("invalid xdxr denominator")
        ex_right_price = (float(pre_close) - cash + rights_price * rights) / denominator
        if ex_right_price <= 0:
            raise ValueError("non-positive ex-right reference price")
        return float(pre_close) / ex_right_price

    @staticmethod
    def _pre_close_by_date(pre_close: pd.Series | None) -> dict[pd.Timestamp, float]:
        if pre_close is None:
            return {}
        if not isinstance(pre_close.index, pd.DatetimeIndex):
            raise ValueError("pre_close must be indexed by session datetime")
        values = pd.to_numeric(pre_close)
        if values.isna().any() or (values <= 0).any():
            raise ValueError("pre_close must contain positive numeric values")
        return {index: float(value) for index, value in values.items()}
