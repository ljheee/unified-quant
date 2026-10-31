#!/usr/bin/env python3
"""Screen reviewed factor partitions for IC stability, collinearity and churn.

The report is diagnostic only. It does not choose factors, establish incremental
value, or establish realized execution costs. A reviewed v3 config can supply a
bounded turnover cost proxy. Inputs must already be published in their owning
stores.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.gate_contracts import sha256_json, validate_contract
from uq.errors import ContractError
from uq.evaluation.factor_screening import screen_factor_panel
from uq.evaluation.factor_cost_proxy import reviewed_v3_turnover_cost_proxy
from uq.evaluation.metrics import session_dates
from uq.factors.store import FactorStore
from uq.research_chain.owning_contracts import (
    BacktestMarketDatasetStore,
    LabelStore,
)
from uq.research_chain.provenance import capture_environment


def _read_factor_generations(path: Path) -> list[str]:
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("factor generation list is unreadable or malformed") from exc
    if (not isinstance(values, list) or not values
            or any(not isinstance(value, str) or len(value) != 64 for value in values)
            or len(values) != len(set(values))):
        raise ContractError("factor generation list must contain unique SHA-256 ids")
    return values


def _calendar_dates(root: Path, generation: str, checksum: str) -> tuple[list[str], dict]:
    store = BacktestMarketDatasetStore(root)
    manifest = store.read_manifest_by(generation, dataset_type="calendar")
    validate_contract("backtest_market_dataset.v1.json", manifest)
    if sha256_json({key: value for key, value in manifest.items()
                    if key != "manifest_digest_sha256"}) != manifest["manifest_digest_sha256"]:
        raise ContractError("factor screen calendar manifest digest mismatch")
    frame = store.read_frame(
        generation, checksum, dataset_type="calendar", required_columns={"date"}
    )
    ordered = frame.sort_values([column for column in ("date", "instrument") if column in frame],
                                kind="mergesort").reset_index(drop=True)
    expected_generation = sha256_json({
        "dataset_type": "calendar",
        "window_start": manifest["window_start"],
        "window_end": manifest["window_end"],
        "columns": list(ordered.columns),
        "rows": ordered.to_dict(orient="records"),
    })
    if expected_generation != generation:
        raise ContractError("factor screen calendar content generation mismatch")
    dates = session_dates(frame["date"].tolist(), label="factor screen calendar")
    dates = sorted(set(dates))
    if not dates:
        raise ContractError("factor screen calendar contains no sessions")
    return dates, manifest


def _cost_proxy_binding(root: Path, generation: str | None, *, start: str, end: str):
    return reviewed_v3_turnover_cost_proxy(root, generation, start=start, end=end)


def build_report(
    *,
    data_root: Path,
    label_root: Path,
    project_root: Path,
    factor_generations: list[str],
    label_generation: str,
    calendar_generation: str,
    calendar_checksum: str,
    start: str,
    end: str,
    top_n: int,
    correlation_threshold: float,
    hac_lags: int | None,
    backtest_config_generation: str | None = None,
) -> dict:
    calendar, calendar_manifest = _calendar_dates(
        data_root, calendar_generation, calendar_checksum
    )
    if start not in calendar or end not in calendar or start > end:
        raise ContractError("factor screen endpoints must be ordered sessions in the bound calendar")
    evaluation_dates = [day for day in calendar if start <= day <= end]
    if not evaluation_dates:
        raise ContractError("factor screen window contains no calendar sessions")
    cost_binding, cost_bps_per_turnover = _cost_proxy_binding(
        data_root,
        backtest_config_generation,
        start=evaluation_dates[0],
        end=evaluation_dates[-1],
    )

    registry = FactorRegistry(project_root)
    factor_store = FactorStore(data_root, registry)
    manifests: list[dict] = []
    frames: list[pd.DataFrame] = []
    for generation in factor_generations:
        manifest, frame = factor_store.read_partition(generation)
        if manifest["partition_date"] not in evaluation_dates:
            raise ContractError("factor generation lies outside the requested screening window")
        if "instrument" not in frame or "datetime" not in frame:
            raise ContractError("factor partition lacks instrument or datetime columns")
        features = [column for column in frame.columns if column not in {"instrument", "datetime"}]
        if not features:
            raise ContractError("factor partition contains no feature columns")
        if manifests and any(manifest.get(key) != manifests[0].get(key) for key in (
            "factor_set", "factor_version", "input_dataset", "input_schema_version", "columns"
        )):
            raise ContractError("factor partitions do not share one reviewed set and schema")
        part = frame[["instrument", *features]].copy()
        part["decision_date"] = manifest["partition_date"]
        frames.append(part[["instrument", "decision_date", *features]])
        manifests.append(manifest)

    by_date = [manifest["partition_date"] for manifest in manifests]
    if len(by_date) != len(set(by_date)) or sorted(by_date) != evaluation_dates:
        raise ContractError("factor generations must cover every requested session exactly once")
    ordered_manifests = sorted(manifests, key=lambda item: item["partition_date"])
    ordered_frames = [
        frame for manifest, frame in sorted(zip(manifests, frames), key=lambda pair: pair[0]["partition_date"])
    ]
    panel = pd.concat(ordered_frames, ignore_index=True)
    feature_names = [column for column in ordered_frames[0].columns
                     if column not in {"instrument", "decision_date"}]

    label_manifest, labels = LabelStore(label_root).read_frame(label_generation)
    if "decision_date" not in labels or "label" not in labels:
        raise ContractError("label set does not expose decision_date and label")
    labels = labels.copy()
    labels["decision_date"] = session_dates(labels["decision_date"].tolist(), label="factor screen labels")
    labels = labels.loc[labels["decision_date"].isin(evaluation_dates)].reset_index(drop=True)
    metrics = screen_factor_panel(
        panel,
        labels,
        features=feature_names,
        calendar_dates=evaluation_dates,
        horizon_sessions=label_manifest["horizon_trading_days"],
        top_n=top_n,
        correlation_threshold=correlation_threshold,
        hac_lags=hac_lags,
        cost_proxy_bps_per_one_way_turnover=cost_bps_per_turnover,
    )
    environment = capture_environment(project_root)
    input_bindings = {
        "factor_set": ordered_manifests[0]["factor_set"],
        "factor_version": ordered_manifests[0]["factor_version"],
        "input_dataset": ordered_manifests[0]["input_dataset"],
        "input_schema_version": ordered_manifests[0]["input_schema_version"],
        "factor_partitions": [{
            "partition_date": manifest["partition_date"],
            "generation_id": manifest["generation_id"],
            "manifest_digest_sha256": manifest["manifest_digest_sha256"],
            "data_checksum_sha256": manifest["data_checksum_sha256"],
        } for manifest in ordered_manifests],
        "label": {
            "generation_id": label_manifest["generation_id"],
            "manifest_digest_sha256": label_manifest["manifest_digest_sha256"],
            "data_checksum_sha256": label_manifest["data_checksum_sha256"],
            "horizon_sessions": label_manifest["horizon_trading_days"],
            "formula_sha256": label_manifest["formula_sha256"],
        },
        "calendar": {
            "generation_id": calendar_generation,
            "manifest_digest_sha256": calendar_manifest["manifest_digest_sha256"],
            "data_checksum_sha256": calendar_checksum,
        },
        "backtest_cost_config": cost_binding,
    }
    report = {
        "record_version": 1,
        "kind": "factor_screen_report",
        "publication_scope": "diagnostic_only_no_factor_selection",
        "window": {
            "start_date": evaluation_dates[0],
            "end_date": evaluation_dates[-1],
            "session_count": len(evaluation_dates),
        },
        "input_bindings": input_bindings,
        "environment_binding": {
            "code_fingerprint": environment["code_fingerprint"],
            "environment_lock_digest_sha256": environment["environment_lock_digest_sha256"],
        },
        "protocol": metrics["protocol"],
        "feature_summary": metrics["summary"],
        "pairwise_collinearity": metrics["pairwise_collinearity"],
        "daily": metrics["daily"],
    }
    report["generation_id"] = sha256_json(report)
    validate_contract("factor_screen_report.v1.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True,
                        help="owning root for factors/ and backtest_market_data/")
    parser.add_argument("--project-root", type=Path, default=Path.cwd(),
                        help="checkout that contains the reviewed config/factor-sets registry")
    parser.add_argument("--label-root", type=Path, required=True)
    parser.add_argument("--factor-generations-json", type=Path, required=True,
                        help="JSON array with one reviewed factor generation per session")
    parser.add_argument("--label-generation", required=True)
    parser.add_argument("--calendar-generation", required=True)
    parser.add_argument("--calendar-checksum", required=True)
    parser.add_argument("--backtest-config-generation",
                        help="reviewed v3 config used only for a bounded turnover cost proxy")
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--top-n", type=int, default=30)
    parser.add_argument("--correlation-threshold", type=float, default=0.90)
    parser.add_argument("--hac-lags", type=int)
    parser.add_argument("--output-json", type=Path, required=True,
                        help="new path; an existing report is never overwritten")
    args = parser.parse_args(argv)
    try:
        if args.start > args.end:
            raise ContractError("factor screen start must not follow end")
        report = build_report(
            data_root=args.data_root,
            label_root=args.label_root,
            project_root=args.project_root,
            factor_generations=_read_factor_generations(args.factor_generations_json),
            label_generation=args.label_generation,
            calendar_generation=args.calendar_generation,
            calendar_checksum=args.calendar_checksum,
            start=args.start.isoformat(),
            end=args.end.isoformat(),
            top_n=args.top_n,
            correlation_threshold=args.correlation_threshold,
            hac_lags=args.hac_lags,
            backtest_config_generation=args.backtest_config_generation,
        )
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        with args.output_json.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    except (ContractError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "factor_screen_rejected", "errors": [str(exc)]}, sort_keys=True))
        return 2
    print(json.dumps({
        "status": "factor_screen_ready",
        "generation_id": report["generation_id"],
        "output": str(args.output_json),
        "features": len(report["feature_summary"]),
        "high_collinearity_pairs": sum(
            item["high_collinearity"] for item in report["pairwise_collinearity"]
        ),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
