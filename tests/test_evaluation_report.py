from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from uq.backtest.engine import BacktestEngine, BacktestResultStore
from uq.contracts.gate_contracts import sha256_json
from uq.errors import ContractError
from uq.evaluation.report import EvaluationReportStore, paired_backtest_inputs
from uq.research_chain.owning_contracts import BacktestConfigStore
from tests.test_backtest_layer import DATES, _make_config, _make_price_panel, _make_weights
from tests.test_evaluation_metrics import daily_frame, supervised_frames
from tests.test_research_chain_phase4 import _config_decision, _result_decision

ROOT = Path(__file__).resolve().parents[1]


def publish_backtest(root, *, weight=.8, config_overrides=None):
    config = _make_config(**(config_overrides or {}))
    config['generation_id'] = config['manifest_digest_sha256'] = '0' * 64
    config = BacktestConfigStore(root).publish(config, quality_decision=_config_decision())
    manifest, artifacts = BacktestEngine(root).run(
        config=config, portfolio_definition={'generation_id': 'f' * 64},
        weight_partitions={DATES[0]: _make_weights(DATES[0], ['A'], weight=weight)},
        price_panel=_make_price_panel(DATES, ['A']),
    )
    BacktestResultStore(root).publish(
        manifest, artifacts, quality_decision=_result_decision(),
        target_weight_generations=[{'decision_date': DATES[0], 'generation_id': 'd' * 64,
                                   'manifest_digest_sha256': 'e' * 64}],
    )
    return manifest, artifacts


def test_report_roundtrip_is_recomputable_and_does_not_authorize_quality(tmp_path):
    store = EvaluationReportStore(tmp_path)
    first = store.publish(daily_frame(), input_bindings={'origin': 'isolated_fixture'})
    second = store.publish(daily_frame().iloc[::-1], input_bindings={'origin': 'isolated_fixture'})
    assert first == second
    assert first['publication_scope'] == 'diagnostic_only_no_quality_authorization'
    manifest, frames = store.read(first['generation_id'])
    assert manifest == first
    assert frames['daily']['date'].tolist() == daily_frame()['date'].tolist()
    alias = tmp_path.parent / (tmp_path.name + '-evaluation-alias')
    alias.symlink_to(tmp_path, target_is_directory=True)
    try:
        assert EvaluationReportStore(alias).read(first['generation_id'])[0] == manifest
    finally:
        alias.unlink()


def test_report_archives_scores_and_labels_and_recomputes_overlap_corrected_ic(tmp_path):
    days, scores, labels = supervised_frames()
    daily = pd.DataFrame({'date': days, 'strategy_return': [.001] * len(days),
                          'benchmark_return': [0.] * len(days), 'invested': [True] * len(days),
                          'turnover': [.1] * len(days)})
    store = EvaluationReportStore(tmp_path)
    manifest = store.publish(daily, input_bindings={'origin': 'fixture'}, predictions=scores,
                             labels=labels, horizon_sessions=5)
    restored, frames = store.read(manifest['generation_id'])
    assert set(frames) == {'daily', 'predictions', 'labels'}
    assert restored['metrics']['information_coefficients']['ic']['valid_sessions'] == 8
    assert restored['protocol']['hac_lags'] == 4
    assert restored['metrics']['information_coefficients']['ic']['effective_sessions'] < 8
    assert restored['metrics']['information_coefficients']['ic']['mean'] == pytest.approx(0)
    assert list((tmp_path / 'research_evaluations').glob('.staging-*')) == []


def test_modified_artifact_is_rejected_not_silently_reused(tmp_path):
    store = EvaluationReportStore(tmp_path)
    manifest = store.publish(daily_frame(), input_bindings={})
    path = store.directory_for(manifest['generation_id']) / 'daily.parquet'
    data = pd.read_parquet(path)
    data.loc[0, 'strategy_return'] = .5
    data.to_parquet(path, index=False)
    with pytest.raises(ContractError, match='checksum mismatch'):
        store.read(manifest['generation_id'])
    with pytest.raises(ContractError, match='checksum mismatch'):
        store.publish(daily_frame(), input_bindings={})


def test_even_rehashed_manifest_with_forged_metrics_fails_recomputation(tmp_path):
    store = EvaluationReportStore(tmp_path)
    manifest = store.publish(daily_frame(), input_bindings={})
    original = store.directory_for(manifest['generation_id'])
    manifest['metrics']['returns']['overall']['annualized_excess_return'] = 1000.
    manifest['generation_id'] = sha256_json({k: v for k, v in manifest.items() if k != 'generation_id'})
    forged = store.directory_for(manifest['generation_id'])
    original.rename(forged)
    (forged / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ContractError, match='do not recompute'):
        store.read(manifest['generation_id'])


