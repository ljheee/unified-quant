from __future__ import annotations

import copy
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from uq.backtest.execution_facts import ExecutionFactsStore, RawExecutionPriceStore, facts_binding
from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import CanonicalV2Store, file_sha256_bytes
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.research_chain.owning_contracts import BacktestConfigStore, BacktestMarketDatasetStore
from tests.test_backtest_v3 import DAYS, NAME, inputs
from tests.test_research_chain_phase4 import _config_decision

ROOT = Path(__file__).resolve().parents[1]


def publish_raw(root, prices):
    store = CanonicalV2Store(root)
    schema = load_schema('config/schemas/bars_daily.v2.yaml')
    for day, group in prices.reset_index().groupby('date'):
        frame = group.rename(columns={'date': 'session_date'}).copy()
        frame['session_date'] = pd.to_datetime(frame['session_date'])
        frame['high'] = frame[['open', 'close']].max(axis=1)
        frame['low'] = frame[['open', 'close']].min(axis=1)
        frame['amount'] = frame['close'] * frame['volume']
        frame['status'] = 'trading'
        frame['limit_up'], frame['limit_down'], frame['adj_factor'] = float('nan'), float('nan'), 1.
        session = dt.date.fromisoformat(day)
        generation = store.prepare_generation(schema, session, frame, {}, {})
        report = QualityReportStore().save(root, {
            'report_version': 1, 'binding_type': 'canonical_v2', 'bound_generation_id': generation,
            'policy': 'reject_all', 'status': 'passed',
            'checks': [{'name': 'coverage', 'threshold': 0, 'observed': 0, 'level': 'error', 'result': 'passed'}],
            'errors': [], 'warnings': [],
        })
        store.publish(schema, session, frame, {}, {},
                      quality_checksum=file_sha256_bytes((report / 'report.json').read_bytes()))


def governed_inputs(root, **kwargs):
    config, prices, facts = inputs(root, **kwargs)
    publish_raw(root, prices)
    config = copy.deepcopy(config)
    config['price_source_binding'], _ = RawExecutionPriceStore(root).read_window(DAYS[0], DAYS[-1])
    store = BacktestMarketDatasetStore(root)
    for kind, frame in (
        ('calendar', pd.DataFrame({'date': DAYS, 'instrument': 'CALENDAR'})),
        ('suspension', pd.DataFrame([{'date': DAYS[0], 'instrument': 'NONE_SUSPENDED', 'suspended': False}])),
        ('corporate_action', pd.DataFrame([{'date': DAYS[0], 'instrument': 'NONE_IMPLEMENTED'}])),
    ):
        manifest = store.publish(frame, dataset_type=kind, window_start=dt.date.fromisoformat(DAYS[0]),
                                 window_end=dt.date.fromisoformat(DAYS[-1]), sources={'fixture': 'isolated'},
                                 producer='test-only')
        keys = ('generation_id', 'checksum_sha256') if kind == 'calendar' else ('dataset_generation_id', 'data_checksum_sha256')
        config[kind + '_binding'] = {keys[0]: manifest['generation_id'], keys[1]: manifest['data_checksum_sha256']}
    for name in ('generation_id', 'manifest_digest_sha256', 'quality_report_checksum_sha256'):
        config[name] = '0' * 64
    config = BacktestConfigStore(root).publish(config, quality_decision=_config_decision())
    return config, prices, facts


def published_portfolio(root, config):
    from uq.contracts.model_layer import model_manifest_identities
    from uq.portfolio.builder import PortfolioBuilder, TargetWeightStore
    from tests.test_portfolio_layer import _make_definition, _make_quality_decision

    definition = _make_definition(scheme_parameters={'n': 1},
        universe_snapshot_generation_id=config['universe_binding']['snapshot_generation_id'])
    definition['constraints']['max_single_weight'] = .1
    definition['generation_id'], definition['manifest_digest_sha256'] = model_manifest_identities(
        definition, schema_name='portfolio_definition')
    manifests, frames = {}, {}
    previous = None
    for day, value in zip(DAYS[:2], [.1, 0.]):
        manifest, frame = PortfolioBuilder(root).build(definition=definition,
            prediction_generation_id='a' * 64, decision_date=day,
            scores=pd.Series({NAME: 1.}), universe_instruments=[NAME], initial_state=previous is None)
        frame['weight'] = value
        manifest['total_stock_weight'] = value
        TargetWeightStore(root).publish(manifest, frame, quality_decision=_make_quality_decision(),
                                       previous_target_weights_generation_id=previous)
        manifests[day], frames[day] = manifest, frame
        previous = manifest['generation_id']
    return definition, manifests, frames


