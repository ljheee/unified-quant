from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..sources.fetch import FetchResult, FetchStatus


class DeterministicDemoAdapter:
    """Credential-free adapter used for tests and local examples."""

    def __init__(
        self,
        source_name: str,
        rows: pd.DataFrame,
        volume_multiplier: float = 1.0,
        close_delta_pct: float = 0.0,
        schema_version: str = "test",
    ) -> None:
        self.source_name = source_name
        self._rows = rows.copy()
        self.volume_multiplier = volume_multiplier
        self.close_delta_pct = close_delta_pct
        self.schema_version = schema_version

    def fetch(
        self,
        dataset: str,
        instruments: list[str],
        start: str,
        end: str,
        fields: list[str],
    ) -> FetchResult:
        date_column = "session_date" if "session_date" in self._rows.columns else "datetime"
        selected = self._rows[
            self._rows["instrument"].isin(instruments)
            & ((start == "" or self._rows[date_column] >= pd.Timestamp(start)))
            & ((end == "" or self._rows[date_column] <= pd.Timestamp(end)))
        ].copy()
        selected = selected[fields].copy()
        if "volume" in selected:
            selected["volume"] = selected["volume"] * self.volume_multiplier
        if "close" in selected:
            selected["close"] = selected["close"] * (1 + self.close_delta_pct)
        rows = selected.reset_index(drop=True)
        observed = pd.to_datetime(rows[date_column]) if not rows.empty else pd.Series(dtype="datetime64[ns]")
        fetched_at = datetime.now()
        missing_fields = set(fields) - set(rows.columns)
        if missing_fields:
            status = FetchStatus.PARTIAL_FIELDS
        elif rows.empty and instruments:
            status = FetchStatus.EMPTY
        elif len(rows["instrument"].drop_duplicates()) < len(set(instruments)):
            status = FetchStatus.PARTIAL_ROWS
        else:
            status = FetchStatus.SUCCESS
        return FetchResult(
            rows=rows,
            status=status,
            source=self.source_name,
            dataset=dataset,
            schema_version=self.schema_version,
            requested_fields=frozenset(fields),
            delivered_fields=frozenset(set(fields) & set(rows.columns)),
            source_fetched_at=fetched_at,
            observed_start=observed.min().date() if not observed.empty else None,
            observed_end=observed.max().date() if not observed.empty else None,
        )
