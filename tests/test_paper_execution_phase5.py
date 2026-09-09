from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


SOURCE = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted(Path("tests").glob("test_paper_execution*.py"))
)

RELEASE_ROOT = Path("evidence/paper-execution/release")


def test_phase_5_all_phase_records_exited() -> None:
    records = sorted(Path("evidence/paper-execution").glob("phase-*/phase-record.json"))
    assert len(records) == 5
    for path in records:
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["status"] == "exited", path
        for row in record["acceptance"]:
            assert row["status"] != "TBD", (path, row)
            assert row["status"] == "implemented", (path, row)
            assert row["test_id"], (path, row)
            pattern = rf"\bdef {re.escape(row['test_id'])}\b"
            assert re.search(pattern, SOURCE), (path, row)


def test_phase_5_no_broker_live_network_path() -> None:
    forbidden = re.compile(
        r"broker|live|network|credential|streaming|websocket|secret",
        re.IGNORECASE,
    )
    paths = list(Path("src/uq/execution").glob("**/*.py"))
    paths.append(Path("src/uq/runtime.py"))
    matches = [
        path
        for path in paths
        if any(
            forbidden.search(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    ]
    assert matches == []


def test_phase_5_release_records_reconcile_gate_evidence() -> None:
    release = json.loads((RELEASE_ROOT / "release-record.json").read_text())
    phase = json.loads((RELEASE_ROOT / "phase-record.json").read_text())
    index = json.loads((RELEASE_ROOT / "evidence-index.json").read_text())

    assert release["status"] == "released"
    assert phase["status"] == "exited_after_remote_gate"
    assert phase["blocked_by"] == []
    assert index["status"] == "released"
    assert release["remote_gate"]["summary"] == {"failed": 0, "passed": 10, "total": 10}

    base = json.loads(Path(release["local_gates"]["base"]["report_path"]).read_text())
    qlib = json.loads(Path(release["local_gates"]["qlib"]["report_path"]).read_text())
    assert base["git_commit"] == release["final_implementation_commit"]
    assert qlib["git_commit"] == release["final_implementation_commit"]
    assert base["test_result"] == qlib["test_result"] == "passed"

    aggregated = json.loads(Path(release["remote_gate"]["aggregated_path"]).read_text())
    assert aggregated["git_commit"] == release["remote_gate"]["commit_bound"]
    assert aggregated["summary"] == release["remote_gate"]["summary"]
    cells = {row["cell"] for row in aggregated["cells"]}
    assert cells == set(release["remote_gate"]["covered_cells"])

    for record in index["records"]:
        path = Path(record["path"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]

    assert release["record_version"] >= 3
    assert release["evidence_commit"] == "9aababa3c475f53858ce9b0c6881b2ddc5967e48"
    assert release["initial_release_ci_run_id"] == "34354376483"
    assert release["final_release_ci_run_id"] == "34356489780"

    marker = (RELEASE_ROOT / "MARKER.md").read_text()
    marker_sections = marker.split("## ")
    assert len(marker_sections) >= 4
    assert "## v1 — superseded by final CR" in marker
    assert "## v2 — evidence reconciliation" in marker
    assert "## v3 — current final CR evidence" in marker
    assert marker.index("## v1") < marker.index("## v2") < marker.index("## v3")
    current_marker = marker_sections[-1]
    assert release["final_implementation_commit"] in current_marker
    assert release["evidence_commit"] in current_marker
    assert release["final_release_ci_run_id"] in current_marker
