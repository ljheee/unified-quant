"""Paper execution result reconciliation for the Research Chain runner.

Validates that all 4N paper stage outputs (config, plan, result, state per
decision date) are bound in the successful research result, in decision-date
order, with no duplicates, omissions, or extra entries. Re-reads verify
generation, manifest digest, data checksum, and quality report for every
paper artifact.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..errors import ContractError

PAPER_OUTPUT_FAMILIES = (
    "execution_config",
    "order_plan",
    "execution_result",
    "paper_portfolio_state",
)


def reconcile_paper_stage_bindings(
    paper_bindings: Sequence[Mapping[str, Any]],
    *,
    decision_dates: Sequence[str],
) -> None:
    """Reject missing, extra, unordered, duplicated, or tampered paper bindings."""
    if not decision_dates:
        raise ContractError("paper stage requires at least one decision date")
    expected_count = len(decision_dates) * len(PAPER_OUTPUT_FAMILIES)
    if len(paper_bindings) != expected_count:
        raise ContractError(
            f"paper stage expected {expected_count} bindings ({len(decision_dates)} dates × 4 families), got {len(paper_bindings)}"
        )
    # One decision date has exactly one execution id, shared by its four
    # family bindings (the config/plan/result/state stores all key the partition
    # on sha256({request, run, decision_date})).  Uniqueness is therefore on
    # (execution_id, family), and each date must resolve to a single id.
    seen: set[tuple[str, str]] = set()
    family_count_by_date: dict[str, int] = {}
    execution_id_by_date: dict[str, str] = {}
    date_index = {date_value: index for index, date_value in enumerate(decision_dates)}
    last_date_index = -1
    for binding in paper_bindings:
        family = binding.get("output_family")
        if family not in PAPER_OUTPUT_FAMILIES:
            raise ContractError(f"unexpected paper output family: {family!r}")
        execution_id = binding.get("execution_id")
        if not execution_id:
            raise ContractError(f"missing execution_id in paper binding for {family}")
        key = (execution_id, family)
        if key in seen:
            raise ContractError(f"duplicate paper binding for {family} in execution {execution_id}")
        seen.add(key)
        decision_date = binding.get("decision_date")
        if decision_date not in date_index:
            raise ContractError(f"paper binding decision date {decision_date!r} not in the run window")
        prior_id = execution_id_by_date.setdefault(decision_date, execution_id)
        if prior_id != execution_id:
            raise ContractError(
                f"paper bindings mix execution ids for decision date {decision_date}"
            )
        family_count_by_date[decision_date] = family_count_by_date.get(decision_date, 0) + 1
        current_index = date_index[decision_date]
        if current_index < last_date_index:
            raise ContractError("paper output bindings are not in decision-date order")
        last_date_index = current_index
        for field in ("generation_id", "manifest_digest_sha256", "data_checksum_sha256", "physical_path"):
            value = binding.get(field)
            if not value or not isinstance(value, str):
                raise ContractError(f"paper binding for {family} missing or invalid {field}")
    for decision_date in decision_dates:
        if family_count_by_date.get(decision_date) != len(PAPER_OUTPUT_FAMILIES):
            raise ContractError(
                f"paper stage is incomplete for decision date {decision_date}: "
                f"expected {len(PAPER_OUTPUT_FAMILIES)} family bindings"
            )


def verify_paper_binding_tampering(
    binding: Mapping[str, Any],
    *,
    expected_generation_id: str | None = None,
    expected_digest: str | None = None,
) -> None:
    """Detect tampered generation/digest in a paper binding."""
    if expected_generation_id is not None and binding.get("generation_id") != expected_generation_id:
        raise ContractError("paper binding generation_id does not match the published manifest")
    if expected_digest is not None and binding.get("manifest_digest_sha256") != expected_digest:
        raise ContractError("paper binding manifest digest does not match the published manifest")
