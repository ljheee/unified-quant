from __future__ import annotations

import pandas as pd
import pytest

from uq.backtest.engine import BacktestEngine, BacktestResultStore
from uq.backtest.execution_facts import ExecutionFactsStore, facts_binding
from uq.backtest.corporate_actions import ACTION_EVENT_COLUMNS, dividend_coverage_row, rights_issue_coverage_row
from uq.contracts.model_layer import ModelContractLoader
from uq.errors import ContractError
from uq.evaluation.exposures import calculate_position_exposures
from uq.research_chain.owning_contracts import BacktestConfigStore
from tests.test_research_chain_phase4 import _config_decision, _result_decision

DAYS = ["2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10", "2025-01-13"]
NAME = "600000.XSHG"
MODEL = {
    "entitlement_rule": "record_date_end_of_session_shares",
    "ex_date_receivables": "included_in_nav_not_available_for_orders",
    "cash_settlement": "spendable_on_pay_date_session",
    "stock_settlement": "included_in_nav_on_ex_date_and_sellable_from_session_after_div_listdate",
    "fractional_stock_entitlement": "reject",
    "rights_issue": "reviewed_complete_window_exclusion_list_required",
}


def _action_panel():
    dividend = {
        "event_type": "dividend", "event_id": "a" * 64, "instrument": NAME,
        "record_date": DAYS[1], "ex_date": DAYS[2], "pay_date": DAYS[4],
        "div_listdate": DAYS[4], "cash_div_after_tax": 0.1, "stock_div_per_share": 0.5,
        "source_announcement_date": DAYS[0], "implementation_announcement_date": DAYS[1],
        "source_record_id": "a" * 64, "source_approval_ref": "dividend-review-2025-01",
        "reason": "implemented_dividend", "window_start": None, "window_end": None,
        "coverage_complete": None,
    }
    return pd.DataFrame([
        dividend,
        rights_issue_coverage_row(start_date=DAYS[0], end_date=DAYS[-1],
                                 source_approval_ref="rights-review-2025-01"),
        dividend_coverage_row(start_date=DAYS[0], end_date=DAYS[-1],
                              source_approval_ref="dividend-review-2025-01"),
    ], columns=ACTION_EVENT_COLUMNS)


def _inputs(root):
    prices = pd.DataFrame([
        {"date": day, "instrument": NAME, "open": opening, "close": close, "volume": 10000.}
        for day, opening, close in zip(
            DAYS, [10., 10., 6.6, 6.6, 6.6, 6.6], [10., 10., 6.6, 6.6, 6.6, 6.6]
        )
    ]).set_index(["date", "instrument"])
    facts = pd.DataFrame([
        {
            "date": day, "instrument": NAME, "visible_at": day + "T09:00:00+08:00",
            "board": "main", "is_st": False, "suspended": False, "has_price_limit": True,
            "limit_up": (7.33 if day >= DAYS[2] else 11.),
            "limit_down": (6.0 if day >= DAYS[2] else 9.),
            "limit_reference_price": (6.67 if day >= DAYS[2] else 10.),
            "raw_previous_close": (10. if day == DAYS[2] else 6.6 if day > DAYS[2] else 10.),
            "buy_lot": 100, "min_buy_shares": 100, "sell_lot": 100, "industry": "bank",
        }
        for day in DAYS
    ])
    fact_manifest = ExecutionFactsStore(root).publish(
        facts, source_id="synthetic-company-action-fixture", source_approval_ref="test-only",
    )
    binding = {
        "dataset_type": "corporate_action_events", "dataset_generation_id": "d" * 64,
        "data_checksum_sha256": "e" * 64,
    }
    config = {
        "contract_version": 4, "schema_version": "1.0.0", "backtest_name": "v4-actions",
        "start_date": DAYS[0], "end_date": DAYS[-1],
        "execution_model": {
            "type": "daily_t1_open", "sellable_quantity_rule": "prior_day_holding_only",
            "lot_rule_source": "execution_facts", "volume_participation_cap": .5,
            "fill_rate_multiplier": 1., "rebalance_every_n_sessions": 1,
        },
        "cost_model": {
            "commission_bps": 0., "stamp_duty_bps": 0., "slippage_bps": 0.,
            "minimum_commission_cny": 0., "commission_basis": "executed_notional",
            "fee_rounding": "half_up_cent",
            "impact_model": {"type": "linear", "coefficient_bps": 0., "maximum_bps": 0.},
        },
        "limit_rules": {"type": "published_daily_price_bands", "price_basis": "raw_unadjusted_cny"},
        "calendar_binding": {"generation_id": "1" * 64, "checksum_sha256": "2" * 64},
        "price_source_binding": {
            "family": "bars_daily_v2", "window_start": DAYS[0], "window_end": DAYS[-1],
            "set_digest_sha256": "3" * 64, "member_count": len(DAYS),
        },
        "universe_binding": {"snapshot_generation_id": "4" * 64}, "initial_capital": 10000.,
        "run_id": "00000000-0000-4000-8000-000000000000",
        "created_at": "1970-01-01T00:00:00+00:00",
        "quality_report_checksum_sha256": "0" * 64,
        "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64,
        "corporate_action_binding": binding,
        "corporate_action_model": MODEL,
        "suspension_binding": {"dataset_generation_id": "5" * 64, "data_checksum_sha256": "6" * 64},
        "execution_facts_binding": facts_binding(fact_manifest),
        "risk_constraints": {
            "max_single_weight": 1., "max_industry_weight": None,
            "max_turnover": None, "cash_reserve": 0.,
        },
    }
    config = BacktestConfigStore(root).publish(config, quality_decision=_config_decision())
    ModelContractLoader.validate("backtest_config_v4", config)
    return config, prices, facts, _action_panel()


