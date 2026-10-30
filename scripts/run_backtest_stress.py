#!/usr/bin/env python3
"""Replay reviewed v3 weights under cost stresses and an explicit capital grid."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from uq.evaluation.stress import run_backtest_stress
from publish_backtest_config import _load_decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--baseline-generation', required=True)
    parser.add_argument('--capital-grid', type=float, nargs='+', required=True)
    parser.add_argument('--minimum-fill-ratio', type=float, required=True)
    parser.add_argument('--maximum-risk-breach-sessions', type=int, required=True)
    parser.add_argument('--require-positive-return', action=argparse.BooleanOptionalAction, required=True)
    parser.add_argument('--quality-decisions-json', type=Path, required=True)
    parser.add_argument('--report-root', type=Path)
    args = parser.parse_args(argv)
    report = run_backtest_stress(args.data_root, args.baseline_generation,
        capital_grid=args.capital_grid, capacity_policy={
            'minimum_fill_ratio': args.minimum_fill_ratio,
            'maximum_risk_breach_sessions': args.maximum_risk_breach_sessions,
            'require_positive_return': args.require_positive_return,
        }, config_quality_decision=_load_decision(args.quality_decisions_json, 'backtest_config'),
        result_quality_decision=_load_decision(args.quality_decisions_json, 'backtest_result'),
        report_root=args.report_root)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
