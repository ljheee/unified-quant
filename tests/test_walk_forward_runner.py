from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import UUID

import pytest

from uq.contracts.gate_contracts import sha256_json
from uq.errors import ContractError
from uq.evaluation.walk_forward import WalkForwardStore
from uq.evaluation.walk_forward_runner import (
    ResearchFoldExecutor, ResearchFoldJob, run_walk_forward,
)
from tests.test_walk_forward import protocol, publish_calendar, request_for_fold


def jobs_for(plan, root):
    return [ResearchFoldJob(request_for_fold(fold, index), root / fold['fold_id'], {})
            for index, fold in enumerate(plan['folds'])]


class RecordingExecutor:
    """Control-flow test double, not evidence of real Qlib training."""
    def __init__(self, *, fail=None, drift=None):
        self.calls = []
        self.fail = fail
        self.drift = drift
        self.preflight_calls = 0

    def preflight(self, plan, jobs):
        self.preflight_calls += 1

    def execute(self, job):
        self.calls.append(job.request['run_id'])
        if len(self.calls) == self.fail:
            raise ContractError('injected second-fold failure')
        return {'status': 'published', 'fixture': True}

    def verify(self, fold, job, outcome):
        return {'fold_id': fold['fold_id'], 'status': 'passed', 'test_dates': fold['test_dates'],
                'ordered_features': ['other'] if len(self.calls) == self.drift else ['feature'],
                'factor_set': 'test', 'factor_version': '1.0.0', 'label_set_name': 'return_5d'}


def test_fold_batch_stops_on_failure_and_keeps_verified_prefix_without_stitching(tmp_path):
    plan = protocol()
    executor = RecordingExecutor(fail=2)
    store = WalkForwardStore(tmp_path / 'evidence')
    with pytest.raises(ContractError, match='second-fold failure'):
        run_walk_forward(plan, jobs_for(plan, tmp_path), executor=executor, store=store,
                         allow_short_diagnostic=True)
    assert len(executor.calls) == 2
    attempts = list((store.root / 'walk_forward/attempts').rglob('attempt=*.json'))
    assert len(attempts) == 1
    document = json.loads(attempts[0].read_text())
    checksum = document.pop('checksum_sha256')
    assert sha256_json(document) == checksum
    assert document['status'] == 'failed'
    assert len(document['fold_records']) == 1
    assert document['capital_semantics'] == 'independent_fold_backtests; no_stitched_pnl'
    assert document['error_type'] == 'ContractError'
    assert document['failed_fold']['fold_id'] == plan['folds'][1]['fold_id']


def test_short_coverage_is_rejected_before_any_execution_without_explicit_diagnostic_flag(tmp_path):
    plan = protocol()
    executor = RecordingExecutor()
    with pytest.raises(ContractError, match='one year'):
        run_walk_forward(plan, jobs_for(plan, tmp_path), executor=executor, store=WalkForwardStore(tmp_path))
    assert executor.preflight_calls == 0
    assert executor.calls == []
    assert not (tmp_path / 'walk_forward').exists()


def test_invalid_late_request_is_detected_before_the_first_fold(tmp_path):
    plan = protocol()
    jobs = jobs_for(plan, tmp_path)
    jobs[-1].request['dataset_policy_template']['split_policy']['splits'][-1]['start_date'] = '1990-01-01'
    executor = RecordingExecutor()
    with pytest.raises(ContractError):
        run_walk_forward(plan, jobs, executor=executor, store=WalkForwardStore(tmp_path),
                         allow_short_diagnostic=True)
    assert executor.calls == []
    assert not (tmp_path / 'walk_forward').exists()


def test_actual_feature_drift_stops_before_accepting_the_next_fold(tmp_path):
    plan = protocol()
    executor = RecordingExecutor(drift=2)
    with pytest.raises(ContractError, match='actual features'):
        run_walk_forward(plan, jobs_for(plan, tmp_path), executor=executor, store=WalkForwardStore(tmp_path),
                         allow_short_diagnostic=True)
    assert len(executor.calls) == 2
    attempt = json.loads(next((tmp_path / 'walk_forward/attempts').rglob('attempt=*.json')).read_text())
    assert len(attempt['fold_records']) == 1


