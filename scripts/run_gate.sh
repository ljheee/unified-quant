#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATE_DIR="${UQ_GATE_DIR:-$ROOT/.gate}"
mkdir -p "$GATE_DIR"
PYTHON_BIN="${PYTHON_BIN:-$(/usr/bin/env bash "$ROOT/scripts/python-for-gate.sh")}"

if command -v uv >/dev/null 2>&1; then
  runner="uv"
  lockfile_digest="$("$PYTHON_BIN" -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("uv.lock").read_bytes()).hexdigest())')"
  uv export --format requirements-txt --hashes > "$GATE_DIR/requirements.lock.txt"
else
  runner="lockfile-fallback"
  lockfile_digest="$("$PYTHON_BIN" -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("uv.lock").read_bytes()).hexdigest())')"
  "$PYTHON_BIN" scripts/export_locked_requirements.py > "$GATE_DIR/requirements.lock.txt"
fi

requirements_digest="$(sha256sum "$GATE_DIR/requirements.lock.txt" | awk '{print $1}')"
printf '%s\n' "$requirements_digest" > "$GATE_DIR/requirements.lock.txt.sha256"

if [[ "$runner" == "uv" ]]; then
  install_command=(uv sync --locked --extra dev --extra real)
  test_command=(uv run --no-sync python -m pytest)
  "${install_command[@]}"
  "${test_command[@]}"
else
  if [[ ! -x "$GATE_DIR/venv/bin/python" ]]; then
    "$PYTHON_BIN" -m venv "$GATE_DIR/venv"
  fi
  "$GATE_DIR/venv/bin/pip" install -r "$GATE_DIR/requirements.lock.txt"
  "$GATE_DIR/venv/bin/pip" install -e . --no-deps
  install_command=("$GATE_DIR/venv/bin/pip install -r .gate/requirements.lock.txt && $GATE_DIR/venv/bin/pip install -e . --no-deps")
  test_command=("$GATE_DIR/venv/bin/python" -m pytest)
  "$GATE_DIR/venv/bin/python" -m pytest
fi

git_commit="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
if [[ "$runner" == "uv" ]]; then
  python_version="$(uv run --no-sync python -c 'import sys; print(sys.version.split()[0])')"
else
  python_version="$("$GATE_DIR/venv/bin/python" -c 'import sys; print(sys.version.split()[0])')"
fi

printf '%s\n' "${install_command[*]} && ${test_command[*]}" > "$GATE_DIR/command.txt"

"$PYTHON_BIN" - \
  "$GATE_DIR/command.txt" \
  "$runner" \
  "$python_version" \
  "$lockfile_digest" \
  "$requirements_digest" \
  "$git_commit" \
  "$GATE_DIR/gate-report.json" <<'PY'
import json
import platform
import sys
from datetime import datetime, timezone

command, runner, python_version, lockfile_digest, requirements_digest, git_commit, output = sys.argv[1:]
report = {
    "command": open(command, encoding="utf-8").read().strip(),
    "extras": ["dev", "real"],
    "git_commit": git_commit,
    "lockfile_sha256": lockfile_digest,
    "platform": platform.platform(),
    "python_version": python_version,
    "requirements_lock_sha256": requirements_digest,
    "runner": runner,
    "test_result": "passed",
    "utc_timestamp": datetime.now(timezone.utc).isoformat(),
}
with open(output, "w", encoding="utf-8") as handle:
    json.dump(report, handle, sort_keys=True, indent=2)
    handle.write("\n")
PY

echo "gate report: $GATE_DIR/gate-report.json"
