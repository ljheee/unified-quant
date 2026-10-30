"""Actual exposure diagnostics reconstruct holdings from reviewed v3 fills."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.evaluation.exposures import calculate_position_exposures, evaluate_published_exposures
from tests.test_backtest_stress import baseline

ROOT = Path(__file__).resolve().parents[1]


def _frames():
    days = ['2025-01-06', '2025-01-07']
    equity = pd.DataFrame([
        {'date': days[0], 'portfolio_value': 1000., 'cash': 1000., 'holdings_value': 0.},
        {'date': days[1], 'portfolio_value': 1000., 'cash': 700., 'holdings_value': 300.},
    ])
    fills = pd.DataFrame([
        {'date': days[1], 'instrument': 'A', 'side': 'buy',
         'filled_shares': 10, 'status': 'filled'},
        {'date': days[1], 'instrument': 'B', 'side': 'buy',
         'filled_shares': 10, 'status': 'filled'},
    ])
    raw = pd.DataFrame([
        {'session_date': pd.Timestamp(day), 'instrument': instrument,
         'close': close, 'volume': volume}
        for day in days for instrument, close, volume in
        [('A', 10., 1000.), ('B', 20., 50.)]
    ])
    facts = pd.DataFrame([
        {'date': day, 'instrument': instrument, 'industry': industry, 'suspended': False}
        for day in days for instrument, industry in [('A', 'bank'), ('B', 'technology')]
    ])
    return {'start_date': days[0], 'end_date': days[1]}, equity, fills, raw, facts


def test_position_weights_industry_and_prior_turnover_are_recomputed():
    config, equity, fills, raw, facts = _frames()
    report = calculate_position_exposures(
        config=config, equity=equity, fills=fills, raw_prices=raw, execution_facts=facts)
    day = report['daily'][1]
    assert day['max_single_weight'] == pytest.approx(.2)
    assert day['single_name_hhi'] == pytest.approx(.05)
    assert day['industry_weights'] == {'bank': pytest.approx(.1),
                                       'technology': pytest.approx(.2)}
    assert day['max_industry_weight'] == pytest.approx(.2)
    assert day['max_position_to_prior_turnover_ratio'] == pytest.approx(.2)
    assert day['cash_weight'] == pytest.approx(.7)
    assert report['summary']['market_cap_exposure_status'] == (
        'unavailable_without_governed_market_cap_source')


def test_missing_classification_and_prior_liquidity_are_explicit():
    config, equity, fills, raw, facts = _frames()
    facts.loc[(facts['date'] == '2025-01-07') & (facts['instrument'] == 'B'),
              'industry'] = None
    raw.loc[(raw['session_date'] == pd.Timestamp('2025-01-06')) &
            (raw['instrument'] == 'B'), 'volume'] = 0.
    report = calculate_position_exposures(
        config=config, equity=equity, fills=fills, raw_prices=raw, execution_facts=facts)
    day = report['daily'][1]
    assert day['unclassified_weight'] == pytest.approx(.2)
    assert day['unknown_liquidity_weight'] == pytest.approx(.2)
    assert day['max_industry_weight'] == pytest.approx(.1)


def test_suspended_holding_uses_last_visible_close_and_bad_returned_close_rejects():
    config, equity, fills, raw, facts = _frames()
    config['end_date'] = '2025-01-08'
    equity = pd.concat([equity, pd.DataFrame([{
        'date': '2025-01-08', 'portfolio_value': 1000.,
        'cash': 700., 'holdings_value': 300.,
    }])], ignore_index=True)
    raw = pd.concat([raw, pd.DataFrame([{
        'session_date': pd.Timestamp('2025-01-08'),
        'instrument': 'A', 'close': 10., 'volume': 1000.,
    }, {
        'session_date': pd.Timestamp('2025-01-08'),
        'instrument': 'B', 'close': float('nan'), 'volume': 0.,
    }])], ignore_index=True)
    facts = pd.concat([facts, pd.DataFrame([
        {'date': '2025-01-08', 'instrument': 'A', 'industry': 'bank', 'suspended': False},
        {'date': '2025-01-08', 'instrument': 'B', 'industry': 'technology', 'suspended': True},
    ])], ignore_index=True)
    report = calculate_position_exposures(
        config=config, equity=equity, fills=fills, raw_prices=raw, execution_facts=facts)
    assert report['daily'][2]['industry_weights']['technology'] == pytest.approx(.2)
    bad_facts = facts.copy()
    bad_facts.loc[(bad_facts['date'] == '2025-01-08') & (bad_facts['instrument'] == 'B'),
                  'suspended'] = False
    with pytest.raises(ContractError, match='unusable current close'):
        calculate_position_exposures(config=config, equity=equity, fills=fills,
                                     raw_prices=raw, execution_facts=bad_facts)


def test_mismatched_equity_and_oversold_inventory_fail_closed():
    config, equity, fills, raw, facts = _frames()
    equity.loc[1, 'holdings_value'] = 301.
    equity.loc[1, 'portfolio_value'] = 1001.
    with pytest.raises(ContractError, match='do not reproduce reviewed holdings value'):
        calculate_position_exposures(config=config, equity=equity, fills=fills,
                                     raw_prices=raw, execution_facts=facts)
    _, equity, fills, _, _ = _frames()
    fills.loc[0, 'side'] = 'sell'
    with pytest.raises(ContractError, match='oversell inventory'):
        calculate_position_exposures(config=config, equity=equity, fills=fills,
                                     raw_prices=raw, execution_facts=facts)


def test_reviewed_v3_sources_and_cli_are_read_and_raw_tamper_rejected(tmp_path):
    backtest = baseline(tmp_path)
    report = evaluate_published_exposures(
        backtest_root=tmp_path, backtest_generation=backtest['generation_id'])
    assert [row['held_instruments'] for row in report['daily']] == [0, 1, 0]
    assert report['daily'][1]['industry_weights']['bank'] == pytest.approx(
        report['daily'][1]['max_single_weight'])
    assert report['source_bindings']['backtest']['generation_id'] == backtest['generation_id']
    command = [sys.executable, str(ROOT / 'scripts/evaluate_portfolio_exposures.py'),
               '--backtest-root', str(tmp_path), '--backtest-generation', backtest['generation_id']]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['report']['summary'] == report['summary']
    raw_path = tmp_path / 'canonical/bars_daily/v2/date=2025-01-07/data.parquet'
    with raw_path.open('ab') as stream:
        stream.write(b'tamper')
    with pytest.raises(ContractError, match='checksum'):
        evaluate_published_exposures(backtest_root=tmp_path,
                                     backtest_generation=backtest['generation_id'])
