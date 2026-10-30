#!/usr/bin/env python3
"""Archive source-declared, pre-open execution facts for a reviewed v3 config.

The input is an offline Parquet table with the exact execution-facts v1 columns.
Source approval is a maintainer-supplied reference, not a signature issued here.
The config publisher subsequently binds these exact bytes into its external
quality review. This command does not contact vendors or infer historical ST,
industry, board, lot or price-limit rules from today's instrument metadata.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from uq.backtest.execution_facts import ExecutionFactsStore, facts_binding


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--input-parquet', type=Path, required=True)
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--source-approval-ref', required=True)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    manifest = ExecutionFactsStore(args.data_root).publish(
        pd.read_parquet(args.input_parquet), source_id=args.source_id,
        source_approval_ref=args.source_approval_ref,
    )
    report = {
        'producer': 'scripts/publish_backtest_execution_facts.py',
        'execution_facts_binding': facts_binding(manifest),
        'source_id': manifest['source_id'], 'source_approval_ref': manifest['source_approval_ref'],
        'window_start': manifest['window_start'], 'window_end': manifest['window_end'],
        'row_count': manifest['row_count'],
        'approval_scope': 'source-declared input; external config review still required',
    }
    payload = json.dumps(report, indent=2, ensure_ascii=False) + '\n'
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding='utf-8')
    print(payload, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
