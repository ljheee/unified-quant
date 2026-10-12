import json
import subprocess
import sys
from pathlib import Path

import pytest

from uq.errors import ContractError



def test_v4_stage_plan_review_signature_chain_verifies_and_rejects_tamper(tmp_path):
    import json
    import subprocess
    import sys
    from uq.contracts.model_layer import research_stage_plan_v4_sha256
    from uq.research_chain.contracts import verify_stage_plan_review

    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "review.json"
    subprocess.run(
        [sys.executable, str(root / "scripts" / "sign_stage_plan_review.py"), "--version", "4", "--output", str(output)],
        check=True, cwd=root,
    )
    review = json.loads(output.read_text())
    stage_plan = research_stage_plan_v4_sha256()
    verify_stage_plan_review(review, stage_plan_sha256=stage_plan)
    with pytest.raises(ContractError, match="signature mismatch"):
        verify_stage_plan_review({**review, "reviewer": "someone-else"}, stage_plan_sha256=stage_plan)


def test_v4_request_schema_is_registered_but_runtime_stays_disabled():
    import json
    from jsonschema import Draft202012Validator
    from uq.research_chain.contracts import research_request_schema_name

    assert research_request_schema_name({"contract_version": 4}) == "research_run_request_v4"
    schema = json.loads((Path(__file__).resolve().parents[1] / "config/schemas/contracts/research_run_request_v4.json").read_text())
    Draft202012Validator.check_schema(schema)
    assert "index_membership_binding" in schema["required"]
    assert schema["properties"]["stage_plan_review"]["properties"]["review_type"]["const"] == "research_stage_plan_v4_activation"


def test_v4_and_v3_share_the_stage_order_and_unknown_versions_fail_closed():
    # The stage plans of v3 and v4 are identical (both insert paper_execution);
    # the mapping must not silently fall back to the released v1 order for v4,
    # or every v4 state would be numbered as if paper_execution did not exist.
    from uq.errors import ContractError
    from uq.research_chain.resolver import stage_plan_for_request

    assert stage_plan_for_request({"contract_version": 4}) == stage_plan_for_request(
        {"contract_version": 3}
    )
    assert "paper_execution" in stage_plan_for_request({"contract_version": 4})
    assert stage_plan_for_request({}) == stage_plan_for_request({"contract_version": 1})
    assert stage_plan_for_request({"contract_version": 2}) == stage_plan_for_request({})
    assert stage_plan_for_request({"contract_version": 5}) == stage_plan_for_request({"contract_version": 4})
    with pytest.raises(ContractError, match="contract version"):
        stage_plan_for_request({"contract_version": 6})


def test_v3_and_v4_execution_are_refused_until_the_paper_runtime_lands():
    # Fail-closed lock for pendding 4c: the contract basis (request v3/v4,
    # stage plan, input-set resolver branch) is live, but the paper-execution
    # runtime stages of paper-execution-research-integration-plan are not. A
    # v3/v4 run must be refused outright rather than executed while silently
    # skipping paper_execution.
    from types import SimpleNamespace

    from uq.research_chain.runner import ResearchChainRunner

    for version in (3, 4):
        with pytest.raises(ContractError, match="paper-execution runtime is not enabled"):
            ResearchChainRunner._validate_plan(
                SimpleNamespace(), SimpleNamespace(request={"contract_version": version})
            )