def test_success_records_oos_coverage_without_certifying_trading_or_aggregating_reset_pnl(tmp_path):
    plan = protocol(periods=480)
    executor = RecordingExecutor()
    output, path = run_walk_forward(plan, jobs_for(plan, tmp_path), executor=executor,
                                    store=WalkForwardStore(tmp_path))
    assert len(executor.calls) == len(plan['folds'])
    assert output['status'] == 'folds_passed'
    assert output['one_year_coverage_met']
    assert output['trading_acceptance'] == 'not_certified_by_fold_execution'
    assert 'return' not in output
    assert path.exists()


def test_retry_preserves_plan_and_uses_same_request_run_ids_in_a_new_attempt(tmp_path):
    plan = protocol()
    jobs = jobs_for(plan, tmp_path)
    store = WalkForwardStore(tmp_path)
    first, second = RecordingExecutor(), RecordingExecutor()
    _, one = run_walk_forward(plan, jobs, executor=first, store=store, allow_short_diagnostic=True)
    before = one.read_bytes()
    _, two = run_walk_forward(plan, jobs, executor=second, store=store, allow_short_diagnostic=True)
    assert first.calls == second.calls
    assert one != two
    assert one.read_bytes() == before
    assert store.read_plan(plan['generation_id']) == plan


def test_research_executor_never_accepts_external_scores_and_delegates_to_existing_runner(tmp_path, monkeypatch):
    from uq.research_chain import cli
    plan = protocol()
    job = jobs_for(plan, tmp_path)[0]
    captured = {}
    def execute(request, **kwargs):
        captured.update(kwargs)
        assert request is job.request
        return {'status': 'published'}
    monkeypatch.setattr(cli, '_execute', execute)
    assert ResearchFoldExecutor(Path(__file__).resolve().parents[1]).execute(job)['status'] == 'published'
    assert captured['scores_document'] is None
    assert captured['decisions_document'] is job.decisions_document
    assert captured['data_root'] == job.data_root


def test_production_preflight_refuses_unverified_calendar_before_fitting(tmp_path):
    plan = protocol()
    executor = ResearchFoldExecutor(Path(__file__).resolve().parents[1])
    with pytest.raises(ContractError, match='published-calendar plan'):
        run_walk_forward(plan, jobs_for(plan, tmp_path), executor=executor,
                         store=WalkForwardStore(tmp_path), allow_short_diagnostic=True)
    assert not (tmp_path / 'walk_forward').exists()


def test_v7_missing_paper_risk_documents_are_rejected_by_batch_preflight(tmp_path):
    from uq.contracts.model_layer import research_contract_identities
    from uq.evaluation.walk_forward import WalkForwardSpec, plan_from_published_calendar
    from tests.test_research_v7_lightgbm import _build_v7_request

    manifest = publish_calendar(tmp_path / 'calendar')
    plan = plan_from_published_calendar(
        tmp_path / 'calendar', manifest['generation_id'], manifest['data_checksum_sha256'],
        WalkForwardSpec(20, 10, 10),
    )
    jobs = []
    for index, fold in enumerate(plan['folds']):
        request = _build_v7_request()
        request['dataset_policy_template']['split_policy'] = copy.deepcopy(fold['split_policy'])
        request['window_start_date'] = fold['train_start']
        request['window_end_date'] = fold['label_observation_end']
        request['run_id'] = str(UUID(int=index + 1, version=4))
        request['request_content_generation_id'] = request['manifest_digest_sha256'] = '0' * 64
        request['request_content_generation_id'], request['manifest_digest_sha256'] = (
            research_contract_identities(request, schema_name='research_run_request_v7')
        )
        jobs.append(ResearchFoldJob(request, tmp_path / fold['fold_id'], {'quality_decisions': {}}))

    with pytest.raises(ContractError, match='v5-v8 requires membership, market and paper risk documents'):
        run_walk_forward(
            plan, jobs, executor=ResearchFoldExecutor(Path(__file__).resolve().parents[1]),
            store=WalkForwardStore(tmp_path / 'evidence'), allow_short_diagnostic=True,
        )
    assert not (tmp_path / 'evidence/walk_forward').exists()


