from __future__ import annotations

import math
from datetime import date
from typing import Any, Sequence

import numpy as np
import pandas as pd

from ..errors import ContractError

ANNUALIZATION_SESSIONS = 252
REGIMES = frozenset({'up', 'down', 'sideways'})


def session_dates(values: Sequence[Any], *, label: str) -> list[str]:
    """Normalize timestamps at the boundary, then preserve session order."""
    out = []
    for value in values:
        if isinstance(value, (pd.Timestamp, date)):
            value = value.date().isoformat() if isinstance(value, pd.Timestamp) else value.isoformat()
        try:
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError as exc:
            raise ContractError(f'{label} requires ISO session dates') from exc
        out.append(value)
    return out


def _finite(frame: pd.DataFrame, columns: list[str]) -> None:
    if frame.empty:
        return
    for column in columns:
        if not pd.api.types.is_numeric_dtype(frame[column]) or pd.api.types.is_bool_dtype(frame[column]):
            raise ContractError(f'{column} must be numeric')
        try:
            values = frame[column].to_numpy(dtype=float)
        except (TypeError, ValueError) as exc:
            raise ContractError(f'{column} must be numeric') from exc
        if not np.isfinite(values).all():
            raise ContractError(f'{column} contains non-finite values')


def _drawdown(returns: np.ndarray, dates: list[str]) -> dict[str, Any]:
    wealth = np.cumprod(1.0 + returns)
    peak, peak_index, max_dd = 1.0, -1, 0.0
    worst_peak, worst_trough = -1, None
    longest_underwater = 0
    for index, value in enumerate(wealth):
        if value >= peak:
            peak, peak_index = float(value), index
        else:
            longest_underwater = max(longest_underwater, index - peak_index)
            dd = 1.0 - value / peak
            if dd > max_dd:
                max_dd, worst_peak, worst_trough = float(dd), peak_index, index
    recovery = None
    if worst_trough is not None:
        peak_value = 1.0 if worst_peak == -1 else wealth[worst_peak]
        recovery = next((i for i in range(worst_trough + 1, len(wealth))
                         if wealth[i] >= peak_value), None)
    return {
        'max_drawdown': max_dd,
        'peak_date': None if worst_peak == -1 else dates[worst_peak],
        'trough_date': None if worst_trough is None else dates[worst_trough],
        'recovery_date': None if recovery is None else dates[recovery],
        'recovery_sessions': None if recovery is None else recovery - worst_peak,
        'longest_underwater_sessions': longest_underwater,
        'unrecovered': worst_trough is not None and recovery is None,
    }


def _return_summary(frame: pd.DataFrame, annualization: int) -> dict[str, Any]:
    strategy = frame['strategy_return'].to_numpy(dtype=float)
    benchmark = frame['benchmark_return'].to_numpy(dtype=float)
    active = strategy - benchmark
    count = len(frame)
    try:
        with np.errstate(over='raise', invalid='raise', under='ignore'):
            strategy_growth = float(np.prod(1 + strategy))
            benchmark_growth = float(np.prod(1 + benchmark))
        if min(strategy_growth, benchmark_growth) <= 0:
            raise ValueError('growth underflow')
        strategy_annual = math.expm1(math.log(strategy_growth) * annualization / count)
        benchmark_annual = math.expm1(math.log(benchmark_growth) * annualization / count)
        relative_annual = math.expm1((math.log(strategy_growth) - math.log(benchmark_growth))
                                    * annualization / count)
    except (FloatingPointError, OverflowError, ValueError) as exc:
        raise ContractError('evaluation arithmetic exceeds finite precision') from exc
    active_std = float(np.std(active, ddof=1)) if count > 1 else 0.0
    holding = frame['invested'].to_numpy(dtype=bool)
    holding_count = int(holding.sum())
    return {
        'sessions': count,
        'cumulative_return': strategy_growth - 1,
        'benchmark_cumulative_return': benchmark_growth - 1,
        'annualized_return': strategy_annual,
        'benchmark_annualized_return': benchmark_annual,
        'annualized_excess_return': strategy_annual - benchmark_annual,
        'relative_annualized_return': relative_annual,
        'tracking_error': active_std * math.sqrt(annualization) if count > 1 else None,
        'information_ratio': float(np.mean(active) / active_std * math.sqrt(annualization))
                             if count > 1 and active_std > 1e-15 else None,
        'all_period_win_rate': float(np.mean(strategy > 0)),
        'holding_sessions': holding_count,
        'holding_day_win_rate': float(np.mean(strategy[holding] > 0)) if holding_count else None,
        'avg_daily_turnover': float(frame['turnover'].mean()),
        'total_turnover': float(frame['turnover'].sum()),
        'drawdown': _drawdown(strategy, frame['date'].tolist()),
    }


