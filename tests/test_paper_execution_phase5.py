from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


SOURCE = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted(Path("tests").glob("test_paper_execution*.py"))
)


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
