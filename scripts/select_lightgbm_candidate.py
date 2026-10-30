#!/usr/bin/env python3
"""Run the bounded LightGBM candidate selector on a verified model dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from uq.models.dataset_writer import DatasetWriter
from uq.models.lightgbm_selection import (
    DEFAULT_CANDIDATES,
    receipt_json,
    select_lightgbm_candidate,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-store", required=True, type=Path, help="root containing the governed datasets/ store")
    parser.add_argument("--dataset-name", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--candidates-json", type=Path, help="optional JSON array of one to three parameter objects")
    args = parser.parse_args()

    manifest, frame = DatasetWriter(args.dataset_store).read(
        args.dataset_name, args.dataset_version, args.generation_id
    )
    candidates = DEFAULT_CANDIDATES
    if args.candidates_json:
        candidates = json.loads(args.candidates_json.read_text(encoding="utf-8"))
        if not isinstance(candidates, list) or not all(isinstance(item, dict) for item in candidates):
            parser.error("--candidates-json must contain a JSON array of parameter objects")
    result = select_lightgbm_candidate(
        dataset_manifest=manifest,
        dataset_frame=frame,
        candidates=candidates,
        seed=args.seed,
        thread_count=args.threads,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "selection-receipt.json").write_text(
        receipt_json(result.receipt), encoding="utf-8"
    )
    (args.output_dir / "model.txt").write_bytes(result.model_text)
    result.test_predictions.to_parquet(args.output_dir / "test-predictions.parquet", index=False)
    print(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
