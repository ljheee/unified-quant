from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.contracts.gate_contracts import sha256_bytes, sha256_json
from uq.contracts.gate_contracts import sha256_json, validate_contract
from uq.contracts.tushare_dependency import TushareDependencyStore
from uq.errors import ContractError
from uq.evaluation.factor_screening import select_factor_candidates
from uq.evaluation import tushare_factor_screening
from uq.evaluation.tushare_factor_screening import build_tushare_candidate_screen_report
from uq.models.tushare_factor_candidates import (
    TushareFactorCandidateStore,
    build_tushare_factor_candidates,
)


def _calendar_partition(root, day: date) -> None:
    directory = root / "canonical" / "bars_daily" / "v2" / f"date={day.isoformat()}"
    directory.mkdir(parents=True)
    frame = pd.DataFrame({
        "instrument": ["000001.XSHE"],
        "session_date": [pd.Timestamp(day)],
        "close": [10.0],
    })
    path = directory / "data.parquet"
    frame.to_parquet(path, index=False)
    checksum = file_sha256_bytes(path.read_bytes())
    manifest = {
        "manifest_version": 1,
        "dataset": "bars_daily",
        "schema_version": "v2",
        "partition_date": day.isoformat(),
        "row_count": len(frame),
        "columns": frame.columns.tolist(),
        "dtypes": {key: str(value) for key, value in frame.dtypes.items()},
        "data_checksum_sha256": checksum,
        "schema_checksum_sha256": "a" * 64,
        "code_fingerprint": "b" * 64,
        "run_id": "00000000-0000-4000-8000-000000000001",
        "created_at": "2024-01-03T00:00:00+00:00",
        "lineage": {},
        "source_versions": {},
        "quality_report_checksum": "",
        "raw_artifacts": {},
    }
    generation = sha256_json(manifest)
    manifest["generation_id"] = generation
    manifest["trust_anchor_sha256"] = sha256_bytes(generation.encode("ascii"))
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _daily_basic(day: str) -> pd.DataFrame:
    return pd.DataFrame({
        "ts_code": ["000001.SZ", "600000.SH"],
        "trade_date": [day, day],
        "pe_ttm": [10.0, -2.0],
        "pb": [2.0, 1.0],
        "ps_ttm": [5.0, 4.0],
        "dv_ttm": [2.5, 0.0],
        "turnover_rate_f": [1.5, 0.0],
        "volume_ratio": [1.2, 0.8],
        "total_mv": [1000.0, 2000.0],
    })


def _moneyflow(day: str) -> pd.DataFrame:
    return pd.DataFrame({
        "ts_code": ["000001.SZ", "600000.SH"],
        "trade_date": [day, day],
        "net_mf_amount": [10.0, -20.0],
        "buy_lg_amount": [6.0, 5.0],
        "buy_elg_amount": [4.0, 5.0],
        "sell_lg_amount": [3.0, 8.0],
        "sell_elg_amount": [2.0, 7.0],
    })


def _raw_store(root, day: str, *, include_flow: bool = True) -> None:
    store = TushareDependencyStore(root)
    fetched = datetime(2026, 10, 31, tzinfo=timezone.utc)
    store.publish(
        "daily_basic", query={"trade_date": day}, source_fetched_at=fetched,
        frame=_daily_basic(day),
    )
    if include_flow:
        store.publish(
            "moneyflow", query={"trade_date": day}, source_fetched_at=fetched,
            frame=_moneyflow(day),
        )


def test_build_candidate_bundle_lags_to_next_calendar_session_and_keeps_raw_lineage(tmp_path):
    bars_root = tmp_path / "bars"
    first, decision = date(2024, 1, 2), date(2024, 1, 3)
    _calendar_partition(bars_root, first)
    _calendar_partition(bars_root, decision)
    source_root = tmp_path / "source"
    _raw_store(source_root, "20240102")

    bundle = build_tushare_factor_candidates(
        source_root=source_root,
        bars_root=bars_root,
        start_date=decision,
        end_date=decision,
    )

    assert set(bundle.factors["decision_date"]) == {"2024-01-03"}
    assert set(bundle.factors["instrument"]) == {"000001.XSHE", "600000.XSHG"}
    first_stock = bundle.factors.set_index("instrument").loc["000001.XSHE"]
    assert first_stock["earnings_yield_ttm"] == pytest.approx(0.1)
    assert first_stock["book_to_price"] == pytest.approx(0.5)
    assert first_stock["dividend_yield_ttm"] == pytest.approx(0.025)
    assert first_stock["moneyflow_net_to_market_cap"] == pytest.approx(0.01)
    assert pd.isna(bundle.factors.set_index("instrument").loc["600000.XSHG", "earnings_yield_ttm"])
    assert set(bundle.exposures["datetime"]) == {"2024-01-03"}
    assert bundle.exposures.iloc[0]["available_at"] == "2024-01-03T07:00:00+00:00"
    assert bundle.manifest["strict_pit_eligible"] is False
    assert bundle.manifest["historical_vintage_verified"] is False
    assert {entry["trade_date"] for entry in bundle.manifest["source_snapshots"]} == {"2024-01-02"}
    assert {entry["dataset"] for entry in bundle.manifest["source_snapshots"]} == {"daily_basic", "moneyflow"}

    output = TushareFactorCandidateStore(tmp_path / "candidate-store")
    first_manifest = output.publish(bundle)
    assert output.publish(bundle) == first_manifest
    readback = output.read(
        first_manifest["generation_id"],
        source_root=source_root,
        bars_root=bars_root,
    )
    assert readback["manifest"] == first_manifest
    validate_contract("tushare_factor_candidate.v1.json", first_manifest)


