"""Deterministic strategy-level loss-control state evaluation."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from ..contracts.model_layer import sha256_json
from ..errors import ContractError
from .contracts import (
    risk_contract_identities,
    validate_enforceable_de_risk_contract,
    validate_risk_de_risk_contract_governance,
    validate_risk_exception_governance,
    validate_risk_manifest,
    validate_risk_policy_governance,
    validate_risk_policy_scope_compatibility,
)
from .stores import make_binding

_CODE_FINGERPRINT = hashlib.sha256(b"uq/risk/state-evaluation-v1").hexdigest()
_RUN_ID = "00000000-0000-0000-0000-000000000001"
_LOSS_CONTROL_ACTIONS = {"de_risk", "flatten", "halt_strategy"}


def _binding(document: Mapping[str, Any], *, family: str) -> dict[str, str]:
    return {
        "family": family,
        "generation_id": document["generation_id"],
        "manifest_digest_sha256": document["manifest_digest_sha256"],
    }


def _iso_date(value: Any, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name} must be an ISO date") from exc


def _iso_datetime(value: Any, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name} must be an ISO date-time") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ContractError(f"{name} must be a sha256 digest")
    return value


def _validate_policy(policy: Mapping[str, Any], policy_review: Mapping[str, Any]) -> None:
    validate_risk_manifest("risk_policy", dict(policy))
    if policy["policy_scope"] != "strategy":
        raise ContractError("strategy state requires a strategy policy")
    validate_risk_policy_scope_compatibility(policy)
    if policy["activation_status"] != "approved":
        raise ContractError("risk policy is not approved")
    validate_risk_policy_governance(policy, policy_review)
    if len(policy["rules"]) != 1:
        raise ContractError("strategy state evaluation requires exactly one rule")
    for rule in policy["rules"]:
        if rule["rule_scope"] != "strategy":
            raise ContractError("strategy state policy contains a cross-scope rule")
        if rule["metric"] not in {"strategy_drawdown", "strategy_volatility"}:
            raise ContractError(f"unsupported strategy state metric: {rule['metric']}")
        if rule["action"] not in _LOSS_CONTROL_ACTIONS:
            raise ContractError(f"strategy state rule has unsupported action: {rule['action']}")
    if len(policy["rules"]) != len({rule["rule_id"] for rule in policy["rules"]}):
        raise ContractError("strategy policy contains duplicate rule ids")


def _validate_calendar_binding(binding: Mapping[str, Any]) -> None:
    if set(binding) != {"family", "generation_id", "manifest_digest_sha256"}:
        raise ContractError("risk calendar binding is malformed")
    if binding["family"] != "trading_calendar_v1":
        raise ContractError("risk state requires trading_calendar_v1")
    _sha256(binding["generation_id"], "risk calendar generation")
    _sha256(binding["manifest_digest_sha256"], "risk calendar manifest digest")


def _validate_observation(observation: Mapping[str, Any], metric_names: set[str]) -> dict[str, Any]:
    if not isinstance(observation, Mapping):
        raise ContractError("strategy observation must be a mapping")
    as_of_date = _iso_date(observation.get("as_of_date"), "observation as_of_date")
    visible_through = _iso_datetime(observation.get("visible_through"), "observation visible_through")
    if visible_through.date() < as_of_date:
        raise ContractError("strategy observation visibility precedes its date")
    observation_id = _sha256(observation.get("observation_generation_id"), "observation generation")
    metrics = observation.get("metrics")
    if not isinstance(metrics, Mapping) or not metric_names.issubset(metrics):
        raise ContractError("strategy observation lacks declared metrics")
    validated_metrics: dict[str, float] = {}
    for name in metric_names:
        value = metrics[name]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise ContractError(f"strategy observation metric is non-finite: {name}")
        if float(value) < 0.0:
            raise ContractError(f"strategy observation metric is negative: {name}")
        validated_metrics[name] = round(float(value), 12)
    return {
        "as_of_date": as_of_date,
        "visible_through": visible_through,
        "observation_generation_id": observation_id,
        "metrics": validated_metrics,
    }


def _validate_de_risk_contract(
    contract: Mapping[str, Any] | None,
    review: Mapping[str, Any] | None,
) -> None:
    if contract is None:
        raise ContractError("strategy loss-control action requires a de-risk contract")
    validate_risk_manifest("risk_de_risk_contract", dict(contract))
    if review is None:
        raise ContractError("strategy loss-control action requires a de-risk contract review")
    validate_risk_manifest("risk_review_decision", dict(review))
    validate_risk_de_risk_contract_governance(contract, review)
    if contract.get("status") != "approved" or not contract.get("execution_available"):
        raise ContractError("risk de-risk contract is not enforceable")


def _validate_exception(
    exception: Mapping[str, Any] | None,
    review: Mapping[str, Any] | None,
) -> None:
    if exception is None or review is None:
        raise ContractError("strategy exception requires both exception and review")
    validate_risk_manifest("risk_exception", dict(exception))
    validate_risk_manifest("risk_review_decision", dict(review))
    validate_risk_exception_governance(exception, review)


def _compare(observed: float, operator: str, threshold: float) -> bool:
    if operator == "greater_than":
        return observed > threshold
    if operator == "greater_than_or_equal":
        return observed >= threshold
    if operator == "less_than":
        return observed < threshold
    if operator == "less_than_or_equal":
        return observed <= threshold
    raise ContractError(f"unsupported risk operator: {operator}")


def _event(
    *,
    event_class: str,
    scope_id: str,
    event_sequence: int,
    as_of_date: date,
    visible_through: datetime,
    policy_generation_id: str,
    state_generation_id: str,
    reason: str,
) -> dict[str, Any]:
    unsigned = {
        "event_class": event_class,
        "scope_type": "strategy",
        "scope_id": scope_id,
        "event_sequence_number": event_sequence,
        "as_of_date": as_of_date.isoformat(),
        "visible_through": visible_through.isoformat(),
        "policy_generation_id": policy_generation_id,
        "state_generation_id": state_generation_id,
        "decision_generation_id": None,
        "reason": reason,
    }
    event_id = sha256_json(unsigned)
    event = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        **unsigned,
        "event_id": event_id,
        "producer": {
            "producer_code_fingerprint": _CODE_FINGERPRINT,
            "run_id": _RUN_ID,
            "created_at": "1970-01-01T00:00:00+00:00",
        },
        "serialization_profile": "json-canonical-v1",
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    generation, digest = risk_contract_identities(event, schema_name="risk_event")
    event["generation_id"] = generation
    event["manifest_digest_sha256"] = digest
    return event


def _state_document(
    *,
    payload: Mapping[str, Any],
    as_of_date: date,
    visible_through: datetime,
    scope_id: str,
    policy: Mapping[str, Any],
    observation: Mapping[str, Any],
    prior_state: Mapping[str, Any] | None,
    supersedes: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    content = (
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    upstream_bindings = [
        _binding(policy, family="risk_policy_v1"),
        {
            "family": "strategy_observation_v1",
            "generation_id": observation["observation_generation_id"],
            "manifest_digest_sha256": sha256_json({
                "as_of_date": observation["as_of_date"].isoformat(),
                "visible_through": observation["visible_through"].isoformat(),
                "metrics": observation["metrics"],
            }),
        },
    ]
    if prior_state is not None:
        upstream_bindings.append(_binding(prior_state, family="risk_state_v1"))
    document = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "state_scope_type": "strategy",
        "scope_id": scope_id,
        "as_of_date": as_of_date.isoformat(),
        "visible_through": visible_through.isoformat(),
        "state_status": "initial" if prior_state is None else "updated",
        "previous_state_generation_id": prior_state["generation_id"] if prior_state else None,
        "supersedes_state_generation_id": prior_state["generation_id"] if supersedes else None,
        "calendar_binding": payload["calendar_binding"],
        "upstream_bindings": upstream_bindings,
        "metric_names": sorted(payload["metrics"]),
        "files": [{
            "path": "state.json",
            "sha256": hashlib.sha256(content).hexdigest(),
            "byte_size": len(content),
            "serialization_profile": "json-canonical-v1",
        }],
        "producer": {
            "producer_code_fingerprint": _CODE_FINGERPRINT,
            "run_id": _RUN_ID,
            "created_at": "1970-01-01T00:00:00+00:00",
        },
        "serialization_profile": "json-canonical-v1",
        "generation_id": "0" * 64,
        "manifest_digest_sha256": "0" * 64,
    }
    generation, digest = risk_contract_identities(document, schema_name="risk_state")
    document["generation_id"] = generation
    document["manifest_digest_sha256"] = digest
    return document, payload


def evaluate_strategy_state(
    *,
    policy: Mapping[str, Any],
    policy_review: Mapping[str, Any],
    observations: list[Mapping[str, Any]],
    calendar_binding: Mapping[str, Any],
    de_risk_contract: Mapping[str, Any] | None = None,
    de_risk_review: Mapping[str, Any] | None = None,
    exception: Mapping[str, Any] | None = None,
    exception_review: Mapping[str, Any] | None = None,
    prior_state: Mapping[str, Any] | None = None,
    prior_payload: Mapping[str, Any] | None = None,
    event_sequence: int = 0,
) -> dict[str, Any]:
    """Evaluate observations and return immutable state, events, and action."""
    _validate_policy(policy, policy_review)
    _validate_calendar_binding(calendar_binding)
    if not isinstance(observations, list) or not observations:
        raise ContractError("strategy state observations must be a non-empty list")
    if not isinstance(event_sequence, int) or isinstance(event_sequence, bool) or event_sequence < 0:
        raise ContractError("risk event sequence must be non-negative")

    if exception is not None:
        _validate_exception(exception, exception_review)
    if prior_state is not None:
        validate_risk_manifest("risk_state", dict(prior_state))
        if prior_payload is None or not isinstance(prior_payload, Mapping):
            raise ContractError("prior risk state payload is required")
        if prior_state["state_scope_type"] != "strategy":
            raise ContractError("prior risk state scope mismatch")

    rules = sorted(policy["rules"], key=lambda rule: (rule["priority"], rule["rule_id"]))
    rule = rules[0]
    metric_names = {item["metric"] for item in policy["rules"]}
    active = bool(prior_payload["active"]) if prior_payload is not None else False
    consecutive_triggers = int(prior_payload["consecutive_trigger_days"]) if prior_payload else 0
    last_trigger_date = prior_payload.get("last_trigger_date") if prior_payload else None
    cooldown_until = prior_payload.get("cooldown_until_date") if prior_payload else None
    previous_state = dict(prior_state) if prior_state is not None else None
    previous_payload = dict(prior_payload) if prior_payload is not None else None
    events: list[dict[str, Any]] = []
    sequence = event_sequence
    result: dict[str, Any] | None = None
    last_observation: dict[str, Any] | None = None

    for raw_observation in observations:
        observation = _validate_observation(raw_observation, metric_names)
        last_observation = observation
        as_of = observation["as_of_date"]
        visible_through = observation["visible_through"]
        if previous_state is not None:
            previous_as_of = _iso_date(previous_state["as_of_date"], "prior state as_of_date")
            previous_visible = _iso_datetime(previous_state["visible_through"], "prior state visibility")
            if as_of < previous_as_of:
                raise ContractError("risk state observation is stale")
            if as_of == previous_as_of and visible_through <= previous_visible:
                raise ContractError("late risk state visibility must be strictly later")
        observed = observation["metrics"][rule["metric"]]
        breached = _compare(observed, rule["operator"], float(rule["threshold"]))
        consecutive_triggers = consecutive_triggers + 1 if breached else 0
        was_active = active
        exit_threshold = rule.get("hysteresis_exit_threshold")
        if was_active and exit_threshold is not None:
            active = _compare(observed, rule["operator"], float(exit_threshold))
        elif was_active:
            active = breached
        elif as_of.isoformat() <= (cooldown_until or ""):
            active = False
        elif breached:
            active = True
            last_trigger_date = as_of.isoformat()
            cooldown_until = (as_of + timedelta(days=int(rule.get("cooldown_days", 0)))).isoformat()

        current_event_count = len(events)
        entered = active and not was_active
        cleared = was_active and not active
        planned_action = rule["action"] if entered else None
        if planned_action is not None:
            _validate_de_risk_contract(de_risk_contract, de_risk_review)
            validate_enforceable_de_risk_contract(de_risk_contract, action=planned_action)

        exception_applied = False
        exception_expired = False
        if planned_action is not None and exception is not None:
            scope_matches = (
                exception["scope_type"] == "strategy"
                and exception["scope_id"] == str(policy["policy_id"])
            )
            if (
                not scope_matches
                or exception["rule_id"] != rule["rule_id"]
                or exception["permitted_action"] != planned_action
            ):
                raise ContractError("risk exception does not match the loss-control action")
            if as_of > _iso_date(exception["expires_at"], "exception expires_at"):
                exception_expired = True
            elif exception["status"] == "approved":
                exception_applied = True
            else:
                raise ContractError("risk exception is not approved")

        if entered and not exception_applied:
            sequence += 1
            events.append(_event(
                event_class="risk_triggered",
                scope_id=str(policy["policy_id"]),
                event_sequence=sequence,
                as_of_date=as_of,
                visible_through=visible_through,
                policy_generation_id=policy["generation_id"],
                state_generation_id="0" * 64,
                reason=f"{rule['rule_id']} triggered at {observed}",
            ))
        if entered and exception_applied:
            sequence += 1
            events.append(_event(
                event_class="exception_applied",
                scope_id=str(policy["policy_id"]),
                event_sequence=sequence,
                as_of_date=as_of,
                visible_through=visible_through,
                policy_generation_id=policy["generation_id"],
                state_generation_id="0" * 64,
                reason=f"exception {exception['exception_id']} suppresses {planned_action}",
            ))
        if entered and exception_expired:
            sequence += 1
            events.append(_event(
                event_class="exception_expired",
                scope_id=str(policy["policy_id"]),
                event_sequence=sequence,
                as_of_date=as_of,
                visible_through=visible_through,
                policy_generation_id=policy["generation_id"],
                state_generation_id="0" * 64,
                reason=f"exception {exception['exception_id']} expired; enforcement restored",
            ))
        if cleared:
            sequence += 1
            events.append(_event(
                event_class="risk_cleared",
                scope_id=str(policy["policy_id"]),
                event_sequence=sequence,
                as_of_date=as_of,
                visible_through=visible_through,
                policy_generation_id=policy["generation_id"],
                state_generation_id="0" * 64,
                reason=f"{rule['rule_id']} cleared at {observed}",
            ))

        state_payload = {
            "calendar_binding": dict(calendar_binding),
            "metrics": dict(observation["metrics"]),
            "active": active,
            "triggered_rule_id": rule["rule_id"],
            "consecutive_trigger_days": consecutive_triggers,
            "last_trigger_date": last_trigger_date,
            "cooldown_until_date": cooldown_until,
        }
        document, payload = _state_document(
            payload=state_payload,
            as_of_date=as_of,
            visible_through=visible_through,
            scope_id=str(policy["policy_id"]),
            policy=policy,
            observation=observation,
            prior_state=previous_state,
            supersedes=previous_state is not None and previous_state["as_of_date"] == as_of.isoformat(),
        )
        for event in events[current_event_count:]:
            event["state_generation_id"] = document["generation_id"]
            event["generation_id"], event["manifest_digest_sha256"] = risk_contract_identities(
                event, schema_name="risk_event"
            )
        sequence += 1
        events.append(_event(
            event_class="state_snapshot_published",
            scope_id=document["scope_id"],
            event_sequence=sequence,
            as_of_date=as_of,
            visible_through=visible_through,
            policy_generation_id=policy["generation_id"],
            state_generation_id=document["generation_id"],
            reason=f"state {document['generation_id']} published",
        ))
        previous_state = document
        previous_payload = payload
        result = {
            "state": document,
            "state_payload": payload,
            "events": list(events),
            "action": "allow" if exception_applied else (planned_action if entered else None),
            "transition_semantics": de_risk_contract["transition_semantics"] if planned_action else None,
            "exception_applied": exception_applied,
            "exception_expired": exception_expired,
        }

    if result is None or last_observation is None:
        raise ContractError("strategy state produced no result")
    return result
