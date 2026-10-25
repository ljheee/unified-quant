#!/usr/bin/env python3
"""Score decision dates by loading the published model artifact.

This is the production entry point that closes the gap the research chain had
since its first release: `prediction_publication` took its scores from an
external `--scores-json`, so the governed model artifact was never actually
loaded. Any number that reached portfolio construction came from outside the
chain.

The output is the same `research_scores.v1` document `--scores-json` accepts, so
it drops into the existing execute path unchanged.

Usage:
  scripts/score_decision_dates.py \
      --data-root data/full-a-root \
      --dataset-name research_baseline \
      --export-generation <qlib_dataset_export generation> \
      --run-generation <model_run generation> \
      --artifact-generation <model_artifact generation> \
      --start 2025-09-10 --end 2025-09-23 \
      --out evidence/.../scores.json

The three generations are read from the request's stage state, not guessed: an
artifact that does not belong to this dataset is refused by the reader.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from uq.errors import ContractError  # noqa: E402
from uq.models.inference import score_decision_dates  # noqa: E402


def _refuse(message: str) -> int:
    print(json.dumps({"status": "configuration_failure", "error": message}))
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--dataset-name", required=True)
    parser.add_argument("--export-generation", required=True)
    parser.add_argument("--run-generation", required=True)
    parser.add_argument("--artifact-generation", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD, first decision date")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD, last decision date")
    parser.add_argument("--qlib-version", default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    calendar_path = (
        args.data_root / "qlib_exports" / f"dataset={args.dataset_name}"
        / f"generation={args.export_generation}" / "calendars" / "day.txt"
    )
    if not calendar_path.is_file():
        return _refuse(f"unpublished Qlib export calendar: {calendar_path}")
    calendar = [line for line in calendar_path.read_text(encoding="utf-8").splitlines() if line]
    decision_dates = [day for day in calendar if args.start <= day <= args.end]
    if not decision_dates:
        return _refuse(
            f"no exported sessions inside {args.start}..{args.end}; "
            f"calendar spans {calendar[0]}..{calendar[-1]}"
        )

    try:
        result = score_decision_dates(
            export_root=args.data_root / "qlib_exports",
            dataset_name=args.dataset_name,
            export_generation_id=args.export_generation,
            run_generation_id=args.run_generation,
            artifact_generation_id=args.artifact_generation,
            decision_dates=decision_dates,
            expected_qlib_version=args.qlib_version,
        )
    except ContractError as exc:
        return _refuse(f"inference refused: {exc}")

    document = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "scores_by_decision_date": {
            decision_date: frame.to_dict("records")
            for decision_date, frame in result.scores.items()
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": "scored",
        "decision_dates": len(result.scores),
        "rows": sum(len(frame) for frame in result.scores.values()),
        "unscorable_rows": result.unscorable_rows,
        "out": str(args.out),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
