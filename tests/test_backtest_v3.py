from __future__ import annotations

import pandas as pd
import pytest

from uq.backtest.engine import BacktestEngine, BacktestResultStore
from uq.backtest.execution_facts import ExecutionFactsStore, facts_binding
from uq.errors import ContractError
from uq.research_chain.owning_contracts import BacktestConfigStore
from tests.test_backtest_layer import _make_config
from tests.test_research_chain_phase4 import _config_decision, _result_decision

DAYS = ['2025-01-06', '2025-01-07', '2025-01-08']
NAME = '600000.XSHG'


def inputs(root, *, capital=10000., volume=10000., costs=None, risk=None, execution=None, facts_change=None):
    prices = pd.DataFrame([{'date': day, 'instrument': NAME, 'open': 10., 'close': 10., 'volume': volume}
                           for day in DAYS]).set_index(['date', 'instrument'])
    facts = pd.DataFrame([{
        'date': day, 'instrument': NAME, 'visible_at': day + 'T09:00:00+08:00',
        'board': 'main', 'is_st': False, 'suspended': False, 'has_price_limit': True,
        'limit_up': 11., 'limit_down': 9., 'limit_reference_price': 10., 'raw_previous_close': 10.,
        'buy_lot': 100, 'min_buy_shares': 100, 'sell_lot': 100, 'industry': 'bank',
    } for day in DAYS])
    if facts_change:
        facts_change(facts)
    fact_manifest = ExecutionFactsStore(root).publish(facts, source_id='isolated-exchange-fixture',
                                                       source_approval_ref='test-only-not-production')
    cost_model = {'commission_bps': 0., 'stamp_duty_bps': 0., 'slippage_bps': 10.,
                  'minimum_commission_cny': 5., 'commission_basis': 'executed_notional',
                  'fee_rounding': 'half_up_cent',
                  'impact_model': {'type': 'square_root', 'coefficient_bps': 0., 'maximum_bps': 100.}}
    cost_model.update(costs or {})
    model = {'type': 'daily_t1_open', 'sellable_quantity_rule': 'prior_day_holding_only',
             'lot_rule_source': 'execution_facts', 'volume_participation_cap': .1,
             'fill_rate_multiplier': 1., 'rebalance_every_n_sessions': 1}
    model.update(execution or {})
    constraints = {'max_single_weight': 1., 'max_industry_weight': None, 'max_turnover': None, 'cash_reserve': 0.}
    constraints.update(risk or {})
    config = _make_config(contract_version=3, start_date=DAYS[0], end_date=DAYS[-1], initial_capital=capital,
        cost_model=cost_model, execution_model=model, limit_rules={'type': 'published_daily_price_bands', 'price_basis': 'raw_unadjusted_cny'},
        execution_facts_binding=facts_binding(fact_manifest), risk_constraints=constraints,
        price_source_binding={'family': 'bars_daily_v2', 'window_start': DAYS[0], 'window_end': DAYS[-1],
                              'set_digest_sha256': 'a' * 64, 'member_count': 3})
    config['generation_id'] = config['manifest_digest_sha256'] = '0' * 64
    config = BacktestConfigStore(root).publish(config, quality_decision=_config_decision())
    return config, prices, facts


def weights(value):
    return pd.DataFrame({'instrument': [NAME], 'weight': [value]})


def run(root, config, prices, facts, *, targets=None, **kwargs):
    return BacktestEngine(root).run(config=config, portfolio_definition={'generation_id': 'b' * 64},
        weight_partitions=targets or {DAYS[0]: weights(.1), DAYS[1]: weights(0.)},
        price_panel=prices, execution_facts=facts, **kwargs)


def test_both_sides_pay_slippage_and_minimum_commission_and_nav_reconciles(tmp_path):
    config, prices, facts = inputs(tmp_path)
    manifest, artifacts = run(tmp_path, config, prices, facts)
    fills = artifacts['fills']
    assert fills['filled_shares'].tolist() == [100, 100]
    assert fills['net_execution_price'].tolist() == pytest.approx([10.01, 9.99])
    assert fills['commission_fee'].tolist() == [5., 5.]
    assert artifacts['equity_curve']['portfolio_value'].tolist() == pytest.approx([10000., 9994., 9988.])
    assert artifacts['equity_curve']['holdings_value'].iloc[-1] == 0
    assert manifest['summary_metrics']['total_slippage_cny'] == pytest.approx(2.)
    assert manifest['summary_metrics']['total_commission_cny'] == 10.
    assert manifest['summary_metrics']['cumulative_return'] == pytest.approx(-.0012)
    assert artifacts['daily_metrics']['executed_turnover'].iloc[1] == pytest.approx(.05)


