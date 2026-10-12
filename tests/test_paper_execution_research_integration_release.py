"""Release-evidence lock for the paper-execution research-integration slice.

The plan requires every phase to exit against a successful local gate plus the
ten-cell remote matrix on the final implementation HEAD, with each cell's raw
artifacts archived.  This test re-derives those claims from the persisted
evidence instead of trusting the release record's prose.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYER_ROOT = ROOT / "evidence/paper-execution-research-integration"
RELEASE = LAYER_ROOT / "release"

PHASE_DIRS = ("phase-0", "phase-0r", "phase-0.5", "phase-1", "phase-2", "phase-3")

_DEFINED_TESTS = set(
    re.findall(
        r"\n\s*def (test_[A-Za-z0-9_]+)",
        "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(ROOT.glob("tests/*.py"))
        ),
    )
)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _phase_record(directory: str) -> dict:
    base = RELEASE if directory == "release" else LAYER_ROOT / directory
    return _read(base / "phase-record.json")


def test_every_phase_is_exited_with_executable_acceptance_rows() -> None:
    phases = [*PHASE_DIRS, "release"]
    for directory in phases:
        record = _phase_record(directory)
        assert record["status"] in {"exited", "exited_after_remote_gate"}, (
            directory,
            record["status"],
        )
        assert record["blocked_by"] == [], directory
        assert record["acceptance_rows"], directory
        for row in record["acceptance_rows"]:
            assert row["status"] == "passed", (directory, row["id"], row["status"])
            assert row["test_ids"], (directory, row["id"])
            for identifier in row["test_ids"]:
                if identifier.endswith(".json"):
                    assert (ROOT / identifier).is_file(), identifier
                    continue
                assert identifier.rsplit("::", 1)[-1] in _DEFINED_TESTS, (
                    directory,
                    row["id"],
                    identifier,
                )


def test_local_gate_and_remote_matrix_bind_the_released_head() -> None:
    release = _read(RELEASE / "release-record.json")
    assert release["status"] == "released"
    head = release["final_implementation_commit"]

    local = _read(ROOT / release["local_gate"]["path"])
    assert local["test_result"] == "passed"
    assert local["git_commit"] == head
    local_lock = hashlib.sha256(
        (ROOT / release["local_gate"]["requirements_lock_path"]).read_bytes()
    ).hexdigest()
    assert local_lock == release["local_gate"]["requirements_lock_sha256"]
    assert (ROOT / release["local_gate"]["requirements_lock_sha256_path"]).read_text().strip() == local_lock

    aggregated = _read(ROOT / release["remote_gate"]["aggregated_path"])
    assert aggregated["git_commit"] == release["remote_gate"]["commit_bound"] == head
    assert aggregated["summary"] == release["remote_gate"]["summary"] == {
        "failed": 0,
        "passed": 10,
        "total": 10,
    }
    assert set(aggregated["expected_cells"]) == set(release["remote_gate"]["covered_cells"])

    run = _read(ROOT / release["remote_gate"]["run_path"])
    assert run["headSha"] == head
    assert run["conclusion"] == "success"
    assert run["status"] == "completed"
    assert str(run["databaseId"]) == str(aggregated["run_id"]) == release["remote_gate"]["run_id"]


def test_remote_cells_archive_distinct_raw_environments() -> None:
    release = _read(RELEASE / "release-record.json")
    head = release["final_implementation_commit"]
    aggregated = _read(ROOT / release["remote_gate"]["aggregated_path"])

    cells = aggregated["cells"]
    assert {row["cell"] for row in cells} == set(aggregated["expected_cells"])
    observed = set()
    for row in cells:
        report = _read(ROOT / row["gate_report"])
        assert report["git_commit"] == head, row["cell"]
        assert report["test_result"] == "passed", row["cell"]
        # The aggregate must copy each cell verbatim, never restate one
        # environment for the whole matrix.
        assert report["platform"] == row["platform"], row["cell"]
        assert report["python_version"] == row["python_version"], row["cell"]
        declared = (ROOT / row["requirements_lock_sha256_file"]).read_text().strip()
        actual = hashlib.sha256((ROOT / row["requirements_lock"]).read_bytes()).hexdigest()
        assert declared == actual == row["requirements_lock_sha256"], row["cell"]
        observed.add((row["platform"], row["python_version"], "qlib" in row["cell"]))

    assert len(observed) == 10, "the ten cells are not ten distinct environments"
    assert any(item[0].startswith("macOS-") for item in observed)
    assert any(item[0].startswith("Linux-") for item in observed)
    assert {item[1].split(".")[1] for item in observed} == {"11", "12", "13"}


def test_phase_records_bind_the_released_head() -> None:
    release = _read(RELEASE / "release-record.json")
    head = release["final_implementation_commit"]
    for directory in ("phase-0r", "phase-0.5", "phase-1", "phase-2", "phase-3"):
        record = _phase_record(directory)
        assert record["implementation_commit"] == head, directory
        assert record["local_gate"]["commit_bound"] == head, directory
        assert record["remote_gate"]["commit_bound"] == head, directory
        assert record["remote_gate"]["run_id"] == release["remote_gate"]["run_id"], directory


def test_release_evidence_index_is_complete_and_verifiable() -> None:
    index_path = RELEASE / "evidence-index.json"
    index = _read(index_path)
    assert index["status"] == "released"
    recorded = {row["path"]: row["sha256"] for row in index["records"]}
    expected = {
        path.relative_to(ROOT).as_posix()
        for path in RELEASE.rglob("*")
        if path.is_file() and path != index_path
    }
    for directory in PHASE_DIRS:
        expected.add((LAYER_ROOT / directory / "phase-record.json").relative_to(ROOT).as_posix())
    assert expected <= set(recorded), sorted(expected - set(recorded))
    for path, digest in recorded.items():
        target = ROOT / path
        assert target.is_file(), path
        assert hashlib.sha256(target.read_bytes()).hexdigest() == digest, path
