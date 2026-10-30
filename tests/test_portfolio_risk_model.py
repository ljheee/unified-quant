from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from uq.contracts.model_layer import ModelContractLoader, research_contract_identities
from uq.errors import ContractError
from uq.portfolio.builder import PortfolioBuilder
from uq.portfolio.risk_model import (
    PortfolioRiskReturnsStore,
    covariance_for_decision,
    risk_parity_weights,
)


ASSET_A = "000001.XSHG"
ASSET_B = "000002.XSHE"
SESSIONS = [f"2026-01-0{day}" for day in range(1, 6)]


def _returns() -> pd.DataFrame:
    rows = []
    values = {
        ASSET_A: [-0.01, 0.0, 0.01, 0.0, 0.9],
        ASSET_B: [0.0, -0.02, 0.0, 0.02, -0.9],
    }
    for instrument, series in values.items():
        rows.extend(
            {"date": session, "instrument": instrument, "return": value}
            for session, value in zip(SESSIONS, series, strict=True)
        )
    return pd.DataFrame(rows)


def test_equal_risk_contribution_weights_use_only_prior_sessions() -> None:
    covariance = covariance_for_decision(
        _returns(), calendar_sessions=SESSIONS, decision_date=SESSIONS[-1],
        instruments=[ASSET_A, ASSET_B], lookback_sessions=4,
    )
    weights = risk_parity_weights(covariance)
    contributions = weights * (covariance.to_numpy() @ weights)
    assert weights == pytest.approx([2 / 3, 1 / 3], rel=1e-8)
    assert contributions / contributions.sum() == pytest.approx([0.5, 0.5], abs=1e-9)


def test_risk_parity_solver_supports_correlated_positive_definite_covariance() -> None:
    covariance = np.array([[0.04, 0.01, 0.005], [0.01, 0.02, 0.004], [0.005, 0.004, 0.01]])
    weights = risk_parity_weights(covariance)
    contributions = weights * (covariance @ weights)
    assert weights.sum() == pytest.approx(1.0)
    assert contributions / contributions.sum() == pytest.approx(
        np.full(3, 1 / 3), abs=1e-8
    )


@pytest.mark.parametrize(
    "covariance",
    [
        np.array([[1.0, 2.0], [2.0, 1.0]]),
        np.array([[1.0, 0.1], [0.2, 1.0]]),
        np.array([[1.0, np.nan], [np.nan, 1.0]]),
    ],
)
def test_risk_parity_rejects_invalid_covariance(covariance: np.ndarray) -> None:
    with pytest.raises(ContractError):
        risk_parity_weights(covariance)


def test_covariance_rejects_missing_selected_asset_returns() -> None:
    frame = _returns()
    frame = frame[~((frame["date"] == SESSIONS[1]) & (frame["instrument"] == ASSET_B))]
    with pytest.raises(ContractError, match="incomplete"):
        covariance_for_decision(
            frame, calendar_sessions=SESSIONS, decision_date=SESSIONS[-1],
            instruments=[ASSET_A, ASSET_B], lookback_sessions=4,
        )


def test_portfolio_builder_routes_v3_risk_parity_and_fails_closed_without_returns(
    tmp_path: Path,
) -> None:
    store = PortfolioRiskReturnsStore(tmp_path)
    risk_manifest = store.publish(
        _returns(), calendar_sessions=SESSIONS,
        calendar_generation_id="1" * 64,
        calendar_manifest_digest_sha256="2" * 64,
        calendar_data_checksum_sha256="3" * 64,
        source_id="offline-test-panel",
        source_approval_ref="reviewed-test-input",
    )
    risk_manifest, risk_frame = store.read(risk_manifest["generation_id"])
    definition = {
        "contract_version": 3,
        "weight_scheme": "risk_parity",
        "scheme_parameters": {"n": 2, "lookback_sessions": 4},
        "score_policy": {"direction": "descending", "nan_policy": "exclude"},
        "constraints": {"max_single_weight": 1.0, "cash_reserve": 0.0},
        "generation_id": "a" * 64,
        "universe_snapshot_generation_id": "b" * 64,
        "risk_model_binding": {
            "family": "portfolio_risk_returns_v1",
            "generation_id": risk_manifest["generation_id"],
            "manifest_digest_sha256": risk_manifest["manifest_digest_sha256"],
        },
    }
    builder = PortfolioBuilder("unused-test-store")
    kwargs = {
        "definition": definition,
        "prediction_generation_id": "c" * 64,
        "decision_date": SESSIONS[-1],
        "scores": pd.Series({ASSET_A: 2.0, ASSET_B: 1.0}),
        "universe_instruments": [ASSET_A, ASSET_B],
    }
    with pytest.raises(ContractError, match="governed daily returns"):
        builder.build(**kwargs)
    with pytest.raises(ContractError, match="finite selected scores"):
        builder.build(**{**kwargs, "scores": pd.Series({ASSET_A: np.inf, ASSET_B: 1.0})})
    _, frame = builder.build(
        **kwargs,
        risk_return_frame=risk_frame,
        risk_return_calendar_sessions=SESSIONS,
        risk_return_manifest=risk_manifest,
    )
    weights = frame.set_index("instrument")["weight"]
    assert weights[ASSET_A] == pytest.approx(2 / 3, rel=1e-8)
    assert weights[ASSET_B] == pytest.approx(1 / 3, rel=1e-8)


