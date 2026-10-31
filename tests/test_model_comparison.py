from __future__ import annotations

import copy
from pathlib import Path

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.evaluation import model_comparison


def _fixtures():
    common_request = {
        'window_start_date': '2024-01-01', 'window_end_date': '2025-12-31',
        'decision_timezone': 'Asia/Shanghai',
        'factor_binding': {'generation_id': 'a' * 64},
        'universe_snapshot_binding': {'generation_id': 'b' * 64},
        'adjusted_price_binding': {'generation_id': 'c' * 64},
        'label_binding': {'generation_id': 'd' * 64},
        'feature_preprocessing_binding': {'generation_id': 'e' * 64},
        'dataset_policy_template': {'split_policy': {'purge_trading_days': 5}},
        'portfolio_definition_template': {'weight_scheme': 'top_n_equal_weight'},
        'backtest_config_binding': {
            'generation_id': '9' * 64, 'manifest_digest_sha256': '8' * 64,
        },
    }
    dataset = {
        'generation_id': '1' * 64, 'manifest_digest_sha256': '2' * 64,
        'dataset_name': 'shared', 'semantic_version': '1.0.0',
        'ordered_features': ['factor_a', 'factor_b'], 'factor_set': 'alpha',
        'factor_version': '1.0.0', 'factor_generation_ids': ['a' * 64],
        'label_set_name': 'return_5d', 'label_generation_id': 'd' * 64,
        'universe_snapshot_generation_id': 'b' * 64,
        'split_policy': {'purge_trading_days': 5, 'embargo_trading_days': 2, 'splits': [
            {'name': 'train', 'start_date': '2024-01-01', 'end_date': '2024-12-31'},
            {'name': 'validation', 'start_date': '2025-01-01', 'end_date': '2025-03-31'},
            {'name': 'test', 'start_date': '2025-04-01', 'end_date': '2025-12-31'},
        ]},
        'missing_policy': 'fail_closed', 'row_count': 1000,
        'data_checksum_sha256': '7' * 64, 'logical_fingerprint': '6' * 64,
        'feature_preprocessing_generation_id': 'e' * 64,
        'input_feature_schema_generation_id': '5' * 64,
        'input_feature_schema_manifest_digest_sha256': '4' * 64,
    }
    results, requests, datasets, backtests = {}, {}, {}, {}
    for kind, result_id, request_id, dataset_id, backtest_id in (
        ('baseline', '3' * 64, '4' * 64, '1' * 64, 'a' * 64),
        ('candidate', '5' * 64, '6' * 64, '2' * 64, 'b' * 64),
    ):
        request = {
            **copy.deepcopy(common_request),
            'request_content_generation_id': request_id,
            'manifest_digest_sha256': 'f' * 64,
            'run_id': f'{kind}-run',
            'model_definition_template': {
                'algorithm': 'qlib_linear' if kind == 'baseline' else 'qlib_lightgbm',
            },
        }
        dataset_manifest = copy.deepcopy(dataset)
        dataset_manifest['generation_id'] = dataset_id
        dataset_manifest['manifest_digest_sha256'] = '0' * 64 if kind == 'baseline' else '1' * 64
        dataset_binding = {
            'output_family': 'model_dataset', 'generation_id': dataset_id,
            'manifest_digest_sha256': dataset_manifest['manifest_digest_sha256'],
            'data_checksum_sha256': dataset_manifest['data_checksum_sha256'],
            'physical_path': (
                f"datasets/dataset=shared/version=1.0.0/generation={dataset_id}/manifest.json"
            ),
        }
        backtest_binding = {
            'output_family': 'backtest_result', 'generation_id': backtest_id,
            'manifest_digest_sha256': 'c' * 64,
            'data_checksum_sha256': 'd' * 64,
            'quality_decision_checksum_sha256': 'e' * 64,
            'physical_path': f'backtest_results/result={backtest_id}',
        }
        result = {
            'final_status': 'passed', 'request_content_generation_id': request_id,
            'request_manifest_digest_sha256': request['manifest_digest_sha256'],
            'run_id': request['run_id'],
            'stage_records': [{'output_bindings': [dataset_binding, backtest_binding]}],
        }
        results[result_id] = result
        requests[request_id] = request
        datasets[dataset_id] = dataset_manifest
        backtests[backtest_id] = (
            {
                'generation_id': backtest_id, 'manifest_digest_sha256': 'c' * 64,
                'equity_curve_artifact': {'checksum_sha256': 'd' * 64},
                'quality_report_checksum_sha256': 'e' * 64,
            },
            {'contract_version': 3, 'generation_id': '9' * 64, 'manifest_digest_sha256': '8' * 64},
            {},
        )
    return results, requests, datasets, backtests


