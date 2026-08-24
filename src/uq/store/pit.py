from __future__ import annotations

import pandas as pd

from ..errors import ContractError

_REQUIRED = {"event_key", "announcement_datetime", "revision", "value", "source_event_id"}


class PitStore:
    """In-memory append-only revision store for event-driven datasets."""

    def __init__(self) -> None:
        self._events: list[dict[str, object]] = []

    def append(self, rows: pd.DataFrame) -> None:
        missing = _REQUIRED - set(rows.columns)
        if missing:
            raise ContractError(f"PIT rows missing required fields: {sorted(missing)}")
        if pd.DataFrame(self._events + rows.to_dict(orient="records")).duplicated(["event_key", "announcement_datetime", "revision"]).any():
            raise ContractError("duplicate PIT revision")
        self._events.extend(rows.to_dict(orient="records"))

    def read_asof(self, decision_time: pd.Timestamp, event_keys: set[str] | None = None) -> pd.DataFrame:
        frame = pd.DataFrame(self._events)
        if frame.empty:
            return frame
        visible = frame[pd.to_datetime(frame["announcement_datetime"]) <= decision_time]
        if event_keys is not None:
            visible = visible[visible["event_key"].isin(event_keys)]
        if visible.empty:
            return visible
        ordered = visible.sort_values(
            ["event_key", "announcement_datetime", "revision", "source_event_id"],
            ascending=[True, False, False, True],
        )
        return ordered.drop_duplicates("event_key", keep="first").reset_index(drop=True)
