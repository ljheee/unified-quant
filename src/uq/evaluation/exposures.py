"""Reconstruct actual v3 holdings and report point-in-time exposure diagnostics."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from ..backtest.execution_facts import ExecutionFactsStore, RawExecutionPriceStore, facts_binding
from ..errors import ContractError
from .metrics import session_dates
from .report import _verified_result


def _number(value: object, *, label: str, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ContractError(f'invalid exposure {label}')
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f'invalid exposure {label}') from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ContractError(f'invalid exposure {label}')
    return result


def calculate_position_exposures(
    *, config: dict, equity: pd.DataFrame, fills: pd.DataFrame,
    raw_prices: pd.DataFrame, execution_facts: pd.DataFrame,
) -> dict:
    """Rebuild end-of-day positions from fills; unavailable classifications stay visible."""
    if ({'date', 'portfolio_value', 'cash', 'holdings_value'} - set(equity)
            or {'date', 'instrument', 'side', 'filled_shares', 'status'} - set(fills)
            or {'session_date', 'instrument', 'close', 'volume'} - set(raw_prices)
            or {'date', 'instrument', 'industry', 'suspended'} - set(execution_facts)):
        raise ContractError('exposure inputs have incomplete columns')
    days = session_dates(equity['date'].tolist(), label='exposure equity dates')
    if (not days or days != sorted(set(days))
            or days[0] != config['start_date'] or days[-1] != config['end_date']):
        raise ContractError('exposure equity calendar disagrees with backtest window')
    price_dates = session_dates(raw_prices['session_date'].tolist(), label='exposure raw dates')
    fact_dates = session_dates(execution_facts['date'].tolist(), label='exposure fact dates')
    fill_dates = session_dates(fills['date'].tolist(), label='exposure fill dates')
    if (set(price_dates) != set(days) or set(fact_dates) != set(days)
            or not set(fill_dates) <= set(days)):
        raise ContractError('exposure prices, facts or fills disagree with equity sessions')
    relevant_names = fills['instrument'].tolist()
    if any(not isinstance(instrument, str) or not instrument for instrument in relevant_names):
        raise ContractError('invalid exposure fill instrument')
    relevant = set(relevant_names)
    prices_by_day: dict[str, dict[str, tuple[float | None, float | None]]] = {day: {} for day in days}
    for day, row in zip(price_dates, raw_prices.itertuples(index=False)):
        if not isinstance(row.instrument, str):
            raise ContractError('invalid exposure raw price key')
        if row.instrument not in relevant:
            continue
        if row.instrument in prices_by_day[day]:
            raise ContractError('duplicate or invalid exposure raw price key')
        try:
            close, volume = float(row.close), float(row.volume)
        except (TypeError, ValueError) as exc:
            raise ContractError('invalid exposure raw price') from exc
        prices_by_day[day][row.instrument] = (
            close if math.isfinite(close) and close > 0 else None,
            volume if math.isfinite(volume) and volume > 0 else None,
        )
    industries: dict[tuple[str, str], str | None] = {}
    suspended: dict[tuple[str, str], bool] = {}
    for day, row in zip(fact_dates, execution_facts.itertuples(index=False)):
        if not isinstance(row.instrument, str):
            raise ContractError('invalid exposure industry key')
        if row.instrument not in relevant:
            continue
        key = (day, row.instrument)
        if key in industries:
            raise ContractError('duplicate exposure industry key')
        if not isinstance(row.suspended, (bool, np.bool_)):
            raise ContractError('invalid exposure suspension classification')
        industries[key] = row.industry
        suspended[key] = bool(row.suspended)
    orders: dict[str, list[dict]] = {day: [] for day in days}
    for day, row in zip(fill_dates, fills.to_dict('records')):
        orders[day].append(row)

    holdings: dict[str, int] = {}
    last_close: dict[str, float] = {}
    previous_liquidity: dict[str, tuple[float | None, float | None]] = {}
    daily = []
    for day, equity_row in zip(days, equity.to_dict('records')):
        current_prices = prices_by_day[day]
        for order in orders[day]:
            instrument, side = order['instrument'], order['side']
            quantity = order['filled_shares']
            if (not isinstance(instrument, str) or side not in {'buy', 'sell'}
                    or type(quantity) is not int or quantity < 0
                    or (quantity > 0) != (order['status'] == 'filled')):
                raise ContractError('invalid exposure fill state')
            if quantity:
                current = holdings.get(instrument, 0)
                updated = current + quantity * (1 if side == 'buy' else -1)
                if updated < 0:
                    raise ContractError('exposure reconstruction would oversell inventory')
                if updated:
                    holdings[instrument] = updated
                else:
                    holdings.pop(instrument, None)
        nav = _number(equity_row['portfolio_value'], label='portfolio value', positive=True)
        cash = _number(equity_row['cash'], label='cash')
        recorded_holdings = _number(equity_row['holdings_value'], label='holdings value')
        if cash < -0.01 or recorded_holdings < -0.01 or abs(nav - cash - recorded_holdings) > 0.011:
            raise ContractError('exposure equity accounting does not reconcile')
        values = {}
        for instrument, quantity in holdings.items():
            current = current_prices.get(instrument)
            close = current[0] if current is not None else None
            if close is None:
                if current is not None:
                    if not suspended.get((day, instrument), False) or instrument not in last_close:
                        raise ContractError('held instrument has unusable current close')
                elif not suspended.get((day, instrument), False) or instrument not in last_close:
                    raise ContractError('held instrument has no visible valuation close')
                close = last_close[instrument]
            values[instrument] = quantity * close
        if abs(sum(values.values()) - recorded_holdings) > 0.011:
            raise ContractError('exposure positions do not reproduce reviewed holdings value')
        weights = {instrument: value / nav for instrument, value in values.items()}
        by_industry: dict[str, float] = {}
        unclassified = 0.
        unknown_liquidity = 0.
        liquidity_ratios = []
        for instrument, weight in weights.items():
            key = (day, instrument)
            if key not in industries:
                raise ContractError('held instrument lacks visible execution facts')
            industry = industries[key]
            if industry is None:
                unclassified += weight
            elif isinstance(industry, str) and industry.strip():
                by_industry[industry] = by_industry.get(industry, 0.) + weight
            else:
                raise ContractError('invalid exposure industry classification')
            prior = previous_liquidity.get(instrument)
            if prior is None or prior[0] is None or prior[1] is None:
                unknown_liquidity += weight
            else:
                liquidity_ratios.append(values[instrument] / (prior[0] * prior[1]))
        cash_weight = cash / nav
        if (not math.isfinite(cash_weight) or
                any(not math.isfinite(value) for value in
                    [*weights.values(), *by_industry.values(), *liquidity_ratios])):
            raise ContractError('exposure arithmetic is not finite')
        daily.append({
            'date': day, 'portfolio_value_cny': nav, 'cash_weight': cash_weight,
            'max_single_weight': max(weights.values(), default=0.),
            'single_name_hhi': sum(weight * weight for weight in weights.values()),
            'industry_weights': dict(sorted(by_industry.items())),
            'max_industry_weight': max(by_industry.values()) if by_industry else None,
            'unclassified_weight': unclassified,
            'max_position_to_prior_turnover_ratio': max(liquidity_ratios) if liquidity_ratios else None,
            'unknown_liquidity_weight': unknown_liquidity,
            'held_instruments': len(holdings),
        })
        last_close.update({instrument: close for instrument, (close, _) in current_prices.items()
                           if close is not None})
        previous_liquidity = current_prices
    return {
        'profile': 'actual_v3_position_exposures_v1',
        'scope': 'diagnostic_only_no_quality_authorization',
        'daily': daily,
        'summary': {
            'max_single_weight': max(row['max_single_weight'] for row in daily),
            'max_industry_weight': max((row['max_industry_weight'] or 0. for row in daily)),
            'max_unclassified_weight': max(row['unclassified_weight'] for row in daily),
            'max_position_to_prior_turnover_ratio': max(
                (row['max_position_to_prior_turnover_ratio'] or 0. for row in daily)),
            'max_unknown_liquidity_weight': max(row['unknown_liquidity_weight'] for row in daily),
            'market_cap_exposure_status': 'unavailable_without_governed_market_cap_source',
        },
    }


def evaluate_published_exposures(*, backtest_root: Path, backtest_generation: str) -> dict:
    """Read reviewed v3 inputs and recompute actual holdings, never target-only weights."""
    root = Path(backtest_root)
    backtest, config, artifacts = _verified_result(root, backtest_generation)
    if backtest['contract_version'] != 3:
        raise ContractError('actual exposure evaluation requires a v3 backtest')
    raw_binding, raw = RawExecutionPriceStore(root).read_window(
        config['start_date'], config['end_date'], config['price_source_binding'])
    facts_manifest, facts = ExecutionFactsStore(root).read(
        config['execution_facts_binding']['generation_id'])
    if facts_binding(facts_manifest) != config['execution_facts_binding']:
        raise ContractError('exposure execution facts binding differs from reviewed config')
    report = calculate_position_exposures(
        config=config, equity=artifacts['equity_curve'], fills=artifacts['fills'],
        raw_prices=raw, execution_facts=facts,
    )
    report['source_bindings'] = {
        'backtest': {'generation_id': backtest_generation,
                     'manifest_digest_sha256': backtest['manifest_digest_sha256']},
        'config': {'generation_id': config['generation_id'],
                   'manifest_digest_sha256': config['manifest_digest_sha256']},
        'execution_facts': config['execution_facts_binding'],
        'raw_prices': raw_binding,
    }
    return report
