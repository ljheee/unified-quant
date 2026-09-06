from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from uq.contracts.model_layer import sha256_json

ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = ROOT / "evidence/research-chain/release"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_release_record_evidence_is_complete_and_bound() -> None:
    record = _read_json(RELEASE_DIR / "release-record.json")
    index = _read_json(RELEASE_DIR / "evidence-index.json")
    phase_record = _read_json(RELEASE_DIR / "phase-record.json")
    local_gate = _read_json(RELEASE_DIR / "final-gate-report.json")
    remote_run = _read_json(RELEASE_DIR / "remote-matrix/run.json")
    aggregate = _read_json(RELEASE_DIR / "remote-matrix/aggregated-gates.json")

    release_head = record["release_head"]
    assert record["status"] == "released"
    assert record["release"] == "v0.8"
    assert phase_record["status"] == "exited_after_remote_gate"
    assert local_gate["git_commit"] == release_head
    assert remote_run["headSha"] == release_head
    assert aggregate["git_commit"] == release_head
    assert record["remote_ci_run_id"] == str(remote_run["databaseId"])

    phase_paths = record["evidence"]["phase_records"]
    phase_entry = next(entry for entry in index["entries"] if entry["id"] == "phase-records")
    assert phase_paths == phase_entry["paths"]
    assert len(phase_paths) == 7
    assert all((ROOT / path).is_file() for path in phase_paths)

    relative_entries = [entry for entry in index["entries"] if entry.get("path")]
    assert relative_entries
    for entry in relative_entries:
        path = RELEASE_DIR / entry["path"] if not entry["path"].startswith("evidence/") else ROOT / entry["path"]
        assert path.is_file(), entry["path"]

    completed = subprocess.run(
        [sys.executable, "scripts/verify_research_evidence.py", "evidence/research-chain/release/remote-matrix"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(completed.stdout)
    assert summary["git_commit"] == release_head
    assert summary["cell_count"] == 10


def test_release_record_identity_fields_are_complete_and_reproducible() -> None:
    record = _read_json(RELEASE_DIR / "release-record.json")
    expected_generation = sha256_json({
        key: value for key, value in record.items()
        if key not in {"release_record_content_generation_id", "manifest_digest_sha256"}
    })
    assert record["release_record_content_generation_id"] == expected_generation

    digest_document = {key: value for key, value in record.items() if key != "manifest_digest_sha256"}
    assert record["manifest_digest_sha256"] == sha256_json(digest_document)


def test_all_phase_record_test_ids_exist() -> None:
    record = _read_json(RELEASE_DIR / "release-record.json")
    test_ids: list[str] = []
    for evidence_path in record["evidence"]["phase_records"]:
        phase_record = _read_json(ROOT / evidence_path)
        for criterion in phase_record["exit_criteria"]:
            test_ids.extend(criterion.get("test_ids", []))

    assert test_ids
    for test_id in test_ids:
        if "::" not in test_id:
            continue
        module_path, test_name = test_id.split("::", 1)
        assert not module_path.startswith("github-actions:"), test_id
        source = (ROOT / module_path).read_text(encoding="utf-8")
        assert re.search(rf"^def {re.escape(test_name)}\(", source, flags=re.MULTILINE), test_id
