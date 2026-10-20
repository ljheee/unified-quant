#!/usr/bin/env python3
"""Read-only structural gate for persisted Lesson promotion evidence.

This checker intentionally does not inspect git history, choose a review range,
read or write reviewed-through, or create/update review archives.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sys
from dataclasses import dataclass

LESSON_ID_RE = re.compile(r"L-[0-9]{4,}\Z")
RULE_ID_RE = re.compile(r"PS-[0-9]{4,}\Z")
LESSON_HEADING_RE = re.compile(r"^### (L-[0-9]{4,}) — (.+?)\s*$")
RULE_HEADING_RE = re.compile(r"^##### (PS-[0-9]{4,}) — (.+?)\s*$")
EVIDENCE_ROW_RE = re.compile(r"^\s{2,}- `([0-9]{4}-[0-9]{2}-[0-9]{2})` — (.+?)\s*$")
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
EVIDENCE_LESSONS_RE = re.compile(
    r"^- \*\*Evidence lessons:\*\* `(?P<ids>L-[0-9]{4,}(?:`, `L-[0-9]{4,})*)`$"
)


@dataclass(frozen=True)
class Lesson:
    lesson_id: str
    status: str | None
    promoted_rule: str | None
    review_dates: frozenset[str]


@dataclass(frozen=True)
class PromotedRule:
    rule_id: str
    evidence_lesson_ids: tuple[str, ...]


def _marked_block(text: str, name: str, errors: list[str]) -> tuple[str, int]:
    """Return a marked block and its 1-based starting line."""
    start_marker = f"<!-- {name}:START -->"
    end_marker = f"<!-- {name}:END -->"
    if text.count(start_marker) != 1 or text.count(end_marker) != 1:
        errors.append(f"expected exactly one {start_marker} and one {end_marker} marker")
        return "", 1
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker)
    if start > end:
        errors.append(f"{start_marker} appears after {end_marker}")
        return "", 1
    first_line = text.count("\n", 0, start) + 1
    return text[start:end], first_line


def _field_values(body: str, name: str) -> list[str]:
    pattern = re.compile(rf"^- \*\*{re.escape(name)}:\*\*\s*(.*?)\s*$", re.MULTILINE)
    return pattern.findall(body)


def _heading_like(line: str) -> bool:
    """Recognize hash-prefixed records even with invalid indentation/spacing."""
    return re.match(r"^[ \t]*#+", line) is not None


def _record_starts(lines: list[str], first_line: int, errors: list[str], kind: str) -> list[int]:
    starts = [index for index, line in enumerate(lines) if _heading_like(line)]
    first = starts[0] if starts else len(lines)
    for index, line in enumerate(lines[:first]):
        if line.strip() and not line.lstrip().startswith("<!--"):
            errors.append(
                f"{kind}:{first_line + index}: unrecognized content before first record"
            )
    return starts


def _valid_iso_date(value: str) -> bool:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _parse_lesson_record(
    lesson_id: str,
    body: str,
    expected_status: str,
    errors: list[str],
    location: str,
) -> Lesson:
    statuses = _field_values(body, "Status")
    status = statuses[0] if len(statuses) == 1 else None
    if len(statuses) != 1:
        errors.append(f"{location}: expected exactly one Status field")
    elif status != expected_status:
        errors.append(f"{location}: record in {expected_status} block must have Status: {expected_status}")

    promoted_values = _field_values(body, "Promoted rule")
    promoted_rule: str | None = None
    if len(promoted_values) > 1:
        errors.append(f"{location}: duplicate Promoted rule fields")
    elif promoted_values:
        candidate = promoted_values[0].strip(" `")
        if not RULE_ID_RE.fullmatch(candidate):
            errors.append(f"{location}: invalid Promoted rule ID {promoted_values[0]!r}")
        else:
            promoted_rule = candidate

    promotion_dates = _field_values(body, "Promotion date")
    if expected_status == "promoted":
        if len(promotion_dates) != 1 or not _valid_iso_date(promotion_dates[0].strip(" `")):
            errors.append(f"{location}: promoted records require one valid Promotion date")
        if promoted_rule is None:
            errors.append(f"{location}: promoted records require one valid Promoted rule ID")
    elif promoted_rule is not None or promotion_dates:
        errors.append(f"{location}: active records cannot carry promotion metadata")

    evidence_headers = [
        index for index, line in enumerate(body.splitlines())
        if line.strip() == "- **Review evidence:**"
    ]
    review_dates: set[str] = set()
    if len(evidence_headers) != 1:
        errors.append(f"{location}: expected exactly one Review evidence field; legacy Evidence/Seen fields do not count")
    else:
        lines = body.splitlines()
        in_evidence = False
        row_count = 0
        for line in lines:
            if line.strip() == "- **Review evidence:**":
                in_evidence = True
                continue
            if in_evidence and re.match(r"^- \*\*[^*]+:\*\*", line):
                in_evidence = False
            if not in_evidence or not line.strip():
                continue
            match = EVIDENCE_ROW_RE.fullmatch(line)
            if not match:
                errors.append(f"{location}: malformed Review evidence row: {line.strip()!r}")
                continue
            row_count += 1
            review_date, citation = match.groups()
            if not _valid_iso_date(review_date):
                errors.append(f"{location}: invalid review date {review_date!r}")
                continue
            links = MARKDOWN_LINK_RE.findall(citation)
            if not links or any(not link.strip() or "<" in link or ">" in link for link in links):
                errors.append(f"{location}: each dated evidence row requires a non-placeholder Markdown link")
                continue
            review_dates.add(review_date)
        if row_count == 0:
            errors.append(f"{location}: Review evidence must contain at least one dated evidence row")

    return Lesson(lesson_id, status, promoted_rule, frozenset(review_dates))


def _parse_lessons(text: str, errors: list[str]) -> dict[str, Lesson]:
    records: dict[str, Lesson] = {}
    for block_name, expected_status in (
        ("ACTIVE_LESSONS", "active"),
        ("PROMOTED_LESSONS", "promoted"),
    ):
        block, first_line = _marked_block(text, block_name, errors)
        lines = block.splitlines()
        starts = _record_starts(lines, first_line, errors, "LESSONS.md")
        for position, start_index in enumerate(starts):
            end_index = starts[position + 1] if position + 1 < len(starts) else len(lines)
            heading = lines[start_index]
            match = LESSON_HEADING_RE.fullmatch(heading)
            location = f"LESSONS.md:{first_line + start_index}"
            if not match:
                errors.append(f"{location}: lesson headings must use '### L-0001 — <rule>'; legacy headings are not evidence")
                continue
            lesson_id, _title = match.groups()
            if lesson_id in records:
                errors.append(f"{location}: duplicate Lesson ID {lesson_id}")
                continue
            body = "\n".join(lines[start_index + 1:end_index])
            records[lesson_id] = _parse_lesson_record(
                lesson_id, body, expected_status, errors, location
            )
    return records


def _parse_standard_rules(text: str, errors: list[str]) -> dict[str, PromotedRule]:
    block, first_line = _marked_block(text, "PROMOTED_RULES", errors)
    lines = block.splitlines()
    starts = _record_starts(lines, first_line, errors, "docs/code-standards.md")
    rules: dict[str, PromotedRule] = {}
    for position, start_index in enumerate(starts):
        end_index = starts[position + 1] if position + 1 < len(starts) else len(lines)
        heading = lines[start_index]
        location = f"docs/code-standards.md:{first_line + start_index}"
        match = RULE_HEADING_RE.fullmatch(heading)
        if not match:
            errors.append(f"{location}: promoted rules must use '##### PS-0001 — <rule>'")
            continue
        rule_id, _title = match.groups()
        if rule_id in rules:
            errors.append(f"{location}: duplicate promoted-rule ID {rule_id}")
            continue
        body = lines[start_index + 1:end_index]
        scope_values = _field_values("\n".join(body), "Scope")
        if len(scope_values) != 1 or not scope_values[0]:
            errors.append(f"{location}: rule requires exactly one non-empty Scope field")

        evidence_fields = [line for line in body if line.startswith("- **Evidence lessons:**")]
        if len(evidence_fields) != 1:
            errors.append(f"{location}: rule requires exactly one Evidence lessons field")
            lesson_ids: tuple[str, ...] = ()
        else:
            evidence_match = EVIDENCE_LESSONS_RE.fullmatch(evidence_fields[0])
            if not evidence_match:
                errors.append(f"{location}: Evidence lessons must be a comma-separated list of backtick-delimited L-IDs")
                lesson_ids = ()
            else:
                lesson_ids = tuple(re.findall(r"L-[0-9]{4,}", evidence_match.group("ids")))
                if len(set(lesson_ids)) != len(lesson_ids):
                    errors.append(f"{location}: duplicate Lesson ID in Evidence lessons")
        rules[rule_id] = PromotedRule(rule_id, lesson_ids)
    return rules


def validate_documents(lessons_text: str, standards_text: str) -> list[str]:
    """Return structural/threshold violations; an empty list means the gate passes."""
    errors: list[str] = []
    lessons = _parse_lessons(lessons_text, errors)
    rules = _parse_standard_rules(standards_text, errors)

    for rule_id, rule in rules.items():
        evidence_dates: set[str] = set()
        for lesson_id in rule.evidence_lesson_ids:
            lesson = lessons.get(lesson_id)
            if lesson is None:
                errors.append(f"{rule_id}: Evidence lessons references unknown Lesson ID {lesson_id}")
                continue
            if lesson.status != "promoted":
                errors.append(f"{rule_id}: referenced Lesson {lesson_id} is not in promoted status")
            if lesson.promoted_rule != rule_id:
                errors.append(f"{rule_id}: Lesson {lesson_id} does not name this promoted rule")
            evidence_dates.update(lesson.review_dates)
        if len(evidence_dates) < 3:
            errors.append(
                f"{rule_id}: requires at least 3 distinct review-evidence dates; found {len(evidence_dates)}"
            )

    for lesson_id, lesson in lessons.items():
        if lesson.status == "promoted" and lesson.promoted_rule:
            rule = rules.get(lesson.promoted_rule)
            if rule is None:
                errors.append(f"{lesson_id}: refers to missing standards rule {lesson.promoted_rule}")
            elif lesson_id not in rule.evidence_lesson_ids:
                errors.append(f"{lesson_id}: is promoted to {lesson.promoted_rule} but is not cited by that rule")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate persisted Lesson IDs and promoted-rule evidence (read-only; no range or watermark operations)."
    )
    parser.add_argument("--lessons", type=pathlib.Path, default=pathlib.Path("LESSONS.md"))
    parser.add_argument("--standards", type=pathlib.Path, default=pathlib.Path("docs/code-standards.md"))
    args = parser.parse_args(argv)
    try:
        lessons_text = args.lessons.read_text(encoding="utf-8")
        standards_text = args.standards.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Lesson promotion validation failed: {exc}", file=sys.stderr)
        return 2

    errors = validate_documents(lessons_text, standards_text)
    if errors:
        print("Lesson promotion validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Lesson promotion validation passed (persisted Lessons and standards only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