def test_report_refuses_symlinked_artifacts_and_invalid_generations(tmp_path):
    store = EvaluationReportStore(tmp_path)
    manifest = store.publish(daily_frame(), input_bindings={})
    path = store.directory_for(manifest['generation_id']) / 'daily.parquet'
    other = tmp_path / 'outside.parquet'
    path.rename(other)
    path.symlink_to(other)
    with pytest.raises(ContractError, match='symbolic links'):
        store.read(manifest['generation_id'])
    with pytest.raises(ContractError, match='invalid evaluation generation'):
        store.read('../outside')


def test_pair_uses_real_reviewed_store_readbacks_and_includes_liquidation_exposure(tmp_path):
    left, right = tmp_path / 'strategy', tmp_path / 'benchmark'
    strategy, _ = publish_backtest(left)
    benchmark, _ = publish_backtest(right, weight=.2)
    daily, bindings = paired_backtest_inputs(left, strategy['generation_id'], right,
                                           benchmark['generation_id'], start=DATES[0], end=DATES[-1])
    assert len(daily) == 5
    assert not daily['invested'].iloc[0]
    assert daily['invested'].iloc[1:].all()
    assert daily['strategy_return'].sum() > daily['benchmark_return'].sum()
    assert 'cost_model' in bindings['comparability_checked_fields']
    assert bindings['historical_pit_completeness'].startswith('not_verified')
    report = EvaluationReportStore(tmp_path / 'reports').publish(daily, input_bindings=bindings)
    assert report['metrics']['returns']['overall']['holding_sessions'] == 4


@pytest.mark.parametrize('override,field', [
    ({'cost_model': {'commission_bps': 5., 'slippage_bps': 1., 'stamp_duty_bps': 5.}}, 'cost_model'),
    ({'initial_capital': 2000000.}, 'initial_capital'),
    ({'universe_binding': {'snapshot_generation_id': 'f' * 64}}, 'universe_binding'),
    ({'execution_model': {'type': 'daily_t1_open', 'board_lot': 100,
                         'sellable_quantity_rule': 'prior_day_holding_only',
                         'volume_participation_cap': .25}}, 'execution_model'),
])
def test_unfair_benchmark_comparisons_are_refused(tmp_path, override, field):
    strategy, _ = publish_backtest(tmp_path / 'strategy')
    benchmark, _ = publish_backtest(tmp_path / 'benchmark', config_overrides=override)
    with pytest.raises(ContractError, match=field):
        paired_backtest_inputs(tmp_path / 'strategy', strategy['generation_id'],
                               tmp_path / 'benchmark', benchmark['generation_id'],
                               start=DATES[0], end=DATES[-1])


def test_forged_config_and_review_cannot_enter_comparison(tmp_path):
    strategy, _ = publish_backtest(tmp_path / 'strategy')
    benchmark, _ = publish_backtest(tmp_path / 'benchmark')
    path = (tmp_path / 'benchmark/backtest_configs' /
            f"generation={benchmark['backtest_config_generation_id']}" / 'manifest.json')
    config = json.loads(path.read_text())
    config['cost_model']['commission_bps'] = 0.
    path.write_text(json.dumps(config))
    with pytest.raises(ContractError, match='unpublished backtest config|manifest identity mismatch'):
        paired_backtest_inputs(tmp_path / 'strategy', strategy['generation_id'],
                               tmp_path / 'benchmark', benchmark['generation_id'],
                               start=DATES[0], end=DATES[-1])


