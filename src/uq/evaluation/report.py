"""Verified paired-backtest diagnostics with immutable, recomputable inputs."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ..backtest.engine import BacktestResultStore
from ..contracts.canonical_v2 import fsync_dir, fsync_tree
from ..contracts.gate_contracts import canonical_json, sha256_json, validate_contract
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError
from ..models.predictions import PredictionBuilder
from ..research_chain.owning_contracts import BacktestConfigStore, BacktestMarketDatasetStore, LabelStore
from .metrics import evaluate_returns, information_coefficients, session_dates

_COMPARABLE_CONFIG_FIELDS = (
    'contract_version', 'schema_version', 'start_date', 'end_date', 'cost_model',
    'execution_model', 'limit_rules', 'initial_capital', 'calendar_binding',
    'price_source_binding', 'universe_binding', 'corporate_action_binding', 'suspension_binding',
    'execution_facts_binding', 'risk_constraints',
)


def _identity(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ContractError('invalid evaluation generation id')


def _dated(frame: pd.DataFrame, *, columns: set[str], label: str) -> pd.DataFrame:
    if frame.empty or columns - set(frame):
        raise ContractError(f'{label} is empty or missing columns: {sorted(columns - set(frame))}')
    work = frame.copy()
    work['date'] = session_dates(work['date'].tolist(), label=label)
    if work['date'].duplicated().any():
        raise ContractError(f'{label} contains duplicate sessions')
    return work.sort_values('date', kind='mergesort').reset_index(drop=True)


def _verified_result(root: Path, generation: str) -> tuple[dict, dict, dict[str, pd.DataFrame]]:
    _identity(generation)
    partition = root / 'backtest_results' / f'result={generation}'
    reject_symlinks_within(root, partition, label='evaluation backtest input')
    for name in ('manifest.json', 'equity_curve.parquet', 'daily_metrics.parquet', 'fills.parquet',
                 'corporate_actions.parquet'):
        reject_symlinks_within(root, partition / name, label='evaluation backtest input')
    manifest, artifacts = BacktestResultStore(root).read(generation)
    # The backtest reader verifies payload hashes/review binding. Also pin the
    # public generation/digest itself through the same identity rule as the writer.
    from ..contracts.model_layer import model_manifest_identities
    version = manifest['contract_version']
    family = f'backtest_result_v{version}' if version in {2, 3, 4} else 'backtest_result'
    expected = model_manifest_identities(manifest, schema_name=family)
    if expected != (generation, manifest['manifest_digest_sha256']):
        raise ContractError('evaluation backtest manifest identity mismatch')
    config = BacktestConfigStore(root).read_manifest(manifest['backtest_config_generation_id'])
    config_path = root / 'backtest_configs' / f"generation={config['generation_id']}" / 'manifest.json'
    reject_symlinks_within(root, config_path, label='evaluation config input')
    config_version = config['contract_version']
    config_family = f'backtest_config_v{config_version}' if config_version in {2, 3, 4} else 'backtest_config'
    _verify_review(root, manifest, family, 'backtest_result_v1')
    _verify_review(root, config, config_family, 'backtest_config_v1')
    if manifest['price_source_binding'] != config['price_source_binding']:
        raise ContractError('backtest price lineage disagrees with its config')
    if version != config_version:
        raise ContractError('backtest contract version disagrees with its config')
    if version in {3, 4}:
        from ..backtest.execution_facts import ExecutionFactsStore, facts_binding

        facts, _ = ExecutionFactsStore(root).read(config['execution_facts_binding']['generation_id'])
        if (facts_binding(facts) != config['execution_facts_binding']
                or manifest['execution_facts_binding'] != config['execution_facts_binding']):
            raise ContractError('backtest execution facts lineage disagrees with its config')
    if version == 4 and manifest.get('corporate_action_binding') != config.get('corporate_action_binding'):
        raise ContractError('backtest corporate-action lineage disagrees with its config')
    if version == 4:
        from ..backtest.corporate_actions import ACTION_EVENT_COLUMNS, normalize_action_events

        binding = config['corporate_action_binding']
        events = BacktestMarketDatasetStore(root).read_frame(
            binding['dataset_generation_id'], binding['data_checksum_sha256'],
            dataset_type='corporate_action_events', required_columns=set(ACTION_EVENT_COLUMNS),
        )
        calendar = BacktestMarketDatasetStore(root).read_frame(
            config['calendar_binding']['generation_id'], config['calendar_binding']['checksum_sha256'],
            dataset_type='calendar', required_columns={'date'},
        )
        normalize_action_events(
            events, start_date=config['start_date'], end_date=config['end_date'],
            trading_dates=sorted(calendar['date'].astype(str).unique().tolist()),
        )
        if manifest.get('corporate_action_semantics') != config.get('corporate_action_model'):
            raise ContractError('backtest corporate-action semantics disagree with its config')
    if (manifest['period_start'], manifest['period_end']) != (config['start_date'], config['end_date']):
        raise ContractError('backtest period disagrees with its config')
    return manifest, config, artifacts


def _verify_review(root: Path, manifest: dict, schema: str, binding_type: str) -> None:
    from ..contracts.model_layer import ModelContractLoader, bind_reviewed_quality_decision, model_manifest_identities
    ModelContractLoader.validate(schema, manifest)
    if schema == 'model_dataset':
        # Dataset identity excludes the review; its signed subject is the final
        # generation, unlike backtest config/result whose review enters identity.
        provisional = manifest['generation_id']
    else:
        provisional, _ = model_manifest_identities(
            {**manifest, 'quality_report_checksum_sha256': '0' * 64}, schema_name=schema,
        )
    checksum = manifest['quality_report_checksum_sha256']
    path = root / 'external_quality_reviews' / f'{checksum}.json'
    reject_symlinks_within(root, path, label='evaluation input review')
    try:
        report = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ContractError('evaluation input review unavailable or malformed') from exc
    decision = {key: value for key, value in report.items()
                if key not in {'bound_generation_id', 'report_checksum_sha256'}}
    decision['subject_generation_id'] = report.get('bound_generation_id')
    subject_content = manifest['weights_checksum_sha256'] if schema == 'target_weights' else provisional
    verified, verified_checksum = bind_reviewed_quality_decision(
        decision, binding_type=binding_type, subject_generation_id=provisional,
        subject_content_sha256=subject_content,
    )
    if verified_checksum != checksum or verified != report:
        raise ContractError('evaluation input review checksum mismatch')


def paired_backtest_inputs(
    strategy_root: Path, strategy_generation: str, benchmark_root: Path, benchmark_generation: str,
    *, start: str, end: str, regimes: Mapping[str, str] | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Require identical economics/market/universe bindings before comparing returns.

    A benchmark is another executed backtest, not a frictionless price index.
    Its construction scheme is still an external, reviewed portfolio choice.
    This function does not attest historical universe completeness or OOS splits.
    """
    start, end = session_dates([start, end], label='evaluation window')
    if start > end:
        raise ContractError('evaluation start follows end')
    strategy, strategy_config, strategy_frames = _verified_result(Path(strategy_root), strategy_generation)
    benchmark, benchmark_config, benchmark_frames = _verified_result(Path(benchmark_root), benchmark_generation)
    for field in _COMPARABLE_CONFIG_FIELDS:
        if strategy_config.get(field) != benchmark_config.get(field):
            raise ContractError(f'benchmark comparability mismatch: {field}')
    required_daily = {'date', 'daily_return', 'executed_turnover'}
    left = _dated(strategy_frames['daily_metrics'], columns=required_daily, label='strategy metrics')
    right = _dated(benchmark_frames['daily_metrics'], columns=required_daily, label='benchmark metrics')
    equity = _dated(strategy_frames['equity_curve'], columns={'date', 'holdings_value'}, label='strategy equity')
    if left['date'].tolist() != right['date'].tolist() or left['date'].tolist() != equity['date'].tolist():
        raise ContractError('benchmark, strategy and equity calendars differ')
    if start not in set(left['date']) or end not in set(left['date']):
        raise ContractError('evaluation window endpoints are not covered by backtest sessions')
    holdings = equity['holdings_value'].to_numpy(dtype=float)
    if not np.isfinite(holdings).all() or (holdings < 0).any():
        raise ContractError('invalid holdings values in strategy equity')
    invested = (holdings > 0) | np.r_[False, holdings[:-1] > 0]
    fills = strategy_frames['fills']
    filled_days = set(fills.loc[(fills['status'] == 'filled') & (fills['filled_shares'] > 0), 'date'])
    invested |= left['date'].isin(filled_days).to_numpy()
    daily = pd.DataFrame({
        'date': left['date'], 'strategy_return': left['daily_return'],
        'benchmark_return': right['daily_return'], 'invested': invested,
        'turnover': left['executed_turnover'],
    })
    daily = daily.loc[(daily['date'] >= start) & (daily['date'] <= end)].reset_index(drop=True)
    if regimes is not None:
        if set(regimes) != set(daily['date']):
            raise ContractError('market-regime dates must exactly match evaluation sessions')
        daily['regime'] = daily['date'].map(regimes)
    # Validate and compute now, before any diagnostic output is written.
    evaluate_returns(daily)
    bindings = {
        'strategy': {'generation_id': strategy_generation,
                     'manifest_digest_sha256': strategy['manifest_digest_sha256'],
                     'artifacts': {name: strategy[name + '_artifact']
                                   for name in ('daily_metrics', 'equity_curve', 'fills')}},
        'benchmark': {'generation_id': benchmark_generation,
                      'manifest_digest_sha256': benchmark['manifest_digest_sha256'],
                      'artifacts': {name: benchmark[name + '_artifact']
                                    for name in ('daily_metrics', 'equity_curve', 'fills')}},
        'comparison_config': {field: strategy_config.get(field) for field in _COMPARABLE_CONFIG_FIELDS},
        'strategy_config_generation_id': strategy_config['generation_id'],
        'benchmark_config_generation_id': benchmark_config['generation_id'],
        'comparability_checked_fields': list(_COMPARABLE_CONFIG_FIELDS),
        'out_of_sample_attestation': 'not_verified_by_paired_backtest_reader',
        'historical_pit_completeness': 'not_verified_by_paired_backtest_reader',
    }
    return daily, bindings


