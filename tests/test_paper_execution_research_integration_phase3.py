from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from uq.errors import ContractError
from uq.execution.result_reconciliation import (
    PAPER_OUTPUT_FAMILIES,
    reconcile_paper_stage_bindings,
    verify_paper_binding_tampering,
)


def _make_bindings(dates: list[str]) -> list[dict]:
    bindings = []
    for day_index, decision_date in enumerate(dates):
        for family in PAPER_OUTPUT_FAMILIES:
            bindings.append({
                "output_family": family,
                "execution_id": f"exec-{day_index:04d}-{family}",
                "decision_date": decision_date,
                "generation_id": "a" * 64,
                "manifest_digest_sha256": "b" * 64,
                "data_checksum_sha256": "c" * 64,
                "physical_path": f"paper/{family}/{decision_date}",
            })
    return bindings


def test_paper_outputs_reconciled_in_result() -> None:
    dates = ["2025-06-02", "2025-06-03"]
    bindings = _make_bindings(dates)
    assert len(bindings) == 8
    reconcile_paper_stage_bindings(bindings, decision_dates=dates)


def test_paper_result_binding_tampering_rejected() -> None:
    dates = ["2025-06-02", "2025-06-03"]
    bindings = _make_bindings(dates)

    with pytest.raises(ContractError, match="expected 8"):
        reconcile_paper_stage_bindings(bindings[:-1], decision_dates=dates)

    extra = _make_bindings(dates) + [_make_bindings(["2025-06-02"])[0]]
    with pytest.raises(ContractError, match="expected 8"):
        reconcile_paper_stage_bindings(extra, decision_dates=dates)

    out_of_order = _make_bindings(list(reversed(dates)))
    with pytest.raises(ContractError, match="not in decision-date order"):
        reconcile_paper_stage_bindings(out_of_order, decision_dates=dates)

    duplicated = _make_bindings(dates)
    duplicated[1] = dict(duplicated[0])
    with pytest.raises(ContractError, match="duplicate"):
        reconcile_paper_stage_bindings(duplicated, decision_dates=dates)

    unknown_family = _make_bindings(dates)
    unknown_family[0]["output_family"] = "unknown_family"
    with pytest.raises(ContractError, match="unexpected paper output family"):
        reconcile_paper_stage_bindings(unknown_family, decision_dates=dates)

    tampered = _make_bindings(dates)
    tampered[0]["generation_id"] = "0" * 64
    with pytest.raises(ContractError, match="generation_id does not match"):
        verify_paper_binding_tampering(tampered[0], expected_generation_id="a" * 64)

    wrong_digest = _make_bindings(dates)
    wrong_digest[0]["manifest_digest_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="manifest digest does not match"):
        verify_paper_binding_tampering(wrong_digest[0], expected_digest="b" * 64)


def test_paper_failure_hides_accepted_result() -> None:
    """A failed paper stage produces no reconciled bindings."""
    with pytest.raises(ContractError, match="at least one decision date"):
        reconcile_paper_stage_bindings([], decision_dates=[])

    dates = ["2025-06-02"]
    with pytest.raises(ContractError, match="expected 4"):
        reconcile_paper_stage_bindings([], decision_dates=dates)

    with pytest.raises(ContractError, match="missing or invalid"):
        bad = _make_bindings(dates)
        del bad[0]["generation_id"]
        reconcile_paper_stage_bindings(bad, decision_dates=dates)
