#!/usr/bin/env python3
"""Recompute factor diagnostics for a stored Tushare candidate bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from uq.contracts.gate_contracts import validate_contract
from uq.errors import ContractError
from uq.evaluation.factor_cost_proxy import reviewed_v3_turnover_cost_proxy
from uq.evaluation.tushare_factor_screening import build_tushare_candidate_screen_report
from uq.models.tushare_factor_candidates import TushareFactorCandidateStore
from uq.research_chain.owning_contracts import LabelStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--candidate-generation", required=True)
    parser.add_argument("--source-root", type=Path, required=True,
                        help="re-verify every referenced Tushare raw snapshot")
    parser.add_argument("--bars-root", type=Path, required=True,
                        help="re-verify every referenced canonical calendar partition")
    parser.add_argument("--label-root", type=Path, required=True)
    parser.add_argument("--label-generation", required=True)
    parser.add_argument("--cost-data-root", type=Path,
                        help="root containing a reviewed v3 backtest config")
    parser.add_argument("--backtest-config-generation",
                        help="reviewed v3 config used for the bounded turnover-cost proxy")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--top-n", type=int, default=30)
    parser.add_argument("--correlation-threshold", type=float, default=0.90)
    parser.add_argument("--hac-lags", type=int)
    parser.add_argument("--out", type=Path, required=True,
                        help="new JSON output path; existing reports are never overwritten")
    args = parser.parse_args(argv)
    try:
        if args.out.exists():
            raise ContractError("factor candidate screen output already exists")
        candidate = TushareFactorCandidateStore(args.candidate_root).read(
            args.candidate_generation,
            source_root=args.source_root,
            bars_root=args.bars_root,
        )
        candidate_manifest = candidate["manifest"]
        factor_panel = pd.read_parquet(candidate["path"] / "factors.parquet")
        label_manifest, labels = LabelStore(args.label_root).read_frame(args.label_generation)
        if (args.cost_data_root is None) != (args.backtest_config_generation is None):
            raise ContractError("--cost-data-root and --backtest-config-generation must be supplied together")
        cost_binding = None
        cost_bps = None
        if args.backtest_config_generation is not None:
            sessions = sorted(factor_panel["decision_date"].astype(str).unique().tolist())
            cost_binding, cost_bps = reviewed_v3_turnover_cost_proxy(
                args.cost_data_root,
                args.backtest_config_generation,
                start=sessions[0],
                end=sessions[-1],
            )
        report = build_tushare_candidate_screen_report(
            candidate_manifest=candidate_manifest,
            factor_panel=factor_panel,
            label_manifest=label_manifest,
            labels=labels,
            project_root=args.project_root,
            top_n=args.top_n,
            correlation_threshold=args.correlation_threshold,
            hac_lags=args.hac_lags,
            cost_proxy_bps_per_one_way_turnover=cost_bps,
            backtest_cost_binding=cost_binding,
        )
        validate_contract("tushare_factor_candidate_screen_report.v1.json", report)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ContractError, OSError, ValueError) as exc:
        print(json.dumps({"status": "screen_rejected", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({
        "status": "diagnostic_report_created",
        "generation_id": report["generation_id"],
        "window": report["window"],
        "strict_pit_eligible": report["strict_pit_eligible"],
        "features": len(report["feature_summary"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
