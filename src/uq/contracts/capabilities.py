from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import pandas as pd

from ..sources.fetch import FetchResult

class SourceAdapter(Protocol):
    source_name: str

    def fetch(
        self,
        dataset: str,
        instruments: list[str],
        start: str,
        end: str,
        fields: list[str],
    ) -> FetchResult:
        """Return canonical-shaped rows wrapped in a typed fetch envelope."""


@dataclass(frozen=True)
class SourceCapability:
    source_name: str
    dataset: str
    schema_version: str
    priority: int
    provides: frozenset[str]
    fallback: bool = False
    coverage: dict[str, Any] | None = None
    authentication: str = "none"
    latency_class: str = "end_of_day"
    quota: dict[str, Any] | None = None
    reliability: str = "unknown"
    correction_window: str | None = None
    revision_support: bool = False


@dataclass(frozen=True)
class DatasetContract:
    dataset: str
    schema_version: str
    required_fields: tuple[str, ...]
    owners: dict[str, str]
    primary_source: str
    row_policy: dict[str, Any] | None = None
    cross_validation: dict[str, dict[str, Any]] = field(default_factory=dict)
    sources: dict[str, SourceCapability] = field(default_factory=dict)

    def eligible_sources(self, fields: set[str]) -> list[SourceCapability]:
        return sorted(
            (item for item in self.sources.values() if fields <= item.provides),
            key=lambda item: (item.source_name != self.primary_source, item.priority, item.source_name),
        )
