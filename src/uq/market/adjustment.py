from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from datetime import date

import pandas as pd


DERIVATION_VERSION = "adj_factor.exchange_v2"


@dataclass(frozen=True)
class AdjustmentDerivation:
    frame: pd.DataFrame
    version: str
    provenance: str
    snapshot_id: str | None = None
    effective_date_table_checksum: str | None = None


class XdxrAdjustmentDeriver:
    """Derive backward (pre-adjusted) cumulative factors from exchange XDXR events.

    Semantics (``adj_factor.exchange_v2``): factors are **multiply-basis** and
    anchored at ``1.0`` on the latest session. ``adjusted_close = close *
    adj_factor`` yields a return-continuous series: an event with ex-date ``E``
    multiplies the factor of every session strictly before ``E`` by
    ``ex_right_price / pre_close`` (< 1), so the pre-event adjusted close equals
    the ex-date adjusted close and the measured return across the event is the
    true economic return. This matches the Tushare cumulative-factor convention
    and the downstream consumers (``factors/adjusted_price.py``,
    ``models/labels.py``), which multiply.

    The exchange-style ex-right reference price is:

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
        fields = {
            name: float(event.get(name) or 0.0)
            for name in ("fenhong", "songzhuangu", "peigu", "peigujia")
        }
        if not all(math.isfinite(value) for value in fields.values()):
            raise ValueError("xdxr event contains non-finite amount fields")
        cash = fields["fenhong"] / 10.0
        bonus = fields["songzhuangu"] / 10.0
        rights = fields["peigu"] / 10.0
        rights_price = fields["peigujia"]
        denominator = 1.0 + bonus + rights
        if pre_close is None or not math.isfinite(pre_close) or pre_close <= 0:
            raise ValueError("exchange formula requires positive pre_close")
        if denominator <= 0:
            raise ValueError("invalid xdxr denominator")
        ex_right_price = (float(pre_close) - cash + rights_price * rights) / denominator
        if not math.isfinite(ex_right_price) or ex_right_price <= 0:
            raise ValueError("non-positive ex-right reference price")
        return ex_right_price / float(pre_close)

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
