from __future__ import annotations

import hashlib
import json
from pathlib import Path

from uq.contracts.model_layer import ModelContractLoader

ROOT = Path(__file__).resolve().parents[1]
PHASE_ROOT = ROOT / "evidence/paper-execution-research-integration/phase-0"


def test_phase_0_evidence_index_hashes_are_complete() -> None:
    record = json.loads((PHASE_ROOT / "phase-record.json").read_text())
    index = json.loads((PHASE_ROOT / "evidence-index.json").read_text())
    assert record["phase"] == 0
    assert record["acceptance_rows"]
    assert all(row["status"] == "passed" for row in record["acceptance_rows"])
    assert record["blocked_by"]
    paths = {item["path"] for item in index["records"]}
    assert str(PHASE_ROOT.relative_to(ROOT) / "phase-record.json") in paths
    assert str(PHASE_ROOT.relative_to(ROOT) / "gate-reports/gate-report.json") in paths
    assert str(PHASE_ROOT.relative_to(ROOT) / "fixtures/research_run_request_v3-valid.json") in paths
    for item in index["records"]:
        path = Path(item["path"])
        assert path.is_file(), item["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]


def test_phase_0_valid_fixture_remains_loadable() -> None:
    payload = json.loads(
        (PHASE_ROOT / "fixtures/research_run_request_v3-valid.json").read_text()
    )
    ModelContractLoader.validate("research_run_request_v3", payload)
