#!/usr/bin/env python3
"""Publish a recomputable, costed linear-versus-LightGBM comparison."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from uq.errors import ContractError
from uq.evaluation.model_comparison import paired_model_backtest_inputs
from uq.evaluation.report import EvaluationReportStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--baseline-result-generation', required=True)
    parser.add_argument('--baseline-result-digest', required=True)
    parser.add_argument('--candidate-result-generation', required=True)
    parser.add_argument('--candidate-result-digest', required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--regimes-json', type=Path)
    args = parser.parse_args(argv)
    try:
        regimes = None
        if args.regimes_json is not None:
            regimes = json.loads(args.regimes_json.read_text(encoding='utf-8'))
            if not isinstance(regimes, dict):
                raise ContractError('regime document must map session dates to regimes')
        daily, bindings = paired_model_backtest_inputs(
            args.data_root,
            baseline_result_generation=args.baseline_result_generation,
            baseline_result_digest=args.baseline_result_digest,
            candidate_result_generation=args.candidate_result_generation,
            candidate_result_digest=args.candidate_result_digest,
            start=args.start, end=args.end, regimes=regimes,
        )
        report = EvaluationReportStore(args.output_root).publish(daily, input_bindings=bindings)
    except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'model_comparison_rejected', 'errors': [str(exc)]}, sort_keys=True))
        return 2
    print(json.dumps({
        'status': 'model_comparison_published',
        'generation_id': report['generation_id'],
        'metrics': report['metrics'],
        'model_comparison': bindings['model_comparison'],
    }, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
