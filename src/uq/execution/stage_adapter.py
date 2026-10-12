"""Paper execution stage adapter for the Research Chain orchestrator.

Per decision date the adapter produces execution_config, order plan,
execution result, and next portfolio state using only the owning-layer
planner/engine/state-builder APIs. It does not sign quality reports or
bypass store validation. Publication stores are passed in by the caller
(the runner); the adapter only invokes their publish/read APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from ..contracts.model_layer import (
    initial_state_provenance_sha256,
    paper_execution_identities,
    sha256_json,
)
from ..errors import ContractError
from ..risk.contracts import risk_contract_identities
from ..risk.publication import ConfigPublicationBinding
from ..research_chain.adapters import build_stage_state
from .engine import PaperExecutionEngine
from .planner import PaperOrderPlanner
from .state import PaperPortfolioStateBuilder


def _binding(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "family": value["family"],
        "generation_id": value["generation_id"],
        "manifest_digest_sha256": value["manifest_digest_sha256"],
    }


def _restate_family(binding: Mapping[str, Any], family: str) -> dict[str, Any]:
    return {
        "family": family,
        "generation_id": binding["generation_id"],
        "manifest_digest_sha256": binding["manifest_digest_sha256"],
    }


def _holdings_from_state_frame(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {
            "instrument": str(row.instrument),
            "quantity": int(row.quantity),
            "average_cost": float(row.average_cost),
            "buy_locked_quantity": int(row.buy_locked_quantity),
            "sellable_quantity": int(row.sellable_quantity),
        }
        for row in frame.sort_values("instrument").itertuples(index=False)
    ]


class PaperExecutionStageAdapter:
    """Orchestrate per-decision-date paper execution from target weights."""

    def __init__(
        self,
        template: Mapping[str, Any],
        *,
        planner: PaperOrderPlanner | None = None,
        engine: PaperExecutionEngine | None = None,
        state_builder: PaperPortfolioStateBuilder | None = None,
        market: Any | None = None,
        config_store: Any | None = None,
        plan_store: Any | None = None,
        result_store: Any | None = None,
        state_store: Any | None = None,
        risk_decision_store: Any | None = None,
    ) -> None:
        self.template = dict(template)
        self.planner = planner or PaperOrderPlanner()
        self.engine = engine or PaperExecutionEngine()
        self.state_builder = state_builder or PaperPortfolioStateBuilder()
        self.market = market
        self.config_store = config_store
        self.plan_store = plan_store
        self.result_store = result_store
        self.state_store = state_store
        self.risk_decision_store = risk_decision_store

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

    def run(
        self,
        plan: Any,
        *,
        portfolio_stage_result: Any,
        decision_dates: Sequence[str],
        market: Any,
        quality_decisions: Mapping[str, Any],
        run_store: Any,
        risk_decision_source: Callable[..., Mapping[str, Any]],
        runner_identity: Mapping[str, str],
        created_at: str,
    ) -> PaperExecutionStageResult:
        """Publish the ordered per-decision-date paper execution chain.

        For each decision date, and in order: config, order plan, execution
        result, next portfolio state.  Every publication is read back before the
        stage state is published, so a failed readback cannot leave a partial
        success behind.
        """
        if self.market is None or self.config_store is None:
            raise ContractError("paper stage adapter is not wired to a market source and stores")
        dates = list(decision_dates)
        if not dates:
            raise ContractError("paper stage requires at least one decision date")
        sessions = market.sessions()
        session_index = {session.isoformat(): index for index, session in enumerate(sessions)}
        calendar_binding = market.calendar_binding()
        target_weights_frames = portfolio_stage_result.target_weight_frames
        target_weight_manifests = portfolio_stage_result.target_weight_manifests

        bindings: list[dict[str, Any]] = []
        previous_state_manifest: dict[str, Any] | None = None
        previous_state_frame: pd.DataFrame | None = None
        initial_holdings, initial_cash = self.initial_holdings_and_cash()

        for position, decision_date in enumerate(dates):
            if decision_date not in session_index:
                raise ContractError(f"decision date {decision_date} is not a governed session")
            execution_index = session_index[decision_date] + 1
            if execution_index >= len(sessions):
                raise ContractError(f"decision date {decision_date} has no following execution session")
            execution_date = sessions[execution_index].isoformat()

            target_frame = target_weights_frames.get(decision_date)
            target_manifest = target_weight_manifests.get(decision_date)
            if target_frame is None or target_manifest is None:
                raise ContractError(f"portfolio stage has no target weights for {decision_date}")

            state_mode = "initial" if position == 0 else "continuation"
            if state_mode == "initial":
                if previous_state_manifest is not None:
                    raise ContractError("paper stage initial date already consumed a prior state")
                input_holdings = initial_holdings
                opening_cash = initial_cash
                input_state_binding = None
                initial_state = {
                    "holdings": [dict(holding) for holding in initial_holdings],
                    "cash": float(initial_cash),
                    "valuation_price_basis": self.template["initial_state"]["valuation_price_basis"],
                }
                initial_state["provenance_sha256"] = initial_state_provenance_sha256(initial_state)
            else:
                if previous_state_manifest is None or previous_state_frame is None:
                    raise ContractError("paper continuation date is missing its prior state")
                input_holdings = _holdings_from_state_frame(previous_state_frame)
                opening_cash = float(previous_state_manifest["cash"])
                input_state_binding = {
                    "family": "paper_portfolio_state_v1",
                    "generation_id": previous_state_manifest["generation_id"],
                    "manifest_digest_sha256": previous_state_manifest["manifest_digest_sha256"],
                    "as_of_date": previous_state_manifest["state_date"],
                }
                initial_state = None

            execution_id = self.compute_execution_id(
                plan.request["request_content_generation_id"], plan.request["run_id"], decision_date
            )
            config = self._config_manifest(
                execution_id=execution_id,
                decision_date=decision_date,
                execution_date=execution_date,
                state_mode=state_mode,
                target_weights_binding=target_manifest,
                input_state_binding=input_state_binding,
                initial_state=initial_state,
                market=market,
                calendar_binding=calendar_binding,
                decision_session=date.fromisoformat(decision_date),
                execution_session=sessions[execution_index],
                run_id=plan.request["run_id"],
                created_at=created_at,
            )
            config["generation_id"], config["manifest_digest_sha256"] = paper_execution_identities(
                config, schema_name="execution_config"
            )
            risk_decision = dict(risk_decision_source(
                decision_date,
                config["generation_id"],
                {
                    "execution_date": execution_date,
                    "state_mode": state_mode,
                    "target_weights_manifest": target_manifest,
                    "target_weights_frame": target_frame,
                    "definition": portfolio_stage_result.definition_manifest,
                },
            ))
            if risk_decision.get("binding_config_generation_id") != config["generation_id"]:
                raise ContractError("risk decision source returned a decision for another config generation")
            risk_generation, risk_digest = risk_contract_identities(
                risk_decision, schema_name="risk_decision"
            )
            config["risk_decision_binding"] = {
                "family": "risk_decision_v1",
                "generation_id": risk_generation,
                "manifest_digest_sha256": risk_digest,
            }
            config["generation_id"], config["manifest_digest_sha256"] = paper_execution_identities(
                config, schema_name="execution_config"
            )
            if self.risk_decision_store is not None:
                self.risk_decision_store.publish(risk_decision, {
                    "action": risk_decision["action"],
                    "constraints": risk_decision["constraints"],
                    "decision_digest": risk_decision["decision_digest"],
                    "findings": risk_decision["findings"],
                })
            self.config_store.publish(
                config,
                quality_decision=quality_decisions["execution_config"],
                risk_decision=risk_decision,
                config_publication=ConfigPublicationBinding(
                    decision=risk_decision, config_generation_id=config["generation_id"]
                ),
            )
            published_config = self.config_store.read(config["generation_id"])

            execution_session = sessions[execution_index]
            decision_prices = market.decision_prices(date.fromisoformat(decision_date))
            execution_market = market.execution_market(execution_session)
            suspended = market.suspended(execution_session)
            corporate_excluded = market.corporate_action_excluded(execution_session)
            overlap = suspended & corporate_excluded
            if overlap:
                raise ContractError(
                    f"paper inputs have overlapping risk exclusions: {sorted(overlap)}"
                )

            plan_frame, residual = self.plan_date(
                published_config, target_frame,
                None if state_mode == "initial" else input_holdings,
                decision_prices,
                opening_cash=opening_cash,
            )
            plan_manifest = _plan_manifest(config=published_config, frame=plan_frame, residual=residual)
            self.plan_store.publish(
                plan_manifest, plan_frame,
                quality_decision=quality_decisions["order_plan"],
                risk_decision=risk_decision,
            )
            plan_manifest, plan_frame = self.plan_store.read(plan_manifest["generation_id"])

            result_frame, result_manifest = self.execute_date(
                published_config, plan_manifest, plan_frame, target_frame, input_holdings,
                execution_market, decision_prices,
                opening_cash=opening_cash,
                suspended_instruments=suspended,
                corporate_action_excluded=corporate_excluded,
            )
            self.result_store.publish(
                result_manifest, result_frame,
                quality_decision=quality_decisions["execution_result"],
            )
            result_manifest, result_frame = self.result_store.read(result_manifest["generation_id"])

            state_frame, state_manifest = self.state_builder.build(
                published_config,
                result_manifest=result_manifest,
                result_frame=result_frame,
                plan_manifest=plan_manifest,
                plan_frame=plan_frame,
                target_weights_manifest=target_manifest,
                target_weights_frame=target_frame,
                execution_market=execution_market,
                decision_prices=decision_prices,
                input_holdings=list(input_holdings),
                opening_cash=opening_cash,
                previous_state_manifest=previous_state_manifest,
                previous_state_frame=previous_state_frame,
            )
            self.state_store.publish(
                state_manifest, state_frame,
                quality_decision=quality_decisions["paper_portfolio_state"],
            )
            state_manifest, state_frame = self.state_store.read(state_manifest["generation_id"])

            bindings.extend(self._bindings_for_date(
                config=published_config,
                plan_manifest=plan_manifest,
                result_manifest=result_manifest,
                state_manifest=state_manifest,
            ))
            previous_state_manifest = state_manifest
            previous_state_frame = state_frame

        final_state = build_stage_state(
            plan,
            stage="paper_execution",
            output_bindings=bindings,
            runner_identity=runner_identity,
            created_at=created_at,
        )
        published_state = run_store.publish_state(final_state, stage="paper_execution")
        return PaperExecutionStageResult(
            bindings=list(bindings),
            decision_dates=dates,
            published_state=published_state,
        )

    def _config_manifest(
        self,
        *,
        execution_id: str,
        decision_date: str,
        execution_date: str,
        state_mode: str,
        target_weights_binding: Mapping[str, Any],
        input_state_binding: Mapping[str, Any] | None,
        initial_state: Mapping[str, Any] | None,
        market: Any,
        calendar_binding: Mapping[str, Any],
        decision_session: str,
        execution_session: str,
        run_id: str,
        created_at: str,
    ) -> dict[str, Any]:
        template = self.template
        return {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "execution_id": execution_id,
            "mode": "paper",
            "state_mode": state_mode,
            "market": template["market"],
            "decision_date": decision_date,
            "execution_date": execution_date,
            "target_weights_binding": {
                "family": "target_weights_v1",
                "generation_id": target_weights_binding["generation_id"],
                "manifest_digest_sha256": target_weights_binding["manifest_digest_sha256"],
            },
            "input_state_binding": dict(input_state_binding) if input_state_binding else None,
            "initial_state": dict(initial_state) if initial_state else None,
            "market_data_binding": market.bars_binding(decision_session),
            # The template binds governed publisher set-families, but the paper
            # execution contract names the lineage artifact families
            # (trading_calendar_v1 / suspension_snapshot_v1 / corporate_action_v1).
            # Keep the publisher generation/digest, restate the contract family.
            "calendar_binding": _restate_family(calendar_binding, "trading_calendar_v1"),
            "suspension_binding": _restate_family(
                market.suspension_binding(execution_session), "suspension_snapshot_v1"
            ),
            "corporate_action_binding": _restate_family(
                market.corporate_action_binding(execution_session), "corporate_action_v1"
            ),
            "risk_decision_binding": None,
            "lot_size": template["lot_size"],
            "fee_policy": dict(template["fee_policy"]),
            "price_policy": dict(template["price_policy"]),
            "quantity_policy": dict(template["quantity_policy"]),
            "data_delay_policy": template["data_delay_policy"],
            "data_delay_reason": template["data_delay_reason"],
            "serialization_profiles": dict(template["serialization_profiles"]),
            "run_id": run_id,
            "created_at": created_at,
            "quality_report_checksum_sha256": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
            "generation_id": "0" * 64,
        }

    def _bindings_for_date(
        self,
        *,
        config: Mapping[str, Any],
        plan_manifest: Mapping[str, Any],
        result_manifest: Mapping[str, Any],
        state_manifest: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        def binding(family: str, manifest: Mapping[str, Any], *, checksum: str, path: str) -> dict[str, Any]:
            return {
                "output_family": family,
                "generation_id": manifest["generation_id"],
                "manifest_digest_sha256": manifest["manifest_digest_sha256"],
                "data_checksum_sha256": checksum,
                "physical_path": path,
                "quality_decision_checksum_sha256": manifest["quality_report_checksum_sha256"],
                "failure_reason": None,
            }

        execution_id = config["execution_id"]
        return [
            binding(
                "execution_config", config,
                checksum=config["manifest_digest_sha256"],
                path=f"execution-configs/{execution_id}/generation={config['generation_id']}",
            ),
            binding(
                "order_plan", plan_manifest,
                checksum=plan_manifest["data_checksum_sha256"],
                path=(
                    f"order-plans/{execution_id}/{plan_manifest['execution_date']}"
                    f"/generation={plan_manifest['generation_id']}"
                ),
            ),
            binding(
                "execution_result", result_manifest,
                checksum=result_manifest["data_checksum_sha256"],
                path=(
                    f"execution-results/{execution_id}/{result_manifest['execution_date']}"
                    f"/generation={result_manifest['generation_id']}"
                ),
            ),
            binding(
                "paper_portfolio_state", state_manifest,
                checksum=state_manifest["data_checksum_sha256"],
                path=(
                    f"paper-states/{execution_id}/{state_manifest['state_date']}"
                    f"/generation={state_manifest['generation_id']}"
                ),
            ),
        ]


@dataclass(frozen=True)
class PaperExecutionStageResult:
    """Ordered 4N paper stage bindings plus the published stage state."""

    bindings: list[dict[str, Any]]
    decision_dates: list[str]
    published_state: Any


def _plan_manifest(
    *,
    config: Mapping[str, Any],
    frame: pd.DataFrame,
    residual: Sequence[int],
) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "execution_id": config["execution_id"],
        "state_mode": config["state_mode"],
        "execution_config_generation_id": config["generation_id"],
        "execution_date": config["execution_date"],
        "decision_date": config["decision_date"],
        "data_file": "data.parquet",
        "data_checksum_sha256": "0" * 64,
        "columns": PaperOrderPlanner.COLUMNS,
        "dtypes": PaperOrderPlanner.DTYPES,
        "row_count": int(len(frame)),
        "key_uniqueness": ["plan_sequence"],
        "logical_fingerprint": _plan_logical_fingerprint(frame),
        "serialization_profile_id": "parquet-v1",
        "target_weights_binding": config["target_weights_binding"],
        "input_state_binding": config["input_state_binding"],
        "initial_state_provenance_sha256": (
            config["initial_state"]["provenance_sha256"]
            if config["state_mode"] == "initial"
            else None
        ),
        "market_data_binding": {
            "family": config["market_data_binding"]["dataset_family"],
            "generation_id": config["market_data_binding"]["generation_id"],
            "manifest_digest_sha256": config["market_data_binding"]["manifest_digest_sha256"],
        },
        "calendar_binding": config["calendar_binding"],
        "suspension_binding": config["suspension_binding"],
        "corporate_action_binding": config["corporate_action_binding"],
        "risk_decision_binding": config["risk_decision_binding"],
        "ordered_cash_residual_sequence": list(residual),
        "run_id": config["run_id"],
        "created_at": config["created_at"],
        "quality_report_checksum_sha256": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
        "generation_id": "0" * 64,
    }


def _plan_logical_fingerprint(frame: pd.DataFrame) -> str:
    records = frame.sort_values("plan_sequence", kind="mergesort").to_dict(orient="records")
    return sha256_json(records)
