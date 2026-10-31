from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.gate_contracts import sha256_json
from uq.errors import ContractError
from uq.evaluation.walk_forward import WalkForwardStore
from uq.evaluation import walk_forward_continuous as continuous
from tests.test_backtest_v3 import NAME, inputs
from tests.test_research_chain_phase4 import _config_decision, _result_decision
from tests.test_walk_forward import protocol


def test_fold_targets_are_reloaded_from_each_verified_backtest_and_cover_oos(monkeypatch, tmp_path):
    days = ['2025-01-06', '2025-01-07', '2025-01-08']
    plan = {'folds': [{'fold_id': 'fold-0000', 'test_dates': days[:2]},
                      {'fold_id': 'fold-0001', 'test_dates': days[2:]}]}
    records = []
    expected_manifests = {}
    for index, day in enumerate(days):
        root = tmp_path / f'fold-{index // 2}'
        digest = sha256_json({'backtest': day})
        target_digest = sha256_json({'target': day})
        generation = sha256_json({'weights': day})
        expected_manifests[(generation, day)] = {
            'generation_id': generation, 'manifest_digest_sha256': target_digest,
            'weights_checksum_sha256': sha256_json({'frame': day}),
        }
        if index % 2 == 0:
            records.append({
                'fold_id': f'fold-{index // 2}', 'data_root': str(root),
                'result_content_generation_id': sha256_json({'result': index // 2}),
                'result_manifest_digest_sha256': sha256_json({'result-digest': index // 2}),
                'request_content_generation_id': sha256_json({'request': index // 2}),
                'run_id': f'run-{index // 2}', 'backtest_generation_id': sha256_json({'bt': index // 2}),
            })
    plan['folds'][0]['backtest_days'] = days[:2]
    plan['folds'][1]['backtest_days'] = days[2:]
    # Each fold record is sourced from one result manifest with its own set of dates.
    by_root = {str(tmp_path / 'fold-0'): days[:2], str(tmp_path / 'fold-1'): days[2:]}
    for record in records:
        record['backtest_manifest_digest_sha256'] = sha256_json({'bt-digest': record['fold_id']})

    class FakeResearchRunStore:
        def __init__(self, root):
            self.root = Path(root)

        def read_result(self, generation, digest):
            record = next(item for item in records if item['data_root'] == str(self.root))
            assert (generation, digest) == (
                record['result_content_generation_id'], record['result_manifest_digest_sha256'])
            output = {
                'output_family': 'backtest_result', 'generation_id': record['backtest_generation_id'],
                'manifest_digest_sha256': record['backtest_manifest_digest_sha256'],
            }
            return {
                'final_status': 'passed', 'request_content_generation_id': record['request_content_generation_id'],
                'run_id': record['run_id'], 'stage_records': [{'output_bindings': [output]}],
            }

    def verified(root, generation):
        record = next(item for item in records if item['data_root'] == str(root))
        bindings = []
        for day in by_root[str(root)]:
            source = next(key for key in expected_manifests if key[1] == day)
            binding = expected_manifests[source]
            bindings.append({'decision_date': day, 'generation_id': binding['generation_id'],
                             'manifest_digest_sha256': binding['manifest_digest_sha256']})
        return ({'manifest_digest_sha256': record['backtest_manifest_digest_sha256'],
                 'target_weight_bindings': bindings}, {'contract_version': 3, 'cost_model': {}}, {})

    class FakeTargetWeightStore:
        def __init__(self, root):
            self.root = Path(root)

        def read(self, generation, day):
            manifest = expected_manifests[(generation, day)]
            return manifest, pd.DataFrame({'instrument': [NAME], 'weight': [0.1]})

    monkeypatch.setattr(continuous, 'FileResearchRunStore', FakeResearchRunStore)
    monkeypatch.setattr(continuous, '_verified_result', verified)
    monkeypatch.setattr(continuous, 'TargetWeightStore', FakeTargetWeightStore)
    monkeypatch.setattr(continuous, '_verify_review', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(continuous, '_verified_target_weights', lambda root, generation, day: (
        expected_manifests[(generation, day)], pd.DataFrame({'instrument': [NAME], 'weight': [0.1]}),
    ))
    attempt = {'fold_records': records}

    partitions, sources, configs = continuous._fold_targets(plan, attempt)

    assert list(partitions) == days
    assert [source['decision_date'] for source in sources] == days
    assert all(frame['weight'].tolist() == [0.1] for frame in partitions.values())
    assert len(configs) == 2


@pytest.mark.parametrize(
    'terminal_suspended,expected_liquidation', [(False, True), (True, False)]
)
def test_continuous_replay_uses_one_v3_engine_account_and_attempts_terminal_liquidation(
    monkeypatch, tmp_path, terminal_suspended, expected_liquidation,
):
    from uq.backtest.engine import BacktestResultStore

    plan = protocol(periods=145)
    oos_dates = [day for fold in plan['folds'] for day in fold['test_dates']]
    replay_dates = plan['calendar_dates'][
        plan['calendar_dates'].index(oos_dates[0]):plan['calendar_dates'].index(oos_dates[-1]) + 3
    ]
    walk_root, result_root = tmp_path / 'walk-forward', tmp_path / 'replay'
    walk_store = WalkForwardStore(walk_root)
    walk_store.publish_plan(plan)
    records = [{'fold_id': fold['fold_id'], 'status': 'passed', 'test_dates': fold['test_dates']}
               for fold in plan['folds']]
    attempt_path = walk_store.publish_attempt(plan, records, status='passed', error_type=None)

    template, _template_prices, template_facts = inputs(tmp_path / 'template')
    prices = pd.DataFrame([
        {'date': day, 'instrument': NAME, 'open': 10., 'close': 10., 'volume': 100_000.}
        for day in replay_dates
    ]).set_index(['date', 'instrument'])
    from tests.test_backtest_execution_facts import publish_raw
    from uq.backtest.execution_facts import ExecutionFactsStore, RawExecutionPriceStore, facts_binding
    from uq.research_chain.owning_contracts import BacktestConfigStore, BacktestMarketDatasetStore

    publish_raw(result_root, prices)
    facts = pd.DataFrame([
        {**template_facts.iloc[0].to_dict(), 'date': day,
         'visible_at': day + 'T09:00:00+08:00',
         'suspended': terminal_suspended and day == replay_dates[-1]}
        for day in replay_dates
    ])
    facts_manifest = ExecutionFactsStore(result_root).publish(
        facts, source_id='isolated-continuous-fixture', source_approval_ref='test-only-not-production',
    )
    market = BacktestMarketDatasetStore(result_root)
    bindings = {}
    market_frames = {
        'calendar': pd.DataFrame({'date': replay_dates, 'instrument': ['__calendar__'] * len(replay_dates)}),
        'suspension': pd.DataFrame({'date': replay_dates, 'instrument': [NAME] * len(replay_dates),
                                    'suspended': [False] * len(replay_dates)}),
        'corporate_action': pd.DataFrame({'date': [replay_dates[0]], 'instrument': ['000001.XSHE']}),
    }
    for kind, frame in market_frames.items():
        published = market.publish(
            frame, dataset_type=kind, window_start=date.fromisoformat(replay_dates[0]),
            window_end=date.fromisoformat(replay_dates[-1]), sources={'fixture': 'isolated'},
            producer='test_walk_forward_continuous',
        )
        if kind == 'calendar':
            bindings['calendar_binding'] = {
                'generation_id': published['generation_id'],
                'checksum_sha256': published['data_checksum_sha256'],
            }
        else:
            bindings[kind + '_binding'] = {
                'dataset_generation_id': published['generation_id'],
                'data_checksum_sha256': published['data_checksum_sha256'],
            }

    config = dict(template)
    config.update(bindings)
    config['start_date'], config['end_date'] = replay_dates[0], replay_dates[-1]
    config['initial_capital'] = 100_000.
    config['execution_facts_binding'] = facts_binding(facts_manifest)
    config['price_source_binding'], _ = RawExecutionPriceStore(result_root).read_window(
        replay_dates[0], replay_dates[-1],
    )
    for name in ('generation_id', 'manifest_digest_sha256', 'quality_report_checksum_sha256'):
        config[name] = '0' * 64
    config = BacktestConfigStore(result_root).publish(config, quality_decision=_config_decision())

    from uq.contracts.model_layer import model_manifest_identities
    from uq.portfolio.builder import PortfolioBuilder, TargetWeightStore
    from tests.test_portfolio_layer import _make_definition, _make_quality_decision

    source_root = tmp_path / 'source-fold'
    definition = _make_definition(
        scheme_parameters={'n': 1},
        constraints={'max_single_weight': 0.1, 'max_industry_weight': None,
                     'max_turnover': None, 'cash_reserve': 0.0},
    )
    definition['generation_id'], definition['manifest_digest_sha256'] = model_manifest_identities(
        definition, schema_name='portfolio_definition',
    )
    weight_partitions, sources = {}, []
    for day in oos_dates:
        manifest, frame = PortfolioBuilder(source_root).build(
            definition=definition, prediction_generation_id='b' * 64, decision_date=day,
            scores=pd.Series({NAME: 1.}), universe_instruments=[NAME],
        )
        TargetWeightStore(source_root).publish(
            manifest, frame, quality_decision=_make_quality_decision(),
        )
        published, frame = TargetWeightStore(source_root).read(manifest['generation_id'], day)
        weight_partitions[day] = frame
        sources.append({
            'decision_date': day, 'store_root': str(source_root.absolute()),
            'generation_id': published['generation_id'],
            'manifest_digest_sha256': published['manifest_digest_sha256'],
            'weights_checksum_sha256': published['weights_checksum_sha256'],
        })
    monkeypatch.setattr(continuous, '_fold_targets', lambda _plan, _attempt: (
        weight_partitions, sources, [config for _ in _plan['folds']],
    ))

    outcome = continuous.run_continuous_walk_forward_backtest(
        walk_forward_root=walk_root, attempt_path=attempt_path,
        backtest_root=result_root, backtest_config_generation_id=config['generation_id'],
        quality_decision=_result_decision(),
    )

    result, artifacts = BacktestResultStore(result_root).read(outcome['backtest_result_generation_id'])
    assert result['period_start'] == replay_dates[0]
    assert result['period_end'] == replay_dates[-1]
    assert [item['decision_date'] for item in result['target_weight_bindings']] == oos_dates
    assert len(artifacts['equity_curve']) == len(replay_dates)
    # The account carries holdings across folds, then makes a final guarded sale.
    assert artifacts['fills']['filled_shares'].gt(0).sum() == (3 if expected_liquidation else 2)
    assert artifacts['fills']['date'].iloc[-1] == replay_dates[-1]
    assert artifacts['fills']['side'].iloc[-1] == 'sell'
    terminal_value = float(artifacts['equity_curve']['holdings_value'].iloc[-1])
    assert (terminal_value <= 1e-8) is expected_liquidation
    receipt = json.loads(Path(outcome['receipt_path']).read_text(encoding='utf-8'))
    assert receipt['record_version'] == 2
    assert receipt['terminal_liquidation_attempted'] is True
    assert receipt['terminal_liquidation'] is expected_liquidation
    assert receipt['terminal_liquidation_decision_date'] == replay_dates[-2]
    assert receipt['terminal_liquidation_execution_date'] == replay_dates[-1]
    assert receipt['terminal_remaining_holdings_value'] == terminal_value
    if not expected_liquidation:
        assert terminal_value > 0.
        assert receipt['terminal_liquidation_unfilled_statuses'] == ['skipped_suspended']
    assert receipt['capital_semantics'] == 'single_backtest_engine_run_continuous_cash_and_holdings'
    assert continuous.WalkForwardContinuousStore(walk_root).read(
        outcome['backtest_result_generation_id']) == receipt
    assert outcome['trading_acceptance'] == 'not_certified_by_continuous_replay'

    forged_receipt = dict(receipt)
    forged_receipt['terminal_remaining_holdings_value'] = True
    forged_receipt['checksum_sha256'] = sha256_json({
        key: value for key, value in forged_receipt.items() if key != 'checksum_sha256'
    })
    receipt_path = Path(outcome['receipt_path'])
    receipt_path.write_text(json.dumps(forged_receipt, sort_keys=True) + '\n', encoding='utf-8')
    with pytest.raises(ContractError, match='terminal liquidation does not recompute'):
        continuous.WalkForwardContinuousStore(walk_root).read(
            outcome['backtest_result_generation_id'])
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + '\n', encoding='utf-8')

    from uq.backtest.engine import BacktestEngine
    monkeypatch.setattr(BacktestEngine, 'run', lambda *_args, **_kwargs: pytest.fail('retry must read existing replay'))
    retried = continuous.run_continuous_walk_forward_backtest(
        walk_forward_root=walk_root, attempt_path=attempt_path,
        backtest_root=result_root, backtest_config_generation_id=config['generation_id'],
        quality_decision=_result_decision(),
    )
    assert retried['status'] == 'continuous_backtest_reused'
    assert retried['backtest_result_generation_id'] == outcome['backtest_result_generation_id']


def test_continuous_replay_rejects_partial_attempt_before_reading_weights(tmp_path, monkeypatch):
    plan = protocol(periods=145)
    walk_store = WalkForwardStore(tmp_path)
    walk_store.publish_plan(plan)
    record = {'fold_id': plan['folds'][0]['fold_id'], 'status': 'passed',
              'test_dates': plan['folds'][0]['test_dates']}
    path = walk_store.publish_attempt(plan, [record], status='failed', error_type='RuntimeError',
                                      failed_fold={'fold_id': plan['folds'][1]['fold_id']})
    monkeypatch.setattr(continuous, '_fold_targets', lambda *_: pytest.fail('must reject before loading weights'))
    with pytest.raises(ContractError, match='fully passed'):
        continuous.run_continuous_walk_forward_backtest(
            walk_forward_root=tmp_path, attempt_path=path, backtest_root=tmp_path / 'backtest',
            backtest_config_generation_id='a' * 64, quality_decision=_result_decision(),
        )


def test_continuous_receipt_v1_remains_readable_without_terminal_liquidation(tmp_path, monkeypatch):
    plan = protocol(periods=145)
    walk_root, backtest_root = tmp_path / 'walk-forward', tmp_path / 'backtest'
    store = WalkForwardStore(walk_root)
    store.publish_plan(plan)
    fold_records = [
        {'fold_id': fold['fold_id'], 'status': 'passed', 'test_dates': fold['test_dates']}
        for fold in plan['folds']
    ]
    attempt_path = store.publish_attempt(plan, fold_records, status='passed', error_type=None)
    attempt = store.read_attempt(attempt_path)
    oos_dates = [day for fold in plan['folds'] for day in fold['test_dates']]
    replay_dates = plan['calendar_dates'][
        plan['calendar_dates'].index(oos_dates[0]):plan['calendar_dates'].index(oos_dates[-1]) + 2
    ]
    sources = []
    for day in oos_dates:
        generation = sha256_json({'generation': day})
        digest = sha256_json({'manifest': day})
        sources.append({
            'decision_date': day, 'store_root': str((tmp_path / 'weights').absolute()),
            'generation_id': generation, 'manifest_digest_sha256': digest,
            'weights_checksum_sha256': sha256_json({'weights': day}),
        })
    economics = {
        'contract_version': 3, 'schema_version': '1.0.0', 'cost_model': {},
        'execution_model': {}, 'limit_rules': {}, 'initial_capital': 100000.,
        'risk_constraints': {},
    }
    monkeypatch.setattr(continuous, '_fold_targets', lambda *_args: ({}, sources, [economics]))
    manifest_by_generation = {
        source['generation_id']: {
            'generation_id': source['generation_id'],
            'manifest_digest_sha256': source['manifest_digest_sha256'],
            'weights_checksum_sha256': source['weights_checksum_sha256'],
        }
        for source in sources
    }
    monkeypatch.setattr(continuous, '_verified_target_weights', lambda _root, generation, _day: (
        manifest_by_generation[generation], pd.DataFrame(),
    ))

    config_generation = sha256_json({'config': 'v1-receipt'})
    result_generation = sha256_json({'result': 'v1-receipt'})
    sequence = sha256_json({
        'kind': 'walk_forward_continuous_target_sequence',
        'plan_generation_id': plan['generation_id'],
        'attempt_checksum_sha256': attempt['checksum_sha256'],
        'target_weight_sources': sources,
    })
    target_bindings = [
        {key: source[key] for key in ('decision_date', 'generation_id', 'manifest_digest_sha256')}
        for source in sources
    ]
    result = {
        'generation_id': result_generation,
        'manifest_digest_sha256': sha256_json({'result_digest': 'v1-receipt'}),
        'backtest_config_generation_id': config_generation,
        'portfolio_definition_generation_id': sequence,
        'target_weight_bindings': target_bindings,
    }
    config = {
        **economics, 'generation_id': config_generation,
        'start_date': replay_dates[0], 'end_date': replay_dates[-1],
    }
    artifacts = {'equity_curve': pd.DataFrame({'holdings_value': [1000.]})}
    monkeypatch.setattr(continuous, '_verified_result', lambda *_args: (result, config, artifacts))

    receipt = {
        'record_version': 1, 'kind': 'walk_forward_continuous_backtest',
        'publication_scope': 'evidence_only_no_quality_authorization',
        'plan_generation_id': plan['generation_id'], 'walk_forward_root': str(walk_root.absolute()),
        'attempt_path': str(attempt_path.absolute()), 'attempt_checksum_sha256': attempt['checksum_sha256'],
        'backtest_root': str(backtest_root.absolute()),
        'backtest_config_generation_id': config_generation,
        'backtest_result_generation_id': result_generation,
        'backtest_result_manifest_digest_sha256': result['manifest_digest_sha256'],
        'portfolio_sequence_generation_id': sequence,
        'window': {'start_date': replay_dates[0], 'end_date': replay_dates[-1],
                   'oos_decision_sessions': len(oos_dates), 'replay_sessions': len(replay_dates)},
        'target_weight_sources': sources,
        'capital_semantics': 'single_backtest_engine_run_continuous_cash_and_holdings',
        'terminal_liquidation': False,
        'trading_acceptance': 'not_certified_by_continuous_replay',
    }
    receipt['checksum_sha256'] = sha256_json(receipt)
    path = walk_root / 'walk_forward' / 'continuous_backtests' / f'result={result_generation}.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(receipt, sort_keys=True) + '\n', encoding='utf-8')

    assert continuous.WalkForwardContinuousStore(walk_root).read(result_generation) == receipt
