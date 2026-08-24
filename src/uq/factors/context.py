from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from ..contracts.schema import Schema
from ..store.reader import ManifestFirstReader


class FactorContext:
    def __init__(self, root: Path, schema: Schema) -> None:
        self._reader = ManifestFirstReader(root)
        self.schema = schema

    def read_bars(
        self,
        partition_date: date | None = None,
        *,
        start: date | None = None,
        end: date | None = None,
        instruments: list[str] | set[str] | tuple[str, ...] | None = None,
        fields: list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> pd.DataFrame:
        if partition_date is not None:
            if start is not None or end is not None:
                raise ValueError("use either partition_date or start/end, not both")
            dates = [partition_date]
            start_date = end_date = partition_date
        else:
            if start is None or end is None:
                raise ValueError("start and end are required when partition_date is omitted")
            if start > end:
                raise ValueError("start must not be after end")
            start_date, end_date = start, end
            dates = [date.fromordinal(start_date.toordinal() + offset) for offset in range((end_date - start_date).days + 1)]

        frames: list[pd.DataFrame] = []
        for current in dates:
            try:
                frames.append(self._reader.read(self.schema, current))
            except FileNotFoundError as exc:
                raise ValueError(f"canonical range contains unpublished date: {current.isoformat()}") from exc

        frame = pd.concat(frames, ignore_index=True)
        if instruments is not None:
            wanted = set(instruments)
            frame = frame[frame["instrument"].isin(wanted)]
            missing = wanted - set(frame["instrument"])
            if missing:
                raise ValueError(f"instruments absent from canonical range: {sorted(missing)}")
        if fields is not None:
            missing_fields = set(fields) - set(frame.columns)
            if missing_fields:
                raise ValueError(f"fields unavailable in canonical schema: {sorted(missing_fields)}")
            frame = frame[list(fields)]
        return frame.reset_index(drop=True)
