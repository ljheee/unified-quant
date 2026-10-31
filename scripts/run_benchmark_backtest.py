#!/usr/bin/env python3
"""Publish PIT benchmark weights and replay them under a reviewed v3/v4 config."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from uq.errors import ContractError
from uq.evaluation.benchmark_backtest import publish_benchmark_backtest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True,
                        help="owning backtest, universe and market-data store")
    parser.add_argument("--benchmark-root", type=Path, required=True,
                        help="immutable benchmark-weight panel store")
    parser.add_argument("--membership-root", type=Path, required=True,
                        help="owning point-in-time index membership store")
    parser.add_argument("--strategy-result-generation", required=True,
                        help="reviewed v3/v4 result whose config and rebalance schedule are reused")
    parser.add_argument("--walk-forward-root", type=Path,
                        help="continuous walk-forward receipt store; enables its terminal-liquidation replay")
    parser.add_argument("--scheme", required=True,
                        choices=("equal_weight", "market_cap_weighted", "industry_neutral"))
    parser.add_argument("--exposure-root", type=Path,
                        help="owning PIT factor exposure store (required for cap/industry weighting)")
    parser.add_argument("--exposure-generation",
                        help="published PIT factor exposure generation")
    parser.add_argument("--quality-decisions-json", type=Path, required=True,
                        help="external backtest_result_v1 decision or reviewer-provider config")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        if (args.exposure_root is None) != (args.exposure_generation is None):
            raise ContractError("--exposure-root and --exposure-generation must be provided together")
        from publish_backtest_config import _load_decision

        report = publish_benchmark_backtest(
            data_root=args.data_root,
            benchmark_root=args.benchmark_root,
            membership_root=args.membership_root,
            strategy_result_generation_id=args.strategy_result_generation,
            scheme=args.scheme,
            quality_decision=_load_decision(args.quality_decisions_json, "backtest_result"),
            exposure_root=args.exposure_root,
            exposure_generation_id=args.exposure_generation,
            walk_forward_root=args.walk_forward_root,
        )
    except (ContractError, OSError, ValueError) as exc:
        print(json.dumps({"status": "publication_rejected", "error": str(exc)}, ensure_ascii=False))
        return 2
    encoded = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
