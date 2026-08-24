from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

import pandas as pd


class FetchStatus(str, Enum):
    SUCCESS = "success"
    EMPTY = "empty"
    PARTIAL_FIELDS = "partial_fields"
    PARTIAL_ROWS = "partial_rows"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    UNSUPPORTED_REQUEST = "unsupported_request"


@dataclass(frozen=True)
class FetchResult:
    rows: pd.DataFrame
    status: FetchStatus
    source: str
    dataset: str
    schema_version: str
    requested_fields: frozenset[str]
    delivered_fields: frozenset[str]
    source_fetched_at: datetime
    observed_start: date | None = None
    observed_end: date | None = None
    retryable: bool = False
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)
    raw_artifact_ref: str | None = None
    raw_checksum_sha256: str | None = None

    @property
    def is_usable(self) -> bool:
        return self.status in {FetchStatus.SUCCESS, FetchStatus.EMPTY}
