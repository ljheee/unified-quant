#!/usr/bin/env python3
"""Plan framework-owned updates; install.sh supplies the sole-writer transaction."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import unicodedata

sys.dont_write_bytecode = True

PROFILE = "docs/project-profile.md"
MANIFEST = ".agent-maintenance/framework-version.json"
ROUTE = "SELF-EVOLUTION-FLYWHEEL MANAGED ROUTING"
FRAMEWORK = "SELF-EVOLUTION-FLYWHEEL PROFILE FRAMEWORK"
FACTS = "SELF-EVOLUTION-FLYWHEEL PROFILE FACTS"
OWNED = (
    ".agent-maintenance/flywheel-install.sh",
    ".agent-maintenance/framework-ownership.py",
    ".agent-maintenance/installer-transaction.py",
    ".agent-maintenance/session-start.sh",
    ".github/workflows/lesson-promotion-gate.yml",
    "scripts/freeze-review-range.py",
    "scripts/validate_lesson_promotions.py",
    "scripts/watermark.py",
    "scripts/write-frozen-review.py",
)
HASH_PATHS = set(OWNED) | {PROFILE, "AGENTS.md"}


class OwnershipError(Exception):
    pass


def helper(source):
    filename = os.environ.get("FLYWHEEL_TRANSACTION_HELPER") or str(Path(source) / ".agent-maintenance/installer-transaction.py")
    spec = importlib.util.spec_from_file_location("_flywheel_ownership_transaction", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OwnershipError(f"duplicate manifest key: {key!r}")
        result[key] = value
    return result


def read_regular(tx, root, relative):
    """Read through no-follow directory descriptors, never through target links."""
    root_fd = tx.open_root(root)
    parent_fd = None
    fd = None
    try:
        try:
            parent_fd, name = tx.open_parent(root_fd, relative, False, {"created_directories": []}, "unused")
        except FileNotFoundError:
            return None
        fd = tx.open_existing(parent_fd, name, reject_hardlinks=True)
        if fd is None:
            return None
        if not tx.verify_parent(root, root_fd, relative, parent_fd):
            raise OwnershipError(f"parent changed while reading {relative}")
        _, data = tx.confirmation_snapshot(fd, parent_fd)
        return data
    finally:
        if fd is not None:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def bounds(data, label):
    begin = f"<!-- BEGIN {label} -->".encode()
    end = f"<!-- END {label} -->".encode()
    if data.count(begin) != 1 or data.count(end) != 1:
        raise OwnershipError(f"{label}: missing or duplicated managed markers; preserved; manual handling required")
    lines = data.splitlines(keepends=True)
    offset = 0
    found = {}
    for line in lines:
        stripped = line.rstrip(b"\r\n")
        if stripped in (begin, end):
            found[stripped] = (offset, offset + len(line), line)
        offset += len(line)
    if begin not in found or end not in found or found[begin][0] >= found[end][0]:
        raise OwnershipError(f"{label}: markers must be standalone, ordered lines; preserved; manual handling required")
    first, last = found[begin], found[end]
    ending = b"\r\n" if last[2].endswith(b"\r\n") else (b"\n" if last[2].endswith(b"\n") else b"")
    # END's line terminator belongs to the surrounding project, not the block.
    return first[0], last[0] + len(end), ending


def block(data, label):
    start, end, _ = bounds(data, label)
    return data[start:end]


def profile_bounds(data):
    first = bounds(data, FRAMEWORK)
    facts = bounds(data, FACTS)
    if not (first[1] < facts[0] or facts[1] < first[0]):
        raise OwnershipError("profile framework and facts markers overlap; preserved; manual handling required")
    return first


def replace_profile(current, source):
    start, end, _ = profile_bounds(current)
    profile_bounds(source)
    # Facts, prefix, suffix, and END newline are preserved as original bytes.
    return current[:start] + block(source, FRAMEWORK) + current[end:]


def digest(hashes):
    return sha(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode())


def manifest(tx, root):
    raw = read_regular(tx, root, MANIFEST)
    if raw is None:
        return None
    value = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(value, dict) or set(value) != {"schema", "source_revision", "content_digest", "framework_hashes"} or value["schema"] != 1:
        raise OwnershipError("invalid framework manifest schema; preserved")
    hashes = value["framework_hashes"]
    if not isinstance(hashes, dict) or set(hashes) != HASH_PATHS or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes.values()):
        raise OwnershipError("invalid framework manifest paths/hashes; preserved")
    revision = value["source_revision"]
    if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision)):
        raise OwnershipError("invalid framework source revision; preserved")
    if value["content_digest"] != digest(hashes):
        raise OwnershipError("framework manifest content digest mismatch; preserved")
    return value


def escaped(text):
    result = []
    for char in text:
        if char == "\n" or (not unicodedata.category(char).startswith("C") and unicodedata.category(char) not in {"Zl", "Zp"}):
            result.append(char)
        else:
            result.append(ascii(char)[1:-1])
    return "".join(result)


def diff(relative, current, proposed):
    text = "".join(difflib.unified_diff(current.decode("utf-8", "backslashreplace").splitlines(True), proposed.decode("utf-8", "backslashreplace").splitlines(True), fromfile=relative + " (current)", tofile=relative + " (template)"))
    return escaped(text)


def source_revision(source):
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_IMPLICIT_WORK_TREE", "GIT_PREFIX", "GIT_SHALLOW_FILE",
                "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM"):
        env.pop(key, None)
    try:
        root = subprocess.run(["git", "-C", source, "rev-parse", "--show-toplevel"], env=env, capture_output=True, text=True)
        if root.returncode or os.path.realpath(root.stdout.strip()) != os.path.realpath(source):
            return None  # A copied source snapshot must not inherit a parent project's SHA.
        result = subprocess.run(["git", "-C", source, "rev-parse", "HEAD"], env=env, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None
    except OSError:
        return None  # The content digest still identifies a source snapshot.


def plan(source, target, local_entry, workdir):
    tx = helper(source)
    previous = manifest(tx, target)
    if local_entry and previous is None:
        raise OwnershipError("project-local entry requires its framework version manifest; no legacy migration is performed")
    source_data = {}
    modes = {}
    for relative in (*OWNED, PROFILE, "AGENTS.md"):
        source_relative = "install.sh" if relative == OWNED[0] and not local_entry else relative
        data = read_regular(tx, source, source_relative)
        if data is None:
            raise OwnershipError(f"missing installer source file: {source_relative}")
        source_data[relative] = data
        modes[relative] = stat.S_IMODE(os.stat(Path(source) / source_relative, follow_symlinks=False).st_mode)
    profile_bounds(source_data[PROFILE])
    bounds(source_data["AGENTS.md"], ROUTE)
    current_agents = read_regular(tx, target, "AGENTS.md")
    if current_agents is not None:
        bounds(current_agents, ROUTE)
    hashes = {relative: sha(block(data, FRAMEWORK if relative == PROFILE else ROUTE)) if relative in (PROFILE, "AGENTS.md") else sha(data) for relative, data in source_data.items()}
    if local_entry:
        for relative in HASH_PATHS:
            if previous["framework_hashes"][relative] != hashes[relative]:
                raise OwnershipError(f"diff: local framework file differs from recorded template version: {relative}; expected {previous['framework_hashes'][relative]}, current {hashes[relative]}; preserved. Use an independent reviewed template checkout.")
    operations = []
    conflicts = []
    staged_root = Path(workdir) / "owned-staged"
    for relative in (*OWNED, PROFILE):
        data = source_data[relative]
        current = read_regular(tx, target, relative)
        proposed = replace_profile(current, data) if relative == PROFILE and current is not None else data
        current_hash = None if current is None else sha(block(current, FRAMEWORK) if relative == PROFILE else current)
        allowed = relative == PROFILE or current is None or current_hash == hashes[relative] or (previous is not None and current_hash == previous["framework_hashes"][relative])
        if not allowed:
            conflicts.append("diff: unknown or customized framework file refused: " + relative + "\n" + diff(relative, current, proposed))
        staged = staged_root / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(proposed)
        staged.chmod(modes[relative])
        operations.append({"path": relative, "action": "create" if current is None else ("unchanged" if current == proposed else "update"), "before": None if current is None else sha(current), "after": sha(proposed)})
    if conflicts:
        raise OwnershipError("\n".join(conflicts))
    result = {"schema": 1, "source_revision": previous["source_revision"] if local_entry else source_revision(source), "content_digest": digest(hashes), "framework_hashes": hashes, "operations": operations, "local_entry": bool(local_entry)}
    (Path(workdir) / "ownership-plan.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def load_plan(workdir):
    return json.loads((Path(workdir) / "ownership-plan.json").read_bytes())


def write_one(tx, target, relative, staged, state, workdir, expected_before):
    current = read_regular(tx, target, relative)
    if (None if current is None else sha(current)) != expected_before:
        raise OwnershipError(f"target changed after ownership planning: {relative}; preserved")
    if current == staged.read_bytes():
        print(f"UNCHANGED framework {relative}")
        return
    if current is None:
        tx.op_copy(target, relative, str(staged), state)
        print(f"INSTALLED framework {relative}")
        return
    prefix = str(Path(workdir) / ("owned-proposal-" + relative.replace("/", "__")))
    tx.proposal_command(target, relative, prefix)
    if sha(Path(prefix + ".original").read_bytes()) != expected_before:
        raise OwnershipError(f"target changed while snapshotting: {relative}; preserved")
    tx.proposal_material_command(prefix, str(staged))
    tx.apply_proposal_command(target, relative, prefix, str(staged), state, require_display_manifest=True)
    print(f"UPDATED framework {relative}")


def apply(source, target, workdir, state):
    tx = helper(source)
    result = load_plan(workdir)
    for operation in result["operations"]:
        staged = Path(workdir) / "owned-staged" / operation["path"]
        if sha(staged.read_bytes()) != operation["after"]:
            raise OwnershipError("staged framework differs from the planned template version")
        write_one(tx, target, operation["path"], staged, state, workdir, operation["before"])
    return result


def apply_manifest(source, target, workdir, state):
    tx = helper(source)
    result = load_plan(workdir)
    # Do not claim a complete version until every framework publication is verified.
    for operation in result["operations"]:
        data = read_regular(tx, target, operation["path"])
        if data is None or sha(data) != operation["after"]:
            raise OwnershipError(f"framework version verification failed: {operation['path']}")
    agents = read_regular(tx, target, "AGENTS.md")
    if agents is None or sha(block(agents, ROUTE)) != result["framework_hashes"]["AGENTS.md"]:
        raise OwnershipError("AGENTS managed routing does not match the selected template")
    value = {key: result[key] for key in ("schema", "source_revision", "content_digest", "framework_hashes")}
    staged = Path(workdir) / "owned-manifest.json"
    staged.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    current = read_regular(tx, target, MANIFEST)
    write_one(tx, target, MANIFEST, staged, state, workdir, None if current is None else sha(current))
    return result


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply", "manifest"):
        item = sub.add_parser(command)
        item.add_argument("source")
        item.add_argument("target")
        if command == "plan":
            item.add_argument("local_entry", type=int)
        item.add_argument("workdir")
        if command != "plan":
            item.add_argument("state")
            item.add_argument("--local-entry", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            result = plan(args.source, args.target, args.local_entry, args.workdir)
            print("Ownership: versioned framework code; marked AGENTS/profile regions; precise provider hooks; seed-only project content. Root install.sh is project-owned and excluded.")
        elif args.command == "apply":
            result = apply(args.source, args.target, args.workdir, args.state)
        else:
            result = apply_manifest(args.source, args.target, args.workdir, args.state)
        print(f"Framework template: revision={result['source_revision'] or 'snapshot'} content={result['content_digest']}")
        return 0
    except Exception as exc:
        print(f"ERROR: {escaped(str(exc))}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