def test_v4_accounts_ex_date_receivables_settlement_and_listing_t1(tmp_path):
    config, prices, facts, events = _inputs(tmp_path)
    manifest, frames = BacktestEngine(tmp_path).run(
        config=config,
        portfolio_definition={"generation_id": "b" * 64},
        weight_partitions={DAYS[0]: pd.DataFrame({"instrument": [NAME], "weight": [.1]}),
                           DAYS[2]: pd.DataFrame({"instrument": [NAME], "weight": [0.]}),
                           DAYS[3]: pd.DataFrame({"instrument": [NAME], "weight": [0.]}),
                           DAYS[4]: pd.DataFrame({"instrument": [NAME], "weight": [0.]})},
        price_panel=prices, execution_facts=facts, corporate_action_events=events,
    )
    equity = frames["equity_curve"].set_index("date")
    assert equity.loc[DAYS[2], "cash"] == pytest.approx(9000.)
    assert equity.loc[DAYS[2], "cash_receivable"] == pytest.approx(10.)
    assert equity.loc[DAYS[2], "share_receivable_value"] == pytest.approx(330.)
    assert equity.loc[DAYS[2], "portfolio_value"] == pytest.approx(10000.)
    assert equity.loc[DAYS[3], "cash"] == pytest.approx(9660.)
    assert equity.loc[DAYS[3], "cash_receivable"] == pytest.approx(10.)
    assert equity.loc[DAYS[4], "cash"] == pytest.approx(9670.)
    assert equity.loc[DAYS[4], "share_receivable_value"] == 0.
    listed_day_sell = frames["fills"].query("date == @DAYS[4] and side == 'sell'").iloc[0]
    assert listed_day_sell["filled_shares"] == 0
    assert "t1" in listed_day_sell["constraint_reasons"]
    assert frames["fills"].query("date == @DAYS[5] and side == 'sell'")["filled_shares"].tolist() == [50]
    assert frames["equity_curve"].iloc[-1]["holdings_value"] == pytest.approx(0.)
    assert manifest["contract_version"] == 4
    assert manifest["summary_metrics"]["terminal_cash_receivable_cny"] == 0.
    assert manifest["summary_metrics"]["corporate_action_event_count"] == 1
    assert len(frames["corporate_actions"]) >= 5
    exposures = calculate_position_exposures(
        config=config, equity=frames["equity_curve"], fills=frames["fills"],
        raw_prices=prices.reset_index().rename(columns={"date": "session_date"}),
        execution_facts=facts, corporate_actions=frames["corporate_actions"],
    )
    assert exposures["profile"] == "actual_v4_position_exposures_v1"
    assert exposures["daily"][2]["cash_receivable_weight"] == pytest.approx(.001)
    assert exposures["daily"][2]["held_instruments"] == 1

    BacktestResultStore(tmp_path).publish(
        manifest, frames, quality_decision=_result_decision(),
        target_weight_generations=[{"decision_date": day, "generation_id": "7" * 64,
                                    "manifest_digest_sha256": "8" * 64}
                                   for day in (DAYS[0], DAYS[2], DAYS[3], DAYS[4])],
    )
    restored, artifacts = BacktestResultStore(tmp_path).read(manifest["generation_id"])
    assert restored["contract_version"] == 4
    pd.testing.assert_frame_equal(artifacts["corporate_actions"], frames["corporate_actions"], check_dtype=False)


def test_v4_requires_reviewed_event_panel_and_refuses_fractional_entitlements(tmp_path):
    config, prices, facts, events = _inputs(tmp_path)
    with pytest.raises(ContractError, match="reviewed corporate-action event panel"):
        BacktestEngine(tmp_path).run(
            config=config, portfolio_definition={"generation_id": "b" * 64},
            weight_partitions={DAYS[0]: pd.DataFrame({"instrument": [NAME], "weight": [.1]})},
            price_panel=prices, execution_facts=facts,
        )
    fractional = events.copy()
    fractional.loc[fractional["event_type"] == "dividend", "stock_div_per_share"] = .333
    with pytest.raises(ContractError, match="not a whole number"):
        BacktestEngine(tmp_path).run(
            config=config, portfolio_definition={"generation_id": "b" * 64},
            weight_partitions={DAYS[0]: pd.DataFrame({"instrument": [NAME], "weight": [.1]})},
            price_panel=prices, execution_facts=facts, corporate_action_events=fractional,
        )
