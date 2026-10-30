from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from uq.contracts.gate_contracts import sha256_json
from uq.errors import ContractError
from uq.evaluation.factor_screening import screen_factor_panel
from uq.research_chain.owning_contracts import BacktestMarketDatasetStore


def factor_inputs():
    dates = pd.bdate_range("2025-01-06", periods=4).strftime("%Y-%m-%d").tolist()
    rows = []
    labels = []
    values_by_day = [
        [1., 2., 3., 4.],
        [2., 3., 4., 1.],
        [3., 4., 1., 2.],
        [4., 1., 2., 3.],
    ]
    for day, values in zip(dates, values_by_day, strict=True):
        for instrument, value in zip("ABCD", values, strict=True):
            rows.append({
                "decision_date": day,
                "instrument": instrument,
                "momentum": value,
                "momentum_copy": value * 2 + 1,
                "reversal": -value,
            })
            labels.append({"decision_date": day, "instrument": instrument, "label": value})
    return dates, pd.DataFrame(rows), pd.DataFrame(labels)


def test_factor_screen_reports_hac_rank_ic_pairwise_collinearity_and_turnover_proxy():
    dates, panel, labels = factor_inputs()
    result = screen_factor_panel(
        panel,
        labels,
        features=["momentum", "momentum_copy", "reversal"],
        calendar_dates=dates,
        horizon_sessions=2,
        top_n=2,
        correlation_threshold=.9,
    )
    summaries = {row["feature"]: row for row in result["summary"]}
    assert summaries["momentum"]["mean_rank_ic"] == pytest.approx(1.)
    assert summaries["reversal"]["mean_rank_ic"] == pytest.approx(-1.)
    assert summaries["momentum"]["valid_rank_ic_sessions"] == 4
    assert summaries["momentum"]["positive_rank_ic_fraction"] == 1.
    assert summaries["momentum"]["rank_ic_t_statistic"] is None
    assert summaries["momentum"]["mean_top_n_one_way_turnover_proxy"] == pytest.approx(.5)
    assert summaries["momentum"]["turnover_transitions"] == 3
    pairs = {(row["feature_left"], row["feature_right"]): row
             for row in result["pairwise_collinearity"]}
    assert pairs[("momentum", "momentum_copy")]["high_collinearity"]
    assert pairs[("momentum", "reversal")]["mean_absolute_daily_spearman"] == pytest.approx(1.)
    assert result["protocol"]["top_n_turnover_definition"].endswith("diagnostic_proxy_only")
    assert len(result["daily"]) == len(dates) * 3


def test_missing_feature_session_stays_in_calendar_and_breaks_turnover_transition():
    dates, panel, labels = factor_inputs()
    panel.loc[panel["decision_date"] == dates[1], "momentum"] = np.nan
    result = screen_factor_panel(
        panel,
        labels,
        features=["momentum"],
        calendar_dates=dates,
        horizon_sessions=2,
        top_n=2,
    )
    rows = result["daily"]
    assert [row["decision_date"] for row in rows] == dates
    assert rows[1]["ic_status"] == "insufficient_pairs"
    assert rows[2]["turnover_status"] == "first_session"
    assert rows[2]["top_n_one_way_turnover_proxy"] is None
    assert result["summary"][0]["feature_coverage_sessions"] == 3


def test_turnover_cost_proxy_uses_one_way_turnover_and_is_explicitly_diagnostic():
    dates, panel, labels = factor_inputs()
    result = screen_factor_panel(
        panel,
        labels,
        features=["momentum"],
        calendar_dates=dates,
        horizon_sessions=2,
        top_n=2,
        cost_proxy_bps_per_one_way_turnover=20.0,
    )
    transition = result["daily"][1]
    assert transition["top_n_one_way_turnover_proxy"] == pytest.approx(.5)
    assert transition["turnover_cost_proxy_bps_of_nav"] == pytest.approx(10.)
    assert result["summary"][0]["mean_turnover_cost_proxy_bps_of_nav"] == pytest.approx(10.)
    assert "ignores_minimum_fees_fills_and_constraints" in result["protocol"]["turnover_cost_proxy_definition"]


@pytest.mark.parametrize("value", [-1.0, np.inf, True])
def test_factor_screen_rejects_invalid_turnover_cost_proxy(value):
    dates, panel, labels = factor_inputs()
    with pytest.raises(ContractError, match="turnover cost proxy"):
        screen_factor_panel(
            panel,
            labels,
            features=["momentum"],
            calendar_dates=dates,
            horizon_sessions=2,
            cost_proxy_bps_per_one_way_turnover=value,
        )


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda dates, panel, labels: (dates, pd.concat([panel, panel.iloc[:1]]), labels), "duplicate"),
        (lambda dates, panel, labels: (dates, panel.assign(momentum=np.inf), labels), "non-finite"),
        (lambda dates, panel, labels: (dates, panel, pd.concat([labels, labels.iloc[:1]])), "duplicate"),
        (lambda dates, panel, labels: (dates, panel, labels), "HAC lags"),
    ],
)
def test_factor_screen_rejects_duplicate_nonfinite_and_under_purged_inputs(mutate, match):
    dates, panel, labels = mutate(*factor_inputs())
    options = {"hac_lags": 0} if match == "HAC lags" else {}
    with pytest.raises(ContractError, match=match):
        screen_factor_panel(
            panel,
            labels,
            features=["momentum"],
            calendar_dates=dates,
            horizon_sessions=2,
            **options,
        )


