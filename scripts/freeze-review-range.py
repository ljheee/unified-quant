#!/usr/bin/env python3
"""Freeze a proven, read-only daily-review commit range as canonical JSON."""

from __future__ import annotations

import argparse
import errno
import hashlib
import heapq
import json
import os
import re
import stat
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from scripts.watermark import WatermarkError, parse_watermark
except ModuleNotFoundError:  # direct execution as scripts/freeze-review-range.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from watermark import WatermarkError, parse_watermark


POLICY_PATH = "docs/automation/range-exclusions.json"
RULE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VALID_DIFF_STATUS = {"A", "M", "D", "T", "U", "X", "B"}


class FreezeError(Exception):
    """An unsafe or incomplete range could not be proven."""


def git_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_SHALLOW_FILE",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        "GIT_CEILING_DIRECTORIES",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    ):
        env.pop(name, None)
    env.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return env


def _git_error(args: list[str], stderr: bytes) -> FreezeError:
    detail = stderr.decode("utf-8", errors="replace").strip()
    command = "git " + " ".join(args[:2])
    if detail:
        return FreezeError(f"{command} failed: {detail}")
    return FreezeError(f"{command} failed")


def git_bytes(repo: Path, args: list[str]) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=git_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise _git_error(args, completed.stderr)
    return completed.stdout


def git_text(repo: Path, args: list[str]) -> str:
    try:
        return git_bytes(repo, args).decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise FreezeError(f"git {' '.join(args[:2])} returned non-UTF-8 metadata") from exc


def git_quiet(repo: Path, args: list[str]) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=git_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise _git_error(args, completed.stderr)


def repository_root(start: Path) -> Path:
    try:
        root_text = git_text(start, ["rev-parse", "--show-toplevel"])
    except FreezeError as exc:
        raise FreezeError(f"not a usable Git working tree: {exc}") from exc
    root = Path(root_text)
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise FreezeError(f"cannot resolve repository root: {exc}") from exc


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def read_regular_file_noatime(path: Path, description: str) -> tuple[bytes, tuple[int, ...]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    fd: int | None = None
    if noatime:
        try:
            fd = os.open(path, flags | noatime)
        except OSError as exc:
            if exc.errno not in {
                errno.EPERM,
                errno.EACCES,
                errno.EINVAL,
                getattr(errno, "EOPNOTSUPP", errno.EINVAL),
            }:
                raise FreezeError(f"cannot open {description}: {exc}") from exc
    if fd is None:
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise FreezeError(f"cannot open {description}: {exc}") from exc

    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise FreezeError(f"{description} must be a regular, non-symlink file")
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(fd, 1024 * 1024)
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if _stat_identity(before) != _stat_identity(after):
            raise FreezeError(f"{description} changed while it was being read")
        return b"".join(chunks), _stat_identity(after)
    finally:
        os.close(fd)


def object_id_length(object_format: str) -> int:
    lengths = {"sha1": 40, "sha256": 64}
    try:
        return lengths[object_format]
    except KeyError as exc:
        raise FreezeError(f"unsupported Git object format: {object_format}") from exc


def validate_object_id(value: str, length: int, what: str) -> str:
    if len(value) != length or not re.fullmatch(r"[0-9a-fA-F]+", value):
        raise FreezeError(f"{what} must be a full {length}-character hexadecimal object ID")
    return value.lower()


def read_watermark(repo: Path, length: int) -> tuple[str, bytes, tuple[int, ...]]:
    data, identity = read_regular_file_noatime(repo / "LESSONS.md", "LESSONS.md")
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FreezeError("LESSONS.md is not valid UTF-8") from exc

    try:
        raw_value, _, _ = parse_watermark(data)
    except WatermarkError as exc:
        raise FreezeError(str(exc)) from exc
    if raw_value == "UNKNOWN":
        raise FreezeError(
            "reviewed-through is UNKNOWN; stop and ask a maintainer to choose the first "
            "target-branch commit to include. The exclusive watermark is that commit's "
            "target-branch parent (the target-branch/first parent for a merge). Do not "
            "infer or initialize this starting watermark automatically. If there is no target-branch "
            "parent, stop for an explicit baseline design decision."
        )
    value = validate_object_id(raw_value, length, "reviewed-through")
    if value == "0" * length:
        raise FreezeError("reviewed-through cannot be the all-zero object ID")
    return value, data, identity


def check_history_safety(repo: Path) -> None:
    shallow = git_text(repo, ["rev-parse", "--is-shallow-repository"])
    if shallow != "false":
        raise FreezeError("shallow history is not accepted; refusing to freeze a partial range")

    replace_refs = git_bytes(repo, ["for-each-ref", "--format=%(refname)", "refs/replace/"])
    if replace_refs.strip():
        raise FreezeError("replacement refs are not accepted for ancestry or range proof")

    graft_path_text = git_text(repo, ["rev-parse", "--git-path", "info/grafts"])
    graft_path = Path(graft_path_text)
    if not graft_path.is_absolute():
        graft_path = repo / graft_path
    if os.path.lexists(graft_path):
        raise FreezeError("a graft file is not accepted for ancestry or range proof")


def resolve_commit(repo: Path, oid: str, what: str) -> str:
    try:
        resolved = git_text(repo, ["rev-parse", "--verify", f"{oid}^{{commit}}"])
    except FreezeError as exc:
        raise FreezeError(f"{what} does not resolve to an available commit: {exc}") from exc
    if resolved != oid:
        raise FreezeError(f"{what} does not resolve to the exact supplied commit object ID")
    return resolved


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older, newer],
        cwd=repo,
        env=git_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise _git_error(["merge-base", "--is-ancestor", older, newer], completed.stderr)


