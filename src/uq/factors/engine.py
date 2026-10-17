from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
from typing import Any, Literal

import pandas as pd


from ..contracts.factor_governance import FactorRegistry, FactorSetDefinition
from ..errors import ContractError
from .adjusted_price import calculate_adjusted_factors
from .raw_price import calculate_raw_price_factors
from .store import _validate_factor_frame


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
        if max(self.session_dates) > self.run_visible_cutoff.date():
            raise ContractError("factor request contains input beyond visible cutoff")
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

    def __post_init__(self) -> None:
        if self.status == "rejected" and not self.errors:
            raise ContractError("rejected factor result requires errors")
        if self.status != "rejected" and self.errors:
            raise ContractError("non-rejected factor result cannot contain errors")
        if self.status == "warning" and not self.warnings:
            raise ContractError("warning factor result requires warnings")


class FactorEngine:
    """Governed planning and execution facade for reviewed factor sets."""

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


    def create_result(
        self,
        request: FactorComputeRequest,
        frame: pd.DataFrame,
        *,
        input_lineage: tuple[dict[str, Any], ...] = (),
        quality_report: dict[str, Any] | None = None,
        null_policy: dict[str, Any] | None = None,
    ) -> FactorResult:
        """Materialize governed empty / all-null / partial-result semantics.

        The quality decision is delegated to ``_validate_factor_frame`` -- the very
        function ``FactorStore.publish`` re-derives at publication time -- so a
        report produced here is publishable by construction (cr.md R1).  Before
        that, this method computed its own check set (one ``null_rate`` per value
        column) which ``FactorStore`` rejected outright as duplicate checks, so the
        governed facade could never publish what it planned.

        Status therefore tracks the reviewed thresholds only: partial nulls that
        stay inside ``null_rate_maximum`` are reported through ``warnings`` as
        advisory context instead of downgrading the status.
        """
        def check_results(report: dict[str, Any]) -> dict[tuple[str, float], str]:
            # cr.md contracts OBS-4: a malformed check entry (missing key,
            # non-numeric threshold) must surface as a typed refusal, not a
            # bare KeyError/ValueError escaping the governed error channel.
            try:
                return {
                    (item["name"], float(item["threshold"])): item["result"]
                    for item in report.get("checks", [])
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ContractError(f"malformed quality report check entry: {exc}") from exc

        def failure_message(name: str) -> str:
            if name == "coverage":
                return "coverage below minimum"
            if name == "null_rate":
                return "null rate above maximum: all value columns"
            if name.startswith("null_rate__"):
                return f"null rate above maximum: {name[len('null_rate__'):]}"
            if name.startswith("finite_values__"):
                return f"non-finite values: {name[len('finite_values__'):]}"
            return f"{name} threshold failed"

        expected_names = {item["name"] for item in request.definition.factors}
        missing = sorted(expected_names - set(frame.columns))
        if missing:
            raise ContractError(f"factor result missing definitions: {missing}")
        value_columns = [item["name"] for item in request.definition.factors]
        thresholds = request.definition.document.get("quality_thresholds", {})
        maximum_null_rate = float(thresholds.get("null_rate_maximum", 0.0))
        errors: list[str] = []
        warnings: list[str] = []
        policy = request.definition.document["quality_policy"]
        if frame.empty:
            # Publish refuses an empty frame, so an empty result can only ever be a
            # dry-run staging artifact; a publication intent fails closed instead.
            status = "rejected" if request.intent == "publication" else "empty"
            if status == "rejected":
                errors.append("empty factor results cannot be published")
            local_report: dict[str, Any] = {
                "policy": policy, "status": status, "checks": [],
                "errors": errors, "warnings": warnings,
            }
        else:
            local_report = _validate_factor_frame(frame, request.definition)
            status = local_report["status"]
            errors.extend(
                failure_message(item["name"]) for item in local_report["checks"]
                if item["result"] == "failed"
            )
            for column in value_columns:
                if float(frame[column].isna().mean()) > 0:
                    warnings.append(f"insufficient history emits nulls: {column}")
        report = quality_report or local_report
        if report.get("policy") != policy:
            raise ContractError("factor result quality policy does not match reviewed definition")
        if report.get("status") != status:
            raise ContractError("factor result quality status disagrees with local checks")
        expected_checks = check_results(local_report)
        supplied_checks = check_results(report)
        # cr.md contracts OBS-5: an externally supplied report must carry real
        # checks.  R1's empty-frame branch produces an empty local check list,
        # so without this clause a supplied ``checks: []`` would pass equality
        # where the pre-R1 rule refused it (``not supplied_checks``).
        if quality_report is not None and not supplied_checks:
            raise ContractError("supplied factor quality report carries no checks")
        if supplied_checks != expected_checks:
            raise ContractError("factor result quality checks disagree with local computation")
        return FactorResult(
            frame=frame,
            definitions=tuple(request.definition.factors),
            input_lineage=input_lineage,
            quality_report=report,
            status=status,
            warnings=tuple(warnings),
            errors=tuple(errors),
            null_policy=null_policy or {
                "maximum_null_rate": maximum_null_rate,
                "forward_fill": False,
                "intent": request.intent,
                "staging_only": request.intent == "dry_run",
            },
        )

    def plan(self, request: FactorComputeRequest) -> FactorComputeRequest:
        if request.universe_binding is not None:
            required_universe_fields = {"generation_id", "members_artifact", "valid_from", "valid_to"}
            if set(request.universe_binding) & {"__path__"} or not required_universe_fields <= set(request.universe_binding):
                raise ContractError("invalid factor universe binding")
        return request

    def execute(
        self,
        request: FactorComputeRequest,
        frame: pd.DataFrame,
        *,
        adjustment_snapshot_ids: list[str] | None = None,
        effective_date_table_checksums: list[str] | None = None,
    ) -> FactorResult:
        self.plan(request)
        if request.definition.factor_set == "adjusted":
            if adjustment_snapshot_ids is None or effective_date_table_checksums is None:
                raise ContractError("adjusted factor execution requires governed adjustment lineage")
            calculated = calculate_adjusted_factors(
                frame,
                adjustment_snapshot_ids=adjustment_snapshot_ids,
                effective_date_table_checksums=effective_date_table_checksums,
            )
        elif request.definition.factor_set == "basic":
            if frame.empty:
                required = set(request.definition.document["required_columns"])
                missing = sorted(required - set(frame.columns))
                if missing:
                    raise ContractError(f"missing raw-price input columns: {missing}")
                calculated = frame.copy()
                for item in request.definition.factors:
                    calculated[item["name"]] = float("nan")
            else:
                calculated = calculate_raw_price_factors(frame)
        else:
            raise ContractError(f"unsupported governed factor set: {request.definition.factor_set}")
        lineage = [
            {
                "factor_set": request.definition.factor_set,
                "factor_version": request.definition.factor_version,
                **(
                    {}
                    if adjustment_snapshot_ids is None or effective_date_table_checksums is None
                    else {
                        "adjustment_snapshot_id": adjustment_snapshot_ids[0],
                        "effective_date_table_checksum": effective_date_table_checksums[0],
                    }
                ),
            }
        ]
        return self.create_result(request, calculated, input_lineage=tuple(lineage))
