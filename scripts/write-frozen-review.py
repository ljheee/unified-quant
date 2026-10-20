#!/usr/bin/env python3
"""Write review and Lessons content for one already-frozen range manifest."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

try:
    from scripts.watermark import WatermarkError, parse_watermark
except ModuleNotFoundError:  # direct execution as scripts/write-frozen-review.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from watermark import WatermarkError, parse_watermark


ARCHIVE_START = re.compile(rb"^<!-- frozen-review-range: (sha256:[0-9a-f]{64}) -->$", re.MULTILINE)
ARCHIVE_END = re.compile(rb"^<!-- /frozen-review-range: (sha256:[0-9a-f]{64}) -->$", re.MULTILINE)
LESSON_START = re.compile(rb"^<!-- frozen-review-lessons: (sha256:[0-9a-f]{64}) -->$", re.MULTILINE)
LESSON_END = re.compile(rb"^<!-- /frozen-review-lessons: (sha256:[0-9a-f]{64}) -->$", re.MULTILINE)
PROMOTED_LESSON_START = re.compile(rb"^<!-- frozen-review-promotion: (sha256:[0-9a-f]{64}) -->$", re.MULTILINE)
PROMOTED_LESSON_END = re.compile(rb"^<!-- /frozen-review-promotion: (sha256:[0-9a-f]{64}) -->$", re.MULTILINE)
PROMOTED_STANDARD_START = re.compile(rb"^<!-- frozen-review-standard: (sha256:[0-9a-f]{64}) -->$", re.MULTILINE)
PROMOTED_STANDARD_END = re.compile(rb"^<!-- /frozen-review-standard: (sha256:[0-9a-f]{64}) -->$", re.MULTILINE)
RANGE_KEY = re.compile(r"^sha256:[0-9a-f]{64}$")
OBJECT_ID = re.compile(r"^[0-9a-f]+$")
LOCK_NAME = "frozen-review-writer.lock"
POLICY_PATH = "docs/automation/range-exclusions.json"
RULE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
PROMOTION_HEADING = re.compile(r"^### (L-[0-9]{4,}) — .+$", re.MULTILINE)
LESSON_ID_HEADING = re.compile(r"^### (L-[0-9]{4,})(?:\s|$)")
STANDARD_HEADING = re.compile(r"^##### (PS-[0-9]{4,}) — .+$")


class WriterError(Exception):
    """The frozen range could not be safely written or verified."""


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
        raise WriterError(f"manifest is not canonical JSON data: {exc}") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WriterError(f"manifest contains duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_float(_: str) -> None:
    raise WriterError("manifest contains a non-integer JSON number")


def _reject_constant(_: str) -> None:
    raise WriterError("manifest contains a non-finite JSON number")


def _is_object_id(value: Any, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and OBJECT_ID.fullmatch(value) is not None


def _valid_policy_glob(pattern: str) -> bool:
    if not pattern or pattern.startswith("/") or pattern.endswith("/") or "\\" in pattern:
        return False
    parts = pattern.split("/")
    return not any(
        part in {"", ".", ".."} or ("**" in part and part != "**")
        for part in parts
    )


def _valid_explicit_path(path: Any) -> bool:
    return (
        isinstance(path, str)
        and bool(path)
        and not path.startswith("/")
        and not path.endswith("/")
        and "\\" not in path
        and "\x00" not in path
        and not any(part in {"", ".", ".."} for part in path.split("/"))
        and not any("*" in part or "?" in part for part in path.split("/"))
    )


def validate_manifest_nested(value: dict[str, Any], oid_length: int) -> None:
    commits = value["commits"]
    if not isinstance(commits, list):
        raise WriterError("manifest commits must be an array")
    seen_commits: set[str] = set()
    for item in commits:
        if not isinstance(item, dict) or set(item) != {"sha", "tree", "parents"}:
            raise WriterError("manifest contains a malformed commit record")
        if not _is_object_id(item["sha"], oid_length) or not _is_object_id(item["tree"], oid_length):
            raise WriterError("manifest commit records must contain full lowercase object IDs")
        if item["sha"] in seen_commits:
            raise WriterError("manifest contains a duplicate commit record")
        seen_commits.add(item["sha"])
        parents = item["parents"]
        if not isinstance(parents, list) or any(not _is_object_id(parent, oid_length) for parent in parents):
            raise WriterError("manifest commit parents must be full lowercase object IDs")
        if len(set(parents)) != len(parents):
            raise WriterError("manifest commit record contains duplicate parents")

    policy = value["exclusion_policy"]
    policy_fields = {
        "source_path",
        "source_commit",
        "source_blob",
        "source_sha256",
        "policy_version",
        "rules",
    }
    if not isinstance(policy, dict) or set(policy) != policy_fields:
        raise WriterError("manifest exclusion policy has unexpected or missing fields")
    if policy["source_path"] != POLICY_PATH:
        raise WriterError("manifest exclusion policy source path is invalid")
    policy_version = policy["policy_version"]
    if type(policy_version) is not int or policy_version not in {1, 2, 3}:
        raise WriterError("manifest exclusion policy version is unsupported")
    if not _is_object_id(policy["source_commit"], oid_length) or not _is_object_id(policy["source_blob"], oid_length):
        raise WriterError("manifest exclusion policy source IDs are malformed")
    if policy["source_commit"] != value["through"]:
        raise WriterError("manifest exclusion policy source commit must equal through")
    if not isinstance(policy["source_sha256"], str) or not SHA256_HEX.fullmatch(policy["source_sha256"]):
        raise WriterError("manifest exclusion policy source digest is malformed")
    rules = policy["rules"]
    if not isinstance(rules, list):
        raise WriterError("manifest exclusion policy rules must be an array")
    rules_by_id: dict[str, dict[str, Any]] = {}
    for rule in rules:
        if not isinstance(rule, dict):
            raise WriterError("manifest exclusion policy rule must be an object")
        kind = rule.get("kind")
        if not isinstance(kind, str) or kind not in {"path-glob", "path-prefix", "binary-nul-byte", "gitlink"}:
            raise WriterError("manifest exclusion policy rule has an unknown kind")
        expected = (
            {"id", "kind", "patterns", "reason"}
            if kind == "path-glob"
            else {"id", "kind", "paths", "reason"}
            if kind == "path-prefix"
            else {"id", "kind", "reason"}
        )
        if kind == "path-prefix" and policy_version >= 3 and "producer" in rule:
            expected.add("producer")
        if set(rule) != expected:
            raise WriterError("manifest exclusion policy rule has unexpected or missing fields")
        if kind == "path-prefix" and policy_version < 2:
            raise WriterError("manifest path-prefix rules require exclusion policy version 2")
        rule_id, reason = rule["id"], rule["reason"]
        if not isinstance(rule_id, str) or not RULE_ID.fullmatch(rule_id) or rule_id in rules_by_id:
            raise WriterError("manifest exclusion policy contains an invalid or duplicate rule ID")
        if not isinstance(reason, str) or not reason.strip():
            raise WriterError("manifest exclusion policy rule needs a non-empty reason")
        if kind == "path-glob":
            patterns = rule["patterns"]
            if (
                not isinstance(patterns, list)
                or not patterns
                or any(not isinstance(pattern, str) or not _valid_policy_glob(pattern) for pattern in patterns)
                or len(set(patterns)) != len(patterns)
            ):
                raise WriterError("manifest exclusion policy path-glob patterns are malformed")
        elif kind == "path-prefix":
            paths = rule["paths"]
            if (
                not isinstance(paths, list)
                or any(not _valid_explicit_path(path) for path in paths)
                or len(set(paths)) != len(paths)
            ):
                raise WriterError("manifest exclusion policy path-prefix paths are malformed")
            if policy_version >= 3:
                if paths and "producer" not in rule:
                    raise WriterError("manifest path-prefix rule with paths must describe a concrete producer process")
                if "producer" in rule and (
                    not isinstance(rule["producer"], str) or not rule["producer"].strip()
                ):
                    raise WriterError("manifest path-prefix producer must be a non-empty process description")
        rules_by_id[rule_id] = rule

    excluded_paths = value["excluded_paths"]
    excluded_fields = {
        "commit",
        "parent",
        "path",
        "status",
        "old_mode",
        "new_mode",
        "old_oid",
        "new_oid",
        "rule_id",
        "match",
        "reason",
    }
    valid_modes = {"000000", "100644", "100755", "120000", "160000"}
    valid_statuses = {"A", "M", "D", "T", "U", "X", "B"}
    if not isinstance(excluded_paths, list):
        raise WriterError("manifest excluded_paths must be an array")
    for record in excluded_paths:
        if not isinstance(record, dict) or set(record) != excluded_fields:
            raise WriterError("manifest contains a malformed excluded-path record")
        if not _is_object_id(record["commit"], oid_length):
            raise WriterError("excluded-path commit must be a full lowercase object ID")
        parent = record["parent"]
        if parent is not None and not _is_object_id(parent, oid_length):
            raise WriterError("excluded-path parent must be null or a full lowercase object ID")
        path = record["path"]
        if (
            not isinstance(path, str)
            or not path
            or "\x00" in path
            or path.startswith("/")
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise WriterError("excluded-path path is not a canonical repository-relative path")
        if not isinstance(record["status"], str) or record["status"] not in valid_statuses:
            raise WriterError("excluded-path status is invalid")
        if (
            not isinstance(record["old_mode"], str)
            or record["old_mode"] not in valid_modes
            or not isinstance(record["new_mode"], str)
            or record["new_mode"] not in valid_modes
        ):
            raise WriterError("excluded-path file mode is invalid")
        for field in ("old_oid", "new_oid"):
            oid = record[field]
            if oid is not None and not _is_object_id(oid, oid_length):
                raise WriterError(f"excluded-path {field} must be null or a full lowercase object ID")
        rule_id = record["rule_id"]
        rule = rules_by_id.get(rule_id) if isinstance(rule_id, str) else None
        if rule is None or not isinstance(record["reason"], str) or record["reason"] != rule["reason"]:
            raise WriterError("excluded-path record does not match an exclusion policy rule")
        match = record["match"]
        if not isinstance(match, dict) or len(match) != 1:
            raise WriterError("excluded-path match must contain exactly one classification")
        if set(match) == {"pattern"}:
            if rule["kind"] != "path-glob" or match["pattern"] not in rule["patterns"]:
                raise WriterError("excluded-path pattern does not match its policy rule")
        elif set(match) == {"path_prefix"}:
            prefix = match["path_prefix"]
            if (
                rule["kind"] != "path-prefix"
                or prefix not in rule["paths"]
                or not (path == prefix or path.startswith(prefix + "/"))
            ):
                raise WriterError("excluded-path prefix does not match its policy rule")
        elif set(match) == {"classification"}:
            classification = match["classification"]
            if (
                not isinstance(classification, str)
                or classification not in {"gitlink", "binary-nul-byte"}
                or rule["kind"] != classification
            ):
                raise WriterError("excluded-path classification does not match its policy rule")
        else:
            raise WriterError("excluded-path match has an unknown field")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise WriterError(f"cannot read frozen manifest: {exc}") from exc
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WriterError(f"frozen manifest is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WriterError("frozen manifest root must be a JSON object")
    encoded = canonical_json(value)
    if raw != encoded + b"\n":
        raise WriterError("manifest must be the canonical JSON emitted by the range freezer, including one trailing newline")

    required = {
        "manifest_version",
        "from",
        "through",
        "commits",
        "exclusion_policy",
        "excluded_paths",
        "range_key",
    }
    if set(value) != required:
        raise WriterError("frozen manifest fields do not match manifest version 1")
    if type(value["manifest_version"]) is not int or value["manifest_version"] != 1:
        raise WriterError("unsupported frozen manifest version")
    from_sha, through = value["from"], value["through"]
    if not isinstance(from_sha, str) or not isinstance(through, str):
        raise WriterError("manifest boundaries must be full hexadecimal object IDs")
    if len(from_sha) not in (40, 64) or len(through) != len(from_sha):
        raise WriterError("manifest boundaries must use one supported full object-ID length")
    if not OBJECT_ID.fullmatch(from_sha) or not OBJECT_ID.fullmatch(through):
        raise WriterError("manifest boundaries must be lowercase full hexadecimal object IDs")
    validate_manifest_nested(value, len(from_sha))
    key = value["range_key"]
    if not isinstance(key, str) or not RANGE_KEY.fullmatch(key):
        raise WriterError("manifest range_key is malformed")
    payload = {field: contents for field, contents in value.items() if field != "range_key"}
    expected = "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()
    if key != expected:
        raise WriterError("manifest range_key does not match its canonical contents")
    return value


def validate_relative_archive(value: str) -> tuple[str, ...]:
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        raise WriterError("archive path must be a repository-relative docs/reviews Markdown path")
    parts = tuple(value.split("/"))
    if len(parts) < 3 or parts[:2] != ("docs", "reviews") or not parts[-1].endswith(".md"):
        raise WriterError("archive path must be under docs/reviews/ and end in .md")
    if any(part in ("", ".", "..") for part in parts):
        raise WriterError("archive path must not contain empty, dot, or parent components")
    return parts


def open_absolute_directory(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute():
        raise WriterError("directory path is not absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open("/", flags)
    try:
        for part in absolute.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError:
        os.close(fd)
        raise


def open_child_directory(parent_fd: int, name: str, create: bool) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
        # If a concurrent actor installed a symlink or non-directory, O_NOFOLLOW
        # rejects it. Created empty directories are intentionally not rolled back.
        return os.open(name, flags, dir_fd=parent_fd)


def open_directory_chain(root_fd: int, parts: tuple[str, ...], create: bool) -> int:
    fd = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = open_child_directory(fd, part, create)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError:
        os.close(fd)
        raise


def git_common_dir(repo: Path) -> Path:
    env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
        env.pop(name, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise WriterError(f"cannot locate Git common directory for the writer lock: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise WriterError(f"not a usable Git working tree: {detail or 'git rev-parse failed'}")
    try:
        text = result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise WriterError("Git common directory path is not valid UTF-8") from exc
    if not text or not Path(text).is_absolute():
        raise WriterError("Git did not return an absolute common-directory path")
    return Path(text)


def acquire_writer_lock(common_dir: Path) -> tuple[int, int]:
    directory_fd = open_absolute_directory(common_dir)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    lock_fd: int | None = None
    try:
        lock_fd = os.open(LOCK_NAME, flags, 0o600, dir_fd=directory_fd)
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode):
            raise WriterError("writer lock must be a regular, non-symlink file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return directory_fd, lock_fd
    except (OSError, WriterError):
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(directory_fd)
        raise


def file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_file_at(directory_fd: int, name: str, description: str) -> tuple[bytes | None, tuple[int, ...] | None, int | None]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None, None, None
    except OSError as exc:
        raise WriterError(f"cannot open {description} without following links: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise WriterError(f"{description} must be a regular, non-symlink file")
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
        if file_identity(before) != file_identity(after):
            raise WriterError(f"{description} changed while it was being read")
        return b"".join(chunks), file_identity(after), stat.S_IMODE(after.st_mode)
    finally:
        os.close(fd)


def write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise WriterError("short write while staging a file")
        view = view[written:]


def _cleanup_owned_temp(directory_fd: int, name: str, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        return
    if (info.st_dev, info.st_ino) == identity:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except OSError:
            pass


def verify_staged_temp_at(directory_fd: int, name: str, identity: tuple[int, int]) -> None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise WriterError(f"staged writer path changed before publication: {exc}") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (info.st_dev, info.st_ino) != identity
    ):
        raise WriterError("staged writer path no longer names this transaction's private inode")


def atomic_write_at(
    directory_fd: int,
    name: str,
    content: bytes,
    description: str,
    expected_snapshot: tuple[bytes | None, tuple[int, ...] | None] | None = None,
) -> None:
    old_bytes, old_identity, old_mode = read_file_at(directory_fd, name, description)
    if expected_snapshot is not None and (old_bytes, old_identity) != expected_snapshot:
        raise WriterError(f"{description} changed since it was read; refusing to overwrite newer content")
    temp_name = f".{name}.frozen-review-writer-{secrets.token_hex(12)}.tmp"
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    temp_identity: tuple[int, int] | None = None
    fd: int | None = None
    published = False
    try:
        fd = os.open(temp_name, flags, old_mode if old_mode is not None else 0o644, dir_fd=directory_fd)
        created = os.fstat(fd)
        if not stat.S_ISREG(created.st_mode) or created.st_nlink != 1:
            raise WriterError("staged writer file is not a private regular inode")
        temp_identity = (created.st_dev, created.st_ino)
        write_all(fd, content)
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        staged = bytearray()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            staged.extend(chunk)
        if bytes(staged) != content:
            raise WriterError(f"staged {description} failed read-back verification")
        os.close(fd)
        fd = None

        current_bytes, current_identity, _ = read_file_at(directory_fd, name, description)
        if current_bytes != old_bytes or current_identity != old_identity:
            raise WriterError(f"{description} changed during the writer transaction; refusing to replace it")
        if temp_identity is None:
            raise WriterError("staged writer inode identity was not recorded")
        verify_staged_temp_at(directory_fd, temp_name, temp_identity)
        os.replace(temp_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        published = True
        os.fsync(directory_fd)
        actual, actual_identity, _ = read_file_at(directory_fd, name, description)
        if actual_identity is None or actual_identity[:2] != temp_identity:
            raise WriterError(f"published {description} does not have the staged transaction inode")
        if actual != content:
            raise WriterError(f"{description} failed post-write verification")
    except OSError as exc:
        raise WriterError(f"cannot safely write {description}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if not published:
            _cleanup_owned_temp(directory_fd, temp_name, temp_identity)


def marker_value(lessons: bytes) -> tuple[str, int, int]:
    try:
        value, start, end = parse_watermark(lessons)
    except WatermarkError as exc:
        raise WriterError(str(exc)) from exc
    if value == "UNKNOWN":
        raise WriterError(
            "reviewed-through is UNKNOWN; ask a maintainer to choose the first target-branch "
            "commit to include and confirm its target-branch parent as the exclusive boundary; "
            "do not infer or initialize that starting boundary here. If there is no target-branch "
            "parent, stop for an explicit baseline design decision"
        )
    if len(value) not in (40, 64) or not OBJECT_ID.fullmatch(value):
        raise WriterError("reviewed-through must be a full lowercase hexadecimal object ID")
    return value, start, end


def lesson_section_markers(lines: list[bytes], name: str) -> tuple[int, int | None]:
    legacy_marker = f"<!-- {name} -->".encode("ascii")
    start_marker = f"<!-- {name}:START -->".encode("ascii")
    end_marker = f"<!-- {name}:END -->".encode("ascii")
    legacy = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == legacy_marker]
    starts = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == start_marker]
    ends = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == end_marker]
    if legacy:
        if len(legacy) != 1 or starts or ends:
            raise WriterError(f"LESSONS.md has duplicate or mixed {name} marker formats")
        return legacy[0], None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise WriterError(f"LESSONS.md must contain one ordered {name}:START and {name}:END pair")
    return starts[0], ends[0]


def replace_or_append_block(document: bytes, key: str, body: bytes, start_re: re.Pattern[bytes], end_re: re.Pattern[bytes], start_line: bytes, end_line: bytes, insertion_point: int | None = None) -> bytes:
    if b"\x00" in document or b"\x00" in body:
        raise WriterError("review documents must be UTF-8 text without NUL bytes")
    try:
        document.decode("utf-8", errors="strict")
        body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WriterError("review documents must be valid UTF-8") from exc
    if start_re.search(body) or end_re.search(body):
        raise WriterError("input content must not contain reserved frozen-range markers")

    lines = document.splitlines(keepends=True)
    starts: list[int] = []
    ends: list[int] = []
    open_key: str | None = None
    for index, line in enumerate(lines):
        plain = line.rstrip(b"\r\n")
        start_match, end_match = start_re.fullmatch(plain), end_re.fullmatch(plain)
        if start_match:
            if open_key is not None:
                raise WriterError("nested or unbalanced frozen-range markers are not supported")
            open_key = start_match.group(1).decode("ascii")
            if open_key == key:
                starts.append(index)
        elif end_match:
            closing = end_match.group(1).decode("ascii")
            if open_key != closing:
                raise WriterError("unbalanced frozen-range marker pair")
            if closing == key:
                ends.append(index)
            open_key = None
    if open_key is not None or len(starts) != len(ends):
        raise WriterError("unclosed frozen-range marker")
    if len(starts) > 1:
        raise WriterError("duplicate frozen-range block for this range_key")

    block = start_line + b"\n" + body.rstrip(b"\n") + b"\n" + end_line + b"\n"
    if starts:
        begin, finish = starts[0], ends[0]
        lines[begin : finish + 1] = [block]
        return b"".join(lines)
    if insertion_point is None:
        separator = b"" if not document or document.endswith(b"\n\n") else (b"\n" if document.endswith(b"\n") else b"\n\n")
        return document + separator + block
    if insertion_point < 0 or insertion_point > len(lines):
        raise WriterError("invalid Lessons insertion point")
    lines[insertion_point:insertion_point] = [block]
    return b"".join(lines)


def _replace_lesson_section_content(document: bytes, name: str, content: bytes) -> bytes:
    lines = document.splitlines(keepends=True)
    start, end = lesson_section_markers(lines, name)
    if end is None:
        end = lesson_section_markers(lines, "PROMOTED_LESSONS")[0] if name == "ACTIVE_LESSONS" else len(lines)
    lines[start + 1 : end] = content.splitlines(keepends=True)
    return b"".join(lines)


def range_block_spans(
    section: bytes,
    start_re: re.Pattern[bytes],
    end_re: re.Pattern[bytes],
) -> tuple[list[bytes], list[tuple[int, int, str]]]:
    lines = section.splitlines(keepends=True)
    blocks: list[tuple[int, int, str]] = []
    opened: tuple[int, str] | None = None
    for index, line in enumerate(lines):
        plain = line.rstrip(b"\r\n")
        start_match = start_re.fullmatch(plain)
        end_match = end_re.fullmatch(plain)
        if start_match:
            if opened is not None:
                raise WriterError("nested or unbalanced frozen-range markers are not supported")
            opened = (index, start_match.group(1).decode("ascii"))
        elif end_match:
            key = end_match.group(1).decode("ascii")
            if opened is None or opened[1] != key:
                raise WriterError("unbalanced frozen-range marker pair")
            blocks.append((opened[0], index, key))
            opened = None
    if opened is not None:
        raise WriterError("unclosed frozen-range marker")
    return lines, blocks


def lesson_ids_in_content(content: bytes, label: str, required: bool = False) -> list[str]:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WriterError(f"{label} content must be valid UTF-8") from exc
    ids: list[str] = []
    for line in text.splitlines():
        if not line.startswith("### L-"):
            continue
        heading = PROMOTION_HEADING.fullmatch(line)
        if heading is None:
            raise WriterError(f"{label} Lesson headings must use '### L-0001 — <rule>'")
        ids.append(heading.group(1))
    if required and not ids:
        raise WriterError(f"{label} content must contain at least one stable Lesson ID")
    if len(ids) != len(set(ids)):
        raise WriterError(f"{label} content contains a duplicate Lesson ID")
    return ids


def standard_ids_in_content(content: bytes, label: str, required: bool = False) -> list[str]:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WriterError(f"{label} content must be valid UTF-8") from exc
    ids: list[str] = []
    for line in text.splitlines():
        if not line.startswith("##### PS-"):
            continue
        heading = STANDARD_HEADING.fullmatch(line)
        if heading is None:
            raise WriterError(f"{label} headings must use '##### PS-0001 — <rule>'")
        ids.append(heading.group(1))
    if required and not ids:
        raise WriterError(f"{label} content must contain at least one stable PS rule ID")
    if len(ids) != len(set(ids)):
        raise WriterError(f"{label} content contains a duplicate PS rule ID")
    return ids


def _lesson_id_from_line(line: bytes) -> str | None:
    try:
        text = line.decode("utf-8", errors="strict").rstrip("\r\n")
    except UnicodeDecodeError as exc:
        raise WriterError("Lessons content must be valid UTF-8") from exc
    match = LESSON_ID_HEADING.match(text)
    return match.group(1) if match else None


def remove_promoted_from_active(section: bytes, promoted_ids: set[str]) -> tuple[bytes, dict[str, int]]:
    lines, blocks = range_block_spans(section, LESSON_START, LESSON_END)
    managed_body_lines = {
        index
        for start, end, _key in blocks
        for index in range(start + 1, end)
    }
    counts = {lesson_id: 0 for lesson_id in promoted_ids}
    for index, line in enumerate(lines):
        lesson_id = _lesson_id_from_line(line)
        if lesson_id not in promoted_ids:
            continue
        if index not in managed_body_lines:
            raise WriterError(f"promoted Lesson {lesson_id} is outside a writer-managed active range block")
        counts[lesson_id] += 1
    duplicates = [lesson_id for lesson_id, count in counts.items() if count > 1]
    if duplicates:
        raise WriterError("active Lessons contains duplicate IDs to promote: " + ", ".join(sorted(duplicates)))

    for start, end, _key in reversed(blocks):
        body_lines = lines[start + 1 : end]
        headings = [index for index, line in enumerate(body_lines) if line.startswith(b"### ")]
        if not headings:
            continue
        kept = body_lines[: headings[0]]
        for position, record_start in enumerate(headings):
            record_end = headings[position + 1] if position + 1 < len(headings) else len(body_lines)
            if _lesson_id_from_line(body_lines[record_start]) in promoted_ids:
                continue
            kept.extend(body_lines[record_start:record_end])
        lines[start + 1 : end] = kept
    return b"".join(lines), counts


def _lesson_records(section: bytes, start_re: re.Pattern[bytes], end_re: re.Pattern[bytes]) -> list[tuple[str, int, int, bool]]:
    lines, blocks = range_block_spans(section, start_re, end_re)
    managed = {i for start, end, _key in blocks for i in range(start + 1, end)}
    headings: list[int] = []
    ids: list[str] = []
    for i, line in enumerate(lines):
        text = line.decode("utf-8", errors="strict").rstrip("\r\n")
        # Legacy records without a stable ID remain compatible, but any line
        # that claims to be an L-record must use the canonical heading shape.
        if re.match(r"^[ \t]*#+[ \t]*L-", text):
            match = PROMOTION_HEADING.fullmatch(text)
            if match is None:
                raise WriterError("Lessons contains a malformed Lesson heading")
            headings.append(i)
            ids.append(match.group(1))
    marker_lines = [i for i, line in enumerate(lines) if end_re.fullmatch(line.rstrip(b"\r\n"))]
    records = []
    for n, (lesson_id, start) in enumerate(zip(ids, headings)):
        candidates = [headings[n + 1] if n + 1 < len(headings) else len(lines)]
        candidates.extend(i for i in marker_lines if i > start)
        records.append((lesson_id, start, min(candidates), start in managed))
    return records

def validate_global_lesson_uniqueness(active_section: bytes, promoted_section: bytes) -> None:
    """Reject duplicate IDs across the complete bounded authoritative sections."""
    locations: dict[str, str] = {}
    for section_name, section, start_re, end_re in (
        ("active", active_section, LESSON_START, LESSON_END),
        ("promoted", promoted_section, PROMOTED_LESSON_START, PROMOTED_LESSON_END),
    ):
        for lesson_id, _start, _end, _managed in _lesson_records(section, start_re, end_re):
            previous = locations.get(lesson_id)
            if previous is not None:
                raise WriterError(f"duplicate Lesson ID {lesson_id} across {previous} and {section_name} Lessons")
            locations[lesson_id] = f"{section_name} Lessons"

def _evidence_row_indexes(record: bytes, label: str) -> list[int]:
    indexes: list[int] = []
    in_evidence = False
    headers = 0
    for index, line in enumerate(record.splitlines(keepends=True)):
        plain = line.rstrip(b"\r\n")
        if plain.strip() == b"- **Review evidence:**":
            headers += 1
            if headers > 1:
                raise WriterError(f"{label} contains duplicate Review evidence headers")
            in_evidence = True
            continue
        if in_evidence and re.match(rb"^- \*\*[^*]+:\*\*", plain):
            in_evidence = False
        if in_evidence and plain.strip():
            # Match the persisted validator's date + citation-text language.
            match = re.fullmatch(r"\s{2,}- `([0-9]{4}-[0-9]{2}-[0-9]{2})` — (.+?)\s*", plain.decode("utf-8"))
            if match is None:
                raise WriterError(f"{label} contains malformed Review evidence row")
            try:
                date.fromisoformat(match.group(1))
            except ValueError as exc:
                raise WriterError(f"{label} contains an invalid Review evidence date") from exc
            links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", match.group(2))
            if not links or any(not link.strip() or "<" in link or ">" in link for link in links):
                raise WriterError(f"{label} evidence row requires a non-placeholder Markdown link")
            indexes.append(index)
    if headers != 1:
        raise WriterError(f"{label} must contain exactly one Review evidence section")
    if not indexes:
        raise WriterError(f"{label} must contain at least one Review evidence row")
    return indexes


def _evidence_rows(record: bytes, label: str) -> list[bytes]:
    lines = record.splitlines(keepends=True)
    return [lines[index] for index in _evidence_row_indexes(record, label)]

def _body_without_evidence(record: bytes, label: str) -> bytes:
    indexes = set(_evidence_row_indexes(record, label))
    kept = [line for index, line in enumerate(record.splitlines(keepends=True)) if index not in indexes]
    while kept and not kept[-1].strip():
        kept.pop()
    return b"".join(kept).rstrip(b"\r\n")

def merge_existing_lesson_evidence(section: bytes, body: bytes, key: str) -> bytes:
    """Merge only exact-body records; reject unmanaged/user-edited collisions."""
    existing = {lesson_id: (start, end, managed) for lesson_id, start, end, managed in _lesson_records(section, LESSON_START, LESSON_END)}
    new_lines = body.splitlines(keepends=True)
    new_starts = [i for i, line in enumerate(new_lines) if line.startswith(b"### ")]
    if not new_starts:
        return body
    replacements: dict[str, bytes] = {}
    for n, start in enumerate(new_starts):
        end = new_starts[n + 1] if n + 1 < len(new_starts) else len(new_lines)
        record = b"".join(new_lines[start:end])
        match = PROMOTION_HEADING.fullmatch(new_lines[start].decode("utf-8").rstrip("\r\n"))
        if match is None:
            continue
        lesson_id = match.group(1)
        if lesson_id not in existing:
            continue
        old_start, old_end, managed = existing[lesson_id]
        if not managed:
            raise WriterError(f"Lesson {lesson_id} exists outside a writer-managed range block")
        old = b"".join(section.splitlines(keepends=True)[old_start:old_end])
        if _body_without_evidence(old, "existing Lesson") != _body_without_evidence(record, "active Lessons"):
            raise WriterError(f"Lesson {lesson_id} body conflicts with existing record")
        old_rows = _evidence_rows(old, "existing Lesson")
        new_rows = _evidence_rows(record, "active Lessons")
        # Stable union over the complete old-then-new sequence.  This both
        # repairs duplicates already persisted in the old record and retains
        # every distinct citation, including same-date citations.
        union: list[bytes] = []
        seen: set[bytes] = set()
        for row in old_rows + new_rows:
            if row not in seen:
                seen.add(row)
                union.append(row)
        old_lines = old.splitlines(keepends=True)
        old_indexes = set(_evidence_row_indexes(old, "existing Lesson"))
        old_lines = [line for index, line in enumerate(old_lines) if index not in old_indexes]
        header = next(i for i, line in enumerate(old_lines) if line.strip() == b"- **Review evidence:**")
        old_lines[header + 1:header + 1] = union
        replacements[lesson_id] = b"".join(old_lines)
    if not replacements:
        return body
    out = list(new_lines)
    for n, start in reversed(list(enumerate(new_starts))):
        end = new_starts[n + 1] if n + 1 < len(new_starts) else len(out)
        heading = out[start].decode("utf-8").rstrip("\r\n")
        match = PROMOTION_HEADING.fullmatch(heading)
        if match and match.group(1) in replacements:
            out[start:end] = [replacements[match.group(1)]]
    return b"".join(out)


def remove_existing_managed_lessons(section: bytes, ids: set[str]) -> bytes:
    lines, _blocks = range_block_spans(section, LESSON_START, LESSON_END)
    records = _lesson_records(section, LESSON_START, LESSON_END)
    for lesson_id, start, end, managed in sorted(records, key=lambda item: item[1], reverse=True):
        if lesson_id not in ids:
            continue
        if not managed:
            raise WriterError(f"Lesson {lesson_id} exists outside a writer-managed range block")
        del lines[start:end]
    return b"".join(lines)

def update_promoted_standards(document: bytes, key: str, body: bytes) -> bytes:
    lines = document.splitlines(keepends=True)
    start_marker = b"<!-- PROMOTED_RULES:START -->"
    end_marker = b"<!-- PROMOTED_RULES:END -->"
    starts = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == start_marker]
    ends = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == end_marker]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise WriterError("docs/code-standards.md must contain one ordered PROMOTED_RULES marker pair")
    section = b"".join(lines[starts[0] + 1 : ends[0]])
    _section_lines, existing_blocks = range_block_spans(section, PROMOTED_STANDARD_START, PROMOTED_STANDARD_END)
    new_ids = set(standard_ids_in_content(body, "promoted standards", required=True))
    same_key_ids: set[str] = set()
    for block_start, block_end, existing_key in existing_blocks:
        block_body = b"".join(_section_lines[block_start + 1 : block_end])
        existing_ids = set(standard_ids_in_content(block_body, "existing promoted standards"))
        if existing_key == key:
            same_key_ids.update(existing_ids)
        elif new_ids & existing_ids:
            raise WriterError("promoted standards rule IDs already belong to a different range_key")
    if not same_key_ids.issubset(new_ids):
        raise WriterError("a retry cannot remove a previously written promoted standards rule")
    start = f"<!-- frozen-review-standard: {key} -->".encode("ascii")
    end = f"<!-- /frozen-review-standard: {key} -->".encode("ascii")
    updated_section = replace_or_append_block(
        section,
        key,
        body,
        PROMOTED_STANDARD_START,
        PROMOTED_STANDARD_END,
        start,
        end,
    )
    lines[starts[0] + 1 : ends[0]] = updated_section.splitlines(keepends=True)
    return b"".join(lines)


def update_archive(document: bytes | None, manifest: dict[str, Any], body: bytes) -> bytes:
    current = document or b""
    key = manifest["range_key"]
    start = f"<!-- frozen-review-range: {key} -->".encode("ascii")
    end = f"<!-- /frozen-review-range: {key} -->".encode("ascii")
    metadata = f"<!-- boundaries: from={manifest['from']} through={manifest['through']} -->\n".encode("ascii")
    complete_body = metadata + body.rstrip(b"\n")
    return replace_or_append_block(current, key, complete_body, ARCHIVE_START, ARCHIVE_END, start, end)


def update_lessons(
    document: bytes,
    manifest: dict[str, Any],
    body: bytes,
    promoted_body: bytes | None = None,
) -> bytes:
    marker, _, _ = marker_value(document)
    key = manifest["range_key"]
    start = f"<!-- frozen-review-lessons: {key} -->".encode("ascii")
    end = f"<!-- /frozen-review-lessons: {key} -->".encode("ascii")
    if not body.strip():
        body = f"<!-- no lesson entries for range {key}; a retry may fill this block -->".encode("ascii")

    lines = document.splitlines(keepends=True)
    active, active_end = lesson_section_markers(lines, "ACTIVE_LESSONS")
    promoted, promoted_end = lesson_section_markers(lines, "PROMOTED_LESSONS")
    if (active_end is None) != (promoted_end is None):
        raise WriterError("LESSONS.md must not mix legacy and paired section markers")
    if active >= promoted or (active_end is not None and active_end >= promoted):
        raise WriterError("LESSONS.md must keep ACTIVE_LESSONS before PROMOTED_LESSONS")
    active_section_end = active_end if active_end is not None else promoted
    promoted_section_end = promoted_end if promoted_end is not None else len(lines)
    if active_section_end < active + 1 or promoted_section_end < promoted + 1:
        raise WriterError("LESSONS.md has an invalid ACTIVE_LESSONS section")
    active_section = b"".join(lines[active + 1 : active_section_end])
    promoted_section = b"".join(lines[promoted + 1 : promoted_section_end])
    active_input_ids = set(lesson_ids_in_content(body, "active Lessons"))
    validate_global_lesson_uniqueness(active_section, promoted_section)
    body = merge_existing_lesson_evidence(active_section, body, key)
    active_input_ids = set(lesson_ids_in_content(body, "active Lessons"))
    active_section = remove_existing_managed_lessons(active_section, active_input_ids)
    existing_promoted_ids = set(lesson_ids_in_content(promoted_section, "existing promoted Lessons"))
    reintroduced_ids = active_input_ids & existing_promoted_ids
    if reintroduced_ids:
        raise WriterError(
            "active Lessons content would reintroduce already promoted Lesson IDs: "
            + ", ".join(sorted(reintroduced_ids))
        )

    if promoted_body is not None:
        promoted_ids = set(lesson_ids_in_content(promoted_body, "promoted Lessons", required=True))
        if promoted_ids & active_input_ids:
            raise WriterError("a promoted Lesson must not also be supplied as active content")

        promo_lines, promo_blocks = range_block_spans(
            promoted_section,
            PROMOTED_LESSON_START,
            PROMOTED_LESSON_END,
        )
        current_promotion_ids: set[str] = set()
        seen_promoted_ids: dict[str, str] = {}
        for block_start, block_end, existing_key in promo_blocks:
            block_body = b"".join(promo_lines[block_start + 1 : block_end])
            existing_ids = lesson_ids_in_content(block_body, "existing promoted Lessons")
            for lesson_id in existing_ids:
                if lesson_id in seen_promoted_ids:
                    raise WriterError(f"promoted Lesson {lesson_id} already appears in multiple range blocks")
                seen_promoted_ids[lesson_id] = existing_key
                if existing_key == key:
                    current_promotion_ids.add(lesson_id)
                elif lesson_id in promoted_ids:
                    raise WriterError(f"promoted Lesson {lesson_id} already belongs to a different range_key")
        if not current_promotion_ids.issubset(promoted_ids):
            raise WriterError("a retry cannot remove a previously written promoted Lesson")

        active_section, active_counts = remove_promoted_from_active(active_section, promoted_ids)
        missing = [
            lesson_id
            for lesson_id in promoted_ids
            if active_counts[lesson_id] == 0 and lesson_id not in current_promotion_ids
        ]
        if missing:
            raise WriterError(
                "promoted Lesson must first exist in active history (or this range's retry block): "
                + ", ".join(sorted(missing))
            )
        promoted_start = f"<!-- frozen-review-promotion: {key} -->".encode("ascii")
        promoted_end = f"<!-- /frozen-review-promotion: {key} -->".encode("ascii")
        promoted_section = replace_or_append_block(
            promoted_section,
            key,
            promoted_body,
            PROMOTED_LESSON_START,
            PROMOTED_LESSON_END,
            promoted_start,
            promoted_end,
        )
        document = _replace_lesson_section_content(document, "PROMOTED_LESSONS", promoted_section)

    active_start = f"<!-- frozen-review-lessons: {key} -->".encode("ascii")
    active_end_line = f"<!-- /frozen-review-lessons: {key} -->".encode("ascii")
    active_section = replace_or_append_block(
        active_section,
        key,
        body,
        LESSON_START,
        LESSON_END,
        active_start,
        active_end_line,
    )
    validate_global_lesson_uniqueness(active_section, promoted_section)
    document = _replace_lesson_section_content(document, "ACTIVE_LESSONS", active_section)
    current_marker, _, _ = marker_value(document)
    if current_marker != marker:
        raise WriterError("Lessons updater unexpectedly changed reviewed-through")
    return document


def validate_lessons_document_uniqueness(document: bytes) -> None:
    lines = document.splitlines(keepends=True)
    active, active_end = lesson_section_markers(lines, "ACTIVE_LESSONS")
    promoted, promoted_end = lesson_section_markers(lines, "PROMOTED_LESSONS")
    active_end = active_end if active_end is not None else promoted
    promoted_end = promoted_end if promoted_end is not None else len(lines)
    validate_global_lesson_uniqueness(
        b"".join(lines[active + 1:active_end]),
        b"".join(lines[promoted + 1:promoted_end]),
    )


def set_watermark(document: bytes, target: str) -> bytes:
    _, start, end = marker_value(document)
    return document[:start] + target.encode("ascii") + document[end:]


def verify_block(document: bytes, key: str, start_re: re.Pattern[bytes], end_re: re.Pattern[bytes]) -> None:
    starts = [m for m in start_re.finditer(document) if m.group(1).decode("ascii") == key]
    ends = [m for m in end_re.finditer(document) if m.group(1).decode("ascii") == key]
    if len(starts) != 1 or len(ends) != 1 or starts[0].start() >= ends[0].start():
        raise WriterError("written range block could not be verified")


def write_review(
    repo_path: Path,
    manifest_path: Path,
    archive_rel: str,
    archive_content_path: Path,
    lessons_content_path: Path,
    promoted_lessons_content_path: Path | None = None,
    promoted_standards_content_path: Path | None = None,
) -> dict[str, str]:
    manifest = load_manifest(manifest_path)
    archive_parts = validate_relative_archive(archive_rel)
    if (promoted_lessons_content_path is None) != (promoted_standards_content_path is None):
        raise WriterError("promoted Lessons and promoted standards inputs must be supplied together")
    try:
        archive_body = archive_content_path.read_bytes()
        lesson_body = lessons_content_path.read_bytes()
        promoted_lesson_body = (
            promoted_lessons_content_path.read_bytes()
            if promoted_lessons_content_path is not None
            else None
        )
        promoted_standards_body = (
            promoted_standards_content_path.read_bytes()
            if promoted_standards_content_path is not None
            else None
        )
    except OSError as exc:
        raise WriterError(f"cannot read review content input: {exc}") from exc
    if not archive_body.strip():
        raise WriterError("archive review content must not be empty")
    content_inputs = [("archive", archive_body), ("Lessons", lesson_body)]
    if promoted_lesson_body is not None and promoted_standards_body is not None:
        if not promoted_lesson_body.strip() or not promoted_standards_body.strip():
            raise WriterError("promotion inputs must both contain at least one record")
        lesson_ids_in_content(promoted_lesson_body, "promoted Lessons", required=True)
        standard_ids_in_content(promoted_standards_body, "promoted standards", required=True)
        content_inputs.extend(
            [
                ("promoted Lessons", promoted_lesson_body),
                ("promoted standards", promoted_standards_body),
            ]
        )
    for label, body in content_inputs:
        if b"\x00" in body:
            raise WriterError(f"{label} content must not contain NUL bytes")
        try:
            body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise WriterError(f"{label} content must be valid UTF-8") from exc

    root_fd = open_absolute_directory(repo_path)
    lock_dir_fd: int | None = None
    lock_fd: int | None = None
    archive_parent_fd: int | None = None
    standards_parent_fd: int | None = None
    try:
        # Validate the existing watermark before creating the cooperative lock
        # or any archive directories/files. The second read below remains the
        # transaction's identity-checked snapshot.
        initial_lessons, initial_identity, _ = read_file_at(root_fd, "LESSONS.md", "LESSONS.md")
        if initial_lessons is None or initial_identity is None:
            raise WriterError("LESSONS.md is missing")
        marker_value(initial_lessons)
        common_dir = git_common_dir(repo_path)
        lock_dir_fd, lock_fd = acquire_writer_lock(common_dir)
        archive_name = archive_parts[-1]
        lessons_name = "LESSONS.md"
        lessons_bytes, lessons_identity, _ = read_file_at(root_fd, lessons_name, "LESSONS.md")
        if lessons_bytes is None or lessons_identity is None:
            raise WriterError("LESSONS.md is missing")
        old_watermark, _, _ = marker_value(lessons_bytes)
        if old_watermark not in (manifest["from"], manifest["through"]):
            raise WriterError("reviewed-through must equal this manifest's from or through; refusing a different or stale range")

        standards_bytes: bytes | None = None
        standards_identity: tuple[int, ...] | None = None
        updated_standards: bytes | None = None
        if promoted_lesson_body is not None and promoted_standards_body is not None:
            standards_parent_fd = open_directory_chain(root_fd, ("docs",), create=False)
            standards_bytes, standards_identity, _ = read_file_at(
                standards_parent_fd,
                "code-standards.md",
                "docs/code-standards.md",
            )
            if standards_bytes is None or standards_identity is None:
                raise WriterError("docs/code-standards.md is missing")
            updated_standards = update_promoted_standards(
                standards_bytes,
                manifest["range_key"],
                promoted_standards_body,
            )
        updated_lessons = update_lessons(
            lessons_bytes,
            manifest,
            lesson_body,
            promoted_lesson_body,
        )
        verify_block(updated_lessons, manifest["range_key"], LESSON_START, LESSON_END)
        if promoted_lesson_body is not None:
            verify_block(
                updated_lessons,
                manifest["range_key"],
                PROMOTED_LESSON_START,
                PROMOTED_LESSON_END,
            )
        if updated_standards is not None:
            verify_block(
                updated_standards,
                manifest["range_key"],
                PROMOTED_STANDARD_START,
                PROMOTED_STANDARD_END,
            )

        archive_parent_fd = open_directory_chain(root_fd, archive_parts[:-1], create=True)
        archive_bytes, archive_identity, _ = read_file_at(archive_parent_fd, archive_name, archive_rel)
        updated_archive = update_archive(archive_bytes, manifest, archive_body)
        verify_block(updated_archive, manifest["range_key"], ARCHIVE_START, ARCHIVE_END)
        atomic_write_at(
            archive_parent_fd,
            archive_name,
            updated_archive,
            archive_rel,
            (archive_bytes, archive_identity),
        )
        check_archive, _, _ = read_file_at(archive_parent_fd, archive_name, archive_rel)
        if check_archive != updated_archive:
            raise WriterError("archive re-read did not match the expected range-key upsert")
        verify_block(check_archive, manifest["range_key"], ARCHIVE_START, ARCHIVE_END)

        if updated_standards is not None:
            if standards_parent_fd is None or standards_bytes is None or standards_identity is None:
                raise WriterError("promotion standards snapshot was not recorded")
            atomic_write_at(
                standards_parent_fd,
                "code-standards.md",
                updated_standards,
                "docs/code-standards.md",
                (standards_bytes, standards_identity),
            )
            check_standards, _, _ = read_file_at(
                standards_parent_fd,
                "code-standards.md",
                "docs/code-standards.md",
            )
            if check_standards != updated_standards:
                raise WriterError("standards re-read did not match the expected range-key upsert")
            verify_block(
                check_standards,
                manifest["range_key"],
                PROMOTED_STANDARD_START,
                PROMOTED_STANDARD_END,
            )

        # Lessons content is written while the previous watermark is still present.
        atomic_write_at(
            root_fd,
            lessons_name,
            updated_lessons,
            "LESSONS.md",
            (lessons_bytes, lessons_identity),
        )
        check_lessons, check_lessons_identity, _ = read_file_at(root_fd, lessons_name, "LESSONS.md")
        if check_lessons != updated_lessons:
            raise WriterError("Lessons re-read did not match the expected range-key upsert")
        check_marker, _, _ = marker_value(check_lessons)
        if check_marker != old_watermark:
            raise WriterError("reviewed-through changed before archive and Lessons verification completed")
        verify_block(check_lessons, manifest["range_key"], LESSON_START, LESSON_END)
        if promoted_lesson_body is not None:
            verify_block(
                check_lessons,
                manifest["range_key"],
                PROMOTED_LESSON_START,
                PROMOTED_LESSON_END,
            )

        # Re-verify every requested record immediately before the final durable watermark write.
        final_archive, _, _ = read_file_at(archive_parent_fd, archive_name, archive_rel)
        if final_archive != updated_archive:
            raise WriterError("archive changed before watermark advancement")
        verify_block(final_archive, manifest["range_key"], ARCHIVE_START, ARCHIVE_END)
        if updated_standards is not None and standards_parent_fd is not None:
            final_standards, _, _ = read_file_at(
                standards_parent_fd,
                "code-standards.md",
                "docs/code-standards.md",
            )
            if final_standards != updated_standards:
                raise WriterError("standards changed before watermark advancement")
            verify_block(
                final_standards,
                manifest["range_key"],
                PROMOTED_STANDARD_START,
                PROMOTED_STANDARD_END,
            )

        # Re-scan the complete authoritative sections immediately before the
        # final watermark write, not only the sections touched by this update.
        validate_lessons_document_uniqueness(check_lessons)
        final_lessons = set_watermark(check_lessons, manifest["through"])
        # No file write follows this atomic replacement. On retry where the marker
        # already equals through, this is still the final durable write for the key.
        atomic_write_at(
            root_fd,
            lessons_name,
            final_lessons,
            "LESSONS.md reviewed-through final write",
            (check_lessons, check_lessons_identity),
        )
        committed_lessons, _, _ = read_file_at(root_fd, lessons_name, "LESSONS.md")
        if committed_lessons != final_lessons:
            raise WriterError("final watermark write outcome is uncertain; inspect LESSONS.md before retrying")
        committed_marker, _, _ = marker_value(committed_lessons)
        if committed_marker != manifest["through"]:
            raise WriterError("final watermark verification failed; inspect LESSONS.md before retrying")
        return {"range_key": manifest["range_key"], "from": manifest["from"], "through": manifest["through"], "archive": archive_rel}
    except OSError as exc:
        raise WriterError(f"filesystem safety check failed: {exc}") from exc
    finally:
        if standards_parent_fd is not None:
            os.close(standards_parent_fd)
        if archive_parent_fd is not None:
            os.close(archive_parent_fd)
        if lock_fd is not None:
            os.close(lock_fd)
        if lock_dir_fd is not None:
            os.close(lock_dir_fd)
        os.close(root_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Idempotently write archive and Lessons content for one verified frozen range manifest.")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repository working-tree path")
    parser.add_argument("--manifest", required=True, type=Path, help="canonical JSON emitted by freeze-review-range.py")
    parser.add_argument("--archive", required=True, help="explicit repository-relative docs/reviews/*.md destination")
    parser.add_argument("--archive-content", required=True, type=Path, help="UTF-8 review record body to upsert")
    parser.add_argument("--lessons-content", required=True, type=Path, help="UTF-8 Lessons entries; an empty file records no entries yet")
    parser.add_argument(
        "--promoted-lessons-content",
        type=Path,
        help="complete promoted Lesson records to migrate into PROMOTED_LESSONS",
    )
    parser.add_argument(
        "--promoted-standards-content",
        type=Path,
        help="promoted PS rule records to upsert into docs/code-standards.md",
    )
    args = parser.parse_args(argv)
    try:
        result = write_review(
            args.repo,
            args.manifest,
            args.archive,
            args.archive_content,
            args.lessons_content,
            args.promoted_lessons_content,
            args.promoted_standards_content,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except (WriterError, OSError, subprocess.SubprocessError) as exc:
        print(f"write-frozen-review: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
