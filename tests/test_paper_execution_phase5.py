from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


SOURCE = "\n".join(path.read_text(encoding="utf-8") for path in sorted(Path("tests").glob("test_paper_execution*.py")))


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
    completed = subprocess.run(
        [
            "rg",
            "-l",
            "-i",
            "broker|live|network|credential|streaming|websocket|secret",
            "src/uq/execution",
            "src/uq/runtime.py",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1, completed.stdout