def test_cli_job_documents_cannot_smuggle_external_test_scores(tmp_path):
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('walk_forward_cli', root / 'scripts/run_walk_forward.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / 'jobs.json'
    path.write_text(json.dumps([{'request_json': 'request.json', 'data_root': 'data',
                                'quality_decisions_json': 'decisions.json', 'scores_json': 'leaked.json'}]))
    with pytest.raises(ContractError, match='external scores are not accepted'):
        module._jobs(path)


def prepare_reviewed_fold_jobs(tmp_path, *, drift_cost=False):
    import pandas as pd
    from uq.evaluation.walk_forward import WalkForwardSpec, plan_from_published_calendar
    from uq.models.labels import LabelBuilder
    from uq.models.label_store_writer import LabelSetWriter
    from uq.research_chain.owning_contracts import BacktestConfigStore, BacktestMarketDatasetStore
    from uq.research_chain.provenance import capture_environment
    from tests.test_backtest_layer import _make_config
    from tests.test_model_labels_dataset import _binding
    from tests.test_research_chain_phase4 import _config_decision
    from tests.test_walk_forward import publish_calendar, reidentify
    from datetime import date

    calendar_root = tmp_path / 'calendar'
    manifest = publish_calendar(calendar_root)
    plan = plan_from_published_calendar(calendar_root, manifest['generation_id'], manifest['data_checksum_sha256'],
                                         WalkForwardSpec(20, 10, 10))
    frame = pd.DataFrame([
        {'instrument': name, 'datetime': pd.Timestamp(day), 'close': 10., 'adj_factor': 1.,
         'limit_up': False, 'limit_down': False, 'suspended': False, 'delisted': False,
         'listing_date': pd.Timestamp('2010-01-01', tz='UTC')}
        for name in ['INST0', 'INST1', 'INST2'] for day in plan['calendar_dates']
    ])
    label_manifest, labels = LabelBuilder(name='return_5d', semantic_version='1.0.0').build_with_frame(
        frame, upstream_bindings=[_binding(frame)])
    environment = capture_environment(Path(__file__).resolve().parents[1])
    jobs = []
    for i, fold in enumerate(plan['folds']):
        root = tmp_path / fold['fold_id']
        label = LabelSetWriter(root).publish(label_manifest, labels)
        days = [day for day in plan['calendar_dates'] if fold['train_start'] <= day <= fold['label_observation_end']]
        window = BacktestMarketDatasetStore(root).publish(
            pd.DataFrame({'date': days, 'instrument': '__calendar__'}), dataset_type='calendar',
            window_start=date.fromisoformat(days[0]), window_end=date.fromisoformat(days[-1]),
            sources={'test': 'isolated_fixture'}, producer='test_walk_forward',
        )
        config = _make_config(start_date=days[0], end_date=days[-1],
                              calendar_binding={'generation_id': window['generation_id'],
                                                'checksum_sha256': window['data_checksum_sha256']})
        config['generation_id'] = config['manifest_digest_sha256'] = '0' * 64
        if drift_cost and i == len(plan['folds']) - 1:
            config['cost_model']['commission_bps'] = 5.
        config = BacktestConfigStore(root).publish(config, quality_decision=_config_decision())
        request = request_for_fold(fold, i)
        request['label_binding'].update({'generation_id': label['generation_id'],
                                         'manifest_digest_sha256': label['manifest_digest_sha256']})
        request['backtest_config_binding'].update({'generation_id': config['generation_id'],
                                                   'manifest_digest_sha256': config['manifest_digest_sha256']})
        request['environment'].update({name: environment[name] for name in
                                      ('code_fingerprint', 'environment_lock_digest_sha256')})
        reidentify(request)
        jobs.append(ResearchFoldJob(request, root, {'quality_decisions': {}}))
    return plan, jobs


def test_production_preflight_reverifies_real_calendar_labels_configs_and_environment(tmp_path):
    from uq.evaluation.walk_forward import validate_fold_requests
    plan, jobs = prepare_reviewed_fold_jobs(tmp_path)
    validate_fold_requests(plan, [job.request for job in jobs])
    ResearchFoldExecutor(Path(__file__).resolve().parents[1]).preflight(plan, jobs)
    # Preflight alone never creates a research ledger or model.
    assert not list(tmp_path.rglob('research_runs'))
    assert not list(tmp_path.rglob('model_runs'))


def test_v3_preflight_verifies_all_raw_partitions_and_execution_facts_before_fitting(tmp_path):
    from dataclasses import replace
    import pandas as pd
    from uq.backtest.execution_facts import ExecutionFactsStore, RawExecutionPriceStore, facts_binding
    from uq.research_chain.owning_contracts import BacktestConfigStore
    from tests.test_backtest_execution_facts import publish_raw
    from tests.test_backtest_v3 import NAME, inputs
    from tests.test_research_chain_phase4 import _config_decision
    from tests.test_walk_forward import reidentify

    plan, jobs = prepare_reviewed_fold_jobs(tmp_path)
    template, _, source_facts = inputs(tmp_path / 'v3-template')
    updated = []
    for fold, job in zip(plan['folds'], jobs):
        root = job.data_root
        days = [day for day in plan['calendar_dates']
                if fold['train_start'] <= day <= fold['label_observation_end']]
        prices = pd.DataFrame([{'date': day, 'instrument': NAME, 'open': 10., 'close': 10., 'volume': 10000.}
                               for day in days]).set_index(['date', 'instrument'])
        publish_raw(root, prices)
        facts = pd.DataFrame([{**source_facts.iloc[0].to_dict(), 'date': day,
                               'visible_at': day + 'T09:00:00+08:00'} for day in days])
        facts = ExecutionFactsStore(root).publish(facts, source_id='isolated-fixture', source_approval_ref='test-only')
        config = BacktestConfigStore(root).read_manifest(job.request['backtest_config_binding']['generation_id'])
        config.update({name: copy.deepcopy(template[name]) for name in
                       ('contract_version', 'cost_model', 'execution_model', 'limit_rules', 'risk_constraints')})
        config['execution_facts_binding'] = facts_binding(facts)
        config['price_source_binding'], _ = RawExecutionPriceStore(root).read_window(days[0], days[-1])
        for name in ('generation_id', 'manifest_digest_sha256', 'quality_report_checksum_sha256'):
            config[name] = '0' * 64
        config = BacktestConfigStore(root).publish(config, quality_decision=_config_decision())
        request = copy.deepcopy(job.request)
        request['backtest_config_binding'].update({'generation_id': config['generation_id'],
                                                   'manifest_digest_sha256': config['manifest_digest_sha256']})
        reidentify(request)
        updated.append(replace(job, request=request))
    executor = ResearchFoldExecutor(Path(__file__).resolve().parents[1])
    executor.preflight(plan, updated)
    late = next((updated[-1].data_root / 'backtest_execution_facts').glob('generation=*/data.parquet'))
    with late.open('ab') as stream:
        stream.write(b'tampered-before-any-fit')
    with pytest.raises(ContractError, match='checksum'):
        executor.preflight(plan, updated)
    assert not list(tmp_path.rglob('model_runs'))


def test_late_cost_drift_is_rejected_before_model_or_attempt_publication(tmp_path):
    plan, jobs = prepare_reviewed_fold_jobs(tmp_path, drift_cost=True)
    with pytest.raises(ContractError, match='cost/execution/capital'):
        run_walk_forward(plan, jobs, executor=ResearchFoldExecutor(Path(__file__).resolve().parents[1]),
                         store=WalkForwardStore(tmp_path / 'evidence'), allow_short_diagnostic=True)
    assert not (tmp_path / 'evidence/walk_forward').exists()
    assert not list(tmp_path.rglob('model_runs'))


def test_source_calendar_tampering_is_rejected_before_any_fold_execution(tmp_path):
    import pandas as pd
    plan, jobs = prepare_reviewed_fold_jobs(tmp_path)
    binding = plan['calendar_binding']
    path = Path(binding['store_root']) / 'backtest_market_data' / f"dataset={binding['generation_id']}" / 'data.parquet'
    frame = pd.read_parquet(path).iloc[:-1]
    frame.to_parquet(path, index=False)
    with pytest.raises(ContractError, match='checksum'):
        ResearchFoldExecutor(Path(__file__).resolve().parents[1]).preflight(plan, jobs)


def test_completed_fold_retry_reads_immutable_result_without_reentering_publishers(tmp_path, monkeypatch):
    from uq.research_chain import cli
    from uq.research_chain.resolver import FileResearchRunStore
    from tests.test_research_chain_phase5 import _result
    plan = protocol()
    job = jobs_for(plan, tmp_path)[0]
    result = _result(job.request)
    job.data_root.mkdir(parents=True)
    store = FileResearchRunStore(job.data_root)
    store.publish_request(job.request, path_policy='strict_v1')
    publication = store.publish_result(result, path_policy='strict_v1')
    before = publication.manifest_path.read_bytes()
    def must_not_execute(*args, **kwargs):
        raise AssertionError('completed fold reentered immutable publishers')
    monkeypatch.setattr(cli, '_execute', must_not_execute)
    executor = ResearchFoldExecutor(Path(__file__).resolve().parents[1])
    first = executor.execute(job)
    second = executor.execute(job)
    assert first == second
    assert first['execution_disposition'] == 'read_completed_result'
    assert first['result_content_generation_id'] == result['result_content_generation_id']
    assert publication.manifest_path.read_bytes() == before
    assert not (job.data_root / 'research_runs/states').exists()
    # This tests completed-result discovery only; the outer runner still requires
    # executor.verify, which re-reads models/predictions/dataset/backtest inputs.


def test_invalid_completed_result_never_falls_back_to_rerunning_the_fold(tmp_path, monkeypatch):
    from uq.research_chain import cli
    from uq.research_chain.resolver import FileResearchRunStore
    from tests.test_research_chain_phase5 import _result
    job = jobs_for(protocol(), tmp_path)[0]
    job.data_root.mkdir(parents=True)
    store = FileResearchRunStore(job.data_root)
    result = _result(job.request)
    publication = store.publish_result(result, path_policy='strict_v1')
    result['overall_logical_fingerprint'] = 'f' * 64
    publication.manifest_path.write_text(json.dumps(result))
    called = []
    monkeypatch.setattr(cli, '_execute', lambda *args, **kwargs: called.append(True))
    with pytest.raises(ContractError):
        ResearchFoldExecutor(Path(__file__).resolve().parents[1]).execute(job)
    assert called == []


def test_real_qlib_fold_output_is_verified_through_owning_readers(tmp_path, monkeypatch):
    pytest.importorskip('qlib')
    from dataclasses import replace
    from uq.contracts.model_layer import bind_reviewed_quality_decision, model_manifest_identities
    from uq.research_chain.provenance import capture_environment, verified_runner_identity
    from uq.research_chain.runner import ResearchChainRunner
    from tests import test_research_chain_phase4 as phase4
    from tests.test_research_chain_phase5 import _run_full_chain
    from tests.test_walk_forward import reidentify

    project_root = Path(__file__).resolve().parents[1]
    environment = capture_environment(project_root)
    split_policy = {'purge_trading_days': 5, 'embargo_trading_days': 2, 'splits': [
        {'name': 'train', 'start_date': '2026-01-05', 'end_date': '2026-01-07'},
        {'name': 'validation', 'start_date': '2026-01-19', 'end_date': '2026-01-19'},
        {'name': 'test', 'start_date': '2026-01-29', 'end_date': '2026-01-29'},
    ]}
    original_config = phase4._backtest_config

    def window_config(*args, **kwargs):
        config = original_config(*args, **kwargs)
        config['end_date'] = '2026-01-30'
        config['generation_id'] = config['manifest_digest_sha256'] = '0' * 64
        config['quality_report_checksum_sha256'] = '0' * 64
        provisional, _ = model_manifest_identities(config, schema_name='backtest_config')
        review, checksum = bind_reviewed_quality_decision(phase4._config_decision()(provisional),
            binding_type='backtest_config_v1', subject_generation_id=provisional)
        config['quality_report_checksum_sha256'] = checksum
        config['generation_id'], config['manifest_digest_sha256'] = model_manifest_identities(
            config, schema_name='backtest_config')
        directory = tmp_path / 'external_quality_reviews'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f'{checksum}.json').write_text(json.dumps(review))
        return config

    original_execute = ResearchChainRunner.execute

    def oos_execute(self, plan, **kwargs):
        plan.request['dataset_policy_template']['split_policy'] = copy.deepcopy(split_policy)
        # The older chain fixture supplies input identities only in its resolved
        # plan. A genuine CLI request binds those same inputs in the request too.
        for family, field in [('factor_partition', 'factor_binding'), ('label_set', 'label_binding'),
                              ('feature_preprocessing', 'feature_preprocessing_binding'),
                              ('universe_snapshot', 'universe_snapshot_binding'),
                              ('backtest_config', 'backtest_config_binding')]:
            binding = next(item for item in plan.stage_bindings if item.output_family == family)
            plan.request[field].update({'generation_id': binding.generation_id,
                                         'manifest_digest_sha256': binding.manifest_digest_sha256})
        adjusted = next(item for item in plan.stage_bindings if item.output_family == 'adjusted_price_dataset')
        plan.request['adjusted_price_binding'].update({'generation_id': adjusted.generation_id,
                                                      'data_checksum_sha256': adjusted.data_checksum_sha256})
        plan.request['environment'].update({key: environment[key] for key in
                                           ('code_fingerprint', 'environment_lock_digest_sha256')})
        reidentify(plan.request)
        plan = replace(plan, request_manifest_digest_sha256=plan.request['manifest_digest_sha256'])
        kwargs['runner_identity'] = verified_runner_identity(plan.request['environment'], environment)
        kwargs['scores_by_decision_date'] = None
        kwargs['decision_dates'] = ['2026-01-29']
        return original_execute(self, plan, **kwargs)

    monkeypatch.setattr(phase4, '_backtest_config', window_config)
    monkeypatch.setattr(ResearchChainRunner, 'execute', oos_execute)
    outcome, _store, executed_plan = _run_full_chain(tmp_path)
    job = ResearchFoldJob(executed_plan.request, tmp_path, {})
    fold = {'fold_id': 'fold-0000', 'split_policy': split_policy, 'test_dates': ['2026-01-29']}
    executor = ResearchFoldExecutor(project_root)
    record = executor.verify(fold, job, {
        'status': 'published', 'result_content_generation_id': outcome.result_manifest['result_content_generation_id'],
        'manifest_digest_sha256': outcome.result_manifest['manifest_digest_sha256'],
    })
    assert record['status'] == 'passed'
    assert record['test_dates'] == ['2026-01-29']
    assert record['ordered_features'] == ['volume_ratio_20d']
    assert len(record['prediction_bindings']) == 1
    missing_date = {**fold, 'test_dates': []}
    with pytest.raises(ContractError, match='every test session exactly once'):
        executor.verify(missing_date, job, {
            'status': 'published', 'result_content_generation_id': outcome.result_manifest['result_content_generation_id'],
            'manifest_digest_sha256': outcome.result_manifest['manifest_digest_sha256'],
        })
    # A retry reads that same immutable full result rather than training again.
    reused = executor.execute(job)
    assert reused['execution_disposition'] == 'read_completed_result'
    assert executor.verify(fold, job, reused) == record