def evaluate_returns(daily: pd.DataFrame, *, annualization: int = ANNUALIZATION_SESSIONS) -> dict:
    """Evaluate aligned, already costed returns without silently joining calendars.

    ``invested`` means an inventory or filled trade existed during that return
    interval, including a liquidation day ending in cash. Regimes are supplied
    explicitly; no hindsight market-state labels are invented by this function.
    """
    required = {'date', 'strategy_return', 'benchmark_return', 'invested', 'turnover'}
    if required - set(daily):
        raise ContractError(f'evaluation daily input missing columns: {sorted(required - set(daily))}')
    if daily.empty:
        raise ContractError('evaluation daily input is empty')
    if type(annualization) is not int or annualization < 1:
        raise ContractError('annualization must be a positive integer')
    frame = daily.copy()
    frame['date'] = session_dates(frame['date'].tolist(), label='evaluation')
    if frame['date'].duplicated().any():
        raise ContractError('duplicate evaluation session')
    frame = frame.sort_values('date', kind='mergesort').reset_index(drop=True)
    _finite(frame, ['strategy_return', 'benchmark_return', 'turnover'])
    if (frame[['strategy_return', 'benchmark_return']] <= -1).any().any():
        raise ContractError('evaluation returns must exceed -100%')
    if (frame['turnover'] < 0).any():
        raise ContractError('negative evaluation turnover')
    if not frame['invested'].map(lambda value: isinstance(value, (bool, np.bool_))).all():
        raise ContractError('invested must contain booleans')
    if 'regime' in frame and (frame['regime'].isna().any() or not set(frame['regime']) <= REGIMES):
        raise ContractError('market regimes must be up/down/sideways with complete coverage')
    output = {
        'metric_profile': 'net_return_comparison_v1', 'annualization_sessions': annualization,
        'annualized_excess_definition': 'strategy_cagr_minus_benchmark_cagr',
        'information_ratio_definition': 'mean_daily_arithmetic_active_return/sample_std*sqrt(annualization)',
        'holding_day_definition': 'inventory_or_filled_trade_during_return_interval',
        'drawdown_origin': 'initial_capital_before_first_return',
        'subperiod_definition': 'compound_selected_observations; reset_peak_to_one_per_group',
        'overall': _return_summary(frame, annualization),
        'by_year': {str(year): _return_summary(group, annualization)
                    for year, group in frame.groupby(frame['date'].str[:4], sort=True)},
        'by_regime': {},
        'regime_coverage': 'not_supplied',
    }
    if 'regime' in frame:
        output['by_regime'] = {name: _return_summary(group, annualization)
                               for name, group in frame.groupby('regime', sort=True)}
        output['regime_coverage'] = 'all_three' if set(frame['regime']) == REGIMES else 'partial'
    # Fail closed on arithmetic overflow instead of writing non-standard JSON.
    from ..contracts.model_layer import canonical_json
    try:
        canonical_json(output)
    except (ValueError, OverflowError) as exc:
        raise ContractError('evaluation arithmetic produced non-finite metrics') from exc
    return output


