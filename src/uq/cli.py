from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import date
from datetime import datetime as utc_datetime
from pathlib import Path

from .pipeline.daily import build_tdx_pipeline, build_tushare_crosscheck_pipeline


EXIT_STATUS_CODES = {
    "published": 0,
    "expected_empty": 2,
    "rejected": 3,
    "primary_source_upstream_error": 4,
    "primary_source_rate_limited": 4,
    "primary_source_auth_failed": 4,
    "primary_source_unhealthy": 4,
    "publication_conflict": 5,
    "cross_validation_missing": 3,
    "coverage_unverified": 3,
    "source_rate_limited": 4,
    "source_auth_failed": 4,
    "source_upstream_error": 4,
    "source_unsupported_request": 3,
}


def uq_ingest(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uq-ingest")
    commands = parser.add_subparsers(dest="command", required=True)
    daily = commands.add_parser("daily", help="ingest canonical daily bars")
    daily.add_argument("--date", required=True)
    daily.add_argument("--data-root", type=Path, required=True)
    daily.add_argument("--cross-validate", action="store_true")
    daily.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="repository/config root; override for installed deployments",
    )
    args = parser.parse_args(argv)

    try:
        trade_date = date.fromisoformat(args.date)
    except ValueError as exc:
        parser.error(f"invalid --date: {exc}")

    try:
        skip_health = os.environ.get("UQ_SKIP_HEALTH_PROBE") == "1"
        builder = build_tushare_crosscheck_pipeline if args.cross_validate else build_tdx_pipeline
        pipeline = builder(args.data_root, args.project_root, run_health_probe=not skip_health)
        universe_path = args.project_root / "config/universe/research-whitelist.txt"
        instruments = [line.strip() for line in universe_path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "configuration_failure", "errors": [str(exc)]}, sort_keys=True))
        return 3

    report = pipeline.run(trade_date, instruments)
    fingerprint = hashlib.sha256(args.project_root.resolve().as_posix().encode()).hexdigest()[:12]
    run_id = f"{trade_date.isoformat()}-{fingerprint}-{uuid.uuid4().hex[:8]}"
    report_with_id = json.loads(report.to_json())
    report_with_id["run_id"] = run_id
    report_with_id["recorded_at"] = utc_datetime.now().astimezone().isoformat()
    runs_dir = args.data_root / "runs"
    print(report.to_json())
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        target = runs_dir / f"{run_id}.json"
        staging = target.with_suffix(".json.staging")
        staging.write_text(json.dumps(report_with_id, sort_keys=True), encoding="utf-8")
        os.replace(staging, target)
    except (OSError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "run_report_failure", "run_id": run_id, "errors": [str(exc)]}, sort_keys=True))
        return 5
    return EXIT_STATUS_CODES.get(report.status, 3)


if __name__ == "__main__":
    raise SystemExit(uq_ingest())
