#!/usr/bin/env python3
"""Read-only label-leg audit; write its JSON report only when --out is supplied."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from uq.evaluation.label_audit import audit_label_legs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bars-root', type=Path, required=True)
    parser.add_argument('--label-root', type=Path, required=True)
    parser.add_argument('--label-generation', required=True)
    parser.add_argument('--calendar-generation-id', required=True,
                        help='published independent calendar dataset generation in --bars-root')
    parser.add_argument('--calendar-checksum', required=True,
                        help='calendar dataset data_checksum_sha256')
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    report = audit_label_legs(
        args.bars_root,
        args.label_root,
        args.label_generation,
        args.start,
        args.end,
        calendar_generation_id=args.calendar_generation_id,
        calendar_checksum_sha256=args.calendar_checksum,
    )
    payload = json.dumps(report, indent=2, ensure_ascii=False) + '\n'
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding='utf-8')
    print(payload, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
