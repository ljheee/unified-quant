"""Paper/backtest deviation is an explicit diagnostic, not a live-fill claim."""
from __future__ import annotations

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.evaluation.paper_reconciliation import compare_execution_fills
from uq.evaluation import paper_reconciliation as reconciliation
from tests.test_backtest_stress import baseline

DAY, PREVIOUS = '2025-01-07', '2025-01-06'
BUDGET = {
    'maximum_unmatched_orders': 0,
    'maximum_requested_quantity_gap': 0,
    'maximum_filled_quantity_gap': 0,
    'maximum_execution_price_gap_bps': 15.,
    'maximum_fee_gap_cny': 1.,
}


def _backtest() -> pd.DataFrame:
    return pd.DataFrame([{
        'date': DAY, 'decision_date': PREVIOUS, 'instrument': 'A', 'side': 'buy',
        'target_shares': 100, 'filled_shares': 100, 'net_execution_price': 10.01,
        'commission_fee': 5., 'stamp_duty_fee': 0.,
    }])


def _paper() -> pd.DataFrame:
    return pd.DataFrame([{
        'decision_date': pd.Timestamp(PREVIOUS), 'execution_date': pd.Timestamp(DAY),
        'instrument': 'A', 'side': 'buy', 'requested_quantity': 100,
        'filled_quantity': 100, 'price': 10.02, 'fee_amount': 5.5,
    }])


def test_matching_orders_report_actual_price_and_fee_difference():
    report = compare_execution_fills(_backtest(), _paper(), decision_date=PREVIOUS,
                                     execution_date=DAY, budget=BUDGET)
    assert report['scope'] == 'deterministic_simulation_difference_only'
    assert report['metrics']['orders_compared'] == 1
    assert report['metrics']['maximum_execution_price_gap_bps'] == pytest.approx(9.99000999)
    assert report['metrics']['fee_gap_cny'] == .5
    assert report['budget_status'] == 'within_declared_budget'


def test_quantity_and_unmatched_orders_cannot_hide_behind_price_budget():
    paper = _paper()
    paper.loc[0, 'requested_quantity'] = 200
    paper.loc[0, 'filled_quantity'] = 50
    paper.loc[0, 'instrument'] = 'B'
    report = compare_execution_fills(_backtest(), paper, decision_date=PREVIOUS,
                                     execution_date=DAY, budget=BUDGET)
    assert report['metrics']['unmatched_orders'] == 2
    assert report['metrics']['requested_quantity_gap'] == 300
    assert report['metrics']['filled_quantity_gap'] == 150
    assert report['budget_status'] == 'exceeds_declared_budget'
    assert all(item['execution_price_gap_bps'] is None for item in report['orders'])


def test_wrong_paper_day_and_invalid_budget_fail_closed():
    paper = _paper()
    paper.loc[0, 'execution_date'] = pd.Timestamp('2025-01-08')
    with pytest.raises(ContractError, match='another decision or execution date'):
        compare_execution_fills(_backtest(), paper, decision_date=PREVIOUS,
                                execution_date=DAY, budget=BUDGET)
    with pytest.raises(ContractError, match='explicit budgets'):
        compare_execution_fills(_backtest(), _paper(), decision_date=PREVIOUS,
                                execution_date=DAY, budget={})


def test_empty_execution_day_does_not_report_budget_pass():
    report = compare_execution_fills(_backtest().iloc[:0], _paper().iloc[:0],
                                     decision_date=PREVIOUS, execution_date=DAY,
                                     budget=BUDGET)
    assert report['metrics']['orders_compared'] == 0
    assert report['budget_status'] == 'no_orders_to_compare'


def test_duplicate_or_overfilled_orders_fail_closed():
    paper = _paper()
    with pytest.raises(ContractError, match='duplicate order keys'):
        compare_execution_fills(_backtest(), pd.concat([paper, paper]),
                                decision_date=PREVIOUS, execution_date=DAY, budget=BUDGET)
    paper.loc[0, 'filled_quantity'] = 101
    with pytest.raises(ContractError, match='invalid order quantity'):
        compare_execution_fills(_backtest(), paper, decision_date=PREVIOUS,
                                execution_date=DAY, budget=BUDGET)


