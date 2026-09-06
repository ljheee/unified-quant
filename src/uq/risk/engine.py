"""Deterministic portfolio risk evaluation."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Any, Mapping

import pandas as pd

from ..contracts.canonical_v2 import file_sha256_bytes
from ..contracts.gate_contracts import adjustment_snapshot_generation, validate_contract
from ..contracts.model_layer import ModelContractLoader, model_manifest_identities, sha256_json
from ..errors import ContractError
from .contracts import (
    risk_contract_identities,
    validate_risk_manifest,
    validate_risk_policy_governance,
    validate_risk_policy_scope_compatibility,
)
from .stores import make_binding

_WEIGHT_TOLERANCE = 1e-12
_CODE_FINGERPRINT = hashlib.sha256(b"uq/risk/portfolio-evaluation-v1").hexdigest()
_RUN_ID = "00000000-0000-0000-0000-000000000001"
_ACTION_RANK = {
    "block": 0,
    "halt_strategy": 1,
    "flatten": 2,
    "de_risk": 3,
    "block_new_buy": 4,
    "block_order": 5,
    "resize": 6,
    "escalate_review": 7,
    "warn": 8,
    "allow": 9,
}
_SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2}
_SUPPORTED_METRICS = {
    "portfolio_gross_exposure_limit",
    "portfolio_single_name_limit",
    "portfolio_top_n_limit",
    "portfolio_industry_limit",
    "portfolio_turnover_limit",
    "portfolio_cash_reserve_limit",
}
_RULE_SCOPES = {
    "portfolio_gross_exposure_limit": "portfolio",
    "portfolio_single_name_limit": "portfolio",
    "portfolio_top_n_limit": "portfolio",
    "portfolio_industry_limit": "portfolio",
    "portfolio_turnover_limit": "portfolio",
    "portfolio_cash_reserve_limit": "portfolio",
}


def _binding(document: Mapping[str, Any], *, family: str) -> dict[str, str]:
    return {
        "family": family,
        "generation_id": document["generation_id"],
        "manifest_digest_sha256": document["manifest_digest_sha256"],
    }


def _require_date(value: Any, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name} must be an ISO date") from exc


def _file_entry(path: str, content: bytes) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": file_sha256_bytes(content),
        "byte_size": len(content),
        "serialization_profile": "json-canonical-v1",
    }


def _json_content(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _validate_target_weights(manifest: Mapping[str, Any], frame: pd.DataFrame) -> None:
    ModelContractLoader.validate("target_weights", dict(manifest))
    expected_generation, expected_digest = model_manifest_identities(
        manifest, schema_name="target_weights"
    )
    if manifest["generation_id"] != expected_generation or manifest["manifest_digest_sha256"] != expected_digest:
        raise ContractError("risk target weights identity mismatch")
    if list(frame.columns) != manifest["columns"]:
        raise ContractError("risk target weights columns mismatch")
    if len(frame) != manifest["row_count"] or manifest["instrument_count"] != len(frame):
        raise ContractError("risk target weights row count mismatch")
    if frame.duplicated(subset=["instrument"]).any():
        raise ContractError("risk target weights contain duplicate instruments")
    if frame["weight"].isna().any() or not frame["weight"].map(math.isfinite).all():
        raise ContractError("risk target weights contain non-finite values")
    if frame["weight"].lt(-_WEIGHT_TOLERANCE).any():
        raise ContractError("risk target weights contain negative weights")
    expected_fingerprint = sha256_json({
        "instruments": sorted(frame["instrument"].astype(str).tolist()),
        "weights": [
            round(float(value), 12)
            for value in frame.sort_values("instrument")["weight"].tolist()
        ],
    })
    if manifest["logical_fingerprint"] != expected_fingerprint:
        raise ContractError("risk target weights logical fingerprint mismatch")
    actual_total = float(frame["weight"].clip(lower=0.0).sum())
    if not math.isclose(float(manifest["total_stock_weight"]), actual_total, abs_tol=_WEIGHT_TOLERANCE):
        raise ContractError("risk target weights total stock weight mismatch")


def _validate_definition_binding(
    definition: Mapping[str, Any], target_weights: Mapping[str, Any]
) -> None:
    ModelContractLoader.validate("portfolio_definition", dict(definition))
    expected_generation, expected_digest = model_manifest_identities(
        definition, schema_name="portfolio_definition"
    )
    if definition["generation_id"] != expected_generation or definition["manifest_digest_sha256"] != expected_digest:
        raise ContractError("risk portfolio definition identity mismatch")
    if target_weights["portfolio_definition_generation_id"] != definition["generation_id"]:
        raise ContractError("risk portfolio definition lineage mismatch")
    if target_weights["prediction_set_generation_id"] != definition.get("prediction_set_generation_id"):
        raise ContractError("risk prediction lineage mismatch")


def _validate_universe_binding(
    universe: Mapping[str, Any], definition: Mapping[str, Any], target_weights: Mapping[str, Any]
) -> None:
    if universe.get("generation_id") != definition.get("universe_snapshot_generation_id"):
        raise ContractError("risk universe definition lineage mismatch")
    if universe.get("generation_id") != target_weights.get("universe_snapshot_generation_id"):
        raise ContractError("risk universe target weights lineage mismatch")
    validate_contract("universe_snapshot.v1.json", dict(universe))
    if not isinstance(universe.get("generation_id"), str) or len(universe["generation_id"]) != 64:
        raise ContractError("risk universe generation is invalid")
    expected_generation = adjustment_snapshot_generation(
        {key: value for key, value in universe.items() if key != "generation_id"}
    )
    if universe["generation_id"] != expected_generation:
        raise ContractError("risk universe identity mismatch")


def _validate_policy_document(
    policy: Mapping[str, Any], as_of_date: str, policy_review: Mapping[str, Any]
) -> None:
    validate_risk_manifest("risk_policy", dict(policy))
    if policy["policy_scope"] != "portfolio":
        raise ContractError("portfolio risk policy scope mismatch")
    validate_risk_policy_scope_compatibility(policy)
    if policy["activation_status"] != "approved":
        raise ContractError("portfolio risk policy is not approved")
    validate_risk_policy_governance(policy, policy_review)
    rule_ids = [rule["rule_id"] for rule in policy["rules"]]
    if len(rule_ids) != len(set(rule_ids)):
        raise ContractError("portfolio risk policy contains overlapping rule ids")
    as_of = _require_date(as_of_date, "as_of_date")
    if as_of < date.fromisoformat(policy["effective_from"]):
        raise ContractError("portfolio risk policy is not yet effective")
    effective_to = policy.get("effective_to")
    if effective_to is not None and as_of > date.fromisoformat(effective_to):
        raise ContractError("portfolio risk policy has expired")


def _validate_state_binding(
    state: Mapping[str, Any] | None,
    state_payload: Mapping[str, Any] | None,
    target_weights: Mapping[str, Any],
) -> None:
    if state is None:
        return
    validate_risk_manifest("risk_state", dict(state))
    if state_payload is None:
        raise ContractError("risk state payload is required")
    if state["state_scope_type"] != "portfolio":
        raise ContractError("risk state scope mismatch")
    if state["as_of_date"] != target_weights["decision_date"]:
        raise ContractError("risk state decision date mismatch")
    upstream = [
        binding for binding in state["upstream_bindings"]
        if binding.get("family") == "target_weights_v1"
    ]
    if (
        len(upstream) != 1
        or upstream[0]["generation_id"] != target_weights["generation_id"]
        or upstream[0]["manifest_digest_sha256"] != target_weights["manifest_digest_sha256"]
    ):
        raise ContractError("risk state upstream lineage mismatch")
    if any(metric not in state_payload for metric in state["metric_names"]):
        raise ContractError("risk state payload does not contain declared metrics")


def _metrics(
    frame: pd.DataFrame,
    definition: Mapping[str, Any],
    industry_mapping: Mapping[str, str],
    previous_weights: Mapping[str, float],
) -> dict[str, float]:
    weights = dict(zip(frame["instrument"].astype(str), frame["weight"].astype(float), strict=True))
    gross = sum(value for value in weights.values() if value > 0.0)
    all_instruments = set(weights) | set(previous_weights)
    turnover = 0.5 * sum(
        abs(weights.get(key, 0.0) - previous_weights.get(key, 0.0))
        for key in all_instruments
    )
    return {
        "gross_stock_exposure": round(gross, 12),
        "max_single_weight": round(max(weights.values(), default=0.0), 12),
        "instrument_count": float(len(weights)),
        "industry_weight": round(max(industry_mapping.values(), default=0.0), 12),
        "turnover": round(turnover, 12),
        "cash_reserve": round(max(0.0, 1.0 - gross), 12),
    }


def _compare(observed: float, operator: str, threshold: float) -> bool:
    tolerance = _WEIGHT_TOLERANCE
    if operator == "greater_than":
        return observed > threshold
    if operator == "greater_than_or_equal":
        return observed >= threshold - tolerance
    if operator == "less_than":
        return observed < threshold
    if operator == "less_than_or_equal":
        return observed <= threshold + tolerance
    raise ContractError(f"unsupported risk operator: {operator}")


def _resize_value(
    rule_id: str, observed: float, threshold: float, state_required: bool
) -> float | None:
    if not state_required:
        return threshold
    return threshold


class RiskEngine:
    """Evaluate an immutable portfolio target-weight partition against one policy."""

    def evaluate_portfolio(
        self,
        *,
        policy: Mapping[str, Any],
        target_weights: Mapping[str, Any],
        target_weights_frame: pd.DataFrame,
        definition: Mapping[str, Any],
        universe: Mapping[str, Any],
        as_of_date: str,
        policy_review: Mapping[str, Any],
        industry_mapping: Mapping[str, str] | None = None,
        state: Mapping[str, Any] | None = None,
        state_payload: Mapping[str, Any] | None = None,
        previous_target_weights: Mapping[str, float] | None = None,
    ) -> dict[str, Any]:
        _validate_policy_document(policy, as_of_date, policy_review)
        _validate_target_weights(target_weights, target_weights_frame)
        _validate_definition_binding(definition, target_weights)
        _validate_universe_binding(universe, definition, target_weights)
        _validate_state_binding(state, state_payload, target_weights)

        mapping = dict(industry_mapping or {})
        previous = dict(previous_target_weights or {})
        rules = sorted(policy["rules"], key=lambda rule: (rule["priority"], rule["rule_id"]))
        known_metrics = _metrics(target_weights_frame, definition, mapping, previous)
        findings: list[dict[str, Any]] = []
        for rule in rules:
            if rule["rule_scope"] != "portfolio":
                raise ContractError(f"non-portfolio rule cannot evaluate portfolio: {rule['rule_id']}")
            if rule["rule_id"] not in _SUPPORTED_METRICS:
                raise ContractError(f"unsupported portfolio risk rule: {rule['rule_id']}")
            if _RULE_SCOPES[rule["rule_id"]] != "portfolio":
                raise ContractError(f"portfolio rule has wrong scope: {rule['rule_id']}")
            if rule["metric"] == "industry_weight":
                if not policy.get("industry_membership_binding"):
                    raise ContractError("industry limit requires industry_membership_binding")
                if not mapping:
                    raise ContractError("industry membership mapping is required")
            if rule["state_required"] and state is None:
                raise ContractError(f"risk state is required for rule: {rule['rule_id']}")
            observed = known_metrics.get(rule["metric"])
            if observed is None:
                raise ContractError(f"unresolved risk metric: {rule['metric']}")
            result = "passed" if _compare(observed, rule["operator"], rule["threshold"]) else "failed"
            findings.append({
                "rule_id": rule["rule_id"],
                "observed": observed,
                "threshold": rule["threshold"],
                "operator": rule["operator"],
                "result": result,
                "severity": rule["severity"],
                "action": rule["action"] if result == "failed" else "allow",
                "state_required": rule["state_required"],
            })
        failed = [finding for finding in findings if finding["result"] == "failed"]
        action = "allow"
        if failed:
            failed.sort(
                key=lambda item: (
                    _ACTION_RANK[item["action"]],
                    _SEVERITY_RANK[item["severity"]],
                    item["rule_id"],
                )
            )
            action = failed[0]["action"]
        if action not in _ACTION_RANK:
            raise ContractError(f"unsupported risk action: {action}")
        constraints: list[dict[str, Any]] = []
        if action == "resize":
            for finding in failed:
                if finding["rule_id"] in {
                    "portfolio_gross_exposure_limit",
                    "portfolio_single_name_limit",
                    "portfolio_industry_limit",
                    "portfolio_cash_reserve_limit",
                } and isinstance(finding["threshold"], (int, float)):
                    constraints.append({
                        "constraint_type": "allowed_fraction",
                        "value": float(finding["threshold"]),
                        "instrument": None,
                    })
            if not constraints:
                raise ContractError("resize rule lacks a supported resize target")
        decision_digest = sha256_json({"action": action, "constraints": constraints, "findings": findings})
        decision_payload = {
            "action": action,
            "constraints": constraints,
            "findings": findings,
            "decision_digest": decision_digest,
        }
        decision_content = _json_content(decision_payload)
        state_binding = make_binding(state, family="risk_state_v1") if state is not None else None
        decision = {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "decision_scope": "portfolio_publication",
            "risk_policy_binding": _binding(policy, family="risk_policy_v1"),
            "risk_state_binding": state_binding,
            "input_bindings": [
                _binding(target_weights, family="target_weights_v1"),
                _binding(definition, family="portfolio_definition_v1"),
                {
                    "family": "universe_snapshot_v1",
                    "generation_id": universe["generation_id"],
                    "manifest_digest_sha256": sha256_json(
                        {key: value for key, value in universe.items() if key != "generation_id"}
                    ),
                },
            ],
            "as_of_date": as_of_date,
            "visible_through": f"{as_of_date}T15:00:00+00:00",
            "findings": findings,
            "action": action,
            "constraints": constraints,
            "decision_digest": decision_digest,
            "files": [_file_entry("decision.json", decision_content)],
            "producer": {
                "producer_code_fingerprint": _CODE_FINGERPRINT,
                "run_id": _RUN_ID,
                "created_at": "1970-01-01T00:00:00+00:00",
            },
            "serialization_profile": "json-canonical-v1",
            "generation_id": "0" * 64,
            "manifest_digest_sha256": "0" * 64,
        }
        generation, digest = risk_contract_identities(decision, schema_name="risk_decision")
        decision["generation_id"] = generation
        decision["manifest_digest_sha256"] = digest
        return decision
