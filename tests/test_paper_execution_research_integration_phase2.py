from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from uq.errors import ContractError
from uq.execution.stage_adapter import PaperExecutionStageAdapter
from uq.research_chain.runner import ResearchChainRunner


def test_v5_execution_enabled_v3_v4_refused() -> None:
    """PE24: v5 execution is enabled; v3/v4 remain refused."""
    runner_cls = ResearchChainRunner
    # v5 no longer refused
    assert not hasattr(runner_cls, "_V5_REFUSED")
    # The refusal logic must exclude v5
    import inspect
    source = inspect.getsource(runner_cls._validate_plan)
    assert "{3, 4}" in source
    assert "{3, 4, 5}" not in source


def test_stage_adapter_initial_state_cash_and_id(tmp_path: Path) -> None:
    template = {
        "initial_state": {
            "cash": 1000000.0,
            "holdings": [{"instrument": "600000.XSHG", "quantity": 100}],
            "valuation_price_basis": "decision_close_estimate",
        },
    }
    adapter = PaperExecutionStageAdapter(template)
    holdings, cash = adapter.initial_holdings_and_cash()
    assert cash == 1000000.0
    assert len(holdings) == 1
    assert holdings[0]["instrument"] == "600000.XSHG"

    exec_id = adapter.compute_execution_id("a" * 64, "run-123", "2025-06-02")
    exec_id2 = adapter.compute_execution_id("a" * 64, "run-123", "2025-06-02")
    assert exec_id == exec_id2
    assert len(exec_id) == 64


def test_stage_adapter_continuation_rejects_template_initial_state(tmp_path: Path) -> None:
    template = {
        "initial_state": {
            "continuation_binding": {
                "family": "paper_portfolio_state_v1",
                "generation_id": "a" * 64,
                "manifest_digest_sha256": "b" * 64,
            },
        },
    }
    adapter = PaperExecutionStageAdapter(template)
    with pytest.raises(ContractError, match="continuation binding requires"):
        adapter.initial_holdings_and_cash()


def test_cli_dry_run_routes_v5_to_its_schema(tmp_path, capsys):
    """The CLI dry-run must route by contract version, not the hardcoded v1 schema.

    Before the fix a v5 request was rejected at the CLI boundary
    (`ModelContractLoader.validate("research_run_request", ...)`) even though the
    runner dry-run path is version-aware, so no v5 request could ever be
    published or inspected through the documented entry point.
    """
    import json

    from uq.research_chain.cli import uq_research_run

    root = Path(__file__).resolve().parents[1]
    fixture = (
        root
        / "evidence/paper-execution-research-integration/phase-0r/fixtures"
        / "research_run_request_v5-valid.json"
    )
    data_root = tmp_path / "data"
    exit_code = uq_research_run([
        "--project-root", str(root),
        "--data-root", str(data_root),
        "--mode", "dry-run",
        "--request-json", str(fixture),
    ])
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "dry_run_published"
    assert data_root.joinpath("research_runs", "requests").exists()
    assert data_root.joinpath("research_runs", "states").exists()
    assert not data_root.joinpath("research_runs", "results").exists()
