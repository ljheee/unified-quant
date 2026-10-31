#!/usr/bin/env python3
"""Materialize immutable, diagnostic-only Tushare factor/exposure candidates.

The bundle keeps raw snapshot and canonical calendar bindings. Historical source
vintages are not verified, so it is not eligible for strict PIT publication.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from uq.errors import ContractError
from uq.models.tushare_factor_candidates import (
    TushareFactorCandidateStore,
    build_tushare_factor_candidates,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True,
                        help="root containing tushare_dependencies/")
    parser.add_argument("--bars-root", type=Path, required=True,
                        help="canonical bars store used as the governed calendar")
    parser.add_argument("--output-root", type=Path, required=True,
                        help="immutable local candidate store root")
    parser.add_argument("--start", type=dt.date.fromisoformat)
    parser.add_argument("--end", type=dt.date.fromisoformat)
    parser.add_argument("--out", type=Path, required=True,
                        help="write the publication receipt to a new path")
    args = parser.parse_args(argv)
    try:
        if args.out.exists():
            raise ContractError("receipt output already exists")
        bundle = build_tushare_factor_candidates(
            source_root=args.source_root,
            bars_root=args.bars_root,
            start_date=args.start,
            end_date=args.end,
        )
        manifest = TushareFactorCandidateStore(args.output_root).publish(bundle)
    except (ContractError, OSError, ValueError) as exc:
        print(json.dumps({"status": "candidate_build_rejected", "error": str(exc)}, ensure_ascii=False))
        return 2
    receipt = {
        "record_version": 1,
        "kind": "tushare_factor_candidate_receipt",
        "generation_id": manifest["generation_id"],
        "strict_pit_eligible": manifest["strict_pit_eligible"],
        "historical_vintage_verified": manifest["historical_vintage_verified"],
        "factor_rows": manifest["factor_artifact"]["row_count"],
        "exposure_rows": manifest["exposure_artifact"]["row_count"],
        "sessions": [
            bundle.factors["decision_date"].min(),
            bundle.factors["decision_date"].max(),
        ],
        "factor_columns": manifest["factor_columns"],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "candidate_bundle_published", **receipt}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
