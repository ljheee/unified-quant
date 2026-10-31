#!/usr/bin/env python3
"""Replay a passed walk-forward attempt with one shared v3 account."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from uq.errors import ContractError
from uq.evaluation.walk_forward_continuous import run_continuous_walk_forward_backtest


def _json(path: Path) -> dict:
    document = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(document, dict):
        raise ContractError(f'quality decision must be a JSON object: {path}')
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--walk-forward-root', type=Path, required=True)
    parser.add_argument('--attempt-json', type=Path, required=True)
    parser.add_argument('--backtest-root', type=Path, required=True)
    parser.add_argument('--backtest-config-generation', required=True)
    parser.add_argument('--quality-decision-json', type=Path, required=True,
                        help='externally reviewed backtest-result decision document')
    args = parser.parse_args(argv)
    try:
        outcome = run_continuous_walk_forward_backtest(
            walk_forward_root=args.walk_forward_root, attempt_path=args.attempt_json,
            backtest_root=args.backtest_root,
            backtest_config_generation_id=args.backtest_config_generation,
            quality_decision=_json(args.quality_decision_json),
        )
        print(json.dumps(outcome, sort_keys=True))
        return 0
    except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'continuous_backtest_rejected', 'errors': [str(exc)]}, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