def test_source_facts_are_immutable_sorted_and_verified_on_idempotent_publish(tmp_path):
    config, _, facts = inputs(tmp_path)
    store = ExecutionFactsStore(tmp_path)
    first, restored = store.read(config['execution_facts_binding']['generation_id'])
    again = store.publish(facts.iloc[::-1], source_id=first['source_id'], source_approval_ref=first['source_approval_ref'])
    assert again == first
    assert restored['date'].tolist() == DAYS
    assert restored['visible_at'].iloc[0] == DAYS[0] + 'T01:00:00+00:00'
    directory = store.directory_for(first['generation_id'])
    with (directory / 'data.parquet').open('ab') as stream:
        stream.write(b'tampered')
    with pytest.raises(ContractError, match='data checksum'):
        store.read(first['generation_id'])
    with pytest.raises(ContractError, match='data checksum'):
        store.publish(facts, source_id=first['source_id'], source_approval_ref=first['source_approval_ref'])


def test_execution_facts_symlink_payload_is_refused(tmp_path):
    config, _, _ = inputs(tmp_path)
    store = ExecutionFactsStore(tmp_path)
    directory = store.directory_for(config['execution_facts_binding']['generation_id'])
    (directory / 'data.parquet').rename(tmp_path / 'external.parquet')
    (directory / 'data.parquet').symlink_to(tmp_path / 'external.parquet')
    with pytest.raises(ContractError, match='symlink'):
        store.read(config['execution_facts_binding']['generation_id'])


def test_raw_reader_resolves_real_canonical_quality_and_rejects_binding_or_byte_drift(tmp_path):
    _, prices, _ = inputs(tmp_path)
    publish_raw(tmp_path, prices)
    store = RawExecutionPriceStore(tmp_path)
    binding, frame = store.read_window(DAYS[0], DAYS[-1])
    assert binding['family'] == 'bars_daily_v2'
    assert binding['member_count'] == 3
    assert frame['close'].tolist() == [10., 10., 10.]
    with pytest.raises(ContractError, match='digest'):
        store.read_window(DAYS[0], DAYS[-1], {**binding, 'set_digest_sha256': 'f' * 64})
    path = tmp_path / f'canonical/bars_daily/v2/date={DAYS[1]}/data.parquet'
    with path.open('ab') as stream:
        stream.write(b'tamper')
    with pytest.raises(ContractError, match='checksum'):
        store.read_window(DAYS[0], DAYS[-1], binding)


def test_publish_facts_cli_archives_offline_input_and_names_source_scope(tmp_path):
    config, _, frame = inputs(tmp_path)
    input_path = tmp_path / 'facts.parquet'
    frame.to_parquet(input_path, index=False)
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/publish_backtest_execution_facts.py'),
        '--data-root', str(tmp_path), '--input-parquet', str(input_path),
        '--source-id', 'isolated-exchange-fixture', '--source-approval-ref', 'test-only-not-production'],
        capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert report['execution_facts_binding'] == config['execution_facts_binding']
    assert 'external config review still required' in report['approval_scope']


def test_v3_config_publisher_uses_raw_prices_and_exact_facts_under_external_review(tmp_path):
    from tests.test_publish_backtest_config import _decisions_document, _reviewer_script

    config, _, _ = governed_inputs(tmp_path)
    bindings = {name: config[name] for name in ('calendar_binding', 'suspension_binding', 'corporate_action_binding')}
    market = tmp_path / 'market.json'
    market.write_text(json.dumps({'bindings': bindings}))
    decisions = tmp_path / 'decisions.json'
    _decisions_document(decisions, _reviewer_script(tmp_path / 'reviewer.py'))
    command = [sys.executable, str(ROOT / 'scripts/publish_backtest_config.py'),
        '--data-root', str(tmp_path), '--universe-root', str(tmp_path), '--universe-id', 'fixture',
        '--universe-snapshot', config['universe_binding']['snapshot_generation_id'],
        '--start', DAYS[0], '--end', DAYS[-1], '--market-inputs', str(market),
        '--backtest-name', 'explicit-raw-v3', '--contract-version', '3',
        '--quality-decisions-json', str(decisions),
        '--execution-facts-generation', config['execution_facts_binding']['generation_id'],
        '--minimum-commission-cny', '5', '--impact-coefficient-bps', '10', '--impact-maximum-bps', '100']
    missing_cap = subprocess.run(command, capture_output=True, text=True)
    assert missing_cap.returncode != 0
    assert 'v3 requires --volume-participation-cap' in missing_cap.stderr
    result = subprocess.run(command + ['--volume-participation-cap', '.05'], capture_output=True, text=True, check=True)
    published = BacktestConfigStore(tmp_path).read_manifest(json.loads(result.stdout)['generation_id'])
    assert published['price_source_binding'] == config['price_source_binding']
    assert published['execution_facts_binding'] == config['execution_facts_binding']
    assert published['execution_model']['volume_participation_cap'] == .05
    assert published['cost_model']['minimum_commission_cny'] == 5.
    assert published['risk_constraints']['cash_reserve'] == 0.


