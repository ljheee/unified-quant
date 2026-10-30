#!/usr/bin/env python3
"""Compare reviewed paper executions with a reviewed v3 backtest."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from uq.errors import ContractError
from uq.evaluation.paper_reconciliation import (
    compare_published_execution, compare_published_execution_window,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backtest-root', type=Path, required=True)
    parser.add_argument('--backtest-generation', required=True)
    parser.add_argument('--paper-root', type=Path, required=True)
    paper_input = parser.add_mutually_exclusive_group(required=True)
    paper_input.add_argument('--paper-generation')
    paper_input.add_argument('--paper-generations-json', type=Path,
                             help='JSON array with one paper result ID per backtest decision date')
    parser.add_argument('--target-root', type=Path, help='target-weight store; defaults to backtest root')
    parser.add_argument('--budget-json', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        budget = json.loads(args.budget_json.read_text(encoding='utf-8'))
        common = dict(backtest_root=args.backtest_root,
                      backtest_generation=args.backtest_generation,
                      paper_root=args.paper_root, budget=budget,
                      target_root=args.target_root)
        if args.paper_generations_json is not None:
            generations = json.loads(args.paper_generations_json.read_text(encoding='utf-8'))
            report = compare_published_execution_window(
                **common, paper_generations=generations)
        else:
            report = compare_published_execution(**common,
                                                 paper_generation=args.paper_generation)
    except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'reconciliation_rejected', 'errors': [str(exc)]}, sort_keys=True))
        return 2
    print(json.dumps({'status': 'diagnostic_ready', 'report': report}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
