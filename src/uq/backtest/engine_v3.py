"""Chronological raw-price T+1 replay with explicit liquidity and cost budgets."""
from __future__ import annotations

import math
import uuid
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..contracts.gate_contracts import sha256_json
from ..contracts.model_layer import ModelContractLoader
from ..errors import ContractError
from ..risk.contracts import ORDER_GATE_EXECUTABLE_ACTIONS
from .execution_facts import execution_facts_fingerprint, normalize_execution_facts

_FILL_COLUMNS = [
    'date', 'instrument', 'side', 'target_shares', 'filled_shares',
    'gross_execution_price', 'net_execution_price', 'commission_fee', 'stamp_duty_fee', 'status',
    'decision_date', 'unfilled_shares', 'liquidity_cap_shares', 'impact_bps',
    'slippage_cost', 'impact_cost', 'constraint_reasons', 'limit_reference_price', 'reference_adjustment_bps',
]


def _cent(value: float) -> float:
    if not math.isfinite(value):
        raise ContractError('execution fee arithmetic is not finite')
    with localcontext() as context:
        context.prec = 400
        return float(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def execution_costs(config: dict, *, side: str, shares: int, open_price: float, previous_volume: float) -> dict:
    if side not in {'buy', 'sell'} or type(shares) is not int or shares < 0:
        raise ContractError('invalid execution cost order')
    if shares == 0:
        return {'net_price': open_price, 'commission': 0., 'stamp_duty': 0., 'impact_bps': 0.,
                'slippage_cost': 0., 'impact_cost': 0., 'cash_value': 0.}
    if not math.isfinite(open_price) or open_price <= 0 or not math.isfinite(previous_volume) or previous_volume <= 0:
        raise ContractError('filled execution requires positive price and prior-session volume')
    model = config['cost_model']
    participation = shares / previous_volume
    impact = model['impact_model']
    measure = math.sqrt(participation) if impact['type'] == 'square_root' else participation
    impact_bps = min(impact['maximum_bps'], impact['coefficient_bps'] * measure)
    slippage = model['slippage_bps'] / 10000
    impact_rate = impact_bps / 10000
    net_price = open_price * (1 + (slippage + impact_rate) * (1 if side == 'buy' else -1))
    notional = shares * net_price
    if not math.isfinite(notional) or net_price <= 0:
        raise ContractError('execution price/notional arithmetic is invalid')
    commission = _cent(max(model['minimum_commission_cny'], notional * model['commission_bps'] / 10000))
    stamp = _cent(notional * model['stamp_duty_bps'] / 10000) if side == 'sell' else 0.
    return {'net_price': net_price, 'commission': commission, 'stamp_duty': stamp,
            'impact_bps': impact_bps, 'slippage_cost': shares * open_price * slippage,
            'impact_cost': shares * open_price * impact_rate,
            'cash_value': notional + commission if side == 'buy' else notional - commission - stamp}


def _floor(value: float, lot: int) -> int:
    if not math.isfinite(value) or value < 0 or value > np.iinfo(np.int64).max:
        raise ContractError('execution share arithmetic is invalid')
    return int(value // lot) * lot


def _bounded_quantity(maximum: int, lot: int, allowed: Callable[[int], bool]) -> int:
    low, high = 0, maximum // lot
    while low < high:
        middle = (low + high + 1) // 2
        if allowed(middle * lot):
            low = middle
        else:
            high = middle - 1
    return low * lot


def run_v3(
    *, config: dict, portfolio_definition: dict, weight_partitions: dict[str, pd.DataFrame],
    price_panel: pd.DataFrame, execution_facts: pd.DataFrame | None,
    suspension_dates: set[tuple[str, str]] | None = None,
    corporate_action_instruments: set[str] | None = None,
    risk_gate: Callable[[dict, dict], dict] | None = None,
) -> tuple[dict, dict[str, pd.DataFrame]]:
    ModelContractLoader.validate('backtest_config_v3', config)
    costs = config['cost_model']
    if costs['slippage_bps'] + costs['impact_model']['maximum_bps'] >= 10000:
        raise ContractError('slippage plus maximum impact must remain below 10000 bps')
    if execution_facts is None:
        raise ContractError('v3 requires bound execution facts')
    facts = normalize_execution_facts(execution_facts)
    if execution_facts_fingerprint(facts) != config['execution_facts_binding']['logical_checksum_sha256']:
        raise ContractError('execution facts do not match the bound logical checksum')
    facts = facts.set_index(['date', 'instrument'])
    if (not isinstance(price_panel.index, pd.MultiIndex) or list(price_panel.index.names) != ['date', 'instrument']
            or price_panel.index.duplicated().any() or not {'open', 'close', 'volume'} <= set(price_panel)):
        raise ContractError('v3 needs unique (date,instrument) raw open/close/volume prices')
    dates = sorted(set(price_panel.index.get_level_values('date')))
    if (not dates or dates[0] != config['start_date'] or dates[-1] != config['end_date']
            or any(not isinstance(day, str) or not config['start_date'] <= day <= config['end_date'] for day in dates)):
        raise ContractError('v3 price calendar must exactly cover the configured window')
    if sorted(set(facts.index.get_level_values('date'))) != dates:
        raise ContractError('v3 execution facts must exactly cover the price calendar')
    if not weight_partitions or not set(weight_partitions) <= set(dates):
        raise ContractError('v3 decisions must be nonempty and inside the price calendar')
    risk = config['risk_constraints']
    drawdown_stop = risk.get('max_drawdown_stop')
    volatility_target = risk.get('volatility_target')
    if volatility_target is not None:
        lookback = volatility_target['lookback_sessions']
        min_history = volatility_target['min_history_sessions']
        target_volatility = float(volatility_target['target_annualized'])
        if min_history > lookback:
            raise ContractError('volatility target min_history_sessions cannot exceed lookback_sessions')
        if not math.isfinite(target_volatility) or target_volatility <= 0:
            raise ContractError('volatility target must be finite and positive')
    for day, frame in weight_partitions.items():
        if not {'instrument', 'weight'} <= set(frame) or frame['instrument'].duplicated().any():
            raise ContractError('invalid v3 target-weight keys')
        values = frame['weight'].to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values < 0).any() or values.sum() > 1 + 1e-10:
            raise ContractError('v3 target weights must be finite, nonnegative and sum to at most one')
        if set(frame['instrument']) & (corporate_action_instruments or set()):
            raise ContractError('corporate-action instruments must remain excluded until position events are modeled')
    execution = config['execution_model']
    first_decision = min(dates.index(day) for day in weight_partitions)
    holdings: dict[str, int] = {}
    last_close: dict[str, float] = {}
    cash = float(config['initial_capital'])
    previous_nav = cash
    peak_nav = cash
    completed_returns: list[float] = []
    drawdown_stop_active = False
    fills, equity, daily = [], [], []

    def fact(day: str, instrument: str) -> pd.Series:
        if (day, instrument) not in facts.index:
            raise ContractError(f'missing execution facts for {day}/{instrument}')
        return facts.loc[(day, instrument)]

    def market_price(day: str, instrument: str, column: str) -> float | None:
        if (day, instrument) not in price_panel.index:
            return None
        value = float(price_panel.loc[(day, instrument), column])
        return value if math.isfinite(value) and value > 0 else None

    def position_values(day: str, column: str) -> dict[str, float]:
        values = {}
        for instrument, shares in holdings.items():
            price = market_price(day, instrument, column)
            if price is None:
                if not fact(day, instrument)['suspended'] or instrument not in last_close:
                    raise ContractError(f'no usable {column} for held instrument {instrument} on {day}')
                price = last_close[instrument]
            values[instrument] = shares * price
        return values

    for index, day in enumerate(dates):
        decision_day = dates[index - 1] if index else None
        scheduled = (decision_day in weight_partitions and
                     (index - 1 - first_decision) % execution['rebalance_every_n_sessions'] == 0)
        drawdown_stop_triggered = False
        if drawdown_stop is not None and not drawdown_stop_active:
            prior_drawdown = max(0.0, 1.0 - previous_nav / peak_nav)
            if prior_drawdown + 1e-12 >= float(drawdown_stop):
                drawdown_stop_active = True
                drawdown_stop_triggered = True
        traded = 0.
        day_start_holdings = dict(holdings)
        volatility_estimate = None
        target_exposure_scale = 1.0
        if scheduled and not drawdown_stop_active and volatility_target is not None:
            history = completed_returns[-volatility_target['lookback_sessions']:]
            if len(history) >= volatility_target['min_history_sessions']:
                volatility_estimate = float(np.std(history, ddof=1) * math.sqrt(252))
                if not math.isfinite(volatility_estimate):
                    raise ContractError('trailing strategy volatility estimate is non-finite')
                if volatility_estimate > 1e-12:
                    target_exposure_scale = min(
                        1.0, float(volatility_target['target_annualized']) / volatility_estimate
                    )
        if scheduled or drawdown_stop_active:
            targets = ({} if drawdown_stop_active else
                       weight_partitions[decision_day].set_index('instrument')['weight'].to_dict())
            if target_exposure_scale < 1.0:
                targets = {instrument: float(weight) * target_exposure_scale
                           for instrument, weight in targets.items()}
            desired = {}
            for instrument, weight in targets.items():
                rules = fact(day, instrument)
                opening = market_price(day, instrument, 'open')
                reference = opening or float(rules['raw_previous_close'])
                desired[instrument] = _floor(float(weight) * previous_nav / reference, int(rules['buy_lot']))
            instruments = sorted(set(desired) | set(holdings))
            instruments.sort(key=lambda instrument: (desired.get(instrument, 0) >= holdings.get(instrument, 0), instrument))
            for instrument in instruments:
                current = holdings.get(instrument, 0)
                target = desired.get(instrument, 0)
                if target == current:
                    continue
                side = 'sell' if target < current else 'buy'
                requested = abs(target - current)
                rules = fact(day, instrument)
                opening = market_price(day, instrument, 'open')
                previous_volume = (float(price_panel.loc[(decision_day, instrument), 'volume'])
                                   if (decision_day, instrument) in price_panel.index else 0.)
                if not math.isfinite(previous_volume) or previous_volume < 0:
                    raise ContractError('prior-session volume must be finite and nonnegative')
                lot = int(rules['buy_lot'] if side == 'buy' else rules['sell_lot'])
                odd_liquidation = side == 'sell' and requested == current and requested % lot != 0
                raw_capacity = previous_volume * execution['volume_participation_cap'] * execution['fill_rate_multiplier']
                cap = (requested if odd_liquidation and raw_capacity >= requested
                       else _floor(raw_capacity, lot))
                reasons = []
                actual = min(requested, cap)
                if actual < requested:
                    reasons.append('participation')
                suspended = bool(rules['suspended']) or (day, instrument) in (suspension_dates or set())
                if opening is None and not suspended:
                    raise ContractError('missing raw open for a tradable execution instrument')
                if suspended:
                    reasons.append('suspended')
                    actual = 0
                if opening is not None and instrument in last_close and abs(float(rules['raw_previous_close']) - last_close[instrument]) > .011:
                    raise ContractError('execution facts raw previous close disagrees with canonical raw prices')
                if opening is not None and rules['has_price_limit']:
                    if not float(rules['limit_down']) - 1e-9 <= opening <= float(rules['limit_up']) + 1e-9:
                        raise ContractError('raw open lies outside its published price bands')
                    if (side == 'buy' and opening >= float(rules['limit_up'])) or (side == 'sell' and opening <= float(rules['limit_down'])):
                        reasons.append('limit_up' if side == 'buy' else 'limit_down')
                        actual = 0
                if side == 'sell':
                    available = day_start_holdings.get(instrument, 0)
                    if actual > available:
                        reasons.append('t1')
                    actual = min(actual, available)
                if opening is not None and actual:
                    if risk['max_turnover'] is not None:
                        budget = max(2 * risk['max_turnover'] * previous_nav - traded, 0.)
                        raw_turnover_shares = budget / opening
                        turnover_shares = (requested if odd_liquidation and raw_turnover_shares >= requested
                                           else _floor(raw_turnover_shares, lot))
                        if actual > turnover_shares:
                            reasons.append('turnover')
                            actual = min(actual, turnover_shares)
                    if risk_gate is not None and actual:
                        candidate = {'order_id': f'{decision_day}:{day}:{instrument}:{side}',
                                     'instrument': instrument, 'side': side, 'shares': actual}
                        context = {'execution_date': day, 'execution_price': opening,
                                   'previous_close': float(rules['limit_reference_price']),
                                   'decision_date_volume': previous_volume, 'holdings': dict(holdings), 'cash': cash,
                                   'suspended': False, 'sellable_shares': day_start_holdings.get(instrument, 0),
                                   'board_lot': lot, 'slippage_bps': costs['slippage_bps'],
                                   'limit_up_price': None if not rules['has_price_limit'] else float(rules['limit_up']),
                                   'limit_down_price': None if not rules['has_price_limit'] else float(rules['limit_down'])}
                        decision = risk_gate(candidate, context)
                        if decision['action'] not in ORDER_GATE_EXECUTABLE_ACTIONS:
                            reasons.append('risk_blocked'); actual = 0
                        elif decision['action'] == 'resize':
                            allowed = [item['value'] for item in decision.get('constraints', [])
                                       if item.get('instrument') == instrument and item.get('constraint_type') == 'allowed_shares']
                            if not allowed or any(type(value) is not int or value < 0 for value in allowed):
                                raise ContractError('risk resize requires nonnegative integer allowed_shares')
                            if min(allowed) < actual:
                                reasons.append('risk_resize')
                                actual = min(actual, min(allowed))
                    if actual:
                        if not (odd_liquidation and actual == requested):
                            actual = _floor(actual, lot)
                        if rules['has_price_limit']:
                            def band_allowed(shares):
                                price = execution_costs(config, side=side, shares=shares,
                                    open_price=opening, previous_volume=previous_volume)['net_price']
                                return (float(rules['limit_down']) - 1e-9 <= price
                                        <= float(rules['limit_up']) + 1e-9)
                            if not band_allowed(int(actual)):
                                reasons.append('execution_price_band')
                                actual = _bounded_quantity(actual, lot, band_allowed)
                        if side == 'buy':
                            open_values = position_values(day, 'open')
                            open_nav = cash + sum(open_values.values())
                            industry_exposures = {}
                            if risk['max_industry_weight'] is not None:
                                for held, value in open_values.items():
                                    industry = fact(day, held)['industry']
                                    if industry is None:
                                        raise ContractError('industry cap requires classifications for every traded/held name')
                                    industry_exposures[industry] = industry_exposures.get(industry, 0.) + value
                                if rules['industry'] is None:
                                    raise ContractError('industry cap requires classifications for every traded/held name')

                            def projected(shares):
                                payment = execution_costs(config, side='buy', shares=shares,
                                    open_price=opening, previous_volume=previous_volume)['cash_value']
                                return open_nav - payment + shares * opening, cash - payment

                            def single_allowed(shares):
                                after_nav, _ = projected(shares)
                                exposures = {**open_values, instrument: (current + shares) * opening}
                                return all(value <= risk['max_single_weight'] * after_nav + 1e-9
                                           for value in exposures.values())

                            def industry_allowed(shares):
                                after_nav, _ = projected(shares)
                                exposures = dict(industry_exposures)
                                industry = rules['industry']
                                exposures[industry] = exposures.get(industry, 0.) + shares * opening
                                return all(value <= risk['max_industry_weight'] * after_nav + 1e-9
                                           for value in exposures.values())

                            def cash_allowed(shares):
                                after_nav, after_cash = projected(shares)
                                return after_cash + 1e-9 >= risk['cash_reserve'] * after_nav

                            constraints = [('single_name', single_allowed)]
                            if risk['max_industry_weight'] is not None:
                                constraints.append(('industry', industry_allowed))
                            constraints.append(('cash', cash_allowed))
                            for reason, allowed in constraints:
                                if not allowed(actual):
                                    reasons.append(reason)
                                    actual = _bounded_quantity(actual, lot, allowed)
                            if actual < int(rules['min_buy_shares']):
                                reasons.append('minimum_lot'); actual = 0
                quote = execution_costs(config, side=side, shares=int(actual), open_price=opening or 0.,
                                        previous_volume=previous_volume)
                if side == 'sell' and actual and cash + quote['cash_value'] < -1e-9:
                    reasons.append('cash'); actual = 0
                    quote = execution_costs(config, side=side, shares=0, open_price=opening or 0., previous_volume=previous_volume)
                if actual:
                    cash += quote['cash_value'] * (-1 if side == 'buy' else 1)
                    holdings[instrument] = current + actual * (1 if side == 'buy' else -1)
                    if holdings[instrument] == 0:
                        del holdings[instrument]
                    traded += actual * opening
                fills.append({
                    'date': day, 'instrument': instrument, 'side': side, 'target_shares': requested,
                    'filled_shares': int(actual), 'gross_execution_price': opening or 0.,
                    'net_execution_price': quote['net_price'], 'commission_fee': quote['commission'],
                    'stamp_duty_fee': quote['stamp_duty'], 'status': 'filled' if actual else 'skipped_' + (reasons[-1] if reasons else 'lot'),
                    'decision_date': decision_day, 'unfilled_shares': requested - int(actual),
                    'liquidity_cap_shares': cap, 'impact_bps': quote['impact_bps'],
                    'slippage_cost': quote['slippage_cost'], 'impact_cost': quote['impact_cost'],
                    'constraint_reasons': ','.join(reasons), 'limit_reference_price': float(rules['limit_reference_price']),
                    'reference_adjustment_bps': (float(rules['limit_reference_price']) / float(rules['raw_previous_close']) - 1) * 10000,
                })
        values = position_values(day, 'close')
        nav = cash + sum(values.values())
        if not math.isfinite(nav) or nav <= 0 or cash < -1e-9:
            raise ContractError('v3 portfolio is insolvent or has non-finite accounting')
        industries = {}
        for instrument, value in values.items():
            industry = fact(day, instrument)['industry']
            if risk['max_industry_weight'] is not None and industry is None:
                raise ContractError('industry cap requires classifications for every traded/held name')
            if industry is not None:
                industries[industry] = industries.get(industry, 0.) + value
        breach = (any(value / nav > risk['max_single_weight'] + 1e-10 for value in values.values())
                  or (risk['max_industry_weight'] is not None and any(value / nav > risk['max_industry_weight'] + 1e-10 for value in industries.values()))
                  or cash / nav + 1e-10 < risk['cash_reserve'])
        equity.append({'date': day, 'portfolio_value': nav, 'cash': cash, 'holdings_value': sum(values.values())})
        daily_return = nav / previous_nav - 1
        daily.append({'date': day, 'daily_return': daily_return,
                      'executed_turnover': traded / (2 * previous_nav), 'cash_ratio': cash / nav,
                      'rebalance_scheduled': bool(scheduled), 'risk_budget_breached': bool(breach),
                      'volatility_target_estimated_annualized': volatility_estimate,
                      'volatility_target_scale': target_exposure_scale,
                      'volatility_target_applied': bool(target_exposure_scale < 1.0),
                      'drawdown_stop_active': bool(drawdown_stop_active),
                      'drawdown_stop_triggered': bool(drawdown_stop_triggered)})
        completed_returns.append(daily_return)
        previous_nav = nav
        peak_nav = max(peak_nav, nav)
        for instrument in price_panel.loc[day].index:
            close = market_price(day, instrument, 'close')
            if close is not None:
                last_close[instrument] = close
    artifacts = {'equity_curve': pd.DataFrame(equity), 'daily_metrics': pd.DataFrame(daily),
                 'fills': pd.DataFrame(fills, columns=_FILL_COLUMNS)}
    returns = artifacts['daily_metrics']['daily_return'].to_numpy(dtype=float)
    volatility = float(returns.std(ddof=1)) * math.sqrt(252) if len(returns) > 1 else 0.
    wealth = np.r_[float(config['initial_capital']), artifacts['equity_curve']['portfolio_value'].to_numpy(dtype=float)]
    fill_frame = artifacts['fills']
    requested_notional = float((fill_frame['target_shares'] * fill_frame['gross_execution_price']).sum())
    filled_notional = float((fill_frame['filled_shares'] * fill_frame['gross_execution_price']).sum())
    try:
        annualized_return = math.expm1(math.log(nav / config['initial_capital']) * 252 / len(dates))
    except (OverflowError, ValueError) as exc:
        raise ContractError('v3 annualized return exceeds finite precision') from exc
    drawdown_trigger_dates = artifacts['daily_metrics'].loc[
        artifacts['daily_metrics']['drawdown_stop_triggered'], 'date'
    ].tolist()
    summary = {
        'cumulative_return': nav / config['initial_capital'] - 1,
        'annualized_return': annualized_return,
        'annualized_volatility': volatility,
        'sharpe_ratio': float(returns.mean() * 252 / volatility) if volatility > 1e-12 else None,
        'max_drawdown': float(np.max(1 - wealth / np.maximum.accumulate(wealth))),
        'avg_daily_turnover': float(artifacts['daily_metrics']['executed_turnover'].mean()),
        'win_rate': float((returns > 0).mean()),
        'total_commission_cny': float(fill_frame['commission_fee'].sum()),
        'total_stamp_duty_cny': float(fill_frame['stamp_duty_fee'].sum()),
        'total_slippage_cny': float(fill_frame['slippage_cost'].sum()),
        'total_impact_cny': float(fill_frame['impact_cost'].sum()),
        'requested_notional_cny': requested_notional, 'filled_notional_cny': filled_notional,
        'fill_ratio': filled_notional / requested_notional if requested_notional else None,
        'risk_budget_breach_sessions': int(artifacts['daily_metrics']['risk_budget_breached'].sum()),
        'drawdown_stop_triggered': bool(drawdown_trigger_dates),
        'drawdown_stop_trigger_date': drawdown_trigger_dates[0] if drawdown_trigger_dates else None,
        'drawdown_stop_sessions': int(artifacts['daily_metrics']['drawdown_stop_active'].sum()),
        'volatility_target_applied_sessions': int(artifacts['daily_metrics']['volatility_target_applied'].sum()),
        'minimum_volatility_target_scale': float(artifacts['daily_metrics']['volatility_target_scale'].min()),
    }
    manifest = {
        'contract_version': 3, 'schema_version': '1.0.0',
        'backtest_config_generation_id': config['generation_id'],
        'portfolio_definition_generation_id': portfolio_definition['generation_id'],
        'target_weight_bindings': [], 'price_source_binding': config['price_source_binding'],
        'execution_facts_binding': config['execution_facts_binding'],
        'execution_semantics': 'chronological_raw_daily_t1_v3',
        'period_start': dates[0], 'period_end': dates[-1], 'trading_days': len(dates), 'summary_metrics': summary,
        'run_id': str(uuid.uuid5(uuid.NAMESPACE_URL, sha256_json({'config': config['generation_id'],
                          'portfolio': portfolio_definition['generation_id'], 'summary': summary}))),
        'created_at': '1970-01-01T00:00:00+00:00', 'quality_report_checksum_sha256': '0' * 64,
        'generation_id': '0' * 64, 'manifest_digest_sha256': '0' * 64,
    }
    for name, frame in artifacts.items():
        manifest[name + '_artifact'] = {
            'file': name + '.parquet', 'checksum_sha256': '0' * 64, 'row_count': len(frame),
            'columns': list(frame.columns), 'dtypes': {column: str(dtype) for column, dtype in frame.dtypes.items()},
            'serialization_profile_id': 'parquet-v1',
        }
    return manifest, artifacts
