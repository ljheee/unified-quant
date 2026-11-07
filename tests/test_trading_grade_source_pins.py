"""Current trading-grade source evidence must follow the working tree."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "evidence/trading-grade/current-source-pins-20261107.json"


def test_current_trading_grade_source_pins_cover_historical_scopes_and_are_fresh():
    current = json.loads(RECORD.read_text(encoding="utf-8"))
    historical = current["supersedes"]
    assert len(historical) == 3
    expected_paths = set()
    for name in historical:
        old = json.loads((ROOT / name).read_text(encoding="utf-8"))
        expected_paths.update(
            item["path"] for item in old.get("implementation_files", old.get("files", []))
        )
    rows = current["source_files"]
    assert len(rows) == len(expected_paths)
    assert {row["path"] for row in rows} == expected_paths
    for row in rows:
        name = row["path"]
        normalized = PurePosixPath(name)
        assert not normalized.is_absolute() and ".." not in normalized.parts
        assert normalized.as_posix() == name
        assert re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
        assert (ROOT / name).is_file()
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == row["sha256"], name