def _install_fakes(monkeypatch, fixtures):
    results, requests, datasets, backtests = fixtures

    class ResearchStore:
        def __init__(self, _root):
            pass

        def read_result(self, generation, _digest):
            return results[generation]

        def read_request(self, generation, _digest, _run_id):
            return requests[generation]

    class ManifestLoader:
        def __init__(self, _root):
            pass

        def load(self, _family, path):
            generation = Path(path).parent.name.removeprefix('generation=')
            return datasets[generation]

    class OwningDatasetReader:
        def __init__(self, _root):
            pass

        def read(self, _name, _version, generation):
            return datasets[generation], pd.DataFrame()

    monkeypatch.setattr(model_comparison, 'FileResearchRunStore', ResearchStore)
    monkeypatch.setattr(model_comparison, 'ModelContractLoader', ManifestLoader)
    monkeypatch.setattr(model_comparison, 'DatasetWriter', OwningDatasetReader)
    monkeypatch.setattr(model_comparison, 'reject_symlinks_within', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(model_comparison, '_verified_result', lambda _root, generation: backtests[generation])
    monkeypatch.setattr(model_comparison, 'paired_backtest_inputs', lambda *_args, **_kwargs: (
        pd.DataFrame({
            'date': ['2025-04-01', '2025-04-02'], 'strategy_return': [.01, -.01],
            'benchmark_return': [0., 0.], 'invested': [True, True], 'turnover': [.1, .1],
        }), {'strategy': {'generation_id': 'b' * 64}, 'benchmark': {'generation_id': 'a' * 64}},
    ))


def test_model_comparison_requires_same_dataset_and_costed_portfolio(monkeypatch, tmp_path):
    fixtures = _fixtures()
    _install_fakes(monkeypatch, fixtures)
    daily, bindings = model_comparison.paired_model_backtest_inputs(
        tmp_path, baseline_result_generation='3' * 64, baseline_result_digest='0' * 64,
        candidate_result_generation='5' * 64, candidate_result_digest='0' * 64,
        start='2025-04-01', end='2025-04-02',
    )
    assert daily['strategy_return'].tolist() == [.01, -.01]
    assert bindings['model_comparison']['candidate_is_strategy_side']
    assert bindings['model_comparison']['same_training_data_split_features_and_label']
    assert not bindings['model_comparison']['one_year_oos_met']


@pytest.mark.parametrize('field', ['label_generation_id', 'ordered_features', 'data_checksum_sha256'])
def test_model_comparison_refuses_dataset_drift(monkeypatch, tmp_path, field):
    fixtures = _fixtures()
    results, _requests, datasets, _backtests = fixtures
    candidate_binding = results['5' * 64]['stage_records'][0]['output_bindings'][0]
    altered = copy.deepcopy(datasets['1' * 64])
    altered['generation_id'] = '3' * 64
    altered['manifest_digest_sha256'] = '4' * 64
    altered[field] = ['different'] if field == 'ordered_features' else 'f' * 64
    candidate_binding['generation_id'] = '3' * 64
    candidate_binding['manifest_digest_sha256'] = '4' * 64
    candidate_binding['data_checksum_sha256'] = altered['data_checksum_sha256']
    candidate_binding['physical_path'] = (
        'datasets/dataset=shared/version=1.0.0/generation=3' + '3' * 63 + '/manifest.json'
    )
    datasets['3' * 64] = altered
    _install_fakes(monkeypatch, fixtures)
    with pytest.raises(ContractError, match='dataset mismatch'):
        model_comparison.paired_model_backtest_inputs(
            tmp_path, baseline_result_generation='3' * 64, baseline_result_digest='0' * 64,
            candidate_result_generation='5' * 64, candidate_result_digest='0' * 64,
            start='2025-04-01', end='2025-04-02',
        )