def test_volume_participation_uses_prior_session_not_future_liquidity_and_partially_fills_both_sides(tmp_path):
    config, prices, facts = inputs(tmp_path, volume=1000.)
    targets = {DAYS[0]: weights(.9), DAYS[1]: weights(0.)}
    # Large future volume must not expand the first order's capacity.
    prices.loc[(DAYS[1], NAME), 'volume'] = 1000000.
    _, artifacts = run(tmp_path, config, prices, facts, targets=targets)
    assert artifacts['fills']['filled_shares'].iloc[0] == 100
    assert artifacts['fills']['unfilled_shares'].iloc[0] == 800
    assert 'participation' in artifacts['fills']['constraint_reasons'].iloc[0]


def test_square_root_impact_and_cash_clipping_are_charged_on_actual_quantity(tmp_path):
    config, prices, facts = inputs(tmp_path, costs={'minimum_commission_cny': 0., 'slippage_bps': 0.,
        'impact_model': {'type': 'square_root', 'coefficient_bps': 100., 'maximum_bps': 100.}})
    manifest, artifacts = run(tmp_path, config, prices, facts, targets={DAYS[0]: weights(1.)})
    fill = artifacts['fills'].iloc[0]
    assert fill.filled_shares == 900
    assert fill.impact_bps == pytest.approx(30.)
    assert manifest['summary_metrics']['total_impact_cny'] == pytest.approx(27.)
    assert artifacts['equity_curve']['cash'].iloc[-1] == pytest.approx(973.)


def test_cash_only_undefined_sharpe_is_publishable_and_readable_in_v3(tmp_path):
    config, prices, facts = inputs(tmp_path)
    manifest, artifacts = run(tmp_path, config, prices, facts, targets={DAYS[-1]: weights(.5)})
    assert manifest['summary_metrics']['sharpe_ratio'] is None
    BacktestResultStore(tmp_path).publish(manifest, artifacts, quality_decision=_result_decision(),
        target_weight_generations=[{'decision_date': DAYS[-1], 'generation_id': 'b' * 64,
                                   'manifest_digest_sha256': 'c' * 64}])
    restored, frames = BacktestResultStore(tmp_path).read(manifest['generation_id'])
    assert restored['contract_version'] == 3
    assert restored['summary_metrics']['sharpe_ratio'] is None
    assert frames['fills'].empty


def test_bound_execution_facts_cannot_be_changed_at_runtime(tmp_path):
    config, prices, facts = inputs(tmp_path)
    facts.loc[0, 'limit_up'] = 100.
    with pytest.raises(ContractError, match='logical checksum'):
        run(tmp_path, config, prices, facts)


def test_forward_visible_status_or_limits_cannot_be_published(tmp_path):
    def future(frame):
        frame.loc[1, 'visible_at'] = DAYS[1] + 'T15:00:00+08:00'
    with pytest.raises(ContractError, match='visible by the session open'):
        inputs(tmp_path, facts_change=future)


def test_rebalance_frequency_and_turnover_budget_are_executed_not_just_declared(tmp_path):
    config, prices, facts = inputs(tmp_path, execution={'rebalance_every_n_sessions': 2}, risk={'max_turnover': .05})
    _, artifacts = run(tmp_path, config, prices, facts, targets={DAYS[0]: weights(.9), DAYS[1]: weights(0.)})
    assert artifacts['fills']['filled_shares'].tolist() == [100]
    assert 'turnover' in artifacts['fills']['constraint_reasons'].iloc[0]
    assert artifacts['equity_curve']['holdings_value'].iloc[-1] == 1000.
    assert artifacts['daily_metrics']['rebalance_scheduled'].tolist() == [False, True, False]