def test_published_comparison_reads_real_backtest_target_and_calendar(tmp_path, monkeypatch):
    backtest = baseline(tmp_path)
    target = backtest['target_weight_bindings'][0]
    shared = {
        'execution_id': 'paper-fixture', 'state_mode': 'initial',
        'decision_date': PREVIOUS, 'execution_date': DAY,
        'target_weights_binding': {
            'family': 'target_weights_v1',
            'generation_id': target['generation_id'],
            'manifest_digest_sha256': target['manifest_digest_sha256'],
        },
        'input_state_binding': None,
        'risk_decision_binding': {'generation_id': 'a' * 64},
    }
    paper = {**shared, 'generation_id': 'b' * 64,
             'manifest_digest_sha256': 'c' * 64,
             'order_plan_generation_id': 'd' * 64}
    plan = {**shared, 'generation_id': 'd' * 64,
            'manifest_digest_sha256': '1' * 64,
            'execution_config_generation_id': 'e' * 64}
    paper_config = {**shared, 'generation_id': 'e' * 64,
                    'manifest_digest_sha256': '2' * 64}

    class ResultStore:
        def __init__(self, root):
            pass

        def read(self, generation):
            assert generation == paper['generation_id']
            return paper, _paper()

    class PlanStore:
        def __init__(self, root):
            pass

        def read(self, generation):
            assert generation == paper['order_plan_generation_id']
            return plan, pd.DataFrame()

    class ConfigStore:
        def __init__(self, root):
            pass

        def read(self, generation):
            assert generation == plan['execution_config_generation_id']
            return paper_config

    monkeypatch.setattr(reconciliation, 'ExecutionResultStore', ResultStore)
    monkeypatch.setattr(reconciliation, 'OrderPlanStore', PlanStore)
    monkeypatch.setattr(reconciliation, 'ExecutionConfigStore', ConfigStore)

    def compare(budget):
        return reconciliation.compare_published_execution(
            backtest_root=tmp_path, backtest_generation=backtest['generation_id'],
            paper_root=tmp_path, paper_generation=paper['generation_id'], budget=budget)

    report = compare({**BUDGET, 'maximum_unmatched_orders': 3,
                      'maximum_requested_quantity_gap': 1000000,
                      'maximum_filled_quantity_gap': 1000000})
    assert report['source_bindings']['target_weights'] == target
    assert (report['decision_date'], report['execution_date']) == (PREVIOUS, DAY)

    plan['execution_date'] = '2025-01-08'
    with pytest.raises(ContractError, match='paper result/order plan/config execution_date mismatch'):
        compare(BUDGET)
    plan['execution_date'] = DAY
    paper['target_weights_binding'] = {**shared['target_weights_binding'],
                                       'manifest_digest_sha256': 'f' * 64}
    with pytest.raises(ContractError, match='paper result/order plan/config target_weights_binding mismatch'):
        compare(BUDGET)
    plan['target_weights_binding'] = paper['target_weights_binding']
    paper_config['target_weights_binding'] = paper['target_weights_binding']
    with pytest.raises(ContractError, match='target-weight bindings differ'):
        compare(BUDGET)
    original_target = shared['target_weights_binding']
    paper['target_weights_binding'] = original_target
    plan['target_weights_binding'] = original_target
    paper_config['target_weights_binding'] = original_target
    original_read = reconciliation.BacktestMarketDatasetStore.read_frame

    def reversed_calendar(self, *args, **kwargs):
        return original_read(self, *args, **kwargs).iloc[::-1].reset_index(drop=True)

    monkeypatch.setattr(reconciliation.BacktestMarketDatasetStore,
                        'read_frame', reversed_calendar)
    with pytest.raises(ContractError, match='calendar is not ordered and unique'):
        compare(BUDGET)