def test_factor_screen_refuses_bad_calendar_and_invalid_parameters():
    dates, panel, labels = factor_inputs()
    with pytest.raises(ContractError, match="calendar"):
        screen_factor_panel(
            panel,
            labels,
            features=["momentum"],
            calendar_dates=dates[::-1],
            horizon_sessions=2,
        )
    with pytest.raises(ContractError, match="correlation threshold"):
        screen_factor_panel(
            panel,
            labels,
            features=["momentum"],
            calendar_dates=dates,
            horizon_sessions=2,
            correlation_threshold=0,
        )


def test_screen_report_binds_reviewed_factor_label_calendar_and_environment(monkeypatch, tmp_path):
    from scripts import screen_factors

    dates, panel, labels = factor_inputs()
    calendar = pd.DataFrame({
        "date": dates,
        "instrument": ["calendar"] * len(dates),
    })
    calendar_store = BacktestMarketDatasetStore(tmp_path / "data")
    calendar_manifest = calendar_store.publish(
        calendar,
        dataset_type="calendar",
        window_start=pd.Timestamp(dates[0]).date(),
        window_end=pd.Timestamp(dates[-1]).date(),
        sources={"calendar": "isolated-test-fixture"},
        producer="tests/test_factor_screening.py",
    )
    generations = [f"{index + 1:064x}" for index in range(len(dates))]
    factor_records = {}
    for day, generation in zip(dates, generations, strict=True):
        frame = panel.loc[panel["decision_date"] == day].drop(columns="decision_date").copy()
        frame["datetime"] = day
        frame = frame[["instrument", "datetime", "momentum", "momentum_copy", "reversal"]]
        factor_records[generation] = ({
            "generation_id": generation,
            "manifest_digest_sha256": "a" * 64,
            "data_checksum_sha256": "b" * 64,
            "partition_date": day,
            "factor_set": "basic",
            "factor_version": "1.0.0",
            "input_dataset": "bars_daily",
            "input_schema_version": "v2",
            "columns": list(frame.columns),
        }, frame)

    class FakeFactorStore:
        def __init__(self, _root, _registry):
            pass

        def read_partition(self, generation):
            return factor_records[generation]

    label_manifest = {
        "generation_id": "c" * 64,
        "manifest_digest_sha256": "d" * 64,
        "data_checksum_sha256": "e" * 64,
        "horizon_trading_days": 2,
        "formula_sha256": "f" * 64,
    }

    class FakeLabelStore:
        def __init__(self, _root):
            pass

        def read_frame(self, _generation):
            return label_manifest, labels

    monkeypatch.setattr(screen_factors, "FactorRegistry", lambda _root: object())
    monkeypatch.setattr(screen_factors, "FactorStore", FakeFactorStore)
    monkeypatch.setattr(screen_factors, "LabelStore", FakeLabelStore)
    monkeypatch.setattr(screen_factors, "capture_environment", lambda _root: {
        "code_fingerprint": "1" * 64,
        "environment_lock_digest_sha256": "2" * 64,
    })
    report = screen_factors.build_report(
        data_root=tmp_path / "data",
        label_root=tmp_path / "labels",
        project_root=tmp_path,
        factor_generations=generations,
        label_generation="c" * 64,
        calendar_generation=calendar_manifest["generation_id"],
        calendar_checksum=calendar_manifest["data_checksum_sha256"],
        start=dates[0],
        end=dates[-1],
        top_n=2,
        correlation_threshold=.9,
        hac_lags=None,
    )
    assert report["publication_scope"] == "diagnostic_only_no_factor_selection"
    assert report["input_bindings"]["calendar"]["manifest_digest_sha256"] == calendar_manifest["manifest_digest_sha256"]
    assert len(report["input_bindings"]["factor_partitions"]) == len(dates)
    assert report["environment_binding"]["code_fingerprint"] == "1" * 64
    assert report["input_bindings"]["backtest_cost_config"] is None
    assert report["daily"][1]["turnover_cost_proxy_bps_of_nav"] is None
    assert report["generation_id"] == sha256_json({key: value for key, value in report.items()
                                                     if key != "generation_id"})

    from tests.test_backtest_v3 import inputs as backtest_inputs

    config, _, _ = backtest_inputs(tmp_path / "data")
    cost_report = screen_factors.build_report(
        data_root=tmp_path / "data",
        label_root=tmp_path / "labels",
        project_root=tmp_path,
        factor_generations=generations[:3],
        label_generation="c" * 64,
        calendar_generation=calendar_manifest["generation_id"],
        calendar_checksum=calendar_manifest["data_checksum_sha256"],
        start=dates[0],
        end=dates[2],
        top_n=2,
        correlation_threshold=.9,
        hac_lags=None,
        backtest_config_generation=config["generation_id"],
    )
    assert cost_report["input_bindings"]["backtest_cost_config"]["generation_id"] == config["generation_id"]
    cost_transition = next(row for row in cost_report["daily"]
                           if row["feature"] == "momentum" and row["decision_date"] == dates[1])
    assert cost_transition["turnover_cost_proxy_bps_of_nav"] == pytest.approx(10.)

    no_fill_config, _, _ = backtest_inputs(
        tmp_path / "no-fill-data", execution={"fill_rate_multiplier": 0.}
    )
    with pytest.raises(ContractError, match="fill rate is zero"):
        screen_factors._cost_proxy_binding(
            tmp_path / "no-fill-data",
            no_fill_config["generation_id"],
            start=dates[0],
            end=dates[2],
        )
