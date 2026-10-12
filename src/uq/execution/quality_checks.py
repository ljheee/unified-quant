"""Paper-layer quality check helper for reviewed registry binding.

Extracts the exact-check validation from store internals so the adapter can
call it as a production helper rather than relying on test-only assembly.
"""

from __future__ import annotations

from typing import Any

from ..errors import ContractError


def paper_quality_checks_for(binding_type: str) -> list[str]:
    """Return the reviewed allowed-check names for a paper binding type."""
    from ..contracts.model_layer import ModelQualityReviewRegistry

    binding = ModelQualityReviewRegistry().bindings.get(binding_type)
    if not isinstance(binding, dict):
        raise ContractError(f"no reviewed quality policy for {binding_type}")
    allowed = list(binding.get("allowed_checks", []))
    if not allowed:
        raise ContractError(f"reviewed quality policy for {binding_type} has no allowed checks")
    return allowed


def validate_exact_quality_checks(report: dict[str, Any], binding_type: str) -> None:
    """Validate that the report carries exactly the reviewed allowed checks."""
    expected = paper_quality_checks_for(binding_type)
    checks = report.get("checks")
    actual = [check.get("name") for check in checks or []]
    if sorted(actual) != sorted(expected) or len(actual) != len(set(actual)):
        raise ContractError(
            f"quality report checks do not exactly match reviewed policy for {binding_type}"
        )


def paper_binding_types() -> list[str]:
    """Return the four paper-family binding types with review policy."""
    return [
        "execution_config_v1",
        "order_plan_v1",
        "execution_result_v1",
        "paper_portfolio_state_v1",
    ]
