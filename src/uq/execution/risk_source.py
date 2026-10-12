"""Paper-execution risk-decision source.

The paper stage must obtain one ``order_submission`` risk decision per decision
date, bound to that date's execution-config stable generation, before the config
store will publish.  This module is the production source for that decision.

First slice: the per-date order-submission decision is the governed
portfolio-level risk evaluation of that day's target weights, re-scoped to
``order_submission`` and bound to the config generation.  Per-candidate-order
refinement (evaluating each planned order against order-scope rules) is a later
slice; the decision is already a real evaluated artifact whose policy/review/
state lineage is enforced by ``ExecutionConfigStore.publish``.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import ContractError
from ..risk.contracts import risk_contract_identities
from ..risk.engine import RiskEngine


class RiskPolicyOrderSubmissionSource:
    """Turn a reviewed risk policy into per-decision-date submission decisions."""

    def __init__(
        self,
        *,
        policy: Mapping[str, Any],
        policy_review: Mapping[str, Any],
        state: Mapping[str, Any] | None = None,
        state_payload: Mapping[str, Any] | None = None,
        engine: RiskEngine | None = None,
    ) -> None:
        self.policy = dict(policy)
        self.policy_review = dict(policy_review)
        self.state = dict(state) if state is not None else None
        self.state_payload = dict(state_payload) if state_payload is not None else None
        self.engine = engine or RiskEngine()

    def __call__(
        self,
        decision_date: str,
        config_generation_id: str,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        required = {
            "target_weights_manifest", "target_weights_frame", "definition", "universe",
        }
        missing = sorted(key for key in required if key not in context)
        if missing:
            raise ContractError(f"paper risk source is missing context: {missing}")
        if not isinstance(config_generation_id, str) or len(config_generation_id) != 64:
            raise ContractError("paper risk source requires the config stable generation")
        decision = self.engine.evaluate_portfolio(
            policy=self.policy,
            policy_review=self.policy_review,
            target_weights=context["target_weights_manifest"],
            target_weights_frame=context["target_weights_frame"],
            definition=context["definition"],
            universe=context["universe"],
            as_of_date=decision_date,
            state=self.state,
            state_payload=self.state_payload,
        )
        decision["decision_scope"] = "order_submission"
        decision["binding_config_generation_id"] = config_generation_id
        decision["generation_id"], decision["manifest_digest_sha256"] = risk_contract_identities(
            decision, schema_name="risk_decision"
        )
        return decision
