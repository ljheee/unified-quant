"""Independent portfolio publication risk gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from ..contracts.model_layer import sha256_json
from ..errors import ContractError
from .contracts import risk_contract_identities
from .engine import RiskEngine
from .stores import RiskDecisionStore, RiskEventStore, RiskPolicyStore, RiskRunStore, RiskStateStore, make_binding


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
