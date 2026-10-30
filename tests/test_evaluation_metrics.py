from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from uq.errors import ContractError
from uq.evaluation.metrics import evaluate_returns, hac_mean, information_coefficients


def daily_frame():
    return pd.DataFrame({
        'date': ['2024-12-27', '2024-12-30', '2024-12-31', '2025-01-02', '2025-01-03'],
        'strategy_return': [0., .1, -.2, .25, 0.],
        'benchmark_return': [0., .05, -.1, .1, 0.],
        'invested': [False, True, True, True, False],
        'turnover': [0., .2, .1, .3, 0.],
        'regime': ['sideways', 'up', 'down', 'up', 'sideways'],
    })


def test_net_metrics_distinguish_win_rates_and_recompute_active_returns():
    daily = daily_frame()
    report = evaluate_returns(daily)
    overall = report['overall']
    assert overall['cumulative_return'] == pytest.approx(.1)
    assert overall['annualized_excess_return'] == pytest.approx(
        1.1 ** (252 / 5) - (1.05 * .9 * 1.1) ** (252 / 5))
    assert overall['all_period_win_rate'] == .4
    assert overall['holding_day_win_rate'] == pytest.approx(2 / 3)
    active = np.array([0., .05, -.1, .15, 0.])
    assert overall['information_ratio'] == pytest.approx(active.mean() / active.std(ddof=1) * math.sqrt(252))
    assert overall['total_turnover'] == pytest.approx(.6)
    assert report['by_year']['2024']['sessions'] == 3
    assert report['by_year']['2025']['sessions'] == 2
    assert report['by_regime']['down']['cumulative_return'] == pytest.approx(-.2)
    assert report['regime_coverage'] == 'all_three'
    assert report == evaluate_returns(daily.iloc[::-1].reset_index(drop=True))


def test_drawdown_reports_initial_peak_recovery_and_unrecovered_episode():
    report = evaluate_returns(daily_frame())['overall']['drawdown']
    assert report['max_drawdown'] == pytest.approx(.2)
    assert report['peak_date'] == '2024-12-30'
    assert report['trough_date'] == '2024-12-31'
    assert report['recovery_date'] == '2025-01-02'
    assert report['recovery_sessions'] == 2
    assert not report['unrecovered']
    daily = daily_frame().iloc[:2].copy()
    daily['strategy_return'] = [-.1, 0.]
    drawdown = evaluate_returns(daily)['overall']['drawdown']
    assert drawdown['max_drawdown'] == pytest.approx(.1)
    assert drawdown['peak_date'] is None  # initial capital before first observation
    assert drawdown['trough_date'] == daily['date'].iloc[0]
    assert drawdown['recovery_date'] is None
    assert drawdown['unrecovered']
    assert drawdown['longest_underwater_sessions'] == 2


def test_zero_variance_short_and_all_cash_periods_are_explicitly_undefined():
    daily = daily_frame().iloc[:1].copy()
    overall = evaluate_returns(daily)['overall']
    assert overall['holding_day_win_rate'] is None
    assert overall['holding_sessions'] == 0
    assert overall['information_ratio'] is None
    assert overall['tracking_error'] is None
    assert overall['drawdown']['max_drawdown'] == 0


@pytest.mark.parametrize('column,value,match', [
    ('strategy_return', float('nan'), 'non-finite'),
    ('benchmark_return', float('inf'), 'non-finite'),
    ('strategy_return', -1., '-100%'),
    ('turnover', -.1, 'negative'),
    ('invested', 'False', 'booleans'),
    ('regime', 'bull', 'up/down/sideways'),
    ('date', '2025-13-01', 'ISO'),
])
def test_invalid_metric_inputs_are_refused(column, value, match):
    daily = daily_frame().astype({column: 'object'})
    daily.loc[0, column] = value
    if column in {'strategy_return', 'benchmark_return', 'turnover'}:
        daily[column] = daily[column].astype(float)
    with pytest.raises(ContractError, match=match):
        evaluate_returns(daily)


def test_duplicates_numeric_strings_and_overflow_are_not_published_as_metrics():
    daily = daily_frame()
    with pytest.raises(ContractError, match='duplicate'):
        evaluate_returns(pd.concat([daily, daily.iloc[:1]]))
    daily['strategy_return'] = daily['strategy_return'].astype(str)
    with pytest.raises(ContractError, match='numeric'):
        evaluate_returns(daily)
    daily['strategy_return'] = 1e200
    with pytest.raises(ContractError, match='finite precision'):
        evaluate_returns(daily)


def test_overlap_robust_mean_has_hand_calculated_bartlett_variance():
    # centered observations [-1,-1,1,1]; sum squares=4, lag-one product sum=1.
    # Bartlett lag-one weight=.5 => sandwich meat=5, Var(mean)=5/16.
    result = hac_mean([-1., -1., 1., 1.], lags=1)
    assert result['standard_error'] == pytest.approx(math.sqrt(5) / 4)
    assert result['effective_sessions'] == pytest.approx(3.2)
    assert result['standard_error'] > hac_mean([-1., -1., 1., 1.], lags=0)['standard_error']
    assert result['valid_sessions'] == 4
    assert hac_mean([.1, .2, .3], lags=4)['status'] == 'insufficient_observations'
    assert hac_mean([.1] * 8, lags=4)['status'] == 'zero_variance'


