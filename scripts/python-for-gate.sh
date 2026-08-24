#!/usr/bin/env bash
set -euo pipefail
for candidate in "${PYTHON_BIN:-}" python3.12 python3.11 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11; do
  [[ -z "$candidate" ]] && continue
  if command -v "$candidate" >/dev/null 2>&1 || [[ -x "$candidate" ]]; then
    version="$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || true)"
    if [[ "$version" == 3.12 || "$version" == 3.11 ]]; then
      echo "$candidate"
      exit 0
    fi
  fi
done
echo "Python 3.11+ is required for the lockfile fallback" >&2
exit 2