def test_research_backtest_adapter_executes_v3_from_owning_raw_and_fact_stores(tmp_path):
    from uq.backtest.engine import BacktestEngine, BacktestResultStore
    from uq.evaluation.report import _verified_result
    from uq.research_chain import BacktestStageAdapter, FileResearchRunStore
    from uq.research_chain.adapters import PortfolioStageResult, build_stage_state
    from uq.research_chain.owning_contracts import AdjustedPriceDatasetStore
    from uq.research_chain.resolver import ResolvedExecutionPlan, ResolvedStageBinding
    from tests.test_research_chain_phase4 import RUNNER, _result_decision
    from tests.test_research_chain_phase5 import _request

    config, _, _ = governed_inputs(tmp_path)
    definition, manifests, frames = published_portfolio(tmp_path, config)
    request = _request()
    plan = ResolvedExecutionPlan(request=request, request_manifest_digest_sha256=request['manifest_digest_sha256'],
        stage_plan_sha256='a' * 64, resolved_execution_plan_sha256='b' * 64,
        stage_bindings=(ResolvedStageBinding(stage='backtest_execution', output_family='backtest_config',
            generation_id=config['generation_id'], manifest_digest_sha256=config['manifest_digest_sha256'],
            data_checksum_sha256=config['price_source_binding']['set_digest_sha256']),))
    store = FileResearchRunStore(tmp_path)
    output_bindings = [{
        'output_family': 'target_weights', 'generation_id': manifest['generation_id'],
        'manifest_digest_sha256': manifest['manifest_digest_sha256'],
        'data_checksum_sha256': manifest['weights_checksum_sha256'],
        'physical_path': f"target_weights/date={day}/generation={manifest['generation_id']}",
        'quality_decision_checksum_sha256': manifest['quality_report_checksum_sha256'], 'failure_reason': None,
    } for day, manifest in manifests.items()]
    state = build_stage_state(plan, stage='portfolio_construction', output_bindings=output_bindings,
                              runner_identity=RUNNER)
    portfolio = PortfolioStageResult(definition_manifest=definition, target_weight_manifests=manifests,
        target_weight_frames=frames, published_state=store.publish_state(state, stage='portfolio_construction'))
    adapter = BacktestStageAdapter(backtest_engine=BacktestEngine(tmp_path),
        backtest_result_store=BacktestResultStore(tmp_path), backtest_config_store=BacktestConfigStore(tmp_path),
        run_store=store, adjusted_price_store=AdjustedPriceDatasetStore(tmp_path),
        market_dataset_store=BacktestMarketDatasetStore(tmp_path))
    result = adapter.run(plan, portfolio_stage_result=portfolio, quality_decision=_result_decision(),
                         config_quality_decision=_config_decision(), runner_identity=RUNNER)
    manifest, read_config, artifacts = _verified_result(tmp_path, result.result_manifest['generation_id'])
    assert manifest['contract_version'] == 3
    assert read_config['generation_id'] == config['generation_id']
    assert artifacts['fills']['filled_shares'].tolist() == [100, 100]
    assert artifacts['equity_curve']['portfolio_value'].iloc[-1] == pytest.approx(9988.)
    assert result.published_state.manifest_path.is_file()
    assert not (tmp_path / 'canonical/bars_adjusted').exists()
    portfolio.target_weight_frames[DAYS[0]].loc[0, 'weight'] = .8
    with pytest.raises(ContractError, match='handoff frame differs'):
        adapter.run(plan, portfolio_stage_result=portfolio, quality_decision=_result_decision(),
                    config_quality_decision=_config_decision(), runner_identity=RUNNER)