def validate_complete_history(repo: Path, head: str) -> None:
    # --missing=error and disabled lazy fetching make incomplete/promisor history fail closed.
    git_quiet(repo, ["rev-list", "--objects", "--missing=error", head])
    # Full fsck verifies reachable object contents against their object IDs, not just presence.
    git_quiet(
        repo,
        ["fsck", "--full", "--strict", "--no-reflogs", "--no-dangling", head],
    )


def git_blob_oid(object_format: str, content: bytes) -> str:
    hasher = hashlib.new(object_format)
    hasher.update(b"blob " + str(len(content)).encode("ascii") + b"\0")
    hasher.update(content)
    return hasher.hexdigest()


def committed_policy_blob(repo: Path, head: str, object_format: str, length: int) -> tuple[str, bytes, tuple[int, ...]]:
    output = git_bytes(repo, ["ls-tree", "-z", "--full-tree", head, "--", POLICY_PATH])
    entries = [entry for entry in output.split(b"\0") if entry]
    if len(entries) != 1:
        raise FreezeError(f"{POLICY_PATH} must exist exactly once in the through commit")
    try:
        header, path = entries[0].split(b"\t", 1)
        mode, object_type, oid_bytes = header.split(b" ", 2)
        oid = oid_bytes.decode("ascii", errors="strict")
        path_text = path.decode("utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError) as exc:
        raise FreezeError(f"cannot parse committed exclusion-policy entry: {exc}") from exc
    if path_text != POLICY_PATH or object_type != b"blob" or mode not in {b"100644", b"100755"}:
        raise FreezeError(f"{POLICY_PATH} must be a committed regular-file blob")
    oid = validate_object_id(oid, length, "exclusion-policy blob ID")

    policy_path = repo / POLICY_PATH
    content, identity = read_regular_file_noatime(policy_path, POLICY_PATH)
    if git_blob_oid(object_format, content) != oid:
        raise FreezeError(
            f"working-tree {POLICY_PATH} differs from its committed version at through"
        )
    return oid, content, identity


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def validate_unicode_text(value: str, where: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise FreezeError(f"{where} is not valid UTF-8") from exc


def parse_policy(content: bytes) -> tuple[int, list[dict[str, Any]]]:
    try:
        text = content.decode("utf-8", errors="strict")
        policy = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FreezeError(f"invalid exclusion policy JSON: {exc}") from exc
    if not isinstance(policy, dict) or set(policy) != {"policy_version", "rules"}:
        raise FreezeError("exclusion policy must contain only policy_version and rules")
    policy_version = policy["policy_version"]
    if type(policy_version) is not int or policy_version not in {1, 2, 3}:
        raise FreezeError("unsupported exclusion policy version")
    if not isinstance(policy["rules"], list):
        raise FreezeError("exclusion policy rules must be an array")

    rules: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for index, raw_rule in enumerate(policy["rules"]):
        where = f"exclusion rule {index + 1}"
        if not isinstance(raw_rule, dict):
            raise FreezeError(f"{where} must be an object")
        kind = raw_rule.get("kind")
        expected = (
            {"id", "kind", "patterns", "reason"}
            if kind == "path-glob"
            else {"id", "kind", "paths", "reason"}
            if kind == "path-prefix"
            else {"id", "kind", "reason"}
        )
        if not isinstance(kind, str) or kind not in {"path-glob", "path-prefix", "binary-nul-byte", "gitlink"}:
            raise FreezeError(f"{where} has an unknown kind")
        if kind == "path-prefix" and policy_version >= 3 and "producer" in raw_rule:
            expected.add("producer")
        if kind == "path-prefix" and policy_version < 2:
            raise FreezeError(f"{where} path-prefix rules require exclusion policy version 2")
        if set(raw_rule) != expected:
            raise FreezeError(f"{where} has unexpected or missing fields")
        identifier = raw_rule["id"]
        reason = raw_rule["reason"]
        if not isinstance(identifier, str) or not RULE_ID.fullmatch(identifier):
            raise FreezeError(f"{where} has an invalid id")
        validate_unicode_text(identifier, f"{where} id")
        if identifier in identifiers:
            raise FreezeError(f"duplicate exclusion rule id: {identifier}")
        identifiers.add(identifier)
        if not isinstance(reason, str) or not reason.strip():
            raise FreezeError(f"{where} must state a non-empty reason")
        validate_unicode_text(reason, f"{where} reason")

        normalized: dict[str, Any] = {"id": identifier, "kind": kind}
        if kind == "path-glob":
            patterns = raw_rule["patterns"]
            if not isinstance(patterns, list) or not patterns:
                raise FreezeError(f"{where} must have at least one pattern")
            if any(not isinstance(pattern, str) for pattern in patterns):
                raise FreezeError(f"{where} patterns must be strings")
            for pattern in patterns:
                validate_unicode_text(pattern, f"{where} pattern")
            if len(set(patterns)) != len(patterns):
                raise FreezeError(f"{where} contains duplicate patterns")
            for pattern in patterns:
                validate_glob(pattern, where)
            normalized["patterns"] = list(patterns)
        elif kind == "path-prefix":
            paths = raw_rule["paths"]
            if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                raise FreezeError(f"{where} paths must be an array of literal repository paths")
            for path in paths:
                validate_unicode_text(path, f"{where} path")
            if len(set(paths)) != len(paths):
                raise FreezeError(f"{where} contains duplicate paths")
            for path in paths:
                validate_explicit_path(path, where)
            normalized["paths"] = list(paths)
            if policy_version >= 3:
                if paths and "producer" not in raw_rule:
                    raise FreezeError(f"{where} with paths must describe a concrete producer process")
                if "producer" in raw_rule:
                    producer = raw_rule["producer"]
                    if not isinstance(producer, str) or not producer.strip():
                        raise FreezeError(f"{where} producer must be a non-empty process description")
                    validate_unicode_text(producer, f"{where} producer")
                    normalized["producer"] = producer
        normalized["reason"] = reason
        rules.append(normalized)
    return policy_version, rules


def validate_glob(pattern: str, where: str) -> None:
    if not pattern or pattern.startswith("/") or pattern.endswith("/") or "\\" in pattern:
        raise FreezeError(f"{where} has an invalid repository-relative path glob")
    segments = pattern.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise FreezeError(f"{where} has an invalid path segment")
    for segment in segments:
        if "**" in segment and segment != "**":
            raise FreezeError(f"{where} may use ** only as a whole path segment")


def validate_explicit_path(path: str, where: str) -> None:
    if not path or path.startswith("/") or path.endswith("/") or "\\" in path or "\x00" in path:
        raise FreezeError(f"{where} has an invalid literal repository-relative path")
    try:
        path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise FreezeError(f"{where} path is not valid UTF-8") from exc
    segments = path.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise FreezeError(f"{where} has an invalid path segment")
    if any("*" in segment or "?" in segment for segment in segments):
        raise FreezeError(f"{where} paths must be literal; wildcard characters are not allowed")


def _segment_match(pattern: str, segment: str) -> bool:
    expression = "^" + "".join(
        ".*" if char == "*" else "." if char == "?" else re.escape(char)
        for char in pattern
    ) + "$"
    return re.fullmatch(expression[1:-1], segment, flags=re.DOTALL) is not None


def path_glob_matches(pattern: str, path: str) -> bool:
    pattern_parts = pattern.split("/")
    path_parts = path.split("/")

    @lru_cache(maxsize=None)
    def match(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        current = pattern_parts[pattern_index]
        if current == "**":
            return match(pattern_index + 1, path_index) or (
                path_index < len(path_parts) and match(pattern_index, path_index + 1)
            )
        return (
            path_index < len(path_parts)
            and _segment_match(current, path_parts[path_index])
            and match(pattern_index + 1, path_index + 1)
        )

    return match(0, 0)


def parse_commit(repo: Path, sha: str, length: int) -> dict[str, Any]:
    raw = git_bytes(repo, ["cat-file", "commit", sha])
    headers = raw.split(b"\n\n", 1)[0].splitlines()
    trees: list[str] = []
    parents: list[str] = []
    try:
        for header in headers:
            if header.startswith(b"tree "):
                trees.append(header[5:].decode("ascii", errors="strict"))
            elif header.startswith(b"parent "):
                parents.append(header[7:].decode("ascii", errors="strict"))
    except UnicodeDecodeError as exc:
        raise FreezeError(f"commit {sha} has a malformed object ID header") from exc
    if len(trees) != 1:
        raise FreezeError(f"commit {sha} does not have exactly one tree")
    tree = validate_object_id(trees[0], length, f"tree ID in commit {sha}")
    normalized_parents = [
        validate_object_id(parent, length, f"parent ID in commit {sha}")
        for parent in parents
    ]
    if len(set(normalized_parents)) != len(normalized_parents):
        raise FreezeError(f"commit {sha} has a duplicate parent entry")
    return {"sha": sha, "tree": tree, "parents": normalized_parents}


def deterministic_commit_order(
    repo: Path, from_sha: str, through_sha: str, length: int
) -> list[dict[str, Any]]:
    raw = git_bytes(repo, ["rev-list", "--missing=error", f"{from_sha}..{through_sha}"])
    commit_ids: set[str] = set()
    for line in raw.splitlines():
        try:
            oid = line.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise FreezeError("Git returned a non-ASCII commit ID") from exc
        commit_ids.add(validate_object_id(oid, length, "commit ID in range"))
    if len(commit_ids) != len(raw.splitlines()):
        raise FreezeError("Git returned duplicate commit IDs for the range")

    metadata = {sha: parse_commit(repo, sha, length) for sha in commit_ids}
    children: dict[str, list[str]] = {sha: [] for sha in commit_ids}
    remaining_parents: dict[str, int] = {}
    for sha, commit in metadata.items():
        parents_in_range = [parent for parent in commit["parents"] if parent in commit_ids]
        remaining_parents[sha] = len(parents_in_range)
        for parent in parents_in_range:
            children[parent].append(sha)

    ready = [sha for sha, count in remaining_parents.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        current = heapq.heappop(ready)
        ordered.append(current)
        for child in sorted(children[current]):
            remaining_parents[child] -= 1
            if remaining_parents[child] == 0:
                heapq.heappush(ready, child)
    if len(ordered) != len(commit_ids):
        raise FreezeError("the commit graph could not be ordered as a complete DAG")
    return [metadata[sha] for sha in ordered]


def parse_raw_diff(raw: bytes, length: int) -> list[dict[str, str | bytes | None]]:
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    result: list[dict[str, str | bytes | None]] = []
    cursor = 0
    zero_oid = "0" * length
    while cursor < len(fields):
        header = fields[cursor]
        cursor += 1
        if not header.startswith(b":"):
            raise FreezeError("Git returned an invalid raw diff header")
        pieces = header[1:].split()
        if len(pieces) != 5:
            raise FreezeError("Git returned an incomplete raw diff header")
        try:
            old_mode, new_mode = pieces[0].decode("ascii"), pieces[1].decode("ascii")
            old_oid_raw = pieces[2].decode("ascii")
            new_oid_raw = pieces[3].decode("ascii")
            status = pieces[4].decode("ascii")
        except UnicodeDecodeError as exc:
            raise FreezeError("Git returned non-ASCII raw diff metadata") from exc
        if len(status) != 1 or status not in VALID_DIFF_STATUS:
            raise FreezeError(f"unsupported raw diff status: {status!r}")
        if cursor >= len(fields):
            raise FreezeError("Git returned a raw diff record without a path")
        path_bytes = fields[cursor]
        cursor += 1
        if not path_bytes:
            raise FreezeError("Git returned an empty changed path")
        old_oid = None if old_oid_raw == zero_oid else validate_object_id(
            old_oid_raw, length, "old diff object ID"
        )
        new_oid = None if new_oid_raw == zero_oid else validate_object_id(
            new_oid_raw, length, "new diff object ID"
        )
        result.append(
            {
                "old_mode": old_mode,
                "new_mode": new_mode,
                "old_oid": old_oid,
                "new_oid": new_oid,
                "status": status,
                "path_bytes": path_bytes,
            }
        )
    return result


def blob_contains_nul(repo: Path, oid: str) -> bool:
    process = subprocess.Popen(
        ["git", "cat-file", "blob", oid],
        cwd=repo,
        env=git_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    found = False
    try:
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                break
            if b"\0" in chunk:
                found = True
        stderr = process.stderr.read() if process.stderr is not None else b""
        returncode = process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    if returncode != 0:
        raise _git_error(["cat-file", "blob", oid], stderr)
    return found


def diff_for_parent(repo: Path, commit: dict[str, Any], parent: str | None) -> bytes:
    common = ["diff-tree", "--raw", "-z", "--no-commit-id", "--no-renames", "-r"]
    if parent is None:
        return git_bytes(repo, [*common[:1], "--root", *common[1:], commit["sha"]])
    return git_bytes(repo, [*common, parent, commit["sha"]])


def exclusion_records(
    repo: Path,
    commits: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    length: int,
) -> list[dict[str, Any]]:
    records: list[tuple[int, int, bytes, int, dict[str, Any]]] = []
    for commit_index, commit in enumerate(commits):
        parents: list[str | None] = commit["parents"] if commit["parents"] else [None]
        for parent_index, parent in enumerate(parents):
            diffs = parse_raw_diff(diff_for_parent(repo, commit, parent), length)
            diffs.sort(key=lambda item: item["path_bytes"] or b"")
            for changed in diffs:
                path_bytes = changed["path_bytes"]
                assert isinstance(path_bytes, bytes)
                try:
                    path = path_bytes.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise FreezeError(
                        f"changed path in commit {commit['sha']} is not valid UTF-8"
                    ) from exc
                if path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
                    raise FreezeError(f"Git returned a non-canonical repository path: {path!r}")

                old_mode = changed["old_mode"]
                new_mode = changed["new_mode"]
                old_oid = changed["old_oid"]
                new_oid = changed["new_oid"]
                matched: list[tuple[int, dict[str, Any], str, str]] = []
                is_gitlink = old_mode == "160000" or new_mode == "160000"
                for rule_index, rule in enumerate(rules):
                    kind = rule["kind"]
                    if kind == "path-glob":
                        matched_pattern = next(
                            (pattern for pattern in rule["patterns"] if path_glob_matches(pattern, path)),
                            None,
                        )
                        if matched_pattern is not None:
                            matched.append((rule_index, rule, "pattern", matched_pattern))
                    elif kind == "path-prefix":
                        matched_prefix = next(
                            (
                                prefix
                                for prefix in rule["paths"]
                                if path == prefix or path.startswith(prefix + "/")
                            ),
                            None,
                        )
                        if matched_prefix is not None:
                            matched.append((rule_index, rule, "path_prefix", matched_prefix))
                    elif kind == "gitlink" and is_gitlink:
                        matched.append((rule_index, rule, "classification", "gitlink"))
                    elif kind == "binary-nul-byte" and not is_gitlink:
                        has_nul = False
                        for mode, oid in ((old_mode, old_oid), (new_mode, new_oid)):
                            if mode != "000000" and oid is not None:
                                if not (mode.startswith("100") or mode == "120000"):
                                    raise FreezeError(
                                        f"cannot classify non-blob path {path!r} in commit {commit['sha']}"
                                    )
                                has_nul = blob_contains_nul(repo, oid) or has_nul
                        if has_nul:
                            matched.append(
                                (rule_index, rule, "classification", "binary-nul-byte")
                            )

                for rule_index, rule, match_kind, match_value in matched:
                    record = {
                        "commit": commit["sha"],
                        "parent": parent,
                        "path": path,
                        "status": changed["status"],
                        "old_mode": old_mode,
                        "new_mode": new_mode,
                        "old_oid": old_oid,
                        "new_oid": new_oid,
                        "rule_id": rule["id"],
                        "match": {match_kind: match_value},
                        "reason": rule["reason"],
                    }
                    records.append(
                        (commit_index, parent_index, path_bytes, rule_index, record)
                    )
    records.sort(key=lambda entry: (entry[0], entry[1], entry[2], entry[3]))
    return [record for *_, record in records]


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FreezeError(f"manifest cannot be serialized canonically: {exc}") from exc


def add_range_key(payload: dict[str, Any]) -> dict[str, Any]:
    if "range_key" in payload:
        raise FreezeError("range key input must not already contain range_key")
    key = "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()
    return {**payload, "range_key": key}


def freeze_range(repo_path: Path) -> dict[str, Any]:
    repo = repository_root(repo_path)
    object_format = git_text(repo, ["rev-parse", "--show-object-format"])
    length = object_id_length(object_format)
    check_history_safety(repo)

    from_sha, watermark_bytes, watermark_identity = read_watermark(repo, length)
    from_sha = resolve_commit(repo, from_sha, "reviewed-through")
    head = git_text(repo, ["rev-parse", "--verify", "HEAD^{commit}"])
    head = validate_object_id(head, length, "HEAD")
    head = resolve_commit(repo, head, "HEAD")
    if not is_ancestor(repo, from_sha, head):
        raise FreezeError(
            "reviewed-through is not an ancestor of HEAD; history may have been rebased or force-pushed"
        )

    validate_complete_history(repo, head)
    commits = deterministic_commit_order(repo, from_sha, head, length)
    policy_blob, policy_bytes, policy_identity = committed_policy_blob(
        repo, head, object_format, length
    )
    policy_version, rules = parse_policy(policy_bytes)
    exclusions = exclusion_records(repo, commits, rules, length)
    policy = {
        "source_path": POLICY_PATH,
        "source_commit": head,
        "source_blob": policy_blob,
        "source_sha256": hashlib.sha256(policy_bytes).hexdigest(),
        "policy_version": policy_version,
        "rules": rules,
    }
    payload: dict[str, Any] = {
        "manifest_version": 1,
        "from": from_sha,
        "through": head,
        "commits": commits,
        "exclusion_policy": policy,
        "excluded_paths": exclusions,
    }

    # Refuse output if the state used to calculate the range changed concurrently.
    if git_text(repo, ["rev-parse", "--verify", "HEAD^{commit}"]) != head:
        raise FreezeError("HEAD changed while the range was being frozen; retry explicitly")
    current_watermark, current_watermark_bytes, current_watermark_identity = read_watermark(
        repo, length
    )
    if (
        current_watermark != from_sha
        or current_watermark_bytes != watermark_bytes
        or current_watermark_identity != watermark_identity
    ):
        raise FreezeError("LESSONS.md changed while the range was being frozen; retry explicitly")
    current_policy_bytes, current_policy_identity = read_regular_file_noatime(
        repo / POLICY_PATH, POLICY_PATH
    )
    if (
        current_policy_bytes != policy_bytes
        or current_policy_identity != policy_identity
        or git_blob_oid(object_format, current_policy_bytes) != policy_blob
    ):
        raise FreezeError(f"{POLICY_PATH} changed while the range was being frozen")

    return add_range_key(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate and emit a read-only frozen review-range manifest as canonical JSON."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="repository working-tree path (default: current directory)",
    )
    mode.add_argument(
        "--validate-policy",
        type=Path,
        metavar="PATH",
        help="validate only an exclusion policy using the same parser; no Git history or watermark required",
    )
    args = parser.parse_args(argv)
    try:
        if args.validate_policy is not None:
            content, _ = read_regular_file_noatime(args.validate_policy, "exclusion policy")
            parse_policy(content)
            return 0
        manifest = freeze_range(args.repo)
        sys.stdout.buffer.write(canonical_json(manifest) + b"\n")
        sys.stdout.buffer.flush()
    except (FreezeError, OSError, subprocess.SubprocessError) as exc:
        print(f"freeze-review-range: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