def test_corporate_position_events_remain_fail_closed_until_accounted(tmp_path):
    config, prices, facts = inputs(tmp_path)
    with pytest.raises(ContractError, match='corporate-action'):
        run(tmp_path, config, prices, facts, corporate_action_instruments={NAME})


@pytest.mark.parametrize('board,is_st,upper,opening,filled', [
    ('main', False, 11., 11., False),
    ('main', True, 10.5, 10.5, False),
    ('star', False, 12., 11.5, True),
    ('chinext', False, 12., 11.5, True),
    ('bse', False, 13., 12., True),
])
def test_published_historical_bands_control_fills_instead_of_one_ten_percent_rule(
    tmp_path, board, is_st, upper, opening, filled,
):
    def change(frame):
        frame['board'], frame['is_st'], frame['limit_up'] = board, is_st, upper
    config, prices, facts = inputs(tmp_path, facts_change=change)
    prices.loc[(DAYS[1], NAME), 'open'] = opening
    _, artifacts = run(tmp_path, config, prices, facts, targets={DAYS[0]: weights(.5)})
    assert bool(artifacts['fills']['filled_shares'].iloc[0] > 0) is filled
    if not filled:
        assert 'limit_up' in artifacts['fills']['constraint_reasons'].iloc[0]


def test_unlimited_session_requires_explicit_null_bands_and_does_not_invent_limit(tmp_path):
    def change(frame):
        frame['has_price_limit'] = False
        frame['limit_up'] = frame['limit_down'] = None
    config, prices, facts = inputs(tmp_path, facts_change=change)
    prices.loc[(DAYS[1], NAME), 'open'] = 14.
    _, artifacts = run(tmp_path, config, prices, facts, targets={DAYS[0]: weights(.5)})
    assert artifacts['fills']['filled_shares'].iloc[0] == 300


def test_ex_reference_distortion_is_recorded_and_actual_published_band_is_used(tmp_path):
    def change(frame):
        frame.loc[1, ['limit_reference_price', 'limit_up', 'limit_down']] = [9., 9.9, 8.1]
    config, prices, facts = inputs(tmp_path, facts_change=change)
    prices.loc[(DAYS[1], NAME), 'open'] = 9.9
    _, artifacts = run(tmp_path, config, prices, facts, targets={DAYS[0]: weights(.5)})
    fill = artifacts['fills'].iloc[0]
    assert fill.filled_shares == 0
    assert fill.reference_adjustment_bps == pytest.approx(-1000.)
    assert 'limit_up' in fill.constraint_reasons


def test_sell_participation_leaves_unfilled_holdings_and_charges_only_executed_shares(tmp_path):
    config, prices, facts = inputs(tmp_path, costs={'minimum_commission_cny': 0., 'slippage_bps': 0.})
    prices.loc[(DAYS[1], NAME), 'volume'] = 1000.
    manifest, artifacts = run(tmp_path, config, prices, facts,
                             targets={DAYS[0]: weights(.9), DAYS[1]: weights(0.)})
    assert artifacts['fills']['filled_shares'].tolist() == [900, 100]
    assert artifacts['fills']['unfilled_shares'].tolist() == [0, 800]
    assert artifacts['equity_curve']['holdings_value'].iloc[-1] == 8000.
    assert manifest['summary_metrics']['fill_ratio'] == pytest.approx(10 / 18)


def test_execution_sizing_never_uses_same_session_close(tmp_path):
    config, prices, facts = inputs(tmp_path)
    prices.loc[(DAYS[2], NAME), 'close'] = 11.
    _, artifacts = run(tmp_path, config, prices, facts, targets={DAYS[0]: weights(.1), DAYS[1]: weights(.3)})
    # Marking the existing 100 shares at this future close before sizing would
    # cross the next board-lot boundary and buy 200 rather than 100 more shares.
    assert artifacts['fills']['filled_shares'].tolist() == [100, 100]
    assert artifacts['equity_curve']['portfolio_value'].iloc[-1] == pytest.approx(10188.)


@pytest.mark.parametrize('action,expected', [('allow', 100), ('warn', 100), ('resize', 0),
                                           ('flatten', 0), ('halt_strategy', 0)])
