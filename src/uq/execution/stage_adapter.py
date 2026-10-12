"""Paper execution stage adapter for the Research Chain orchestrator.

Per decision date the adapter produces execution_config, order plan,
execution result, and next portfolio state using only the owning-layer
planner/engine/state-builder APIs. It does not sign quality reports or
bypass store validation. Publication stores are passed in by the caller
(the runner); the adapter only invokes their publish/read APIs.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

import pandas as pd

from ..errors import ContractError
from .engine import PaperExecutionEngine
from .planner import PaperOrderPlanner
from .state import PaperPortfolioStateBuilder


class PaperExecutionStageAdapter:
    """Orchestrate per-decision-date paper execution from target weights."""

    def __init__(
        self,
        template: Mapping[str, Any],
        *,
        planner: PaperOrderPlanner | None = None,
        engine: PaperExecutionEngine | None = None,
        state_builder: PaperPortfolioStateBuilder | None = None,
    ) -> None:
        self.template = dict(template)
        self.planner = planner or PaperOrderPlanner()
        self.engine = engine or PaperExecutionEngine()
        self.state_builder = state_builder or PaperPortfolioStateBuilder()

    def initial_holdings_and_cash(self) -> tuple[list[dict[str, Any]], float]:
        initial = self.template["initial_state"]
        if "continuation_binding" in initial:
            raise ContractError(
                "continuation binding requires a published state; the runner must read it from the state store"
            )
        return [dict(holding) for holding in initial.get("holdings", [])], float(initial["cash"])

    def compute_execution_id(self, request_generation_id: str, run_id: str, decision_date: str) -> str:
        from ..contracts.manifest import canonical_json
        import hashlib
        payload = {
            "request_content_generation_id": request_generation_id,
            "run_id": run_id,
            "decision_date": decision_date,
        }
        return hashlib.sha256(canonical_json(payload)).hexdigest()

    def plan_date(
        self,
        config: Mapping[str, Any],
        target_weights_frame: pd.DataFrame,
        input_holdings: Sequence[Mapping[str, Any]] | None,
        decision_prices: Mapping[str, Mapping[str, float]],
        *,
        opening_cash: float,
    ) -> tuple[pd.DataFrame, list[int]]:
        return self.planner.plan(
            config, target_weights_frame, input_holdings, decision_prices,
            opening_cash=opening_cash,
        )

    def execute_date(
        self,
        config: Mapping[str, Any],
        plan_manifest: Mapping[str, Any],
        plan_frame: pd.DataFrame,
        target_weights_frame: pd.DataFrame,
        input_holdings: Sequence[Mapping[str, Any]],
        execution_market: Mapping[str, Mapping[str, Any]],
        decision_prices: Mapping[str, Mapping[str, float]],
        *,
        opening_cash: float,
        suspended_instruments: set[str] | None = None,
        corporate_action_excluded: set[str] | None = None,
    ) -> tuple[pd.DataFrame, Mapping[str, Any]]:
        return self.engine.execute(
            config, plan_manifest, plan_frame, target_weights_frame,
            input_holdings, execution_market, decision_prices,
            opening_cash=opening_cash,
            suspended_instruments=suspended_instruments or set(),
            corporate_action_excluded=corporate_action_excluded or set(),
        )
