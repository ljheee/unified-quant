"""The historical baseline is metadata, not a claim of a new experiment."""
from __future__ import annotations

import json
from pathlib import Path

from uq.contracts.model_layer import ModelContractLoader, research_contract_identities, sha256_json

ROOT = Path(__file__).resolve().parents[1]


def test_frozen_baseline_binds_the_original_attempt_and_keeps_gaps_explicit():
    record = json.loads((ROOT / 'evidence/trading-grade/baseline-v1.json').read_text())
    checksum = record.pop('archive_checksum_sha256')
    assert sha256_json(record) == checksum
    request = record['request']
    ModelContractLoader.validate('research_run_request_v6', request)
    assert research_contract_identities(request, schema_name='research_run_request_v6') == (
        request['request_content_generation_id'], request['manifest_digest_sha256'],
    )
    result = record['result']
    assert result['request_content_generation_id'] == request['request_content_generation_id']
    assert result['run_id'] == request['run_id'] == '00000000-0000-4000-8000-000000000011'
    assert result['result_content_generation_id'] == 'e1267f29287fbcdfe62efe14edf0940fc9b9b1ecb1934e9227dc3555c8e4a749'
    assert request['environment']['code_fingerprint'] == '0' * 64
    assert result['runner_identity']['lock_digest_sha256'] == '0' * 64
    assert record['status'] == 'frozen_with_reproducibility_gaps'
    assert len(record['missing_evidence']) >= 4
    assert not record['verified_now']['retrained']
    assert not record['verified_now']['all_upstream_payloads_reverified']
    dataset = record['dataset']
    ModelContractLoader.validate('model_dataset', dataset)
    assert dataset['split_policy'] == request['dataset_policy_template']['split_policy']
    assert dataset['row_count'] == 728880
    assert len(dataset['ordered_features']) == 157
    assert len(record['backtest_result']['decision_dates']) == 11
    assert record['backtest_result']['artifact_descriptors']['daily_metrics_artifact']['row_count'] == 160
    assert record['backtest_result']['summary_metrics']['cumulative_return'] == -0.0285056192
    config = record['backtest_config']
    ModelContractLoader.validate('backtest_config_v2', config)
    assert config['generation_id'] == request['backtest_config_binding']['generation_id']
    assert config['cost_model'] == {'commission_bps': 2.5, 'slippage_bps': 1.0, 'stamp_duty_bps': 5.0}
    assert 'volume_participation_cap' not in config['execution_model']
    assert record['effective_legacy_defaults']['volume_participation_cap'] == 0.1