def test_risk_gate_resize_and_non_executable_actions_are_not_bypassed(tmp_path, action, expected):
    config, prices, facts = inputs(tmp_path)
    gate = lambda order, context: {'action': action, 'constraints': [
        {'instrument': NAME, 'constraint_type': 'allowed_shares', 'value': 50}]}
    _, artifacts = run(tmp_path, config, prices, facts, targets={DAYS[0]: weights(.1)}, risk_gate=gate)
    assert artifacts['fills']['filled_shares'].iloc[0] == expected


def test_suspended_held_name_carries_last_close_and_cannot_liquidate(tmp_path):
    def change(frame):
        frame.loc[2, 'suspended'] = True
    config, prices, facts = inputs(tmp_path, facts_change=change)
    # Another instrument retains the session in the calendar, as a real panel does.
    extra = prices.loc[[DAYS[2]]].reset_index()
    extra['instrument'] = '600519.XSHG'
    prices = pd.concat([prices.drop(index=(DAYS[2], NAME)), extra.set_index(['date', 'instrument'])])
    _, artifacts = run(tmp_path, config, prices, facts)
    assert artifacts['fills']['filled_shares'].tolist() == [100, 0]
    assert 'suspended' in artifacts['fills']['constraint_reasons'].iloc[-1]
    assert artifacts['equity_curve']['holdings_value'].iloc[-1] == 1000.


def test_industry_constraint_requires_visible_classification(tmp_path):
    def change(frame):
        frame['industry'] = None
    config, prices, facts = inputs(tmp_path, risk={'max_industry_weight': .2}, facts_change=change)
    with pytest.raises(ContractError, match='requires classifications'):
        run(tmp_path, config, prices, facts)


@pytest.mark.parametrize('constraint,value,reason,shares', [
    ('max_single_weight', .2, 'single_name', 100),
    ('max_industry_weight', .2, 'industry', 100),
    ('cash_reserve', .2, 'cash', 700),
])
def test_buy_budgets_use_nav_after_execution_costs_including_minimum_fee(tmp_path, constraint, value, reason, shares):
    config, prices, facts = inputs(tmp_path, risk={constraint: value})
    manifest, artifacts = run(tmp_path, config, prices, facts, targets={DAYS[0]: weights(.9)})
    fill = artifacts['fills'].iloc[0]
    assert fill.filled_shares == shares
    assert reason in fill.constraint_reasons
    assert manifest['summary_metrics']['risk_budget_breach_sessions'] == 0


def test_passive_price_breach_is_reported_instead_of_claiming_impossible_end_of_day_budget(tmp_path):
    def change(frame):
        frame.loc[2, ['raw_previous_close', 'limit_reference_price', 'limit_up', 'limit_down']] = [
            11., 11., 12.1, 9.9,
        ]
    config, prices, facts = inputs(tmp_path, risk={'max_single_weight': .105}, facts_change=change)
    prices.loc[(DAYS[1], NAME), 'close'] = 11.
    manifest, artifacts = run(tmp_path, config, prices, facts, targets={DAYS[0]: weights(.1)})
    assert artifacts['daily_metrics']['risk_budget_breached'].tolist() == [False, True, False]
    assert manifest['summary_metrics']['risk_budget_breach_sessions'] == 1


def test_max_drawdown_stop_latches_and_retries_exit_at_the_next_open(tmp_path):
    def change(frame):
        frame.loc[2, ['raw_previous_close', 'limit_reference_price', 'limit_up', 'limit_down']] = [
            8., 8., 8.8, 7.2,
        ]

    config, prices, facts = inputs(tmp_path, risk={'max_drawdown_stop': .15}, facts_change=change)
    prices.loc[(DAYS[1], NAME), 'close'] = 8.
    prices.loc[(DAYS[2], NAME), 'open'] = 8.
    manifest, artifacts = run(
        tmp_path, config, prices, facts,
        targets={DAYS[0]: weights(.9), DAYS[1]: weights(.9)},
    )
    daily = artifacts['daily_metrics']
    assert daily['drawdown_stop_triggered'].tolist() == [False, False, True]
    assert daily['drawdown_stop_active'].tolist() == [False, False, True]
    assert artifacts['fills']['side'].tolist() == ['buy', 'sell']
    assert artifacts['fills']['filled_shares'].tolist() == [900, 900]
    assert artifacts['equity_curve']['holdings_value'].iloc[-1] == 0
    assert manifest['summary_metrics']['drawdown_stop_trigger_date'] == DAYS[2]
    assert manifest['summary_metrics']['drawdown_stop_sessions'] == 1


