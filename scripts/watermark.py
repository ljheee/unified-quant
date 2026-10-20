"""Shared strict parser for the LESSONS.md reviewed-through watermark."""
from __future__ import annotations

import re

MARKER = re.compile(rb"<!--\s*reviewed-through\s*:")
MARKER_LINE = re.compile(
    rb"^<!-- reviewed-through: ([^\s<>]+)(?:[ \t]+[^<>]*)? -->$"
)


class WatermarkError(ValueError):
    """LESSONS.md does not contain one canonical first-line watermark."""


def parse_watermark(data: bytes) -> tuple[str, int, int]:
    """Return value and byte offsets after enforcing the shared line contract.

    The marker must occur exactly once, and its complete line must be the first
    line. CRLF is accepted as a line ending; surrounding text is not.
    """
    markers = list(MARKER.finditer(data))
    if len(markers) != 1:
        raise WatermarkError(
            "LESSONS.md must contain exactly one reviewed-through marker; "
            f"found {len(markers)}"
        )
    lines = data.splitlines(keepends=True)
    first_line = lines[0].rstrip(b"\r\n") if lines else b""
    match = MARKER_LINE.fullmatch(first_line)
    if match is None:
        raise WatermarkError("the reviewed-through marker must be the first line of LESSONS.md")
    try:
        value = match.group(1).decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise WatermarkError("reviewed-through is not ASCII") from exc
    return value, match.start(1), match.end(1)
