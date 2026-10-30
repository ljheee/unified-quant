from __future__ import annotations

import copy
import importlib.util
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from uuid import UUID

import pandas as pd
import pytest

from uq.contracts.gate_contracts import sha256_json
from uq.contracts.model_layer import research_contract_identities
from uq.errors import ContractError
from uq.evaluation.walk_forward import (
    WalkForwardSpec, WalkForwardStore, build_walk_forward_plan, plan_from_published_calendar,
    validate_fold_requests, verify_walk_forward_plan,
)
from uq.research_chain.owning_contracts import BacktestMarketDatasetStore

ROOT = Path(__file__).resolve().parents[1]


def protocol(*, mode='rolling', partial=False, periods=145):
    days = pd.bdate_range('2020-01-02', periods=periods).strftime('%Y-%m-%d').tolist()
    spec = WalkForwardSpec(20, 10, 10, mode=mode, partial_final_test=partial)
    return build_walk_forward_plan(days, spec, calendar_binding={'source': 'isolated_fixture'})


def request_for_fold(fold, index):
    request = json.loads((ROOT / 'evidence/research-chain/phase-0/fixtures/research_run_request-valid.json').read_text())
    request['dataset_policy_template']['split_policy'] = copy.deepcopy(fold['split_policy'])
    request['window_start_date'] = fold['train_start']
    request['window_end_date'] = fold['label_observation_end']
    request['run_id'] = str(UUID(int=index + 1, version=4))
    reidentify(request)
    return request


def reidentify(request):
    request['request_content_generation_id'], request['manifest_digest_sha256'] = research_contract_identities(
        request, schema_name='research_run_request')


def publish_calendar(root, periods=145):
    days = pd.bdate_range('2020-01-02', periods=periods).strftime('%Y-%m-%d').tolist()
    manifest = BacktestMarketDatasetStore(root).publish(
        pd.DataFrame({'date': days, 'instrument': ['__calendar__'] * len(days)}), dataset_type='calendar',
        window_start=date.fromisoformat(days[0]), window_end=date.fromisoformat(days[-1]),
        sources={'origin': 'isolated_fixture'}, producer='test_walk_forward',
    )
    return manifest


def test_rolling_and_expanding_use_session_gaps_and_never_overlap_oos_dates():
    rolling, expanding = protocol(), protocol(mode='expanding')
    calendar = rolling['calendar_dates']
    first, second = rolling['folds'][:2]
    train, validation, test = first['split_policy']['splits']
    assert calendar.index(train['start_date']) == 60
    assert calendar.index(validation['start_date']) - calendar.index(train['end_date']) - 1 == 7
    assert calendar.index(test['start_date']) - calendar.index(validation['end_date']) - 1 == 7
    assert calendar.index(first['label_observation_end']) - calendar.index(test['end_date']) == 5
    assert calendar.index(second['train_start']) - calendar.index(first['train_start']) == 10
    assert calendar.index(second['raw_history_start']) == 10
    assert expanding['folds'][0]['train_start'] == expanding['folds'][1]['train_start']
    assert expanding['folds'][1]['split_policy']['splits'][0]['end_date'] == second['split_policy']['splits'][0]['end_date']
    oos = [day for fold in rolling['folds'] for day in fold['test_dates']]
    assert oos == sorted(set(oos))
    assert oos == calendar[104:134]
    assert rolling['unused_test_candidate_dates'] == calendar[134:140]
    assert not rolling['qualification']['one_year_coverage_met']
    verify_walk_forward_plan(rolling)


def test_partial_final_test_is_explicit_and_future_label_tail_is_always_reserved():
    plan = protocol(partial=True)
    assert len(plan['folds']) == 4
    assert plan['folds'][-1]['partial_test']
    assert len(plan['folds'][-1]['test_dates']) == 6
    assert plan['folds'][-1]['label_observation_end'] == plan['calendar_dates'][-1]
    assert plan['unused_test_candidate_dates'] == []


def test_one_year_requires_both_session_count_and_calendar_span():
    # 260 weekdays can span a year, while 252 observations do not necessarily.
    plan = protocol(periods=480)
    assert plan['qualification']['oos_sessions'] > 240
    assert plan['qualification']['oos_calendar_days'] >= 365
    assert plan['qualification']['one_year_coverage_met']
    assert not plan['qualification']['market_regimes_verified']
    assert not plan['qualification']['historical_pit_completeness_verified']


@pytest.mark.parametrize('change,match', [
    ({'purge_sessions': 4}, 'horizon'), ({'warmup_sessions': 59}, '60 warmup'),
    ({'train_sessions': True}, 'positive integer'), ({'validation_sessions': 0}, 'positive integer'),
    ({'embargo_sessions': -1}, 'nonnegative'), ({'mode': 'random'}, 'rolling or expanding'),
    ({'partial_final_test': 1}, 'boolean'),
])
def test_invalid_or_leaky_specs_are_refused(change, match):
    spec = replace(WalkForwardSpec(20, 10, 10), **change)
    with pytest.raises(ContractError, match=match):
        build_walk_forward_plan(protocol()['calendar_dates'], spec, calendar_binding={})