def test_build_candidate_bundle_rejects_missing_prior_session_source(tmp_path):
    bars_root = tmp_path / "bars"
    first, decision = date(2024, 1, 2), date(2024, 1, 3)
    _calendar_partition(bars_root, first)
    _calendar_partition(bars_root, decision)
    _raw_store(tmp_path / "source", "20240102", include_flow=False)

    with pytest.raises(ContractError, match="moneyflow lacks exact raw snapshots"):
        build_tushare_factor_candidates(
            source_root=tmp_path / "source",
            bars_root=bars_root,
            start_date=decision,
            end_date=decision,
        )


def _screen_report():
    report = {
        "window": {"start_date": "2024-01-01", "end_date": "2024-02-25", "session_count": 40},
        "feature_summary": [
            {
                "feature": name,
                "valid_rank_ic_sessions": 40,
                "mean_rank_ic": mean,
                "rank_ic_t_statistic": t_stat,
                "positive_rank_ic_fraction": positive,
                "feature_coverage_sessions": 40,
                "mean_turnover_cost_proxy_bps_of_nav": 5.0,
            }
            for name, mean, t_stat, positive in (
                ("a", 0.10, 3.0, 0.70),
                ("b", 0.09, 2.0, 0.65),
                ("c", -0.10, -3.0, 0.30),
            )
        ],
        "pairwise_collinearity": [
            {"feature_left": "a", "feature_right": "b", "valid_sessions": 35,
             "mean_absolute_daily_spearman": 0.95},
            {"feature_left": "a", "feature_right": "c", "valid_sessions": 35,
             "mean_absolute_daily_spearman": 0.20},
            {"feature_left": "b", "feature_right": "c", "valid_sessions": 35,
             "mean_absolute_daily_spearman": 0.25},
        ],
    }
    report["generation_id"] = sha256_json(report)
    return report


def _selection_policy():
    return {
        "protocol_version": 1,
        "score_directions": {"a": 1, "b": 1, "c": -1},
        "minimum_valid_rank_ic_sessions": 30,
        "minimum_rank_ic_t_statistic": 1.0,
        "minimum_positive_rank_ic_fraction": 0.6,
        "minimum_coverage_fraction": 0.8,
        "minimum_pairwise_sessions": 30,
        "maximum_absolute_daily_spearman": 0.9,
        "maximum_mean_turnover_cost_bps": 10.0,
        "maximum_selected_features": 2,
    }


def test_factor_selection_is_configured_directional_and_deduplicates_collinear_candidates():
    result = select_factor_candidates(_screen_report(), policy=_selection_policy())
    assert result["selected_candidates"] == ["a", "c"]
    decisions = {item["feature"]: item for item in result["decisions"]}
    assert decisions["b"]["status"] == "deprioritized"
    assert decisions["b"]["reasons"] == ["collinear_with_higher_priority:a"]
    assert decisions["c"]["adjusted_mean_rank_ic"] == pytest.approx(0.1)
    assert result["publication_scope"] == "validation_only_shortlist_no_factor_promotion"
    validate_contract("factor_selection_shortlist.v1.json", result)


def test_factor_selection_blocks_candidates_when_pairwise_history_is_missing():
    report = _screen_report()
    report["pairwise_collinearity"][0]["valid_sessions"] = 2
    report["generation_id"] = sha256_json({
        key: value for key, value in report.items() if key != "generation_id"
    })
    result = select_factor_candidates(report, policy=_selection_policy())
    assert "a" not in result["selected_candidates"]
    assert "b" not in result["selected_candidates"]
    decisions = {item["feature"]: item for item in result["decisions"]}
    assert "pairwise_collinearity_unmeasured" in decisions["a"]["reasons"]
    assert "pairwise_collinearity_unmeasured" in decisions["b"]["reasons"]