def hac_mean(values: Sequence[float | None], *, lags: int) -> dict[str, Any]:
    """Bartlett Newey-West variance for the mean, with fixed session spacing.

    Missing days occupy their original calendar positions. Centered estimating
    equations are zero for unavailable cross-sections; they are not compressed
    into adjacent sessions. No finite-sample multiplier or normal p-value is used.
    """
    if type(lags) is not int or lags < 0:
        raise ContractError('HAC lags must be a non-negative integer')
    if any(value is not None and (isinstance(value, (bool, np.bool_))
                                  or not isinstance(value, (int, float, np.number)))
           for value in values):
        raise ContractError('HAC values must be numeric or null')
    try:
        array = np.array([np.nan if value is None else value for value in values], dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContractError('HAC values must be numeric or null') from exc
    if np.isinf(array).any():
        raise ContractError('HAC input contains infinite values')
    valid = np.isfinite(array)
    count = int(valid.sum())
    mean = float(array[valid].mean()) if count else None
    output = {
        'mean': mean, 'valid_sessions': count, 'calendar_sessions': len(array), 'lags': lags,
        'method': 'newey_west_bartlett_mean_no_small_sample_correction',
        'standard_error': None, 't_statistic': None, 'effective_sessions': None,
        'status': 'insufficient_observations',
    }
    if count < max(lags + 2, 2):
        return output
    centered = np.where(valid, array - mean, 0.0)
    sum_squares = float(centered @ centered)
    meat = sum_squares
    for lag in range(1, min(lags, len(array) - 1) + 1):
        meat += 2 * (1 - lag / (lags + 1)) * float(centered[lag:] @ centered[:-lag])
    variance = max(meat, 0.0) / count ** 2
    error = math.sqrt(variance)
    output.update({
        'standard_error': error,
        't_statistic': float(mean / error) if error > 1e-15 else None,
        'effective_sessions': min(float(count), count * sum_squares / meat) if meat > 1e-30 else None,
        'status': 'estimated' if error > 1e-15 else 'zero_variance',
    })
    return output


def information_coefficients(
    predictions: pd.DataFrame, labels: pd.DataFrame, *, calendar_dates: list[str],
    horizon_sessions: int, hac_lags: int | None = None,
) -> tuple[dict, pd.DataFrame]:
    """Daily cross-sectional Pearson/rank IC; never use stock rows as independent days."""
    calendar = session_dates(calendar_dates, label='IC calendar')
    if not calendar or calendar != sorted(set(calendar)):
        raise ContractError('IC calendar must be nonempty, sorted and unique')
    if type(horizon_sessions) is not int or horizon_sessions < 1:
        raise ContractError('IC horizon must be a positive integer')
    lags = horizon_sessions - 1 if hac_lags is None else hac_lags
    if type(lags) is not int or lags < horizon_sessions - 1:
        raise ContractError('IC HAC lags must cover the overlapping label horizon')
    normalized = []
    for label, frame, value in [('predictions', predictions, 'score'), ('labels', labels, 'label')]:
        required = {'instrument', 'decision_date', value}
        if required - set(frame):
            raise ContractError(f'{label} missing columns: {sorted(required - set(frame))}')
        work = frame[list(sorted(required))].copy()
        work['decision_date'] = session_dates(work['decision_date'].tolist(), label=label)
        if work['instrument'].isna().any() or not work['instrument'].map(
            lambda name: isinstance(name, str) and bool(name.strip())
        ).all():
            raise ContractError(f'{label} contains invalid instruments')
        if work.duplicated(['instrument', 'decision_date']).any():
            raise ContractError(f'duplicate {label} keys')
        if label == 'predictions' and not set(work['decision_date']) <= set(calendar):
            raise ContractError('predictions contain dates outside the IC calendar')
        _finite(work.loc[work[value].notna()], [value])
        if label == 'predictions' and work[value].isna().any():
            raise ContractError('predictions contain null scores')
        normalized.append(work)
    merged = normalized[0].merge(normalized[1], on=['instrument', 'decision_date'],
                                 how='left', validate='one_to_one', indicator=True)
    groups = {key: group for key, group in merged.groupby('decision_date', sort=False)}
    rows = []
    for day in calendar:
        group = groups.get(day, merged.iloc[:0])
        present = group['_merge'] == 'both'
        complete = group.loc[present & group['label'].notna()]
        ic = rank_ic = None
        status = 'insufficient_pairs'
        if len(complete) >= 3:
            if complete['score'].nunique() < 2 or complete['label'].nunique() < 2:
                status = 'constant_cross_section'
            else:
                ic = _correlation(complete['score'], complete['label'])
                rank_ic = _correlation(complete['score'].rank(method='average'),
                                       complete['label'].rank(method='average'))
                status = 'estimated'
        rows.append({
            'date': day, 'predictions': len(group), 'pairs': len(complete),
            'absent_label_keys': int((~present).sum()),
            'null_labels': int((present & group['label'].isna()).sum()),
            'ic': ic, 'rank_ic': rank_ic, 'status': status,
        })
    daily = pd.DataFrame(rows)
    summary = {
        'metric_profile': 'daily_cross_section_ic_v1', 'label_horizon_sessions': horizon_sessions,
        'rank_tie_method': 'average', 'minimum_pairs': 3,
        'missing_session_policy': 'retain_calendar_positions_in_hac',
        'ic': hac_mean(daily['ic'].tolist(), lags=lags),
        'rank_ic': hac_mean(daily['rank_ic'].tolist(), lags=lags),
        'nominal_prediction_rows': len(predictions),
        'matched_pairs': int(daily['pairs'].sum()),
        'absent_label_keys': int(daily['absent_label_keys'].sum()),
        'null_labels': int(daily['null_labels'].sum()),
    }
    return summary, daily


def _correlation(left: pd.Series, right: pd.Series) -> float:
    vectors = []
    for values in (left, right):
        vector = values.to_numpy(dtype=float)
        vector = vector / float(np.max(np.abs(vector)))
        vector = vector - vector.mean()
        vector /= np.linalg.norm(vector)
        vectors.append(vector)
    return float(np.clip(vectors[0] @ vectors[1], -1, 1))
