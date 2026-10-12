"""Governed market-data reader for the paper-execution stage.

Paper execution binds four published ``MarketDatasetPublisher`` families
(``bars_daily_v2``, ``trading_calendar_snapshot_v1``, ``suspension_snapshot_v1``,
``dividend_event_v1``).  This reader is the single read boundary the paper stage
uses: every frame is byte-verified against its partition manifest, and a missing
or tampered partition fails closed instead of silently degrading to an empty
session.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

import pandas as pd

from ..errors import ContractError

_BARS_COLUMNS = {"open", "high", "low", "close", "volume", "status", "limit_up", "limit_down"}


class PublishedPaperMarketData:
    """Read per-session paper-execution inputs from a governed publisher store."""

    def __init__(
        self,
        publisher: Any,
        *,
        window_start: date,
        window_end: date,
    ) -> None:
        if window_end < window_start:
            raise ContractError("paper market window end precedes its start")
        self.publisher = publisher
        self.window_start = window_start
        self.window_end = window_end

    def sessions(self) -> list[date]:
        """Ordered governed sessions covering the window, fail-closed when empty."""
        sessions: set[date] = set()
        for partition_date in self.publisher.list_partition_dates("trading_calendar_snapshot_v1"):
            manifest = self.publisher.read_manifest("trading_calendar_snapshot_v1", partition_date)
            start = date.fromisoformat(manifest["window_start"])
            end = date.fromisoformat(manifest["window_end"])
            if end < self.window_start or start > self.window_end:
                continue
            sessions.update(
                day
                for day in (date.fromisoformat(value) for value in manifest.get("session_dates", []))
                if self.window_start <= day <= self.window_end
            )
        if not sessions:
            raise ContractError("no governed trading calendar snapshot covers the paper window")
        return sorted(sessions)

    def _partition_binding(self, family: str, session: date) -> dict[str, str]:
        manifest = self.publisher.read_manifest(family, session)
        return {
            "family": family,
            "generation_id": manifest["generation_id"],
            "manifest_digest_sha256": manifest["manifest_digest_sha256"],
        }

    def _frame(self, family: str, session: date) -> pd.DataFrame:
        _manifest, frame = self.publisher.read_partition_frame(family, session)
        return frame

    def bars(self, session: date) -> pd.DataFrame:
        frame = self._frame("bars_daily_v2", session)
        missing = _BARS_COLUMNS - set(frame.columns)
        if missing:
            raise ContractError(f"governed bars partition {session} lacks columns: {sorted(missing)}")
        return frame

    def bars_binding(self, session: date) -> dict[str, Any]:
        manifest = self.publisher.read_manifest("bars_daily_v2", session)
        return {
            "dataset_family": "bars_daily_v2",
            "schema_version": "v2",
            "generation_id": manifest["generation_id"],
            "manifest_digest_sha256": manifest["manifest_digest_sha256"],
            "visibility_timestamp": f"{session.isoformat()}T00:00:00+00:00",
            "price_basis": "decision_close_estimate",
            "required_columns": [
                "open", "high", "low", "close", "volume", "status", "limit_up", "limit_down",
            ],
        }

    def calendar_binding(self) -> dict[str, str]:
        partitions = [
            partition_date
            for partition_date in self.publisher.list_partition_dates("trading_calendar_snapshot_v1")
            if self.window_start
            <= date.fromisoformat(
                self.publisher.read_manifest("trading_calendar_snapshot_v1", partition_date)["window_start"]
            )
            <= self.window_end
        ]
        if not partitions:
            raise ContractError("no governed calendar partition covers the paper window")
        return self._partition_binding("trading_calendar_snapshot_v1", sorted(partitions)[0])

    def suspension_binding(self, session: date) -> dict[str, str]:
        return self._partition_binding("suspension_snapshot_v1", session)

    def corporate_action_binding(self, session: date) -> dict[str, str]:
        return self._partition_binding("dividend_event_v1", session)

    def decision_prices(self, session: date) -> dict[str, dict[str, float]]:
        frame = self.bars(session)
        return {
            str(row.instrument): {"close": float(row.close)}
            for row in frame.itertuples(index=False)
        }

    def execution_market(self, session: date) -> dict[str, dict[str, Any]]:
        frame = self.bars(session)
        market: dict[str, dict[str, Any]] = {}
        for row in frame.itertuples(index=False):
            market[str(row.instrument)] = {
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
                "status": str(row.status),
                "limit_up": float(row.limit_up),
                "limit_down": float(row.limit_down),
            }
        return market

    def suspended(self, session: date) -> set[str]:
        frame = self._frame("suspension_snapshot_v1", session)
        if "instrument" not in frame.columns:
            raise ContractError(f"governed suspension partition {session} lacks instrument")
        return {str(value) for value in frame["instrument"].tolist()}

    def corporate_action_excluded(self, session: date) -> set[str]:
        frame = self._frame("dividend_event_v1", session)
        if list(frame.columns) != ["date", "instrument"]:
            raise ContractError(f"governed corporate-action partition {session} has unexpected columns")
        return {str(value) for value in frame["instrument"].tolist()}

    def bindings(self) -> Mapping[str, Any]:
        """Window-level partition-set bindings for the four template families."""
        return {
            family: self.publisher.build_set_binding(
                family, window_start=self.window_start, window_end=self.window_end
            )
            for family in (
                "bars_daily_v2",
                "trading_calendar_snapshot_v1",
                "suspension_snapshot_v1",
                "dividend_event_v1",
            )
        }