def test_short_unsorted_and_duplicate_calendars_are_refused():
    spec = WalkForwardSpec(20, 10, 10)
    days = protocol()['calendar_dates']
    for calendar in [days[::-1], days + days[-1:], []]:
        with pytest.raises(ContractError, match='sorted, unique and nonempty'):
            build_walk_forward_plan(calendar, spec, calendar_binding={})
    with pytest.raises(ContractError, match='future label tail'):
        build_walk_forward_plan(days[:108], spec, calendar_binding={})
    with pytest.raises(ContractError, match='full walk-forward test fold'):
        build_walk_forward_plan(days[:114], spec, calendar_binding={})


def test_published_calendar_is_read_by_checksum_and_rechecked_for_tampering(tmp_path):
    manifest = publish_calendar(tmp_path)
    plan = plan_from_published_calendar(tmp_path, manifest['generation_id'], manifest['data_checksum_sha256'],
                                         WalkForwardSpec(20, 10, 10))
    assert plan['calendar_binding']['store_root'] == str(tmp_path)
    assert len(plan['folds']) == 3
    path = tmp_path / 'backtest_market_data' / f"dataset={manifest['generation_id']}" / 'data.parquet'
    frame = pd.read_parquet(path)
    frame = frame.iloc[:-1]
    frame.to_parquet(path, index=False)
    with pytest.raises(ContractError, match='checksum'):
        plan_from_published_calendar(tmp_path, manifest['generation_id'], manifest['data_checksum_sha256'],
                                     WalkForwardSpec(20, 10, 10))


def test_plan_store_is_immutable_recomputable_and_allows_root_alias(tmp_path):
    plan = protocol()
    store = WalkForwardStore(tmp_path)
    path = store.publish_plan(plan)
    assert store.publish_plan(plan) == path
    assert store.read_plan(plan['generation_id']) == plan
    alias = tmp_path.parent / (tmp_path.name + '-wf-alias')
    alias.symlink_to(tmp_path, target_is_directory=True)
    try:
        assert WalkForwardStore(alias).read_plan(plan['generation_id']) == plan
    finally:
        alias.unlink()
    forged = copy.deepcopy(plan)
    forged['folds'][1]['test_dates'] = forged['folds'][0]['test_dates']
    forged['generation_id'] = sha256_json({k: v for k, v in forged.items() if k != 'generation_id'})
    with pytest.raises(ContractError, match='does not recompute'):
        store.publish_plan(forged)
    changed = copy.deepcopy(plan)
    changed['qualification']['one_year_coverage_met'] = True
    path.write_text(json.dumps(changed))
    with pytest.raises(ContractError, match='does not recompute'):
        store.read_plan(plan['generation_id'])


def test_plan_store_refuses_internal_symlinks_and_path_generations(tmp_path):
    plan = protocol()
    store = WalkForwardStore(tmp_path / 'store')
    path = store.publish_plan(plan)
    other = tmp_path / 'elsewhere.json'
    path.rename(other)
    path.symlink_to(other)
    with pytest.raises(ContractError, match='symbolic links'):
        store.read_plan(plan['generation_id'])
    with pytest.raises(ContractError, match='invalid walk-forward plan generation'):
        store.read_plan('../elsewhere')


def test_request_batch_is_checked_for_exact_splits_and_frozen_model_settings():
    plan = protocol()
    requests = [request_for_fold(fold, i) for i, fold in enumerate(plan['folds'])]
    validate_fold_requests(plan, requests)
    wrong = copy.deepcopy(requests)
    wrong[-1]['dataset_policy_template']['split_policy']['splits'][-1]['start_date'] = wrong[-1]['window_start_date']
    reidentify(wrong[-1])
    with pytest.raises(ContractError, match='split policy'):
        validate_fold_requests(plan, wrong)
    wrong = copy.deepcopy(requests)
    wrong[-1]['model_definition_template']['hyperparameters']['alpha'] = 20.
    reidentify(wrong[-1])
    with pytest.raises(ContractError, match='settings drift'):
        validate_fold_requests(plan, wrong)
    with pytest.raises(ContractError, match='exactly one request'):
        validate_fold_requests(plan, requests[:-1])


def test_plan_cli_consumes_published_calendar_not_an_unverified_date_list(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location('run_walk_forward', ROOT / 'scripts/run_walk_forward.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calendar = publish_calendar(tmp_path / 'calendar')
    config = tmp_path / 'spec.json'
    config.write_text(json.dumps({'train_sessions': 20, 'validation_sessions': 10, 'test_sessions': 10}))
    assert module.main(['--mode', 'plan', '--output-root', str(tmp_path / 'out'),
                        '--calendar-root', str(tmp_path / 'calendar'), '--calendar-generation', calendar['generation_id'],
                        '--calendar-checksum', calendar['data_checksum_sha256'], '--spec-json', str(config)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'plan_published'
    assert result['folds'] == 3
    assert not result['qualification']['one_year_coverage_met']