def test_industry_neutral_equalizes_sector_budgets_and_applies_a_sector_cap() -> None:
    definition = {
        "contract_version": 3,
        "weight_scheme": "industry_neutral",
        "scheme_parameters": {"n": 3},
        "score_policy": {"direction": "descending", "nan_policy": "exclude"},
        "constraints": {
            "max_single_weight": 1.0,
            "max_industry_weight": 0.4,
            "cash_reserve": 0.0,
        },
        "industry_source_binding": {
            "source_type": "governed_execution_facts",
            "manifest_generation_id": "f" * 64,
            "industry_column": "industry",
        },
        "generation_id": "a" * 64,
        "universe_snapshot_generation_id": "b" * 64,
    }
    third_asset = "000003.XSHG"
    _, frame = PortfolioBuilder("unused-test-store").build(
        definition=definition,
        prediction_generation_id="c" * 64,
        decision_date=SESSIONS[-1],
        scores=pd.Series({ASSET_A: 3.0, ASSET_B: 2.0, third_asset: 1.0}),
        universe_instruments=[ASSET_A, ASSET_B, third_asset],
        industry_by_instrument={
            ASSET_A: "technology", ASSET_B: "technology", third_asset: "healthcare",
        },
    )
    weights = frame.set_index("instrument")["weight"]
    assert weights[ASSET_A] == pytest.approx(0.2)
    assert weights[ASSET_B] == pytest.approx(0.2)
    assert weights[third_asset] == pytest.approx(0.4)

    with pytest.raises(ContractError, match="point-in-time industry"):
        PortfolioBuilder("unused-test-store").build(
            definition=definition,
            prediction_generation_id="c" * 64,
            decision_date=SESSIONS[-1],
            scores=pd.Series({ASSET_A: 3.0, ASSET_B: 2.0, third_asset: 1.0}),
            universe_instruments=[ASSET_A, ASSET_B, third_asset],
        )


def test_risk_returns_store_round_trips_and_detects_tampering(tmp_path: Path) -> None:
    store = PortfolioRiskReturnsStore(tmp_path)
    manifest = store.publish(
        _returns(), calendar_sessions=SESSIONS,
        calendar_generation_id="1" * 64,
        calendar_manifest_digest_sha256="2" * 64,
        calendar_data_checksum_sha256="3" * 64,
        source_id="offline-reviewed-panel",
        source_approval_ref="review-2026-001",
    )
    read_manifest, frame = store.read(manifest["generation_id"])
    assert read_manifest == manifest
    assert len(frame) == 10

    data_path = store.directory_for(manifest["generation_id"]) / "data.parquet"
    tampered = pd.read_parquet(data_path)
    tampered.loc[0, "return"] = 0.123
    tampered.to_parquet(data_path, index=False)
    with pytest.raises(ContractError, match="checksum"):
        store.read(manifest["generation_id"])


def test_risk_returns_publisher_binds_existing_market_calendar(tmp_path: Path) -> None:
    from uq.research_chain.owning_contracts import BacktestMarketDatasetStore

    calendar_store = BacktestMarketDatasetStore(tmp_path)
    calendar_manifest = calendar_store.publish(
        pd.DataFrame({"date": SESSIONS, "instrument": ["CALENDAR"] * len(SESSIONS)}),
        dataset_type="calendar",
        window_start=pd.Timestamp(SESSIONS[0]).date(),
        window_end=pd.Timestamp(SESSIONS[-1]).date(),
        sources={"calendar": "reviewed-test"},
        producer="test",
    )
    input_path = tmp_path / "returns.parquet"
    _returns().to_parquet(input_path, index=False)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/publish_portfolio_risk_returns.py",
            "--data-root", str(tmp_path),
            "--input-parquet", str(input_path),
            "--calendar-generation-id", calendar_manifest["generation_id"],
            "--calendar-data-checksum", calendar_manifest["data_checksum_sha256"],
            "--source-id", "offline-test-panel",
            "--source-approval-ref", "reviewed-test-input",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    published = json.loads(result.stdout)
    risk_store = PortfolioRiskReturnsStore(tmp_path, calendar_store=calendar_store)
    read_manifest, _ = risk_store.read(published["generation_id"])
    assert read_manifest["calendar_generation_id"] == calendar_manifest["generation_id"]


def test_portfolio_v3_template_schema_requires_bound_risk_returns() -> None:
    template = {
        "contract_version": 3,
        "template_version": "3.0.0",
        "status": "reviewed",
        "portfolio_name": "erc-test",
        "weight_scheme": "risk_parity",
        "scheme_parameters": {"n": 2, "lookback_sessions": 60},
        "score_policy": {
            "direction": "descending", "nan_policy": "exclude",
            "tie_policy": "first_by_instrument_id",
        },
        "constraints": {"max_single_weight": 1.0},
        "rebalance_schedule": "daily",
        "universe_snapshot_generation_id": "4" * 64,
        "industry_source_binding": None,
        "risk_model_binding": {
            "family": "portfolio_risk_returns_v1",
            "generation_id": "5" * 64,
            "manifest_digest_sha256": "6" * 64,
        },
        "template_generation_id": "7" * 64,
        "template_manifest_digest_sha256": "8" * 64,
    }
    generation, digest = research_contract_identities(
        template, schema_name="portfolio_definition_template"
    )
    template["template_generation_id"] = generation
    template["template_manifest_digest_sha256"] = digest
    ModelContractLoader.validate("portfolio_definition_template", template)
    template["risk_model_binding"] = None
    with pytest.raises(ContractError):
        ModelContractLoader.validate("portfolio_definition_template", template)