def test_volatility_target_uses_only_completed_returns_and_scales_down_without_leverage(tmp_path):
    risk = {'volatility_target': {
        'target_annualized': .2, 'lookback_sessions': 2, 'min_history_sessions': 2,
    }}

    def change(frame):
        frame.loc[2, ['raw_previous_close', 'limit_reference_price', 'limit_up', 'limit_down']] = [
            11., 11., 12.1, 9.9,
        ]

    config, prices, facts = inputs(tmp_path, risk=risk, facts_change=change)
    prices.loc[(DAYS[1], NAME), 'close'] = 11.
    manifest, artifacts = run(
        tmp_path, config, prices, facts,
        targets={DAYS[0]: weights(.9), DAYS[1]: weights(.9)},
    )
    daily = artifacts['daily_metrics']
    assert daily['volatility_target_estimated_annualized'].iloc[:2].isna().all()
    assert daily['volatility_target_estimated_annualized'].iloc[2] > .2
    assert daily['volatility_target_scale'].iloc[2] == pytest.approx(
        .2 / daily['volatility_target_estimated_annualized'].iloc[2]
    )
    assert 0 < daily['volatility_target_scale'].iloc[2] < 1
    assert daily['volatility_target_applied'].tolist() == [False, False, True]
    assert artifacts['fills']['side'].tolist() == ['buy', 'sell']
    assert artifacts['fills']['filled_shares'].tolist() == [900, 800]
    assert manifest['summary_metrics']['volatility_target_applied_sessions'] == 1
    assert manifest['summary_metrics']['minimum_volatility_target_scale'] < 1


def test_missing_tradable_open_is_a_data_error_not_an_assumed_suspension(tmp_path):
    config, prices, facts = inputs(tmp_path)
    prices.loc[(DAYS[1], NAME), 'open'] = float('nan')
    with pytest.raises(ContractError, match='missing raw open'):
        run(tmp_path, config, prices, facts)


def test_source_minimum_and_increment_allow_star_style_order_sizes(tmp_path):
    def change(frame):
        frame['board'] = 'star'
        frame['buy_lot'], frame['min_buy_shares'], frame['sell_lot'] = 1, 200, 1
    config, prices, facts = inputs(tmp_path, facts_change=change)
    _, small = run(tmp_path, config, prices, facts, targets={DAYS[0]: weights(.1)})
    assert small['fills']['filled_shares'].iloc[0] == 0
    assert 'minimum_lot' in small['fills']['constraint_reasons'].iloc[0]
    _, sufficient = run(tmp_path, config, prices, facts, targets={DAYS[0]: weights(.251)})
    assert sufficient['fills']['filled_shares'].iloc[0] == 251


def test_effective_fill_price_cannot_cross_published_band_due_to_slippage(tmp_path):
    config, prices, facts = inputs(tmp_path, costs={'slippage_bps': 100.})
    prices.loc[(DAYS[1], NAME), 'open'] = 10.99
    _, artifacts = run(tmp_path, config, prices, facts, targets={DAYS[0]: weights(.5)})
    assert artifacts['fills']['filled_shares'].iloc[0] == 0
    assert 'execution_price_band' in artifacts['fills']['constraint_reasons'].iloc[0]


def test_full_odd_lot_liquidation_is_allowed_when_raw_capacity_and_turnover_cover_it(tmp_path):
    def change(frame):
        frame['board'] = 'star'
        frame['buy_lot'], frame['min_buy_shares'], frame['sell_lot'] = 1, 200, 100
    config, prices, facts = inputs(tmp_path, facts_change=change, risk={'max_turnover': .14})
    _, artifacts = run(tmp_path, config, prices, facts,
        targets={DAYS[0]: weights(.251), DAYS[1]: weights(0.)})
    assert artifacts['fills']['filled_shares'].tolist() == [251, 251]
    assert artifacts['fills']['constraint_reasons'].tolist() == ['', '']
    assert artifacts['equity_curve']['holdings_value'].iloc[-1] == 0