def test_window_requires_every_decision_once_and_aggregates_across_days(tmp_path, monkeypatch):
    backtest = baseline(tmp_path)
    decisions = backtest['target_weight_bindings']
    papers, plans, configs, frames = {}, {}, {}, {}
    for index, (binding, execution_date) in enumerate(zip(decisions, [DAY, '2025-01-08'])):
        identity = str(index + 1) * 64
        shared = {
            'execution_id': f'paper-{index}', 'state_mode': 'initial',
            'decision_date': binding['decision_date'], 'execution_date': execution_date,
            'target_weights_binding': {
                'family': 'target_weights_v1',
                'generation_id': binding['generation_id'],
                'manifest_digest_sha256': binding['manifest_digest_sha256'],
            },
            'input_state_binding': None,
            'risk_decision_binding': {'generation_id': 'a' * 64},
        }
        papers[identity] = {**shared, 'generation_id': identity,
                            'manifest_digest_sha256': 'b' * 64,
                            'order_plan_generation_id': str(index + 3) * 64}
        plan_id = papers[identity]['order_plan_generation_id']
        plans[plan_id] = {**shared, 'generation_id': plan_id,
                          'manifest_digest_sha256': 'c' * 64,
                          'execution_config_generation_id': str(index + 5) * 64}
        config_id = plans[plan_id]['execution_config_generation_id']
        configs[config_id] = {**shared, 'generation_id': config_id,
                              'manifest_digest_sha256': 'd' * 64}
        frame = _paper()
        frame['decision_date'] = pd.Timestamp(binding['decision_date'])
        frame['execution_date'] = pd.Timestamp(execution_date)
        frames[identity] = frame

    class ResultStore:
        def __init__(self, root):
            pass

        def read(self, generation):
            return papers[generation], frames[generation]

    class PlanStore:
        def __init__(self, root):
            pass

        def read(self, generation):
            return plans[generation], pd.DataFrame()

    class ConfigStore:
        def __init__(self, root):
            pass

        def read(self, generation):
            return configs[generation]

    monkeypatch.setattr(reconciliation, 'ExecutionResultStore', ResultStore)
    monkeypatch.setattr(reconciliation, 'OrderPlanStore', PlanStore)
    monkeypatch.setattr(reconciliation, 'ExecutionConfigStore', ConfigStore)
    ids = list(papers)
    context_reads = []
    original_context = reconciliation._backtest_context

    def counted_context(root, generation):
        context_reads.append(generation)
        return original_context(root, generation)

    monkeypatch.setattr(reconciliation, '_backtest_context', counted_context)
    def compare(generations, budget=BUDGET):
        return reconciliation.compare_published_execution_window(
            backtest_root=tmp_path, backtest_generation=backtest['generation_id'],
            paper_root=tmp_path, paper_generations=generations, budget=budget)

    wide = {**BUDGET, 'maximum_unmatched_orders': 10,
            'maximum_requested_quantity_gap': 1000000,
            'maximum_filled_quantity_gap': 1000000,
            'maximum_fee_gap_cny': 1000000.}
    report = compare(ids[::-1], wide)
    assert context_reads == [backtest['generation_id']]
    assert report['coverage_complete'] is True
    assert report['expected_decision_dates'] == [item['decision_date'] for item in decisions]
    assert [item['decision_date'] for item in report['daily']] == report['expected_decision_dates']
    assert report['metrics']['sessions_compared'] == 2
    assert report['metrics']['unmatched_orders'] == sum(
        item['metrics']['unmatched_orders'] for item in report['daily'])
    assert report['metrics']['fee_gap_cny'] == pytest.approx(sum(
        item['metrics']['fee_gap_cny'] for item in report['daily']))
    assert report['budget_status'] == 'within_declared_budget'
    assert compare(ids)['budget_status'] == 'exceeds_declared_budget'

    with pytest.raises(ContractError, match='count does not cover'):
        compare(ids[:1])
    with pytest.raises(ContractError, match='unique paper generations'):
        compare([ids[0], ids[0]])
    second, first = papers[ids[1]], papers[ids[0]]
    second['decision_date'] = first['decision_date']
    plans[second['order_plan_generation_id']]['decision_date'] = first['decision_date']
    configs[plans[second['order_plan_generation_id']]['execution_config_generation_id']]['decision_date'] = first['decision_date']
    with pytest.raises(ContractError, match='target-weight bindings differ'):
        compare(ids)
    second_plan = plans[second['order_plan_generation_id']]
    second_config = configs[second_plan['execution_config_generation_id']]
    for item in (second, second_plan, second_config):
        item['execution_date'] = first['execution_date']
        item['target_weights_binding'] = first['target_weights_binding']
    frames[ids[1]]['decision_date'] = pd.Timestamp(first['decision_date'])
    frames[ids[1]]['execution_date'] = pd.Timestamp(first['execution_date'])
    with pytest.raises(ContractError, match='cover every backtest decision date exactly once'):
        compare(ids)
    def context_with_uncovered_fill(root, generation):
        result, config, artifacts, sessions = original_context(root, generation)
        extra = artifacts['fills'].iloc[:1].copy()
        extra['date'] = '2025-01-08'
        return result, config, {
            **artifacts, 'fills': pd.concat([artifacts['fills'], extra], ignore_index=True),
        }, sessions

    monkeypatch.setattr(reconciliation, '_backtest_context', context_with_uncovered_fill)
    with pytest.raises(ContractError, match='outside the target-weight window'):
        compare(ids)
