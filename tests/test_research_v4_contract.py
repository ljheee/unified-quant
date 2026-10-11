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
        [sys.executable, str(root / "scripts" / "sign_stage_plan_review.py"), "--output", str(output)],
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
