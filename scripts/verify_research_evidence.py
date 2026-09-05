#!/usr/bin/env python3
"""Verify preserved Research Chain remote gate aggregation evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

EXPECTED_BASE_CELLS = {
    "macos-latest/3.11",
    "macos-latest/3.12",
    "macos-latest/3.13",
    "ubuntu-latest/3.11",
    "ubuntu-latest/3.12",
    "ubuntu-latest/3.13",
}
EXPECTED_QLIB_CELLS = {
    "macos-latest/3.11/qlib",
    "macos-latest/3.12/qlib",
    "ubuntu-latest/3.11/qlib",
    "ubuntu-latest/3.12/qlib",
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid evidence JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"evidence root must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(directory: Path) -> dict[str, Any]:
    aggregated_path = directory / "aggregated-gates.json"
    run_path = directory / "run.json"
    aggregated = _read_json(aggregated_path)
    run = _read_json(run_path)
    run_id = str(aggregated.get("run_id") or directory.name)
    if not run_id.isdigit():
        raise SystemExit(f"evidence directory must resolve to a numeric CI run id: {directory}")
    if aggregated.get("aggregate_result") != "passed" or run.get("conclusion") != "success":
        raise SystemExit("remote gate evidence is not successful")
    if run.get("status") != "completed":
        raise SystemExit("remote CI run is not completed")
    if str(run.get("databaseId")) != run_id or aggregated.get("run_id") != run_id:
        raise SystemExit("remote CI run id mismatch")
    git_commit = run.get("headSha")
    if aggregated.get("git_commit") != git_commit:
        raise SystemExit("aggregated gate commit does not match CI head")
    cells = aggregated.get("cells")
    if not isinstance(cells, list):
        raise SystemExit("aggregated gates cells must be a list")
    observed: dict[str, dict[str, Any]] = {}
    lockfiles: dict[str, str] = {}
    requirements_groups: dict[tuple[bool, str], str] = {}
    for cell in cells:
        if not isinstance(cell, dict):
            raise SystemExit("aggregated cell must be an object")
        cell_name = cell.get("certified_cell")
        if not isinstance(cell_name, str) or cell_name in observed:
            raise SystemExit(f"missing or duplicate certified cell: {cell_name!r}")
        if cell.get("test_result") != "passed":
            raise SystemExit(f"remote gate cell not passed: {cell_name}")
        if cell.get("git_commit") != git_commit:
            raise SystemExit(f"remote gate cell commit mismatch: {cell_name}")
        lock_digest = cell.get("lockfile_sha256")
        lock_name = cell.get("artifact")
        if not isinstance(lock_digest, str) or len(lock_digest) != 64:
            raise SystemExit(f"invalid lock digest for {cell_name}")
        if not isinstance(lock_name, str) or not lock_name:
            raise SystemExit(f"invalid artifact name for {cell_name}")
        lockfiles.setdefault(lock_digest, lock_name)
        platform = str(cell.get("platform", ""))
        if not (platform.startswith("macOS-") or platform.startswith("Linux-")):
            raise SystemExit(f"remote gate cell has an unexpected platform: {cell_name}, {platform!r}")
        declared_python = cell_name.split("/")[1]
        if not str(cell.get("python_version", "")).startswith(declared_python + "."):
            raise SystemExit(f"remote gate cell Python mismatch: {cell_name}")
        requirements_digest = cell.get("requirements_lock_sha256")
        if not isinstance(requirements_digest, str) or len(requirements_digest) != 64:
            raise SystemExit(f"invalid requirements digest for {cell_name}")
        requirements_groups.setdefault(("qlib" in cell_name, requirements_digest), cell_name)
        observed[cell_name] = cell
    expected = EXPECTED_BASE_CELLS | EXPECTED_QLIB_CELLS
    missing = sorted(expected - set(observed))
    unexpected = sorted(set(observed) - expected)
    if missing or unexpected:
        raise SystemExit(f"remote cell mismatch missing={missing} unexpected={unexpected}")
    if set(aggregated.get("covered_cells", [])) != expected:
        raise SystemExit("covered_cells does not match the canonical ten-cell matrix")
    if len(requirements_groups) != 2:
        raise SystemExit("requirements digests must separate base and Qlib cells")
    return {
        "run_id": run_id,
        "git_commit": git_commit,
        "cell_count": len(observed),
        "covered_cells": sorted(set(expected)),
        "artifact_names": sorted(lockfiles.values()),
        "aggregated_sha256": _sha256(aggregated_path),
        "run_sha256": _sha256(run_path),
    }



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence_directory", type=Path)
    args = parser.parse_args()
    summary = verify(args.evidence_directory)
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
