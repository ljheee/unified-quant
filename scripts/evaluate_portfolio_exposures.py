#!/usr/bin/env python3
"""Report actual position, industry and prior-turnover exposures for a reviewed v3/v4 backtest."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from uq.errors import ContractError
from uq.evaluation.exposures import evaluate_published_exposures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backtest-root', type=Path, required=True)
    parser.add_argument('--backtest-generation', required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate_published_exposures(
            backtest_root=args.backtest_root,
            backtest_generation=args.backtest_generation,
        )
    except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'exposure_rejected', 'errors': [str(exc)]}, sort_keys=True))
        return 2
    print(json.dumps({'status': 'diagnostic_ready', 'report': report}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
