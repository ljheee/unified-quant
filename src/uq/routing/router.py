from __future__ import annotations

from dataclasses import dataclass
import dataclasses
from collections.abc import Callable
from datetime import date
import pandas as pd

from ..contracts.capabilities import DatasetContract, SourceAdapter
from ..market.calendar import TradingCalendar
from ..errors import CapabilityGapError
from ..sources.fetch import FetchResult
from ..sources.fetch import FetchStatus


@dataclass(frozen=True)
class RouteResult:
    results: dict[str, FetchResult]
    coverage_status: str
    selected_sources: list[str]

    @property
    def frames(self) -> dict[str, pd.DataFrame]:
        return {name: result.rows for name, result in self.results.items()}

    @property
    def is_complete(self) -> bool:
        return self.coverage_status == "complete"


class SourceRouter:
    """Route requests to complete sources unless partial mode is explicit."""

    def __init__(
        self,
        contract: DatasetContract,
        adapters: dict[str, SourceAdapter],
        trading_dates: Callable[[date, date], set[date]] | None = None,
        calendar: TradingCalendar | None = None,
    ):
        unknown = set(adapters) - set(contract.sources)
        if unknown:
            raise CapabilityGapError(set(), {})
        if not adapters:
            raise CapabilityGapError(set(contract.required_fields), {})

        for name, adapter in adapters.items():
            if adapter.source_name != name:
                raise ValueError(f"adapter identity mismatch: {adapter.source_name} != {name}")
        self.contract = contract
        self.adapters = dict(adapters)
        self.trading_dates = trading_dates
        self.calendar = calendar or (TradingCalendar(provider=trading_dates) if trading_dates else None)

    def fetch(
        self,
        instruments: list[str],
        start: str,
        end: str,
        fields: list[str] | None = None,
        allow_partial: bool = False,
    ) -> RouteResult:
        requested = set(fields or self.contract.required_fields)
        eligible = [
            capability
            for capability in self.contract.eligible_sources(requested)
            if capability.source_name in self.adapters
        ]
        primary = next((item for item in eligible if item.source_name == self.contract.primary_source), None)
        if primary is None and not allow_partial:
            raise CapabilityGapError(
                requested,
                {name: set(self.contract.sources[name].provides) for name in self.adapters},
            )
        selected = [primary] if primary is not None else []
        if primary is not None:
            validation_sources = sorted({
                str(rule.get("compare_with"))
                for rule in self.contract.cross_validation.values()
                if rule.get("compare_with") in self.adapters
            })
            for source_name in validation_sources:
                if source_name not in selected:
                    capability = self.contract.sources[source_name]
                    selected.append(capability)
        if not selected and allow_partial:
            candidates = [
                capability
                for capability in self.contract.sources.values()
                if capability.source_name in self.adapters
                and requested & capability.provides
            ]
            selected = sorted(candidates, key=lambda item: (not item.fallback, item.priority))

        delivered = requested
        if primary is None:
            covered = set().union(*(item.provides for item in selected)) if selected else set()
            delivered = requested & covered

        results: dict[str, FetchResult] = {}
        for capability in selected:
            available_fields = sorted(delivered & capability.provides)
            results[capability.source_name] = self.adapters[capability.source_name].fetch(
                dataset=self.contract.dataset,
                instruments=instruments,
                start=start,
                end=end,
                fields=available_fields,
            )
        coverage = "unverified"
        if primary is not None:
            primary_result = results[primary.source_name]
            envelope_errors = [
                f"{name} envelope mismatch: dataset={result.dataset}, schema={result.schema_version}, source={result.source}"
                for name, result in results.items()
                if result.dataset != self.contract.dataset
                or result.schema_version != self.contract.schema_version
                or result.source != name
            ]
            if envelope_errors:
                primary_result = dataclasses.replace(
                    primary_result,
                    status=FetchStatus.UNSUPPORTED_REQUEST,
                    errors=tuple(primary_result.errors) + tuple(envelope_errors),
                )
                results[primary.source_name] = primary_result
            observed_fields = set(primary_result.delivered_fields)
            observed_rows = self._observed_keys(primary_result.rows, requested)
            if self.calendar is not None:
                expected_dates = self.calendar.between(date.fromisoformat(start), date.fromisoformat(end))
                observed_dates = {
                    key[1].date() if hasattr(key[1], "date") else key[1]
                    for key in observed_rows
                    if len(key) > 1
                }
                complete_status = primary_result.status == FetchStatus.SUCCESS
                coverage = (
                    "complete"
                    if (
                        complete_status
                        and requested <= observed_fields
                        and {instrument for instrument, _ in observed_rows} >= set(instruments)
                        and observed_dates >= expected_dates
                    )
                    else "partial"
                )
        return RouteResult(results=results, coverage_status=coverage, selected_sources=[item.source_name for item in selected])

    @staticmethod
    def _expected_dates(start: str, end: str) -> set[date]:
        if not start or not end:
            return set()
        dates = pd.date_range(start, end, freq="D")
        return {value.date() for value in dates}

    @staticmethod
    def _observed_keys(frame: pd.DataFrame, fields: set[str]) -> set[tuple[str, ...]]:
        key_columns = ["instrument"]
        if "session_date" in frame.columns or "datetime" in frame.columns:
            key_columns.append(next(column for column in ("session_date", "datetime") if column in frame.columns))
        if not key_columns or "instrument" not in frame.columns:
            return set()
        return set(map(tuple, frame[key_columns].itertuples(index=False, name=None)))
