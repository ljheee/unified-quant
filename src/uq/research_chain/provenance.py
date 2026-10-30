"""Observed code/environment bindings for repository research executions."""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping

from ..contracts.model_layer import sha256_json
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError


def capture_environment(project_root: Path | str) -> dict[str, Any]:
    """Hash actual source/config bytes, including uncommitted runtime changes.

    HEAD is an audit locator, not the code fingerprint. Docs, data, caches and
    evidence are excluded so recording an experiment cannot change its inputs.
    Package versions are observations, not a claim of lockfile conformance.
    """
    root = Path(project_root)
    files = [root / "pyproject.toml"]
    for directory, suffixes in (
        ("src/uq", {".py"}), ("scripts", {".py", ".sh"}),
        ("config", {".json", ".yaml", ".yml"}),
    ):
        base = root / directory
        if not base.is_dir():
            raise ContractError(f"provenance source directory is missing: {directory}")
        reject_symlinks_within(root, base, label="provenance source")
        for parent, directories, names in os.walk(base):
            directories[:] = sorted(name for name in directories if name != "__pycache__")
            for name in directories:
                reject_symlinks_within(root, Path(parent) / name, label="provenance source")
            files.extend(Path(parent) / name for name in names if Path(name).suffix in suffixes)
    records = []
    for path in sorted(files):
        reject_symlinks_within(root, path, label="provenance source")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ContractError(f"provenance source is unavailable: {path.relative_to(root)}") from exc
        records.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
        })
    lock_path = root / "uv.lock"
    reject_symlinks_within(root, lock_path, label="environment lock")
    try:
        lock_digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContractError("research provenance requires a checkout and uv.lock") from exc
    packages = {}
    for name in ("unified-quant", "pandas", "numpy", "pyarrow", "PyYAML", "jsonschema",
                 "cryptography", "pyqlib", "scikit-learn", "lightgbm", "xgboost", "catboost"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {
        "record_version": 1, "kind": "research_environment",
        "fingerprint_profile": "source_tree_sha256_v1",
        "git_head": head,
        "code_fingerprint": sha256_json({"profile": "source_tree_sha256_v1", "files": records}),
        "environment_lock_digest_sha256": lock_digest,
        "source_files": records,
        "runtime": {
            "python": sys.version, "implementation": platform.python_implementation(),
            "platform": platform.platform(), "machine": platform.machine(),
            "packages": packages,
        },
    }


def verified_runner_identity(
    requested: Mapping[str, Any], observed: Mapping[str, Any]
) -> dict[str, str]:
    for field in ("code_fingerprint", "environment_lock_digest_sha256"):
        if requested.get(field) != observed[field]:
            raise ContractError(f"research environment {field} does not match observed bytes")
    return {
        "code_fingerprint": observed["code_fingerprint"],
        "environment_profile": "observed-source-tree-v1",
        "lock_digest_sha256": observed["environment_lock_digest_sha256"],
    }
