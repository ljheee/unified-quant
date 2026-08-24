from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
import hashlib
import json
from typing import Any, Literal


from ..contracts.factor_governance import FactorRegistry, FactorSetDefinition
from ..errors import ContractError


DECISION_ZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class WindowSelector:
    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise ContractError("invalid factor window selector")

    def as_dates(self) -> tuple[date, ...]:
        return (self.start_date, self.end_date)


@dataclass(frozen=True)
class FactorComputeRequest:
    definition: FactorSetDefinition
    session_dates: tuple[date, ...]
    universe_binding: dict[str, Any] | None
    decision_time: datetime
    run_visible_cutoff: datetime
    serialization_profile_id: str | None
    intent: Literal["dry_run", "publication"]
    request_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_dates or list(self.session_dates) != sorted(set(self.session_dates)):
            raise ContractError("factor session dates must be non-empty and strictly ordered")
        if self.run_visible_cutoff < self.decision_time:
            raise ContractError("run visible cutoff precedes decision time")
        object.__setattr__(self, "request_metadata", {
            **self.request_metadata,
            "definition_factor_set": self.definition.factor_set,
            "definition_factor_version": self.definition.factor_version,
            "resolved_serialization_profile_id": self.serialization_profile_id,
        })

    @property
    def execution_plan(self) -> dict[str, Any]:
        payload = {
            "definition": self.definition.document,
            "session_dates": [value.isoformat() for value in self.session_dates],
            "universe_generation_id": None if self.universe_binding is None else self.universe_binding["generation_id"],
            "decision_time": self.decision_time.isoformat(),
            "run_visible_cutoff": self.run_visible_cutoff.isoformat(),
            "serialization_profile_id": self.serialization_profile_id,
            "intent": self.intent,
            "facade_defaults": {
                "single_trade_date": len(self.session_dates) == 1,
                "universe_binding_is_default_none": self.universe_binding is None,
                "resolved_serialization_profile_id": self.serialization_profile_id,
                "decision_time": self.decision_time.isoformat(),
            },
        }
        return {
            "plan_version": 1,
            "payload": payload,
            "plan_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
        }


@dataclass(frozen=True)
class FactorResult:
    frame: pd.DataFrame
    definitions: tuple[dict[str, Any], ...]
    input_lineage: tuple[dict[str, Any], ...]
    quality_report: dict[str, Any]
    status: Literal["passed", "warning", "rejected", "empty"]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    null_policy: dict[str, Any]


class FactorEngine:
    """Contract and deterministic planning facade; factor execution is implemented by phase-specific calculators."""

    def __init__(self, root: Path, registry: FactorRegistry, *, run_visible_cutoff: datetime) -> None:
        self.root = root
        self.registry = registry
        self.run_visible_cutoff = run_visible_cutoff

    def compute(
        self,
        trade_date: date,
        factor_set: str,
        factor_version: str,
        universe_binding: dict[str, Any] | None = None,
    ) -> FactorComputeRequest:
        request = self.build_request(
            trade_date=trade_date,
            factor_set=factor_set,
            factor_version=factor_version,
            universe_binding=universe_binding,
        )
        return self.plan(request)

    def build_request(
        self,
        *,
        trade_date: date,
        factor_set: str,
        factor_version: str,
        session_dates: tuple[date, ...] | None = None,
        window: WindowSelector | None = None,
        universe_binding: dict[str, Any] | None = None,
        decision_time: datetime | None = None,
        run_visible_cutoff: datetime | None = None,
        serialization_profile_id: str | None = None,
        intent: Literal["dry_run", "publication"] = "dry_run",
    ) -> FactorComputeRequest:
        if session_dates is not None and window is not None:
            raise ContractError("factor request cannot supply both sessions and window")
        if session_dates is not None:
            dates = session_dates
        elif window is not None:
            dates = window.as_dates()
        else:
            dates = (trade_date,)
        decision = decision_time or datetime.combine(trade_date, time(15, 0), tzinfo=DECISION_ZONE)
        definition = self.registry.get(factor_set, factor_version)
        profile = serialization_profile_id or "parquet-v1"
        return self.plan(FactorComputeRequest(
            definition=definition,
            session_dates=tuple(dates),
            universe_binding=universe_binding,
            decision_time=decision,
            run_visible_cutoff=run_visible_cutoff or self.run_visible_cutoff,
            serialization_profile_id=profile,
            intent=intent,
            request_metadata={
                "facade": session_dates is None and window is None,
                "facade_trade_date": trade_date.isoformat(),
                "facade_universe_default": universe_binding is None,
            },
        ))


    def plan(self, request: FactorComputeRequest) -> FactorComputeRequest:
        if request.universe_binding is not None:
            required_universe_fields = {"generation_id", "members_artifact", "valid_from", "valid_to"}
            if set(request.universe_binding) & {"__path__"} or not required_universe_fields <= set(request.universe_binding):
                raise ContractError("invalid factor universe binding")
        return request
