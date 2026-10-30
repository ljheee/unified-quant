#!/usr/bin/env python3
"""Compare costed, governed backtests and archive recomputable diagnostics.

This command does not train, ingest, sign quality decisions or authorize model
promotion. Both results must have the same cost/universe/market/constraint
bindings. Evaluation window endpoints are explicit and must be trading sessions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from uq.errors import ContractError
from uq.evaluation.report import EvaluationReportStore, paired_backtest_inputs, published_ic_inputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strategy-root', type=Path, required=True)
    parser.add_argument('--strategy-result', required=True)
    parser.add_argument('--benchmark-root', type=Path, required=True)
    parser.add_argument('--benchmark-result', required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--regimes-json', type=Path)
    parser.add_argument('--prediction-bindings-json', type=Path)
    parser.add_argument('--prediction-root', type=Path)
    parser.add_argument('--label-root', type=Path)
    parser.add_argument('--label-generation')
    parser.add_argument('--hac-lags', type=int)
    args = parser.parse_args(argv)
    try:
        optional = (args.prediction_bindings_json, args.prediction_root, args.label_root, args.label_generation)
        if any(value is not None for value in optional) and not all(value is not None for value in optional):
            raise ContractError('IC requires prediction bindings/root and label root/generation together')
        regimes = json.loads(args.regimes_json.read_text()) if args.regimes_json else None
        daily, bindings = paired_backtest_inputs(
            args.strategy_root, args.strategy_result, args.benchmark_root, args.benchmark_result,
            start=args.start, end=args.end, regimes=regimes,
        )
        scores = labels = horizon = None
        if args.prediction_bindings_json:
            prediction_bindings = json.loads(args.prediction_bindings_json.read_text())
            if not isinstance(prediction_bindings, list):
                raise ContractError('prediction bindings document must be a list')
            scores, labels, ic_bindings = published_ic_inputs(
                args.prediction_root, prediction_bindings, args.label_root, args.label_generation,
                calendar_dates=daily['date'].tolist(),
            )
            horizon = ic_bindings['label']['horizon_trading_days']
            bindings.update(ic_bindings)
        store = EvaluationReportStore(args.output_root)
        report = store.publish(daily, input_bindings=bindings, predictions=scores, labels=labels,
                               horizon_sessions=horizon, hac_lags=args.hac_lags)
    except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'evaluation_rejected', 'errors': [str(exc)]}, sort_keys=True))
        return 2
    print(json.dumps({'status': 'diagnostic_published', 'generation_id': report['generation_id'],
                      'manifest_path': str(store.directory_for(report['generation_id']) / 'manifest.json'),
                      'metrics': report['metrics']}, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
