"""Compare governed paper fills with v3 backtest fills by day or window.

Both engines are deterministic simulations. This diagnostic measures their
disagreement under an explicit budget; it does not attest live execution.
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from ..errors import ContractError
from ..contracts.model_layer import model_manifest_identities
from ..execution.stores import ExecutionConfigStore, ExecutionResultStore, OrderPlanStore
from ..portfolio.builder import TargetWeightStore
from ..research_chain.owning_contracts import BacktestMarketDatasetStore
from .metrics import session_dates
from .report import _verified_result

_BUDGET_FIELDS = {
    'maximum_unmatched_orders', 'maximum_requested_quantity_gap',
    'maximum_filled_quantity_gap', 'maximum_execution_price_gap_bps',
    'maximum_fee_gap_cny',
}


def _budget(values: dict) -> dict:
    if not isinstance(values, dict) or set(values) != _BUDGET_FIELDS:
        raise ContractError('paper reconciliation requires all five explicit budgets')
    result = {}
    for key, value in values.items():
        if key in {'maximum_unmatched_orders', 'maximum_requested_quantity_gap',
                   'maximum_filled_quantity_gap'}:
            if type(value) is not int or value < 0:
                raise ContractError(f'invalid paper reconciliation budget: {key}')
        elif (isinstance(value, bool) or not isinstance(value, (float, int))
              or not math.isfinite(value) or value < 0):
            raise ContractError(f'invalid paper reconciliation budget: {key}')
        result[key] = value
    return result


def _dates(frame: pd.DataFrame, field: str, date: str) -> pd.DataFrame:
    if field not in frame:
        raise ContractError(f'paper reconciliation missing {field}')
    values = session_dates(frame[field].tolist(), label='paper reconciliation')
    return frame.loc[pd.Series(values, index=frame.index) == date].copy()


def compare_execution_fills(
    backtest_fills: pd.DataFrame, paper_fills: pd.DataFrame, *,
    decision_date: str, execution_date: str, budget: dict,
) -> dict:
    """Return differences for the same target date; zero-fill orders count too."""
    decision_date, execution_date = session_dates(
        [decision_date, execution_date], label='paper reconciliation window')
    policy = _budget(budget)
    back_required = {'date', 'decision_date', 'instrument', 'side', 'target_shares',
                     'filled_shares', 'net_execution_price', 'commission_fee', 'stamp_duty_fee'}
    paper_required = {'decision_date', 'execution_date', 'instrument', 'side',
                      'requested_quantity', 'filled_quantity', 'price', 'fee_amount'}
    if back_required - set(backtest_fills) or paper_required - set(paper_fills):
        raise ContractError('paper reconciliation fill columns are incomplete')
    back = _dates(_dates(backtest_fills, 'date', execution_date), 'decision_date', decision_date)
    paper = _dates(_dates(paper_fills, 'execution_date', execution_date),
                   'decision_date', decision_date)
    if len(paper) != len(paper_fills):
        raise ContractError('paper result contains orders for another decision or execution date')

    def rows(frame: pd.DataFrame, *, paper_side: bool) -> dict[tuple[str, str], dict]:
        output = {}
        requested = 'requested_quantity' if paper_side else 'target_shares'
        filled = 'filled_quantity' if paper_side else 'filled_shares'
        price = 'price' if paper_side else 'net_execution_price'
        for item in frame.to_dict('records'):
            key = (item['instrument'], item['side'])
            if (not isinstance(key[0], str) or not key[0]
                    or key[1] not in {'buy', 'sell'} or key in output):
                raise ContractError('paper reconciliation has invalid or duplicate order keys')
            quantity, fill = item[requested], item[filled]
            if (type(quantity) is not int or type(fill) is not int
                    or quantity < 0 or fill < 0 or fill > quantity):
                raise ContractError('paper reconciliation has invalid order quantity')
            try:
                execution_price = float(item[price])
                fee = float(item['fee_amount']) if paper_side else (
                    float(item['commission_fee']) + float(item['stamp_duty_fee']))
            except (TypeError, ValueError) as exc:
                raise ContractError('paper reconciliation has invalid price or fee') from exc
            if (not math.isfinite(execution_price) or not math.isfinite(fee)
                    or execution_price < 0 or fee < 0 or (fill and execution_price <= 0)):
                raise ContractError('paper reconciliation has invalid price or fee')
            output[key] = {'requested': quantity, 'filled': fill,
                           'execution_price': execution_price, 'fee': fee}
        return output

    left, right = rows(back, paper_side=False), rows(paper, paper_side=True)
    differences = []
    for instrument, side in sorted(set(left) | set(right)):
        back_order, paper_order = left.get((instrument, side)), right.get((instrument, side))
        a = back_order or {'requested': 0, 'filled': 0, 'execution_price': 0., 'fee': 0.}
        b = paper_order or {'requested': 0, 'filled': 0, 'execution_price': 0., 'fee': 0.}
        price_gap = (abs(b['execution_price'] / a['execution_price'] - 1) * 10000
                     if a['filled'] and b['filled'] else None)
        differences.append({
            'instrument': instrument, 'side': side,
            'presence': 'both' if back_order and paper_order else
                        ('backtest_only' if back_order else 'paper_only'),
            'backtest_requested': a['requested'], 'paper_requested': b['requested'],
            'backtest_filled': a['filled'], 'paper_filled': b['filled'],
            'requested_quantity_gap': abs(a['requested'] - b['requested']),
            'filled_quantity_gap': abs(a['filled'] - b['filled']),
            'execution_price_gap_bps': price_gap,
            'fee_gap_cny': abs(a['fee'] - b['fee']),
        })
    metrics = {
        'orders_compared': len(differences),
        'unmatched_orders': sum(item['presence'] != 'both' for item in differences),
        'requested_quantity_gap': sum(item['requested_quantity_gap'] for item in differences),
        'filled_quantity_gap': sum(item['filled_quantity_gap'] for item in differences),
        'maximum_execution_price_gap_bps': max(
            (item['execution_price_gap_bps'] or 0. for item in differences), default=0.),
        'fee_gap_cny': sum(item['fee_gap_cny'] for item in differences),
    }
    if (not math.isfinite(metrics['maximum_execution_price_gap_bps'])
            or not math.isfinite(metrics['fee_gap_cny'])):
        raise ContractError('paper reconciliation metric overflow')
    within = (metrics['unmatched_orders'] <= policy['maximum_unmatched_orders']
              and metrics['requested_quantity_gap'] <= policy['maximum_requested_quantity_gap']
              and metrics['filled_quantity_gap'] <= policy['maximum_filled_quantity_gap']
              and metrics['maximum_execution_price_gap_bps']
              <= policy['maximum_execution_price_gap_bps']
              and metrics['fee_gap_cny'] <= policy['maximum_fee_gap_cny'])
    return {
        'profile': 'paper_backtest_fill_difference_v1',
        'scope': 'deterministic_simulation_difference_only',
        'decision_date': decision_date, 'execution_date': execution_date,
        'budget': policy, 'metrics': metrics, 'orders': differences,
        'budget_status': ('no_orders_to_compare' if not differences else
                          'within_declared_budget' if within else 'exceeds_declared_budget'),
    }


def _backtest_context(root: Path, generation: str) -> tuple[dict, dict, dict, list[str]]:
    backtest, config, artifacts = _verified_result(root, generation)
    if backtest['contract_version'] != 3:
        raise ContractError('paper reconciliation requires a v3 backtest')
    calendar_binding = config['calendar_binding']
    calendar = BacktestMarketDatasetStore(root).read_frame(
        calendar_binding['generation_id'], calendar_binding['checksum_sha256'],
        dataset_type='calendar', required_columns={'date'},
    )
    sessions = session_dates(calendar['date'].tolist(), label='backtest calendar')
    if sessions != sorted(set(sessions)):
        raise ContractError('paper reconciliation backtest calendar is not ordered and unique')
    return backtest, config, artifacts, sessions


def _compare_one(
    context: tuple[dict, dict, dict, list[str]], *, paper_root: Path,
    paper_generation: str, target_root: Path,
    budget: dict,
    backtest_fill_groups: dict[tuple[str, str], pd.DataFrame] | None = None,
) -> dict:
    backtest, config, artifacts, sessions = context
    paper, paper_frame = ExecutionResultStore(paper_root).read(paper_generation)
    plan, _ = OrderPlanStore(paper_root).read(paper['order_plan_generation_id'])
    paper_config = ExecutionConfigStore(paper_root).read(plan['execution_config_generation_id'])
    for field in ('execution_id', 'state_mode', 'decision_date', 'execution_date',
                  'target_weights_binding', 'input_state_binding', 'risk_decision_binding'):
        if paper[field] != plan[field] or plan[field] != paper_config[field]:
            raise ContractError(f'paper result/order plan/config {field} mismatch')
    if not (config['start_date'] <= paper['decision_date']
            < paper['execution_date'] <= config['end_date']):
        raise ContractError('paper decision/execution lies outside the backtest window')
    matches = [item for item in backtest['target_weight_bindings']
               if item['decision_date'] == paper['decision_date']]
    if (len(matches) != 1 or paper['target_weights_binding']['generation_id']
            != matches[0]['generation_id']
            or paper['target_weights_binding']['manifest_digest_sha256']
            != matches[0]['manifest_digest_sha256']):
        raise ContractError('paper and backtest target-weight bindings differ')
    target, _ = TargetWeightStore(target_root).read(
        matches[0]['generation_id'], paper['decision_date'])
    if (model_manifest_identities(target, schema_name='target_weights')
            != (matches[0]['generation_id'], matches[0]['manifest_digest_sha256'])):
        raise ContractError('paper reconciliation target-weight identity mismatch')
    if (paper['decision_date'] not in sessions
            or sessions.index(paper['decision_date']) + 1 >= len(sessions)
            or sessions[sessions.index(paper['decision_date']) + 1] != paper['execution_date']):
        raise ContractError('paper execution date is not the next backtest session')
    fills = (artifacts['fills'] if backtest_fill_groups is None else
             backtest_fill_groups.get((paper['execution_date'], paper['decision_date']),
                                      artifacts['fills'].iloc[:0]))
    result = compare_execution_fills(
        fills, paper_frame, decision_date=paper['decision_date'],
        execution_date=paper['execution_date'], budget=budget,
    )
    result['source_bindings'] = {
        'backtest': {'generation_id': backtest['generation_id'],
                     'manifest_digest_sha256': backtest['manifest_digest_sha256']},
        'paper': {'generation_id': paper['generation_id'],
                  'manifest_digest_sha256': paper['manifest_digest_sha256']},
        'paper_order_plan': {'generation_id': plan['generation_id'],
                             'manifest_digest_sha256': plan['manifest_digest_sha256']},
        'paper_config': {'generation_id': paper_config['generation_id'],
                         'manifest_digest_sha256': paper_config['manifest_digest_sha256']},
        'backtest_calendar': config['calendar_binding'],
        'target_weights': matches[0],
    }
    return result


def compare_published_execution(
    *, backtest_root: Path, backtest_generation: str,
    paper_root: Path, paper_generation: str, budget: dict,
    target_root: Path | None = None,
) -> dict:
    """Read both owning stores and require the exact same target-weight intent."""
    backtest_root, paper_root = Path(backtest_root), Path(paper_root)
    policy = _budget(budget)
    context = _backtest_context(backtest_root, backtest_generation)
    return _compare_one(context, paper_root=paper_root,
                        paper_generation=paper_generation,
                        target_root=Path(target_root or backtest_root), budget=policy)


def compare_published_execution_window(
    *, backtest_root: Path, backtest_generation: str,
    paper_root: Path, paper_generations: list[str], budget: dict,
    target_root: Path | None = None,
) -> dict:
    """Require one governed paper result per backtest decision date, then aggregate."""
    policy = _budget(budget)
    if (not isinstance(paper_generations, list) or not paper_generations
            or any(not isinstance(item, str) for item in paper_generations)
            or len(paper_generations) != len(set(paper_generations))):
        raise ContractError('paper reconciliation requires unique paper generations')
    backtest_root, paper_root = Path(backtest_root), Path(paper_root)
    context = _backtest_context(backtest_root, backtest_generation)
    backtest, config, artifacts, sessions = context
    expected = [item['decision_date'] for item in backtest['target_weight_bindings']]
    if not expected or len(paper_generations) != len(expected):
        raise ContractError('paper result count does not cover every backtest decision date')
    fills = artifacts['fills'].reset_index(drop=True)
    if {'date', 'decision_date'} - set(fills):
        raise ContractError('paper reconciliation fill columns are incomplete')
    execution_dates = session_dates(fills['date'].tolist(), label='backtest fill dates')
    decision_dates = session_dates(fills['decision_date'].tolist(), label='backtest decision dates')
    grouped = dict(iter(fills.groupby([
        pd.Series(execution_dates), pd.Series(decision_dates),
    ], sort=False))) if len(fills) else {}
    expected_pairs = set()
    for decision in expected:
        if decision not in sessions or sessions.index(decision) + 1 >= len(sessions):
            raise ContractError('backtest decision has no following execution session')
        expected_pairs.add((sessions[sessions.index(decision) + 1], decision))
    if set(grouped) - expected_pairs:
        raise ContractError('backtest fills contain a decision outside the target-weight window')
    daily = [_compare_one(
        context, paper_root=paper_root,
        paper_generation=generation, target_root=Path(target_root or backtest_root),
        budget=policy, backtest_fill_groups=grouped,
    ) for generation in paper_generations]
    daily.sort(key=lambda item: item['decision_date'])
    if [item['decision_date'] for item in daily] != expected:
        raise ContractError('paper results do not cover every backtest decision date exactly once')
    metrics = {
        'sessions_compared': len(daily),
        'sessions_without_orders': sum(item['metrics']['orders_compared'] == 0 for item in daily),
        'orders_compared': sum(item['metrics']['orders_compared'] for item in daily),
        'unmatched_orders': sum(item['metrics']['unmatched_orders'] for item in daily),
        'requested_quantity_gap': sum(item['metrics']['requested_quantity_gap'] for item in daily),
        'filled_quantity_gap': sum(item['metrics']['filled_quantity_gap'] for item in daily),
        'maximum_execution_price_gap_bps': max(
            item['metrics']['maximum_execution_price_gap_bps'] for item in daily),
        'fee_gap_cny': sum(item['metrics']['fee_gap_cny'] for item in daily),
    }
    if not math.isfinite(metrics['fee_gap_cny']):
        raise ContractError('paper reconciliation metric overflow')
    within = (metrics['unmatched_orders'] <= policy['maximum_unmatched_orders']
              and metrics['requested_quantity_gap'] <= policy['maximum_requested_quantity_gap']
              and metrics['filled_quantity_gap'] <= policy['maximum_filled_quantity_gap']
              and metrics['maximum_execution_price_gap_bps']
              <= policy['maximum_execution_price_gap_bps']
              and metrics['fee_gap_cny'] <= policy['maximum_fee_gap_cny'])
    return {
        'profile': 'paper_backtest_window_difference_v1',
        'scope': 'deterministic_simulation_difference_only',
        'period_start': config['start_date'], 'period_end': config['end_date'],
        'expected_decision_dates': expected, 'coverage_complete': True,
        'budget': policy, 'metrics': metrics,
        'budget_status': ('no_orders_to_compare' if metrics['orders_compared'] == 0 else
                          'within_declared_budget' if within else 'exceeds_declared_budget'),
        'daily': daily,
    }