def test_cli_reads_governed_results_publishes_and_rejects_uncovered_window(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location('evaluate_training', ROOT / 'scripts/evaluate_training.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    strategy, _ = publish_backtest(tmp_path / 'strategy')
    benchmark, _ = publish_backtest(tmp_path / 'benchmark', weight=.2)
    arguments = ['--strategy-root', str(tmp_path / 'strategy'), '--strategy-result', strategy['generation_id'],
                 '--benchmark-root', str(tmp_path / 'benchmark'), '--benchmark-result', benchmark['generation_id'],
                 '--start', DATES[0], '--end', DATES[-1], '--output-root', str(tmp_path / 'reports')]
    assert module.main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'diagnostic_published'
    assert Path(report['manifest_path']).exists()
    arguments[arguments.index('--end') + 1] = '2026-01-31'
    assert module.main(arguments) == 2
    error = json.loads(capsys.readouterr().out)
    assert 'not covered' in error['errors'][0]


def test_ic_entry_point_reads_published_scores_labels_and_reviewed_test_split(tmp_path):
    from uq.contracts.model_layer import (
        _MODEL_QUALITY_REVIEW_REGISTRY, bind_reviewed_quality_decision,
        create_reviewed_quality_decision, model_manifest_identities,
    )
    from uq.evaluation.metrics import information_coefficients
    from uq.evaluation.report import published_ic_inputs
    from uq.models.dataset import DatasetBuilder
    from uq.models.label_store_writer import LabelSetWriter
    from uq.models.labels import LabelBuilder
    from uq.models.predictions import PredictionBuilder
    from tests.review_key import REVIEWER_PRIVATE_KEY
    from tests.test_model_labels_dataset import _adjusted_frame, _binding
    from tests.test_research_chain_phase3 import prediction_decision
    from tests.test_research_chain_phase4 import _publish_model_artifact

    bars = _adjusted_frame(n_instruments=3, n_days=12)
    bars['instrument'] = bars['instrument'].map({'INST0000': 'INST0', 'INST0001': 'INST1', 'INST0002': 'INST2'})
    for index in range(3):
        mask = bars['instrument'] == f'INST{index}'
        bars.loc[mask, 'close'] += pd.Series(range(12), index=bars.index[mask]) * (index + 1) * .1
    label_manifest, label_frame = LabelBuilder(name='return_5d', semantic_version='1.0.0').build_with_frame(
        bars, upstream_bindings=[_binding(bars)])
    label = LabelSetWriter(tmp_path).publish(label_manifest, label_frame)
    dataset = DatasetBuilder(dataset_name='evaluation_fixture', semantic_version='1.0.0').build(
        ordered_features=['volume_ratio_20d'], factor_set='raw_price', factor_version='1.0.0',
        factor_generation_ids=['a' * 64], label_set_name='return_5d', label_generation_id=label['generation_id'],
        universe_snapshot_generation_id='b' * 64, row_count=36,
        split_policy={'purge_trading_days': 5, 'embargo_trading_days': 2, 'splits': [
            {'name': 'train', 'start_date': '2025-01-02', 'end_date': '2025-03-31'},
            {'name': 'validation', 'start_date': '2025-06-02', 'end_date': '2025-06-30'},
            {'name': 'test', 'start_date': '2026-01-05', 'end_date': '2026-01-20'},
        ]},
    )
    generation = dataset['generation_id']
    policy = _MODEL_QUALITY_REVIEW_REGISTRY.bindings['model_dataset_v1']
    decision = create_reviewed_quality_decision(
        binding_type='model_dataset_v1', policy='reject_all', status='passed',
        checks=[{'name': name, 'threshold': 0, 'observed': 0, 'level': 'error', 'result': 'passed'}
                for name in policy['allowed_checks']], errors=[], warnings=[],
        producer_code_fingerprint='0' * 64, private_key_pem=REVIEWER_PRIVATE_KEY,
        subject_generation_id=generation, subject_content_sha256=generation,
    )
    report, checksum = bind_reviewed_quality_decision(decision, binding_type='model_dataset_v1',
                                                      subject_generation_id=generation)
    dataset['quality_report_checksum_sha256'] = checksum
    dataset['generation_id'], dataset['manifest_digest_sha256'] = model_manifest_identities(
        dataset, schema_name='model_dataset',
        exclude_fields={'quality_report_checksum_sha256', 'logical_fingerprint',
                        'input_feature_schema_manifest_digest_sha256'},
    )
    directory = tmp_path / 'datasets/dataset=evaluation_fixture/version=1.0.0' / f'generation={generation}'
    directory.mkdir(parents=True)
    # The evaluator explicitly verifies this manifest/review, not a feature payload.
    (directory / 'manifest.json').write_text(json.dumps(dataset))
    reviews = tmp_path / 'external_quality_reviews'
    reviews.mkdir(exist_ok=True)
    (reviews / f'{checksum}.json').write_text(json.dumps(report))
    run, artifact, artifact_checksum = _publish_model_artifact(tmp_path)
    day = DATES[0]
    builder = PredictionBuilder(tmp_path)
    prediction, binary = builder.build(
        prediction_set_name='evaluation_fixture', model_artifact_generation_id=artifact,
        model_artifact_checksum=artifact_checksum, input_dataset_generation_id=generation,
        run_generation_id=run, artifact_store=builder.artifact_store, decision_date=day,
        scores=pd.DataFrame({'instrument': ['INST0', 'INST1', 'INST2'],
                             'datetime': [pd.Timestamp(day)] * 3, 'score': [.1, .2, .3]}),
        score_semantics={'unit': 'raw_score', 'direction': 'lower_better', 'ranking_scope': 'universe',
                         'tie_policy': 'instrument_order', 'normalization': 'none'},
        eligibility_policy='reviewed-v1', eligibility_status='passed', quality_decision=prediction_decision(),
    )
    builder.publish(prediction, binary)
    bindings = [{'generation_id': prediction['generation_id'], 'decision_date': day}]
    scores, labels, provenance = published_ic_inputs(tmp_path, bindings, tmp_path, label['generation_id'],
                                                    calendar_dates=[day])
    assert scores['score'].tolist() == [-.1, -.2, -.3]
    assert provenance['label']['horizon_trading_days'] == 5
    assert generation in provenance['prediction_datasets']
    timestamp_scores, _, _ = published_ic_inputs(
        tmp_path, bindings, tmp_path, label['generation_id'], calendar_dates=[pd.Timestamp(day)])
    pd.testing.assert_frame_equal(timestamp_scores, scores)
    with pytest.raises(ContractError, match='invalid evaluation generation id'):
        published_ic_inputs(
            tmp_path, [{'decision_date': day}], tmp_path, label['generation_id'], calendar_dates=[day])
    _summary, daily_ic = information_coefficients(scores, labels, calendar_dates=[day], horizon_sessions=5)
    assert daily_ic['rank_ic'].iloc[0] == pytest.approx(-1)
    wrong_time, wrong_binary = builder.build(
        prediction_set_name='evaluation_wrong_time', model_artifact_generation_id=artifact,
        model_artifact_checksum=artifact_checksum, input_dataset_generation_id=generation,
        run_generation_id=run, artifact_store=builder.artifact_store, decision_date=day,
        scores=pd.DataFrame({'instrument': ['INST0', 'INST1', 'INST2'],
                             'datetime': [pd.Timestamp('2025-06-02')] * 3, 'score': [.1, .2, .3]}),
        eligibility_policy='reviewed-v1', eligibility_status='passed', quality_decision=prediction_decision(),
    )
    builder.publish(wrong_time, wrong_binary)
    with pytest.raises(ContractError, match='row dates disagree'):
        published_ic_inputs(tmp_path, [{'generation_id': wrong_time['generation_id'], 'decision_date': day}],
                            tmp_path, label['generation_id'], calendar_dates=[day])
    # Moving the declared test split is a different reviewed dataset. A forged
    # change is rejected rather than letting validation/training dates enter IC.
    dataset['split_policy']['splits'][-1]['start_date'] = '2026-01-06'
    (directory / 'manifest.json').write_text(json.dumps(dataset))
    with pytest.raises(ContractError):
        published_ic_inputs(tmp_path, bindings, tmp_path, label['generation_id'], calendar_dates=[day])


def test_evaluator_refuses_tampered_input_review_signature(tmp_path):
    strategy, _ = publish_backtest(tmp_path / 'strategy')
    benchmark, _ = publish_backtest(tmp_path / 'benchmark')
    path = (tmp_path / 'benchmark/external_quality_reviews' /
            f"{benchmark['quality_report_checksum_sha256']}.json")
    report = json.loads(path.read_text())
    report['review_signature_sha256'] = '0' * 128
    path.write_text(json.dumps(report))
    with pytest.raises(ContractError):
        paired_backtest_inputs(tmp_path / 'strategy', strategy['generation_id'],
                               tmp_path / 'benchmark', benchmark['generation_id'],
                               start=DATES[0], end=DATES[-1])


def test_partial_ic_input_is_refused_before_output_is_created(tmp_path):
    _, scores, _labels = supervised_frames()
    with pytest.raises(ContractError, match='requires predictions, labels and horizon'):
        EvaluationReportStore(tmp_path).publish(daily_frame(), input_bindings={}, predictions=scores)
    assert not (tmp_path / 'research_evaluations').exists()


def test_interrupted_artifact_write_leaves_no_published_or_staging_report(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('simulated disk failure')
    monkeypatch.setattr(pd.DataFrame, 'to_parquet', fail)
    with pytest.raises(OSError, match='disk failure'):
        EvaluationReportStore(tmp_path).publish(daily_frame(), input_bindings={})
    base = tmp_path / 'research_evaluations'
    assert list(base.glob('generation=*')) == []
    assert list(base.glob('.staging-*')) == []


def test_nonfinite_bindings_and_manifest_json_are_typed_rejections(tmp_path):
    store = EvaluationReportStore(tmp_path)
    with pytest.raises(ContractError, match='finite JSON'):
        store.publish(daily_frame(), input_bindings={'invalid': float('nan')})
    manifest = store.publish(daily_frame(), input_bindings={})
    path = store.directory_for(manifest['generation_id']) / 'manifest.json'
    manifest['metrics']['returns']['overall']['cumulative_return'] = float('nan')
    path.write_text(json.dumps(manifest))
    with pytest.raises(ContractError, match='non-finite'):
        store.read(manifest['generation_id'])
