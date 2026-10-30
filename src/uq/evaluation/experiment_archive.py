"""Immutable experiment index over reviewed research and recomputable diagnostics.

This index is evidence, not a quality approval. Source stores remain the owners
of their payloads and are re-read when an archive is opened.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import uuid
from pathlib import Path

import pandas as pd

from ..contracts.canonical_v2 import fsync_dir, fsync_tree
from ..contracts.gate_contracts import canonical_json, sha256_json, validate_contract
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError
from ..research_chain.resolver import FileResearchRunStore
from .metrics import session_dates
from .report import EvaluationReportStore, _verified_result, paired_backtest_inputs

_PART = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


def experiment_name(components: dict) -> str:
    if set(components) != {'universe', 'oos_start', 'oos_end', 'protocol', 'model', 'variant'}:
        raise ContractError('experiment name requires universe, OOS dates, protocol, model and variant')
    for key in ('universe', 'protocol', 'model', 'variant'):
        if not isinstance(components[key], str) or not _PART.fullmatch(components[key]):
            raise ContractError(f'invalid experiment name component: {key}')
    start, end = session_dates([components['oos_start'], components['oos_end']], label='experiment OOS')
    if start > end:
        raise ContractError('experiment OOS start follows end')
    return '-'.join((components['universe'], start, end, components['protocol'],
                     components['model'], components['variant']))


def _environment_receipt(receipt: dict) -> dict:
    if (not isinstance(receipt, dict) or receipt.get('record_version') != 1
            or receipt.get('kind') != 'research_environment'
            or receipt.get('fingerprint_profile') != 'source_tree_sha256_v1'):
        raise ContractError('experiment requires a source-tree environment receipt')
    files = receipt.get('source_files')
    if (not isinstance(files, list) or not files
            or any(not isinstance(item, dict) or set(item) != {'path', 'sha256', 'bytes'}
                   or not isinstance(item['path'], str) or not item['path']
                   or not isinstance(item['sha256'], str) or not _SHA.fullmatch(item['sha256'])
                   or type(item['bytes']) is not int or item['bytes'] < 0 for item in files)
            or len({item['path'] for item in files}) != len(files)):
        raise ContractError('environment source file inventory is invalid')
    try:
        expected = sha256_json({'profile': 'source_tree_sha256_v1', 'files': files})
        canonical_json(receipt)
    except (TypeError, ValueError) as exc:
        raise ContractError('environment receipt contains invalid JSON values') from exc
    if (receipt.get('code_fingerprint') != expected
            or not isinstance(receipt.get('environment_lock_digest_sha256'), str)
            or not _SHA.fullmatch(receipt['environment_lock_digest_sha256'])
            or not isinstance(receipt.get('git_head'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', receipt['git_head'])):
        raise ContractError('environment receipt fingerprint or lock/head identity is invalid')
    return receipt


def _telemetry(root: Path, path: Path, request: dict, receipt: dict) -> dict:
    expected_dir = (root / 'research_runs/telemetry'
                    / f"request={request['request_content_generation_id']}"
                    / f"run={request['run_id']}")
    path = path.absolute()
    if path.parent != expected_dir or not re.fullmatch(r'attempt=[0-9a-f-]{36}\.json', path.name):
        raise ContractError('experiment telemetry path is outside its request/run attempt directory')
    reject_symlinks_within(root, path, label='experiment telemetry')
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ContractError('experiment telemetry is unavailable or malformed') from exc
    if not isinstance(record, dict):
        raise ContractError('experiment telemetry is not an object')
    try:
        expected_checksum = sha256_json({
            key: value for key, value in record.items() if key != 'checksum_sha256'})
    except (TypeError, ValueError) as exc:
        raise ContractError('experiment telemetry contains invalid JSON values') from exc
    if (record.get('checksum_sha256') != expected_checksum
            or record.get('record_version') != 1 or record.get('kind') != 'research_attempt_telemetry'
            or record.get('status') != 'passed'
            or record.get('request_content_generation_id') != request['request_content_generation_id']
            or record.get('run_id') != request['run_id']
            or record.get('environment') != receipt):
        raise ContractError('experiment telemetry does not bind the passed request/environment')
    return record


def _build(
    *, components: dict, data_root: Path, benchmark_root: Path, evaluation_root: Path,
    result_generation: str, result_digest: str, evaluation_generation: str,
    telemetry_path: Path, environment: dict, limitations: list[str],
) -> dict:
    name = experiment_name(components)
    if (not isinstance(limitations, list) or not limitations
            or any(not isinstance(item, str) or not item.strip() for item in limitations)):
        raise ContractError('experiment limitations must be a nonempty list of statements')
    for value in (result_generation, result_digest, evaluation_generation):
        if not isinstance(value, str) or not _SHA.fullmatch(value):
            raise ContractError('experiment source identity is invalid')
    data_root, benchmark_root, evaluation_root = (
        Path(value).absolute() for value in (data_root, benchmark_root, evaluation_root)
    )
    store = FileResearchRunStore(data_root)
    result = dict(store.read_result(result_generation, result_digest))
    if result['final_status'] != 'passed':
        raise ContractError('experiment research result did not pass')
    request_path = (data_root / 'research_runs/requests'
                    / f"request={result['request_content_generation_id']}"
                    / f"run={result['run_id']}" / 'manifest.json')
    result_path = (data_root / 'research_runs/results'
                   / f"request={result['request_content_generation_id']}"
                   / f"run={result['run_id']}" / f'result={result_generation}' / 'manifest.json')
    for path in (request_path, result_path):
        reject_symlinks_within(data_root, path, label='experiment research source')
        if not path.is_file():
            raise ContractError('experiment research source path disagrees with its ledger')
    request = dict(store.read_request(result['request_content_generation_id'],
                                     result['request_manifest_digest_sha256'], result['run_id']))
    if (request['execution_mode'] != 'full_research_run'
            or request['contract_version'] != result['contract_version']
            or not request['window_start_date'] <= components['oos_start']
            <= components['oos_end'] <= request['window_end_date']):
        raise ContractError('experiment OOS window disagrees with research request')
    if components['model'] != request['model_definition_template']['algorithm']:
        raise ContractError('experiment model name disagrees with reviewed request algorithm')
    environment = _environment_receipt(environment)
    if (request['environment']['code_fingerprint'] in {'0' * 64, None}
            or request['environment']['environment_lock_digest_sha256'] in {'0' * 64, None}
            or request['environment']['code_fingerprint'] != environment['code_fingerprint']
            or request['environment']['environment_lock_digest_sha256']
            != environment['environment_lock_digest_sha256']
            or result['runner_identity']['code_fingerprint'] != environment['code_fingerprint']
            or result['runner_identity']['lock_digest_sha256']
            != environment['environment_lock_digest_sha256']):
        raise ContractError('experiment lacks matching nonzero request/result environment binding')
    telemetry = _telemetry(data_root, Path(telemetry_path), request, environment)
    backtest_bindings = [binding for stage in result['stage_records']
                         if stage['stage'] == 'backtest_execution'
                         for binding in stage['output_bindings']
                         if binding['output_family'] == 'backtest_result']
    if len(backtest_bindings) != 1:
        raise ContractError('experiment requires one research backtest result')
    backtest_binding = backtest_bindings[0]
    backtest, config, _ = _verified_result(data_root, backtest_binding['generation_id'])
    if (backtest['manifest_digest_sha256'] != backtest_binding['manifest_digest_sha256']
            or backtest_binding['data_checksum_sha256']
            != backtest['equity_curve_artifact']['checksum_sha256']
            or backtest_binding['quality_decision_checksum_sha256']
            != backtest['quality_report_checksum_sha256']
            or backtest_binding['physical_path']
            != f"backtest_results/result={backtest['generation_id']}"
            or config['generation_id'] != request['backtest_config_binding']['generation_id']
            or config['manifest_digest_sha256']
            != request['backtest_config_binding']['manifest_digest_sha256']):
        raise ContractError('experiment backtest lineage disagrees with research result/request')
    evaluation, frames = EvaluationReportStore(evaluation_root).read(evaluation_generation)
    source = evaluation['input_bindings']
    if (not isinstance(source, dict) or not isinstance(source.get('strategy'), dict)
            or not isinstance(source.get('benchmark'), dict)
            or source['strategy'].get('generation_id') != backtest['generation_id']
            or source['strategy'].get('manifest_digest_sha256') != backtest['manifest_digest_sha256']):
        raise ContractError('experiment evaluation strategy differs from research backtest')
    daily = frames['daily']
    if (daily['date'].iloc[0] != components['oos_start']
            or daily['date'].iloc[-1] != components['oos_end']):
        raise ContractError('experiment OOS dates differ from evaluation sessions')
    regimes = dict(zip(daily['date'], daily['regime'])) if 'regime' in daily else None
    recomputed_daily, recomputed_bindings = paired_backtest_inputs(
        data_root, backtest['generation_id'], benchmark_root,
        source['benchmark']['generation_id'], start=components['oos_start'],
        end=components['oos_end'], regimes=regimes,
    )
    try:
        pd.testing.assert_frame_equal(daily, recomputed_daily, check_exact=True)
    except AssertionError as exc:
        raise ContractError('experiment evaluation daily returns differ from source backtests') from exc
    for key, value in recomputed_bindings.items():
        if source.get(key) != value:
            raise ContractError(f'experiment evaluation source binding drift: {key}')
    benchmark, benchmark_config, _ = _verified_result(
        benchmark_root, source['benchmark']['generation_id'])
    tests = [split for split in request['dataset_policy_template']['split_policy']['splits']
             if split['name'] == 'test']
    if len(tests) > 1 or (tests and not tests[0]['start_date'] <= components['oos_start']
                          <= components['oos_end'] <= tests[0]['end_date']):
        raise ContractError('experiment OOS window is outside declared test split')
    review_refs = sorted({binding['quality_decision_checksum_sha256']
                          for stage in result['stage_records'] for binding in stage['output_bindings']
                          if binding['quality_decision_checksum_sha256'] != '0' * 64}
                         | {item['quality_report_checksum_sha256'] for item in
                            (backtest, config, benchmark, benchmark_config)})
    payload = {
        'record_version': 1, 'kind': 'trading_experiment_archive',
        'publication_scope': 'evidence_only_no_quality_authorization',
        'experiment_name': name, 'components': components,
        'source_locations': {
            'data_root': str(data_root), 'benchmark_root': str(benchmark_root),
            'evaluation_root': str(evaluation_root), 'telemetry_path': str(telemetry_path.absolute()),
        },
        'request': request,
        'result_binding': {'generation_id': result_generation, 'manifest_digest_sha256': result_digest},
        'backtest_binding': {'generation_id': backtest['generation_id'],
                             'manifest_digest_sha256': backtest['manifest_digest_sha256']},
        'evaluation_binding': {'generation_id': evaluation_generation},
        'evaluation_metrics': evaluation['metrics'], 'evaluation_protocol': evaluation['protocol'],
        'environment': environment, 'telemetry': telemetry,
        'external_review_checksums': review_refs,
        'verification_scope': {
            'declared_test_split_covered': bool(tests),
            'evaluation_tables_recomputed': True,
            'paired_backtest_sources_reverified': True,
            'historical_source_bytes_reconstructed': False,
            'upstream_prediction_label_payloads_reread': False,
            'one_year_oos_and_pit_certified': False,
        },
        'limitations': limitations,
    }
    payload['archive_checksum_sha256'] = sha256_json(payload)
    validate_contract('trading_experiment_archive.v1.json', payload)
    return payload


class ExperimentArchiveStore:
    """Index an experiment without copying market data or minting approvals."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path_for(self, name: str) -> Path:
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,255}', name):
            raise ContractError('invalid experiment archive name')
        path = self.root / 'experiments' / name / 'manifest.json'
        reject_symlinks_within(self.root, path, label='experiment archive')
        return path

    def publish(self, **inputs) -> dict:
        record = _build(**inputs)
        path = self.path_for(record['experiment_name'])
        base = path.parent.parent
        base.mkdir(parents=True, exist_ok=True)
        lock_path = base / 'publication.lock'
        reject_symlinks_within(self.root, lock_path, label='experiment archive lock')
        with lock_path.open('a+') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if path.exists():
                existing = self.read(record['experiment_name'])
                if existing != record:
                    raise ContractError('immutable experiment archive already exists with different evidence')
                return existing
            staging = base / f'.staging-{uuid.uuid4().hex}'
            reject_symlinks_within(self.root, staging, label='experiment archive staging')
            try:
                staging.mkdir()
                (staging / 'manifest.json').write_bytes(canonical_json(record) + b'\n')
                fsync_tree(staging)
                os.rename(staging, path.parent)
                fsync_dir(base)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        return self.read(record['experiment_name'])

    def read(self, name: str) -> dict:
        path = self.path_for(name)
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ContractError('experiment archive is unavailable or malformed') from exc
        validate_contract('trading_experiment_archive.v1.json', record)
        if record['experiment_name'] != name or record['archive_checksum_sha256'] != sha256_json({
                key: value for key, value in record.items() if key != 'archive_checksum_sha256'}):
            raise ContractError('experiment archive identity mismatch')
        source = record['source_locations']
        expected = _build(
            components=record['components'], data_root=Path(source['data_root']),
            benchmark_root=Path(source['benchmark_root']),
            evaluation_root=Path(source['evaluation_root']),
            result_generation=record['result_binding']['generation_id'],
            result_digest=record['result_binding']['manifest_digest_sha256'],
            evaluation_generation=record['evaluation_binding']['generation_id'],
            telemetry_path=Path(source['telemetry_path']), environment=record['environment'],
            limitations=record['limitations'],
        )
        if expected != record:
            raise ContractError('experiment archive no longer matches its source evidence')
        return record