def test_candidate_screen_report_binds_bundle_labels_calendar_and_environment(tmp_path, monkeypatch):
    dates = ["2024-01-03", "2024-01-04", "2024-01-05"]
    factors = pd.DataFrame([
        {
            "instrument": instrument,
            "decision_date": day,
            "candidate_a": float(index + day_index),
            "candidate_b": float((index % 2) + 2 * day_index),
        }
        for day_index, day in enumerate(dates)
        for index, instrument in enumerate(["000001.XSHE", "000002.XSHE", "600000.XSHG", "600001.XSHG"])
    ])
    factor_artifact = {
        "filename": "factors.parquet",
        "checksum_sha256": "1" * 64,
        "row_count": len(factors),
        "columns": factors.columns.tolist(),
    }
    exposure_artifact = {
        "filename": "exposures.parquet",
        "checksum_sha256": "2" * 64,
        "row_count": len(factors),
        "columns": ["instrument", "datetime", "market_cap", "liquidity", "available_at"],
    }
    candidate_manifest = {
        "contract_version": 1,
        "kind": "tushare_factor_candidate_bundle",
        "availability_policy": "prior_session_data_visible_by_decision_session_close_assumption_v1",
        "strict_pit_eligible": False,
        "historical_vintage_verified": False,
        "builder_code_sha256": "8" * 64,
        "dependency_lock_sha256": "9" * 64,
        "factor_definitions": [
            {"name": name, "version": "1.0.0",
             "sources": [{"dataset": "daily_basic", "fields": [name]}],
             "formula": "test-only formula"}
            for name in ["candidate_a", "candidate_b"]
        ],
        "exposure_definitions": [
            {"control": "market_cap", "source_dataset": "daily_basic",
             "source_field": "total_mv", "units": "10k CNY"},
            {"control": "liquidity", "source_dataset": "daily_basic",
             "source_field": "turnover_rate_f", "units": "percent"},
        ],
        "calendar_sessions": [
            {"session_date": day, "manifest_generation_id": str(i + 1) * 64,
             "data_checksum_sha256": str(i + 4) * 64}
            for i, day in enumerate(dates)
        ],
        "source_snapshots": [
            {"dataset": dataset, "trade_date": "2024-01-02", "generation_id": str(i + 5) * 64,
             "data_checksum_sha256": str(i + 7) * 64,
             "source_fetched_at": "2026-10-31T00:00:00+00:00", "row_count": 4}
            for i, dataset in enumerate(["daily_basic", "moneyflow"])
        ],
        "factor_columns": ["candidate_a", "candidate_b"],
        "exposure_controls": ["market_cap", "liquidity"],
        "factor_artifact": factor_artifact,
        "exposure_artifact": exposure_artifact,
    }
    candidate_manifest["generation_id"] = sha256_json(candidate_manifest)
    labels = pd.DataFrame([
        {"instrument": row.instrument, "decision_date": row.decision_date,
         "label": row.candidate_a * 0.01}
        for row in factors.itertuples()
    ])
    label_manifest = {
        "generation_id": "a" * 64,
        "manifest_digest_sha256": "b" * 64,
        "data_checksum_sha256": "c" * 64,
        "formula_sha256": "d" * 64,
        "horizon_trading_days": 1,
    }
    monkeypatch.setattr(tushare_factor_screening, "capture_environment", lambda _root: {
        "code_fingerprint": "e" * 64,
        "environment_lock_digest_sha256": "f" * 64,
    })

    report = build_tushare_candidate_screen_report(
        candidate_manifest=candidate_manifest,
        factor_panel=factors,
        label_manifest=label_manifest,
        labels=labels,
        project_root=tmp_path,
        top_n=2,
    )

    assert report["kind"] == "tushare_factor_candidate_screen_report"
    assert report["strict_pit_eligible"] is False
    assert report["input_bindings"]["candidate_bundle"]["generation_id"] == candidate_manifest["generation_id"]
    assert report["input_bindings"]["label"]["generation_id"] == label_manifest["generation_id"]
    validate_contract("tushare_factor_candidate_screen_report.v1.json", report)
    shortlist = select_factor_candidates(report, policy={
        **_selection_policy(),
        "score_directions": {"candidate_a": 1, "candidate_b": 1},
        "minimum_valid_rank_ic_sessions": 1,
        "minimum_pairwise_sessions": 1,
        "maximum_mean_turnover_cost_bps": None,
        "maximum_selected_features": 1,
    })
    assert shortlist["screen_report_generation_id"] == report["generation_id"]