def test_legacy_v1_cash_only_fold_exposes_undefined_sharpe_contract_gap(tmp_path):
    from uq.backtest.engine import BacktestEngine, BacktestResultStore
    from tests.test_backtest_layer import DATES, _make_config, _make_price_panel, _make_weights
    from tests.test_research_chain_phase4 import _result_decision
    # Last-session intent has no T+1 execution session. This is legitimately
    # all cash, not a missing price, failed fit or non-finite market input.
    manifest, artifacts = BacktestEngine(tmp_path).run(
        config=_make_config(), portfolio_definition={'generation_id': 'a' * 64},
        weight_partitions={DATES[-1]: _make_weights(DATES[-1], ['A'])},
        price_panel=_make_price_panel(DATES, ['A']),
    )
    assert manifest['summary_metrics']['sharpe_ratio'] is None
    assert (artifacts['equity_curve']['holdings_value'] == 0).all()
    with pytest.raises(ContractError, match='sharpe_ratio'):
        BacktestResultStore(tmp_path).publish(
            manifest, artifacts, quality_decision=_result_decision(),
            target_weight_generations=[{'decision_date': DATES[-1], 'generation_id': 'b' * 64,
                                       'manifest_digest_sha256': 'c' * 64}],
        )
    assert not (tmp_path / 'backtest_results').exists()
    # Preserve v1 compatibility; a versioned backtest upgrade must add a valid
    # undefined-statistic representation rather than silently widen v1 in place.


