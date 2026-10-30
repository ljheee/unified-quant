#!/usr/bin/env python3
"""Publish offline daily returns bound to a governed backtest calendar.

The input Parquet file must contain exactly ``date``, ``instrument`` and
``return``. Returns are decimal adjusted-close simple returns and are observed
after the named session closes. This command does not download data or create
returns from prices; the source identity and approval reference are retained in
the immutable publication manifest for the reviewed Research Chain request.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from uq.errors import ContractError
from uq.portfolio.risk_model import PortfolioRiskReturnsStore
from uq.research_chain.owning_contracts import BacktestMarketDatasetStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="publish-portfolio-risk-returns")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--input-parquet", type=Path, required=True)
    parser.add_argument("--calendar-generation-id", required=True)
    parser.add_argument("--calendar-data-checksum", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-approval-ref", required=True)
    parser.add_argument("--manifest-out", type=Path)
    args = parser.parse_args(argv)

    try:
        calendar_store = BacktestMarketDatasetStore(args.data_root)
        calendar_manifest = calendar_store.read_manifest_by(
            args.calendar_generation_id, dataset_type="calendar"
        )
        calendar_frame = calendar_store.read_frame(
            args.calendar_generation_id,
            args.calendar_data_checksum,
            dataset_type="calendar",
            required_columns={"date"},
        )
        sessions = sorted(set(calendar_frame["date"].astype(str).tolist()))
        returns = pd.read_parquet(args.input_parquet)
        manifest = PortfolioRiskReturnsStore(
            args.data_root, calendar_store=calendar_store
        ).publish(
            returns,
            calendar_sessions=sessions,
            calendar_generation_id=args.calendar_generation_id,
            calendar_manifest_digest_sha256=calendar_manifest["manifest_digest_sha256"],
            calendar_data_checksum_sha256=args.calendar_data_checksum,
            source_id=args.source_id,
            source_approval_ref=args.source_approval_ref,
        )
        serialized = json.dumps(manifest, sort_keys=True, indent=2)
        if args.manifest_out:
            args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
            args.manifest_out.write_text(serialized + "\n", encoding="utf-8")
        print(serialized)
        return 0
    except (ContractError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "rejected", "errors": [str(exc)]}, sort_keys=True))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
