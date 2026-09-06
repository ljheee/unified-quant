"""Independent portfolio publication risk gate."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from ..contracts.canonical_v2 import file_sha256_bytes
from ..contracts.model_layer import sha256_json
from ..errors import ContractError
from .contracts import risk_contract_identities
from .engine import RiskEngine
from .stores import RiskDecisionStore, RiskEventStore, RiskPolicyStore, RiskRunStore, RiskStateStore, make_binding


_BACKTEST_GATE_CODE_FINGERPRINT = hashlib.sha256(
    b"uq/risk/backtest-pre-trade-gate-v1"
).hexdigest()


def _candidate_binding(order: Mapping[str, Any]) -> dict[str, str]:
    generation = sha256_json(dict(order))
    return {
        "family": "candidate_order_v1",
        "generation_id": generation,
        "manifest_digest_sha256": sha256_json({
            "generation_id": generation,
            "candidate_order": dict(order),
        }),
    }


def _content_binding(document: Mapping[str, Any], *, family: str) -> dict[str, str]:
    generation = sha256_json(dict(document))
    return {
        "family": family,
        "generation_id": generation,
        "manifest_digest_sha256": sha256_json({
            "generation_id": generation,
            "consumer": dict(document),
        }),
    }


class PortfolioPublicationRiskGate:
    """Evaluate target weights and persist immutable risk evidence before publication."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.policy_store = RiskPolicyStore(root)
        self.state_store = RiskStateStore(root)
        self.decision_store = RiskDecisionStore(root)
        self.event_store = RiskEventStore(root)
        self.run_store = RiskRunStore(root)
        self.engine = RiskEngine()
        self._seen_order_ids: set[str] = set()

    def gate(
        self,
        *,
        policy: Mapping[str, Any],
        policy_review: Mapping[str, Any],
        target_weights: Mapping[str, Any],
        target_weights_frame: pd.DataFrame,
        definition: Mapping[str, Any],
        universe: Mapping[str, Any],
        industry_mapping: Mapping[str, str] | None = None,
        state: Mapping[str, Any] | None = None,
        state_payload: Mapping[str, Any] | None = None,
        previous_target_weights: Mapping[str, float] | None = None,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        if as_of_date is None:
            as_of_date = target_weights["decision_date"]
        decision = self.engine.evaluate_portfolio(
            policy=policy,
            policy_review=policy_review,
            target_weights=target_weights,
            target_weights_frame=target_weights_frame,
            definition=definition,
            universe=universe,
            as_of_date=as_of_date,
            industry_mapping=industry_mapping,
            state=state,
            state_payload=state_payload,
            previous_target_weights=previous_target_weights,
        )
        self.policy_store.publish(dict(policy))
        if state is not None and state_payload is not None:
            self.state_store.publish(dict(state), dict(state_payload))
        payload = {
            "action": decision["action"],
            "constraints": decision["constraints"],
            "decision_digest": decision["decision_digest"],
            "findings": decision["findings"],
        }
        self.decision_store.publish(decision, payload)
        if decision["action"] in {"block", "block_order", "block_new_buy", "halt_strategy"}:
            raise ContractError(
                f"portfolio publication blocked by risk decision: {decision['generation_id']}"
            )
        event = {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "event_id": sha256_json({
                "decision_generation_id": decision["generation_id"],
                "event_class": "risk_triggered" if decision["action"] != "allow" else "risk_cleared",
            }),
            "event_class": "risk_triggered" if decision["action"] != "allow" else "risk_cleared",
            "scope_type": "portfolio",
            "scope_id": "publication",
            "event_sequence_number": 1,
            "as_of_date": decision["as_of_date"],
            "visible_through": decision["visible_through"],
            "policy_generation_id": policy["generation_id"],
            "state_generation_id": state["generation_id"] if state is not None else None,
            "decision_generation_id": decision["generation_id"],
            "reason": "portfolio publication risk decision",
            "producer": decision["producer"],
            "serialization_profile": "json-canonical-v1",
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        event_generation, event_digest = risk_contract_identities(event, schema_name="risk_event")
        event["generation_id"] = event_generation
        event["manifest_digest_sha256"] = event_digest
        self.event_store.publish(event)
        run = {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "run_scope": "portfolio_publication",
            "risk_policy_binding": make_binding(policy, family="risk_policy_v1"),
            "risk_state_binding": make_binding(state, family="risk_state_v1") if state is not None else None,
            "consumer_binding": make_binding(target_weights, family="target_weights_v1"),
            "decision_bindings": [make_binding(decision, family="risk_decision_v1")],
            "event_bindings": [make_binding(event, family="risk_event_v1")],
            "as_of_date": decision["as_of_date"],
            "visible_through": decision["visible_through"],
            "producer": decision["producer"],
            "serialization_profile": "json-canonical-v1",
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        run_generation, run_digest = risk_contract_identities(run, schema_name="risk_run")
        run["generation_id"] = run_generation
        run["manifest_digest_sha256"] = run_digest
        self.run_store.publish(run)
        return decision

    def evaluate_order(
        self,
        *,
        policy: Mapping[str, Any],
        policy_review: Mapping[str, Any],
        order: Mapping[str, Any],
        execution_date: str,
        execution_price: float,
        previous_close: float,
        decision_date_volume: float,
        holdings: Mapping[str, int],
        cash: float,
        suspended: bool,
        sellable_shares: int,
        limit_ratio: float,
        board_lot: int,
        slippage_bps: float,
    ) -> dict[str, Any]:
        order_id = str(order["order_id"])
        if order_id in self._seen_order_ids:
            raise ContractError("duplicate or stale order id")
        decision = self.engine.evaluate_order(
            policy=policy,
            policy_review=policy_review,
            order=order,
            execution_date=execution_date,
            execution_price=execution_price,
            previous_close=previous_close,
            decision_date_volume=decision_date_volume,
            holdings=holdings,
            cash=cash,
            suspended=suspended,
            sellable_shares=sellable_shares,
            limit_ratio=limit_ratio,
            board_lot=board_lot,
            slippage_bps=slippage_bps,
            seen_order_ids=self._seen_order_ids,
        )
        self._seen_order_ids.add(order_id)
        return decision

    def run_with_risk_gate(self, engine: Any, *, config: Mapping[str, Any], policy: Mapping[str, Any], policy_review: Mapping[str, Any], **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        decisions: list[dict[str, Any]] = []
        candidate_orders: list[dict[str, Any]] = []
        event_bindings: list[dict[str, str]] = []
        self._seen_order_ids.clear()

        def gate(candidate: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
            decision = self.evaluate_order(
                policy=policy,
                policy_review=policy_review,
                order=candidate,
                execution_date=context["execution_date"],
                execution_price=context["execution_price"],
                previous_close=context["previous_close"],
                decision_date_volume=context["decision_date_volume"],
                holdings=context["holdings"],
                cash=context["cash"],
                suspended=context["suspended"],
                sellable_shares=context["sellable_shares"],
                limit_ratio=context["limit_ratio"],
                board_lot=context["board_lot"],
                slippage_bps=context["slippage_bps"],
            )
            decisions.append(decision)
            candidate_orders.append(dict(candidate))
            self.decision_store.publish(decision, {
                "action": decision["action"],
                "constraints": decision["constraints"],
                "decision_digest": decision["decision_digest"],
                "findings": decision["findings"],
            })
            event = {
                "contract_version": 1,
                "schema_version": "1.0.0",
                "event_id": sha256_json({
                    "decision_generation_id": decision["generation_id"],
                    "event_class": "risk_triggered" if decision["action"] != "allow" else "risk_cleared",
                }),
                "event_class": "risk_triggered" if decision["action"] != "allow" else "risk_cleared",
                "scope_type": "order",
                "scope_id": "backtest_pre_trade",
                "event_sequence_number": len(decisions),
                "as_of_date": decision["as_of_date"],
                "visible_through": decision["visible_through"],
                "policy_generation_id": policy["generation_id"],
                "state_generation_id": None,
                "decision_generation_id": decision["generation_id"],
                "reason": "backtest pre-trade order risk decision",
                "producer": decision["producer"],
                "serialization_profile": "json-canonical-v1",
                "generation_id": "0" * 64,
                "manifest_digest_sha256": "0" * 64,
            }
            event_generation, event_digest = risk_contract_identities(event, schema_name="risk_event")
            event["generation_id"] = event_generation
            event["manifest_digest_sha256"] = event_digest
            self.event_store.publish(event)
            event_bindings.append(make_binding(event, family="risk_event_v1"))
            return decision

        manifest, artifacts = engine.run(config=config, **kwargs, risk_gate=gate)

        payloads = {
            f"decision-{index}.json": {
                "candidate_order": candidate_orders[index],
                "decision": {
                    "action": decision["action"],
                    "constraints": decision["constraints"],
                    "decision_digest": decision["decision_digest"],
                    "findings": decision["findings"],
                },
            }
            for index, decision in enumerate(decisions)
        }
        run = {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "run_scope": "order_submission",
            "risk_policy_binding": make_binding(policy, family="risk_policy_v1"),
            "risk_state_binding": None,
            "consumer_binding": _content_binding(config, family="backtest_config_v1"),
            "decision_bindings": [make_binding(decision, family="risk_decision_v1") for decision in decisions],
            "event_bindings": event_bindings,
            "as_of_date": config["start_date"],
            "visible_through": f"{config['end_date']}T15:00:00+00:00",
            "producer": {
                "producer_code_fingerprint": _BACKTEST_GATE_CODE_FINGERPRINT,
                "run_id": str(uuid.uuid4()),
                "created_at": "1970-01-01T00:00:00+00:00",
            },
            "serialization_profile": "json-canonical-v1",
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        run["files"] = [
            {
                "path": path,
                "sha256": file_sha256_bytes(
                    (json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                ),
                "byte_size": len(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))) + 1,
                "serialization_profile": "json-canonical-v1",
            }
            for path, payload in payloads.items()
        ]
        run_generation, run_digest = risk_contract_identities(run, schema_name="risk_run")
        run["generation_id"] = run_generation
        run["manifest_digest_sha256"] = run_digest
        self.run_store.publish(run, payloads)
        return manifest, artifacts, run

    def read_verified_risk_run(self, generation_id: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        run, payloads = self.run_store.read(generation_id)
        for index, binding in enumerate(run["decision_bindings"]):
            decision, decision_payload = self.decision_store.read(binding["generation_id"])
            if binding != make_binding(decision, family="risk_decision_v1"):
                raise ContractError(f"risk run decision binding mismatch at index {index}")
            expected_payload = {
                "decision": {
                    "action": decision["action"],
                    "constraints": decision["constraints"],
                    "decision_digest": decision["decision_digest"],
                    "findings": decision["findings"],
                },
            }
            path = f"decision-{index}.json"
            if payloads.get(path) is None:
                raise ContractError(f"risk run decision payload mismatch: {path}")
            actual_candidate = payloads[path]["candidate_order"]
            if _candidate_binding(actual_candidate) != decision["input_bindings"][0]:
                raise ContractError(f"risk run candidate order lineage mismatch: {path}")
            if payloads[path]["decision"] != expected_payload["decision"]:
                raise ContractError(f"risk run decision payload mismatch: {path}")
        for index, binding in enumerate(run["event_bindings"]):
            event = self.event_store.read(binding["generation_id"])
            if binding != make_binding(event, family="risk_event_v1"):
                raise ContractError(f"risk run event binding mismatch at index {index}")
        return run, payloads