def test_attempt_reader_rechecks_plan_coverage_even_after_attacker_rehashes_metadata(tmp_path):
    plan = protocol()
    store = WalkForwardStore(tmp_path)
    _, path = run_walk_forward(plan, jobs_for(plan, tmp_path), executor=RecordingExecutor(),
                               store=store, allow_short_diagnostic=True)
    original = store.read_attempt(path)
    assert original['status'] == 'passed'
    forged = copy.deepcopy(original)
    forged['qualification']['one_year_coverage_met'] = True
    forged['checksum_sha256'] = sha256_json({key: value for key, value in forged.items() if key != 'checksum_sha256'})
    path.write_text(json.dumps(forged))
    with pytest.raises(ContractError, match='qualification/scope'):
        store.read_attempt(path)


def test_attempt_reader_refuses_a_rehashed_pass_with_missing_fold(tmp_path):
    plan = protocol()
    store = WalkForwardStore(tmp_path)
    _, path = run_walk_forward(plan, jobs_for(plan, tmp_path), executor=RecordingExecutor(),
                               store=store, allow_short_diagnostic=True)
    forged = store.read_attempt(path)
    forged['fold_records'].pop()
    forged['checksum_sha256'] = sha256_json({key: value for key, value in forged.items() if key != 'checksum_sha256'})
    path.write_text(json.dumps(forged))
    with pytest.raises(ContractError, match='incomplete'):
        store.read_attempt(path)
