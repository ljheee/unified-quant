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


def _dependency_name(dependency: str | dict[str, object]) -> str:
    if isinstance(dependency, dict):
        return str(dependency.get("name", "")).strip().lower()
    return (
        dependency.split(";")[0].split("==")[0].split(">=")[0].split("<=")[0].split("<")[0].split(">")[0].strip().lower()
    )


def _resolve_locked_packages(extras: set[str]) -> set[str] | None:
    if not extras:
        return None
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    selected = {
        dependency.split(";")[0].split("==")[0].split(">=")[0].split("<=")[0].split("<")[0].split(">")[0].strip()
        for dependency in project.get("dependencies", [])
    }
    optional = project.get("optional-dependencies", {})
    for extra in extras:
        if extra not in optional:
            raise SystemExit(f"unknown requested extra: {extra}")
        selected.update(_dependency_name(dependency) for dependency in optional[extra])
    lock_packages = {
        package["name"]: package
        for package in tomllib.loads(Path("uv.lock").read_text(encoding="utf-8")).get("package", [])
    }
    pending = sorted(selected)
    while pending:
        name = pending.pop().lower()
        if name in selected and name not in lock_packages:
            continue
        selected.add(name)
        for dependency in lock_packages.get(name, {}).get("dependencies", []):
            dependency_name = _dependency_name(dependency)
            if dependency_name not in selected:
                selected.add(dependency_name)
                pending.append(dependency_name)
    return selected


def main() -> None:
    extras: set[str] = set()
    args = iter(sys.argv[1:])
    for arg in args:
        if arg == "--extras":
            try:
                extras.update(next(args).split(","))
            except StopIteration as exc:
                raise SystemExit("--extras requires a comma-separated value") from exc
        else:
            raise SystemExit(f"unknown argument: {arg}")
    data = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    selected_packages = _resolve_locked_packages(extras)
    for package in data.get("package", []):
        if selected_packages is not None and package["name"].lower() not in selected_packages:
            continue
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
