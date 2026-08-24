#!/usr/bin/env python3
"""Export pinned requirements from uv.lock for environments without uv."""
from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError as exc:
    raise SystemExit(
        "export_locked_requirements.py requires Python 3.11+; use uv or a newer interpreter"
    ) from exc
import re
import sys
from pathlib import Path


def main() -> None:
    data = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    for package in data.get("package", []):
        if package.get("source", {}).get("editable") or package.get("source", {}).get("virtual"):
            continue
        markers = package.get("resolution-markers") or []
        python_markers = []
        for marker in markers:
            match = re.search(r"python_full_version ([<>=]+) '([^']+)'", marker)
            if match:
                operator, version = match.groups()
                major_minor = ".".join(version.split(".")[:2])
                if operator == ">=":
                    python_markers.append((major_minor, True))
                elif operator == "<":
                    python_markers.append((major_minor, False))
        if python_markers:
            # Select the lock branch matching the current Python minor version.
            current = f"{sys.version_info[0]}.{sys.version_info[1]}"
            selected = False
            for marker in markers:
                match = re.search(r"python_full_version ([<>=]+) '([^']+)'", marker)
                if not match:
                    continue
                operator, raw_version = match.groups()
                version = tuple(map(int, raw_version.split(".")[:3]))
                here = tuple(map(int, current.split(".") + ["0"] * (3 - len(current.split(".")))))
                if operator == ">=":
                    selected = selected or here >= version
                elif operator == "<":
                    selected = selected or here < version
            if not selected:
                continue
            lower = max((version for version, enabled in python_markers if enabled and version <= current), default=None)
            upper = min((version for version, enabled in python_markers if not enabled and version > current), default=None)
            marker_parts = []
            if lower:
                marker_parts.append(f"python_version >= '{lower}'")
            if upper:
                marker_parts.append(f"python_version < '{upper}'")
            if not marker_parts:
                print(f"{package['name']}=={package['version']}")
            else:
                print(f"{package['name']}=={package['version']} ; {' and '.join(marker_parts)}")
        else:
            print(f"{package['name']}=={package['version']}")


if __name__ == "__main__":
    main()
