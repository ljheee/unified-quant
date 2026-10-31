"""Validation diagnostics over checksum-verified Tushare candidate bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from ..contracts.gate_contracts import sha256_json, validate_contract
from ..errors import ContractError
from ..research_chain.provenance import capture_environment
from .factor_screening import screen_factor_panel
from .metrics import session_dates


def build_tushare_candidate_screen_report(
    *,
    candidate_manifest: dict[str, Any],
    factor_panel: pd.DataFrame,
    label_manifest: dict[str, Any],
    labels: pd.DataFrame,
    project_root: Path | str,
    top_n: int = 30,
    correlation_threshold: float = 0.90,
    hac_lags: int | None = None,
    cost_proxy_bps_per_one_way_turnover: float | None = None,
    backtest_cost_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute diagnostics and bind the raw-derived bundle and label generation."""
    validate_contract("tushare_factor_candidate.v1.json", candidate_manifest)
    factor_generation = candidate_manifest["generation_id"]
    if sha256_json({key: value for key, value in candidate_manifest.items()
                    if key != "generation_id"}) != factor_generation:
        raise ContractError("candidate bundle content identity mismatch")
    if (not isinstance(factor_panel, pd.DataFrame) or factor_panel.empty
            or factor_panel.columns.tolist() != [
                "instrument", "decision_date", *candidate_manifest["factor_columns"]
            ] or len(factor_panel) != candidate_manifest["factor_artifact"]["row_count"]):
        raise ContractError("candidate factor frame does not match its manifest")
    dates = session_dates(factor_panel["decision_date"].tolist(), label="candidate screen calendar")
    dates = sorted(set(dates))
    calendar_sessions = candidate_manifest["calendar_sessions"]
    declared_calendar = [entry["session_date"] for entry in calendar_sessions]
    if declared_calendar != sorted(set(declared_calendar)):
        raise ContractError("candidate calendar bindings must be sorted and unique")
    if not set(dates) <= set(declared_calendar):
        raise ContractError("candidate factor dates lie outside its bound canonical calendar")
    horizon = label_manifest.get("horizon_trading_days")
    if type(horizon) is not int or horizon < 1:
        raise ContractError("label manifest has an invalid horizon")
    if (cost_proxy_bps_per_one_way_turnover is None) != (backtest_cost_binding is None):
        raise ContractError("turnover cost proxy must be bound to its reviewed config or omitted")
    if backtest_cost_binding is None:
        backtest_cost_binding = None
    else:
        if (isinstance(cost_proxy_bps_per_one_way_turnover, bool)
                or not isinstance(cost_proxy_bps_per_one_way_turnover, (int, float))
                or cost_proxy_bps_per_one_way_turnover < 0):
            raise ContractError("reviewed config turnover cost proxy must be non-negative")
        backtest_cost_binding = dict(backtest_cost_binding) | {
            "cost_proxy_bps_per_one_way_turnover": float(cost_proxy_bps_per_one_way_turnover)
        }
    label_fields = {
        "generation_id": label_manifest.get("generation_id"),
        "manifest_digest_sha256": label_manifest.get("manifest_digest_sha256"),
        "data_checksum_sha256": label_manifest.get("data_checksum_sha256"),
        "horizon_sessions": horizon,
        "formula_sha256": label_manifest.get("formula_sha256"),
    }
    if any(not isinstance(label_fields[key], str) or len(label_fields[key]) != 64
           for key in ("generation_id", "manifest_digest_sha256", "data_checksum_sha256", "formula_sha256")):
        raise ContractError("label manifest identity is incomplete")
    metrics = screen_factor_panel(
        factor_panel,
        labels,
        features=candidate_manifest["factor_columns"],
        calendar_dates=dates,
        horizon_sessions=horizon,
        top_n=top_n,
        correlation_threshold=correlation_threshold,
        hac_lags=hac_lags,
        cost_proxy_bps_per_one_way_turnover=cost_proxy_bps_per_one_way_turnover,
    )
    environment = capture_environment(project_root)
    bundle_binding = {
        "generation_id": factor_generation,
        "factor_checksum_sha256": candidate_manifest["factor_artifact"]["checksum_sha256"],
        "exposure_checksum_sha256": candidate_manifest["exposure_artifact"]["checksum_sha256"],
        "source_lineage_sha256": sha256_json(candidate_manifest["source_snapshots"]),
    }
    calendar_binding = {
        "session_dates": declared_calendar,
        "binding_sha256": sha256_json(calendar_sessions),
    }
    report: dict[str, Any] = {
        "record_version": 1,
        "kind": "tushare_factor_candidate_screen_report",
        "publication_scope": "diagnostic_only_no_factor_selection",
        "strict_pit_eligible": False,
        "historical_vintage_verified": False,
        "window": {
            "start_date": dates[0],
            "end_date": dates[-1],
            "session_count": len(dates),
        },
        "input_bindings": {
            "candidate_bundle": bundle_binding,
            "label": label_fields,
            "calendar": calendar_binding,
            "backtest_cost_config": backtest_cost_binding,
        },
        "environment_binding": {
            "code_fingerprint": environment["code_fingerprint"],
            "environment_lock_digest_sha256": environment["environment_lock_digest_sha256"],
        },
        "protocol": metrics["protocol"] | {
            "availability_policy": candidate_manifest["availability_policy"],
            "research_source_strict_pit_eligible": False,
        },
        "feature_summary": metrics["summary"],
        "pairwise_collinearity": metrics["pairwise_collinearity"],
        "daily": metrics["daily"],
    }
    report["generation_id"] = sha256_json(report)
    validate_contract("tushare_factor_candidate_screen_report.v1.json", report)
    return report
