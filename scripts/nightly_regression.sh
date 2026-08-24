#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON="${PYTHON:-../one-stop-quant/.venv/bin/python}"
export PYTHONPATH=src
"$PYTHON" -m pytest
DATA_ROOT="$(mktemp -d /tmp/uq-nightly.XXXXXX)"
set -a
# shellcheck disable=SC1091
[ -f .env ] && source .env
set +a
"$PYTHON" -m uq.cli daily \
  --date "${UQ_REGRESSION_DATE:-2026-08-21}" \
  --data-root "$DATA_ROOT" \
  --project-root "$ROOT" \
  --cross-validate
"$PYTHON" scripts/cleanup_retention.py --data-root "$DATA_ROOT" --dry-run
