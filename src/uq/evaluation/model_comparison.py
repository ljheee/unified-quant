"""Compare linear and LightGBM research runs on identical governed inputs."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Mapping

import pandas as pd

from ..contracts.model_layer import ModelContractLoader
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError
from ..models.dataset_writer import DatasetWriter
from ..research_chain.resolver import FileResearchRunStore
from .metrics import session_dates
from .report import _verified_result, paired_backtest_inputs

_DATASET_FIELDS = (
    'ordered_features', 'factor_set', 'factor_version', 'factor_generation_ids',
    'label_set_name', 'label_generation_id', 'universe_snapshot_generation_id',
    'split_policy', 'missing_policy', 'row_count', 'data_checksum_sha256',
    'logical_fingerprint', 'feature_preprocessing_generation_id',
    'input_feature_schema_generation_id', 'input_feature_schema_manifest_digest_sha256',
)
_REQUEST_FIELDS = (
    'window_start_date', 'window_end_date', 'decision_timezone',
    'factor_binding', 'universe_snapshot_binding', 'adjusted_price_binding',
    'label_binding', 'feature_preprocessing_binding', 'dataset_policy_template',
    'portfolio_definition_template',
)


def _one_output(result: dict, family: str) -> dict:
    matches = [binding for stage in result['stage_records']
               for binding in stage['output_bindings']
               if binding.get('output_family') == family]
    if len(matches) != 1:
        raise ContractError(f'model comparison requires one {family} output')
    return matches[0]


def _read_run(root: Path, generation: str, digest: str) -> tuple[dict, dict, dict, dict]:
    store = FileResearchRunStore(root)
    result = dict(store.read_result(generation, digest))
    if result.get('final_status') != 'passed':
        raise ContractError('model comparison requires passed research results')
    request = dict(store.read_request(
        result['request_content_generation_id'], result['request_manifest_digest_sha256'],
        result['run_id'],
    ))
    if (request['request_content_generation_id'], request['manifest_digest_sha256'], request['run_id']) != (
        result['request_content_generation_id'], result['request_manifest_digest_sha256'], result['run_id']
    ):
        raise ContractError('model comparison result/request identity mismatch')
    return result, request, _one_output(result, 'model_dataset'), _one_output(result, 'backtest_result')


def _read_dataset(root: Path, binding: dict) -> dict:
    relative = binding.get('physical_path')
    if not isinstance(relative, str) or not relative:
        raise ContractError('model comparison dataset binding has no path')
    path = root / relative
    reject_symlinks_within(root, path, label='model comparison dataset')
    manifest = ModelContractLoader(root).load('model_dataset', path)
    expected_path = (Path('datasets') / f"dataset={manifest['dataset_name']}"
                     / f"version={manifest['semantic_version']}"
                     / f"generation={manifest['generation_id']}" / 'manifest.json')
    if Path(relative) != expected_path:
        raise ContractError('model comparison dataset path disagrees with its owning layout')
    if (manifest.get('generation_id'), manifest.get('manifest_digest_sha256'),
            manifest.get('data_checksum_sha256')) != (
            binding.get('generation_id'), binding.get('manifest_digest_sha256'),
            binding.get('data_checksum_sha256')):
        raise ContractError('model comparison dataset binding disagrees with its manifest')
    # Read through the owning store so the frame bytes and sidecar are verified,
    # then release the potentially large training frame before reading its peer.
    manifest, frame = DatasetWriter(root).read(
        manifest['dataset_name'], manifest['semantic_version'], manifest['generation_id'])
    del frame
    return manifest


def _assert_same_dataset(left: dict, right: dict) -> None:
    for field in _DATASET_FIELDS:
        if left.get(field) != right.get(field):
            raise ContractError(f'model comparison dataset mismatch: {field}')


def _assert_same_protocol(left: dict, right: dict) -> None:
    for field in _REQUEST_FIELDS:
        if left.get(field) != right.get(field):
            raise ContractError(f'model comparison request mismatch: {field}')


def _verify_dataset_lineage(request: dict, dataset: dict) -> None:
    if (request['label_binding']['generation_id'] != dataset['label_generation_id']
            or request['universe_snapshot_binding']['generation_id']
            != dataset['universe_snapshot_generation_id']
            or request['factor_binding']['generation_id'] not in dataset['factor_generation_ids']
            or request['feature_preprocessing_binding']['generation_id']
            != dataset.get('feature_preprocessing_generation_id')):
        raise ContractError('model comparison dataset does not match its research request lineage')


def _verify_backtest(root: Path, request: dict, binding: dict) -> tuple[dict, dict]:
    manifest, config, _ = _verified_result(root, binding['generation_id'])
    if (binding.get('manifest_digest_sha256') != manifest.get('manifest_digest_sha256')
            or binding.get('data_checksum_sha256')
            != manifest['equity_curve_artifact']['checksum_sha256']
            or binding.get('quality_decision_checksum_sha256')
            != manifest['quality_report_checksum_sha256']
            or binding.get('physical_path') != f"backtest_results/result={manifest['generation_id']}"
            or (request['backtest_config_binding']['generation_id'],
                request['backtest_config_binding']['manifest_digest_sha256'])
            != (config['generation_id'], config['manifest_digest_sha256'])
            or config.get('contract_version') not in {3, 4}):
        raise ContractError('model comparison backtest does not match its research request')
    return manifest, config


def paired_model_backtest_inputs(
    data_root: Path | str, *, baseline_result_generation: str,
    baseline_result_digest: str, candidate_result_generation: str,
    candidate_result_digest: str, start: str, end: str,
    regimes: Mapping[str, str] | None = None,
) -> tuple[pd.DataFrame, dict]:
    """Return candidate-minus-linear costed returns after proving protocol parity.

    The same model dataset content must be used by both runs, and the
    portfolio definition, requested window and reviewed backtest economics must
    match. This diagnostic does not qualify a short OOS period or promote a model.
    """
    root = Path(data_root).absolute()
    start, end = session_dates([start, end], label='model comparison window')
    if start > end:
        raise ContractError('model comparison start follows end')
    baseline, baseline_request, baseline_dataset_binding, baseline_backtest_binding = _read_run(
        root, baseline_result_generation, baseline_result_digest)
    candidate, candidate_request, candidate_dataset_binding, candidate_backtest_binding = _read_run(
        root, candidate_result_generation, candidate_result_digest)
    baseline_algorithm = baseline_request['model_definition_template']['algorithm']
    candidate_algorithm = candidate_request['model_definition_template']['algorithm']
    if (baseline_algorithm, candidate_algorithm) != ('qlib_linear', 'qlib_lightgbm'):
        raise ContractError('model comparison requires qlib_linear baseline and qlib_lightgbm candidate')
    _assert_same_protocol(baseline_request, candidate_request)
    baseline_dataset = _read_dataset(root, baseline_dataset_binding)
    candidate_dataset = _read_dataset(root, candidate_dataset_binding)
    _assert_same_dataset(baseline_dataset, candidate_dataset)
    _verify_dataset_lineage(baseline_request, baseline_dataset)
    _verify_dataset_lineage(candidate_request, candidate_dataset)
    test_splits = [split for split in baseline_dataset['split_policy']['splits']
                   if split['name'] == 'test']
    if (len(test_splits) != 1
            or not test_splits[0]['start_date'] <= start <= end <= test_splits[0]['end_date']):
        raise ContractError('model comparison window must be inside the shared test split')
    baseline_backtest, _ = _verify_backtest(root, baseline_request, baseline_backtest_binding)
    candidate_backtest, _ = _verify_backtest(root, candidate_request, candidate_backtest_binding)

    # Candidate occupies the strategy side so the standard report's excess and
    # information-ratio metrics are candidate minus baseline.
    daily, bindings = paired_backtest_inputs(
        root, candidate_backtest['generation_id'], root, baseline_backtest['generation_id'],
        start=start, end=end, regimes=regimes,
    )
    bindings['model_comparison'] = {
        'protocol': 'same_governed_dataset_and_costed_portfolio_v1',
        'baseline': {
            'algorithm': baseline_algorithm,
            'result_generation_id': baseline_result_generation,
            'result_manifest_digest_sha256': baseline_result_digest,
            'dataset_generation_id': baseline_dataset['generation_id'],
            'dataset_data_checksum_sha256': baseline_dataset['data_checksum_sha256'],
            'backtest_generation_id': baseline_backtest['generation_id'],
        },
        'candidate': {
            'algorithm': candidate_algorithm,
            'result_generation_id': candidate_result_generation,
            'result_manifest_digest_sha256': candidate_result_digest,
            'dataset_generation_id': candidate_dataset['generation_id'],
            'dataset_data_checksum_sha256': candidate_dataset['data_checksum_sha256'],
            'backtest_generation_id': candidate_backtest['generation_id'],
        },
        'same_training_data_split_features_and_label': True,
        'same_portfolio_and_requested_window': True,
        'candidate_is_strategy_side': True,
        'oos_sessions': len(daily),
        'oos_calendar_days': (date.fromisoformat(daily['date'].iloc[-1])
                              - date.fromisoformat(daily['date'].iloc[0])).days + 1,
        'one_year_oos_met': (
            len(daily) >= 252
            and (date.fromisoformat(daily['date'].iloc[-1])
                 - date.fromisoformat(daily['date'].iloc[0])).days >= 365
        ),
    }
    return daily, bindings