def published_ic_inputs(
    prediction_root: Path, predictions: list[dict[str, str]], label_root: Path,
    label_generation: str, *, calendar_dates: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Load reviewed predictions and checksum-verified labels via owning stores."""
    if not predictions:
        raise ContractError('IC requires published predictions')
    calendar = session_dates(calendar_dates, label='evaluation calendar')
    if not calendar or calendar != sorted(set(calendar)):
        raise ContractError('evaluation calendar must be nonempty, sorted and unique')
    _identity(label_generation)
    label_manifest, labels = LabelStore(label_root).read_frame(label_generation)
    horizon = label_manifest['horizon_trading_days']
    pieces, bindings, datasets = [], [], {}
    normalized_bindings = []
    for binding in predictions:
        if not isinstance(binding, dict):
            raise ContractError('published prediction binding must be an object')
        generation = binding.get('generation_id')
        decision_date = binding.get('decision_date')
        _identity(generation)
        day = session_dates([decision_date], label='published prediction binding date')[0]
        normalized_bindings.append((day, generation))
    for day, generation in sorted(normalized_bindings):
        if day not in calendar:
            raise ContractError('published prediction lies outside the evaluation calendar')
        partition = Path(prediction_root) / 'predictions' / f"prediction_set={generation}" / f'date={day}'
        for name in ('manifest.json', 'data.parquet'):
            reject_symlinks_within(Path(prediction_root), partition / name, label='evaluation prediction input')
        manifest, frame = PredictionBuilder(prediction_root).read(generation, day)
        if manifest['generation_id'] != generation:
            raise ContractError('prediction generation does not match requested binding')
        if 'datetime' in frame and set(session_dates(frame['datetime'].tolist(), label='prediction rows')) != {day}:
            raise ContractError('prediction row dates disagree with the published decision date')
        dataset_generation = manifest['input_dataset_generation_id']
        if dataset_generation not in datasets:
            paths = list((Path(prediction_root) / 'datasets').glob(
                f'dataset=*/version=*/generation={dataset_generation}/manifest.json'
            ))
            if len(paths) != 1:
                raise ContractError('IC requires one published input-dataset manifest per prediction')
            from ..contracts.model_layer import ModelContractLoader
            dataset = ModelContractLoader(prediction_root).load('model_dataset', paths[0])
            if dataset['generation_id'] != dataset_generation:
                raise ContractError('prediction input dataset generation disagrees with its path')
            _verify_review(Path(prediction_root), dataset, 'model_dataset', 'model_dataset_v1')
            datasets[dataset_generation] = dataset
        dataset = datasets[dataset_generation]
        if dataset['label_generation_id'] != label_generation:
            raise ContractError('IC label does not match prediction input dataset')
        tests = [split for split in dataset['split_policy']['splits'] if split['name'] == 'test']
        if len(tests) != 1 or not tests[0]['start_date'] <= day <= tests[0]['end_date']:
            raise ContractError('IC prediction date is not in its dataset test split')
        column = manifest['score_semantics']['column']
        direction = manifest['score_semantics']['direction']
        score = frame[column].astype(float) * (1 if direction == 'higher_better' else -1)
        pieces.append(pd.DataFrame({'decision_date': day, 'instrument': frame['instrument'], 'score': score}))
        bindings.append({'generation_id': generation, 'decision_date': day,
                         'manifest_digest_sha256': manifest['manifest_digest_sha256'],
                         'data_checksum_sha256': manifest['data_checksum_sha256'],
                         'input_dataset_generation_id': dataset_generation,
                         'original_score_semantics': manifest['score_semantics']})
    labels = labels.copy()
    labels['decision_date'] = session_dates(labels['decision_date'].tolist(), label='published labels')
    labels = labels.loc[labels['decision_date'].isin(calendar)].reset_index(drop=True)
    scores = pd.concat(pieces, ignore_index=True)
    information_coefficients(scores, labels, calendar_dates=calendar, horizon_sessions=horizon)
    return scores, labels, {
        'predictions': bindings,
        'prediction_datasets': {
            generation: {'manifest_digest_sha256': dataset['manifest_digest_sha256'],
                         'split_policy': dataset['split_policy'],
                         'verification_scope': 'reviewed_manifest_only; feature_payload_not_reloaded'}
            for generation, dataset in datasets.items()
        },
        'label': {'generation_id': label_generation,
                  'manifest_digest_sha256': label_manifest['manifest_digest_sha256'],
                  'data_checksum_sha256': label_manifest['data_checksum_sha256'],
                  'horizon_trading_days': horizon, 'formula_sha256': label_manifest['formula_sha256']},
    }


def _compute(frames: dict[str, pd.DataFrame], protocol: dict) -> dict:
    metrics = {'returns': evaluate_returns(frames['daily'], annualization=protocol['annualization_sessions'])}
    if protocol['label_horizon_sessions'] is not None:
        summary, _daily_ic = information_coefficients(
            frames['predictions'], frames['labels'], calendar_dates=frames['daily']['date'].tolist(),
            horizon_sessions=protocol['label_horizon_sessions'], hac_lags=protocol['hac_lags'],
        )
        metrics['information_coefficients'] = summary
    return metrics


class EvaluationReportStore:
    """Store diagnostic evidence; never authorize quality, model promotion or orders.

    Metrics are recomputed from archived input tables on publication and every
    read. Byte checksums alone are not treated as proof of metric correctness.
    """
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def directory_for(self, generation: str) -> Path:
        _identity(generation)
        path = self.root / 'research_evaluations' / f'generation={generation}'
        reject_symlinks_within(self.root, path, label='evaluation report')
        return path

    def publish(
        self, daily: pd.DataFrame, *, input_bindings: dict,
        predictions: pd.DataFrame | None = None, labels: pd.DataFrame | None = None,
        horizon_sessions: int | None = None, hac_lags: int | None = None,
    ) -> dict:
        if 'date' not in daily:
            raise ContractError('evaluation daily input missing date')
        try:
            canonical_json(input_bindings)
        except (TypeError, ValueError) as exc:
            raise ContractError('evaluation input bindings must be finite JSON') from exc
        frames = {'daily': daily.copy().sort_values('date', kind='mergesort').reset_index(drop=True)}
        supplied = (predictions is not None, labels is not None, horizon_sessions is not None)
        if any(supplied) and not all(supplied):
            raise ContractError('IC publication requires predictions, labels and horizon together')
        if all(supplied):
            if type(horizon_sessions) is not int or horizon_sessions < 1:
                raise ContractError('IC horizon must be a positive integer')
            for name, frame, value in [('predictions', predictions, 'score'), ('labels', labels, 'label')]:
                if {'instrument', 'decision_date', value} - set(frame):
                    raise ContractError(f'{name} missing IC input columns')
            frames.update({
                'predictions': predictions.sort_values(['decision_date', 'instrument'], kind='mergesort').reset_index(drop=True),
                'labels': labels.sort_values(['decision_date', 'instrument'], kind='mergesort').reset_index(drop=True),
            })
        elif hac_lags is not None:
            raise ContractError('HAC lags require IC inputs')
        protocol = {'metric_version': 1, 'annualization_sessions': 252,
                    'label_horizon_sessions': horizon_sessions,
                    'hac_lags': (horizon_sessions - 1 if hac_lags is None else hac_lags)
                                if horizon_sessions is not None else None}
        metrics = _compute(frames, protocol)
        base = self.root / 'research_evaluations'
        reject_symlinks_within(self.root, base, label='evaluation report')
        base.mkdir(parents=True, exist_ok=True)
        lock_path = base / 'publication.lock'
        reject_symlinks_within(self.root, lock_path, label='evaluation lock')
        with lock_path.open('a+') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            staging = base / f'.staging-{uuid.uuid4().hex}'
            try:
                staging.mkdir()
                descriptors = {}
                for name, frame in frames.items():
                    path = staging / f'{name}.parquet'
                    frame.to_parquet(path, index=False)
                    descriptors[name] = {
                        'file': path.name, 'checksum_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'row_count': len(frame), 'columns': list(frame.columns),
                    }
                manifest = {
                    'record_version': 1, 'kind': 'research_evaluation',
                    'publication_scope': 'diagnostic_only_no_quality_authorization',
                    'protocol': protocol, 'input_bindings': input_bindings,
                    'artifacts': descriptors, 'metrics': metrics,
                }
                generation = sha256_json(manifest)
                manifest['generation_id'] = generation
                validate_contract('research_evaluation.v1.json', manifest)
                (staging / 'manifest.json').write_bytes(canonical_json(manifest) + b'\n')
                self._read_directory(staging, generation)
                target = self.directory_for(generation)
                if target.exists():
                    existing, _ = self.read(generation)
                    if existing != manifest:
                        raise ContractError('evaluation identity collision')
                    return existing
                fsync_tree(staging)
                os.rename(staging, target)
                fsync_dir(base)
                return manifest
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def read(self, generation: str) -> tuple[dict, dict[str, pd.DataFrame]]:
        return self._read_directory(self.directory_for(generation), generation)

    def _read_directory(self, directory: Path, generation: str) -> tuple[dict, dict[str, pd.DataFrame]]:
        path = directory / 'manifest.json'
        reject_symlinks_within(self.root, path, label='evaluation manifest')
        try:
            manifest = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ContractError('evaluation manifest unavailable or malformed') from exc
        validate_contract('research_evaluation.v1.json', manifest)
        payload = {key: value for key, value in manifest.items() if key != 'generation_id'}
        try:
            expected_generation = sha256_json(payload)
        except (ValueError, TypeError) as exc:
            raise ContractError('evaluation manifest contains non-finite or invalid JSON') from exc
        if manifest['generation_id'] != generation or expected_generation != generation:
            raise ContractError('evaluation manifest identity mismatch')
        frames = {}
        expected_names = {'daily'} if manifest['protocol']['label_horizon_sessions'] is None else {'daily', 'predictions', 'labels'}
        if set(manifest['artifacts']) != expected_names:
            raise ContractError('evaluation artifact set does not match protocol')
        for name, descriptor in manifest['artifacts'].items():
            if descriptor['file'] != name + '.parquet':
                raise ContractError('evaluation artifact filename mismatch')
            path = directory / descriptor['file']
            reject_symlinks_within(self.root, path, label='evaluation artifact')
            try:
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            except (OSError, ValueError) as exc:
                raise ContractError('evaluation artifact unavailable or malformed') from exc
            if checksum != descriptor['checksum_sha256']:
                raise ContractError('evaluation artifact checksum mismatch')
            try:
                frame = pd.read_parquet(path)
            except (OSError, ValueError) as exc:
                raise ContractError('evaluation artifact unavailable or malformed') from exc
            if len(frame) != descriptor['row_count'] or list(frame.columns) != descriptor['columns']:
                raise ContractError('evaluation artifact shape mismatch')
            frames[name] = frame
        if canonical_json(_compute(frames, manifest['protocol'])) != canonical_json(manifest['metrics']):
            raise ContractError('evaluation metrics do not recompute from archived inputs')
        return manifest, frames
