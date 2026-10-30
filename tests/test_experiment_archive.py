"""Archive a real scratch backtest/evaluation and an owning research ledger."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.gate_contracts import sha256_json
from uq.contracts.model_layer import research_contract_identities
from uq.errors import ContractError
from uq.evaluation.experiment_archive import ExperimentArchiveStore
from uq.evaluation.report import EvaluationReportStore, _verified_result, paired_backtest_inputs
from uq.research_chain.provenance import capture_environment
from uq.research_chain.resolver import FileResearchRunStore
from uq.research_chain.telemetry import RunTelemetry
from uq.research_chain.owning_contracts import BacktestConfigStore
from tests.test_backtest_layer import DATES
from tests.test_evaluation_report import publish_backtest
from tests.test_research_chain_phase5 import _request, _result

ROOT = Path(__file__).resolve().parents[1]


def _sources(tmp_path: Path) -> tuple[ExperimentArchiveStore, dict]:
    data_root, benchmark_root = tmp_path / 'strategy', tmp_path / 'benchmark'
    backtest, _ = publish_backtest(data_root)
    benchmark, _ = publish_backtest(benchmark_root, weight=.2)
    daily, bindings = paired_backtest_inputs(
        data_root, backtest['generation_id'], benchmark_root, benchmark['generation_id'],
        start=DATES[0], end=DATES[-1],
    )
    evaluation_root = tmp_path / 'evaluation'
    evaluation = EvaluationReportStore(evaluation_root).publish(daily, input_bindings=bindings)
    environment = capture_environment(ROOT)
    request = _request()
    request['environment']['code_fingerprint'] = environment['code_fingerprint']
    request['environment']['environment_lock_digest_sha256'] = environment['environment_lock_digest_sha256']
    config = BacktestConfigStore(data_root).read_manifest(backtest['backtest_config_generation_id'])
    request['backtest_config_binding']['generation_id'] = config['generation_id']
    request['backtest_config_binding']['manifest_digest_sha256'] = config['manifest_digest_sha256']
    request['request_content_generation_id'], request['manifest_digest_sha256'] = research_contract_identities(
        request, schema_name='research_run_request',
    )
    result = _result(request)
    result['runner_identity']['code_fingerprint'] = environment['code_fingerprint']
    result['runner_identity']['lock_digest_sha256'] = environment['environment_lock_digest_sha256']
    backtest_stage = next(stage for stage in result['stage_records'] if stage['stage'] == 'backtest_execution')
    binding = backtest_stage['output_bindings'][0]
    binding['output_family'] = 'backtest_result'
    binding['generation_id'] = backtest['generation_id']
    binding['manifest_digest_sha256'] = backtest['manifest_digest_sha256']
    binding['data_checksum_sha256'] = backtest['equity_curve_artifact']['checksum_sha256']
    binding['quality_decision_checksum_sha256'] = backtest['quality_report_checksum_sha256']
    binding['physical_path'] = f"backtest_results/result={backtest['generation_id']}"
    result['readback_status']['backtest_result'] = 'passed'
    result['result_content_generation_id'] = '0' * 64
    result['manifest_digest_sha256'] = '0' * 64
    generation, digest = research_contract_identities(result, schema_name='research_run_result')
    result['result_content_generation_id'], result['manifest_digest_sha256'] = generation, digest
    final = result['stage_records'][-1]['output_bindings'][0]
    final['generation_id'], final['manifest_digest_sha256'] = generation, digest
    final['physical_path'] = (
        f"research_runs/results/request={request['request_content_generation_id']}"
        f"/run={request['run_id']}/result={generation}/manifest.json"
    )
    run_store = FileResearchRunStore(data_root)
    run_store.publish_request(request, path_policy='strict_v1')
    run_store.publish_result(result, path_policy='strict_v1')
    with RunTelemetry(data_root, request, environment) as attempt:
        with attempt.stage('result_reconciliation'):
            pass
    archive = ExperimentArchiveStore(tmp_path / 'archive')
    inputs = dict(
        components={'universe': 'all-a', 'oos_start': DATES[0], 'oos_end': DATES[-1],
                    'protocol': 'single', 'model': 'qlib_linear', 'variant': 'baseline'},
        data_root=data_root, benchmark_root=benchmark_root,
        evaluation_root=evaluation_root, result_generation=generation,
        result_digest=digest, evaluation_generation=evaluation['generation_id'],
        telemetry_path=attempt.path, environment=environment,
        limitations=['five-session synthetic diagnostic; not annual OOS evidence'],
    )
    return archive, inputs


def test_archive_publishes_immutable_name_and_reverifies_owning_sources(tmp_path):
    archive, inputs = _sources(tmp_path)
    record = archive.publish(**inputs)
    assert record['experiment_name'] == 'all-a-2026-01-05-2026-01-09-single-qlib_linear-baseline'
    assert record['verification_scope']['paired_backtest_sources_reverified']
    evaluation, _ = EvaluationReportStore(inputs['evaluation_root']).read(
        inputs['evaluation_generation'])
    for side, root in (('strategy', inputs['data_root']),
                       ('benchmark', inputs['benchmark_root'])):
        generation = evaluation['input_bindings'][side]['generation_id']
        manifest, config, _ = _verified_result(root, generation)
        assert manifest['quality_report_checksum_sha256'] in record['external_review_checksums']
        assert config['quality_report_checksum_sha256'] in record['external_review_checksums']
    assert not record['verification_scope']['declared_test_split_covered']
    assert not record['verification_scope']['one_year_oos_and_pit_certified']
    assert archive.read(record['experiment_name']) == record
    assert archive.publish(**inputs) == record
    with pytest.raises(ContractError, match='different evidence'):
        archive.publish(**{**inputs, 'limitations': ['another limitation']})


def test_concurrent_same_name_archive_publications_are_idempotent(tmp_path):
    archive, inputs = _sources(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(archive.publish, **inputs) for _ in range(2)]
        records = [future.result() for future in futures]
    assert records[0] == records[1]
    assert archive.read(records[0]['experiment_name']) == records[0]


def test_archive_rejects_tamper_even_when_record_is_rehashed(tmp_path):
    archive, inputs = _sources(tmp_path)
    record = archive.publish(**inputs)
    path = archive.path_for(record['experiment_name'])
    record['evaluation_metrics']['returns']['overall']['annualized_excess_return'] = 123.
    record['archive_checksum_sha256'] = sha256_json({
        key: value for key, value in record.items() if key != 'archive_checksum_sha256'
    })
    path.write_text(json.dumps(record))
    with pytest.raises(ContractError, match='no longer matches'):
        archive.read(record['experiment_name'])


def test_archive_rejects_wrong_telemetry_and_rechanged_source_metrics(tmp_path):
    archive, inputs = _sources(tmp_path)
    with pytest.raises(ContractError, match='telemetry path'):
        archive.publish(**{**inputs, 'telemetry_path': tmp_path / 'unrelated.json'})
    record = archive.publish(**inputs)
    path = (Path(inputs['evaluation_root']) / 'research_evaluations'
            / f"generation={record['evaluation_binding']['generation_id']}" / 'daily.parquet')
    daily = pd.read_parquet(path)
    daily.loc[0, 'strategy_return'] = .9
    daily.to_parquet(path, index=False)
    with pytest.raises(ContractError, match='checksum'):
        archive.read(record['experiment_name'])


def test_forged_zero_environment_receipt_cannot_be_archived(tmp_path):
    archive, inputs = _sources(tmp_path)
    fake = {**inputs, 'environment': {**inputs['environment'], 'code_fingerprint': '0' * 64}}
    with pytest.raises(ContractError, match='fingerprint'):
        archive.publish(**fake)


def test_archive_refuses_a_reissued_result_with_false_backtest_artifact_binding(tmp_path):
    archive, inputs = _sources(tmp_path)
    store = FileResearchRunStore(inputs['data_root'])
    original = store.read_result(inputs['result_generation'], inputs['result_digest'])
    forged = copy.deepcopy(original)
    stage = next(item for item in forged['stage_records'] if item['stage'] == 'backtest_execution')
    stage['output_bindings'][0]['data_checksum_sha256'] = 'f' * 64
    forged['result_content_generation_id'] = '0' * 64
    forged['manifest_digest_sha256'] = '0' * 64
    generation, digest = research_contract_identities(forged, schema_name='research_run_result')
    forged['result_content_generation_id'], forged['manifest_digest_sha256'] = generation, digest
    final = forged['stage_records'][-1]['output_bindings'][0]
    final['generation_id'], final['manifest_digest_sha256'] = generation, digest
    final['physical_path'] = (
        f"research_runs/results/request={forged['request_content_generation_id']}"
        f"/run={forged['run_id']}/result={generation}/manifest.json"
    )
    store.publish_result(forged, path_policy='strict_v1')
    assert store.read_result(generation, digest)['result_content_generation_id'] == generation
    with pytest.raises(ContractError, match='backtest lineage'):
        archive.publish(**{**inputs, 'result_generation': generation, 'result_digest': digest})


def test_archive_name_cannot_claim_a_different_model(tmp_path):
    archive, inputs = _sources(tmp_path)
    components = {**inputs['components'], 'model': 'gbdt'}
    with pytest.raises(ContractError, match='model name'):
        archive.publish(**{**inputs, 'components': components})


def test_archive_cli_publishes_and_reports_its_path(tmp_path, capsys):
    archive, inputs = _sources(tmp_path)
    environment_path = tmp_path / 'environment.json'
    limitations_path = tmp_path / 'limitations.json'
    environment_path.write_text(json.dumps(inputs['environment']))
    limitations_path.write_text(json.dumps(inputs['limitations']))
    spec = importlib.util.spec_from_file_location(
        'archive_trading_experiment', ROOT / 'scripts/archive_trading_experiment.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = [
        '--data-root', str(inputs['data_root']), '--benchmark-root', str(inputs['benchmark_root']),
        '--evaluation-root', str(inputs['evaluation_root']), '--archive-root', str(archive.root),
        '--result-generation', inputs['result_generation'], '--result-digest', inputs['result_digest'],
        '--evaluation-generation', inputs['evaluation_generation'],
        '--telemetry-path', str(inputs['telemetry_path']),
        '--environment-json', str(environment_path), '--limitations-json', str(limitations_path),
    ]
    for key, value in inputs['components'].items():
        args.extend(['--' + key.replace('_', '-'), value])
    assert module.main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'archived'
    assert Path(output['manifest_path']).is_file()
    assert archive.read(output['experiment_name'])['archive_checksum_sha256'] == output['archive_checksum_sha256']
    args[args.index('--result-generation') + 1] = 'bad'
    assert module.main(args) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'archive_rejected'