def test_hac_does_not_compress_missing_trading_sessions():
    spaced = hac_mean([1., None, -1., -1., 1.], lags=1)
    compressed = hac_mean([1., -1., -1., 1.], lags=1)
    assert spaced['standard_error'] == pytest.approx(.5)
    assert compressed['standard_error'] == pytest.approx(math.sqrt(3) / 4)
    assert spaced['calendar_sessions'] == 5
    assert spaced['valid_sessions'] == 4


@pytest.mark.parametrize('values', [['not-a-number'], [True], [object()]])
def test_hac_rejects_non_numeric_boundary_values_as_contract_errors(values):
    with pytest.raises(ContractError, match='HAC values must be numeric or null'):
        hac_mean(values, lags=0)


def supervised_frames():
    days = pd.bdate_range('2025-01-06', periods=8).strftime('%Y-%m-%d').tolist()
    predictions, labels = [], []
    for index, day in enumerate(days):
        for name, score, label in zip('ABCD', [1., 1., 2., 3.], [1., 2., 3., 4.]):
            predictions.append({'instrument': name, 'decision_date': day, 'score': score})
            labels.append({'instrument': name, 'decision_date': day,
                           'label': label * (1 if index < 4 else -1)})
    return days, pd.DataFrame(predictions), pd.DataFrame(labels)


def test_ic_is_daily_cross_sectional_and_rank_ties_use_average():
    days, scores, labels = supervised_frames()
    summary, daily = information_coefficients(scores, labels, calendar_dates=days, horizon_sessions=5)
    expected = np.corrcoef([1., 1., 2., 3.], [1., 2., 3., 4.])[0, 1]
    expected_rank = np.corrcoef([1.5, 1.5, 3., 4.], [1., 2., 3., 4.])[0, 1]
    assert daily['ic'].iloc[0] == pytest.approx(expected)
    assert daily['rank_ic'].iloc[0] == pytest.approx(expected_rank)
    assert daily['ic'].iloc[-1] == pytest.approx(-expected)
    assert summary['ic']['mean'] == pytest.approx(0)
    assert summary['ic']['valid_sessions'] == 8
    assert summary['nominal_prediction_rows'] == 32
    assert summary['ic']['lags'] == 4
    assert summary['ic']['effective_sessions'] < 8
    with pytest.raises(ContractError, match='overlapping'):
        information_coefficients(scores, labels, calendar_dates=days, horizon_sessions=5, hac_lags=0)


def test_ic_explains_missing_null_constant_and_insufficient_cross_sections():
    days, scores, labels = supervised_frames()
    missing = (labels['decision_date'] == days[0]) & (labels['instrument'] == 'A')
    labels = labels.loc[~missing].copy()
    labels.loc[(labels['decision_date'] == days[0]) & (labels['instrument'] == 'B'), 'label'] = np.nan
    scores.loc[scores['decision_date'] == days[1], 'score'] = 1.
    scores = scores.loc[scores['decision_date'] != days[2]]
    summary, daily = information_coefficients(scores, labels, calendar_dates=days, horizon_sessions=5)
    assert daily['status'].iloc[0] == 'insufficient_pairs'
    assert daily['status'].iloc[1] == 'constant_cross_section'
    assert daily['predictions'].iloc[2] == 0
    assert summary['absent_label_keys'] == summary['null_labels'] == 1
    assert summary['ic']['valid_sessions'] == 5
    assert summary['ic']['status'] == 'insufficient_observations'


def test_ic_refuses_duplicate_keys_nonfinite_scores_and_dates_outside_calendar():
    days, scores, labels = supervised_frames()
    with pytest.raises(ContractError, match='duplicate predictions'):
        information_coefficients(pd.concat([scores, scores.iloc[:1]]), labels,
                                 calendar_dates=days, horizon_sessions=5)
    with pytest.raises(ContractError, match='outside'):
        information_coefficients(scores, labels, calendar_dates=days[1:], horizon_sessions=5)
    scores.loc[0, 'score'] = np.inf
    with pytest.raises(ContractError, match='non-finite'):
        information_coefficients(scores, labels, calendar_dates=days, horizon_sessions=5)


def test_missing_label_table_is_reported_without_inventing_pairs():
    days, scores, labels = supervised_frames()
    labels = labels.iloc[:0]
    summary, daily = information_coefficients(scores, labels, calendar_dates=days, horizon_sessions=5)
    assert summary['absent_label_keys'] == len(scores)
    assert summary['matched_pairs'] == 0
    assert summary['ic']['status'] == 'insufficient_observations'
    assert daily['ic'].isna().all()
