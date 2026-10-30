"""Reviewed v3 cost/capacity replays of one immutable target-weight program.

Capital here is a tested grid under assumed daily liquidity and impact, not a
calibrated market-capacity estimate. Every changed config and resulting replay
still goes through its owning publisher and an external quality reviewer.
"""
from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import re
import shutil
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from ..backtest.engine import BacktestEngine, BacktestResultStore
from ..backtest.execution_facts import ExecutionFactsStore, RawExecutionPriceStore, facts_binding
from ..contracts.canonical_v2 import fsync_dir, fsync_tree
from ..contracts.gate_contracts import canonical_json, sha256_json, validate_contract
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError
from ..portfolio.builder import TargetWeightStore
from ..research_chain.owning_contracts import BacktestConfigStore, BacktestMarketDatasetStore
from ..research_chain.provenance import capture_environment
from .report import _verified_result, _verify_review

_IDENTITY_FIELDS = {'generation_id', 'manifest_digest_sha256', 'quality_report_checksum_sha256',
                    'run_id', 'created_at'}
_COST_CASES = ('commission_x2', 'slippage_x2', 'fill_rate_half')


def _binding(manifest: dict) -> dict:
    return {name: manifest[name] for name in ('generation_id', 'manifest_digest_sha256')}


def _content(config: dict) -> dict:
    return {key: value for key, value in config.items() if key not in _IDENTITY_FIELDS}


def _variant(config: dict, name: str, capital: float | None = None) -> dict:
    result = copy.deepcopy(config)
    result['backtest_name'] = 'stress-' + name
    for key in ('generation_id', 'manifest_digest_sha256', 'quality_report_checksum_sha256'):
        result[key] = '0' * 64
    if name == 'commission_x2':
        # Double both the rate and the per-order floor, so small-capital
        # commission stress is not silently identical to the base case.
        result['cost_model']['commission_bps'] *= 2
        result['cost_model']['minimum_commission_cny'] *= 2
    elif name == 'slippage_x2':
        result['cost_model']['slippage_bps'] *= 2
    elif name == 'fill_rate_half':
        result['execution_model']['fill_rate_multiplier'] *= .5
    elif name.startswith('capital_') and capital is not None:
        result['initial_capital'] = capital
    else:
        raise ContractError('unknown backtest stress scenario')
    costs = result['cost_model']
    if costs['slippage_bps'] + costs['impact_model']['maximum_bps'] >= 10000:
        raise ContractError('stress slippage plus maximum impact must remain below 10000 bps')
    return result


def _policy(policy: dict) -> dict:
    if not isinstance(policy, dict) or set(policy) != {
        'minimum_fill_ratio', 'maximum_risk_breach_sessions', 'require_positive_return',
    }:
        raise ContractError('capacity policy must explicitly name all three thresholds')
    minimum = policy['minimum_fill_ratio']
    if (isinstance(minimum, bool) or not isinstance(minimum, (int, float))
            or not math.isfinite(minimum) or not 0 <= minimum <= 1
            or type(policy['maximum_risk_breach_sessions']) is not int
            or policy['maximum_risk_breach_sessions'] < 0
            or type(policy['require_positive_return']) is not bool):
        raise ContractError('invalid capacity policy thresholds')
    return dict(policy)


def _grid(values: list[float]) -> list[float]:
    if (not isinstance(values, list) or not values
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) or value <= 0 for value in values)
            or len(set(values)) != len(values)):
        raise ContractError('capital grid must be nonempty, unique and finite-positive')
    return sorted(float(value) for value in values)


def _inputs(root: Path, base: dict, config: dict) -> dict:
    if config['contract_version'] != 3:
        raise ContractError('cost/capacity stress requires a v3 raw execution baseline')
    _, raw = RawExecutionPriceStore(root).read_window(config['start_date'], config['end_date'],
                                                    config['price_source_binding'])
    raw['date'] = pd.to_datetime(raw['session_date']).dt.strftime('%Y-%m-%d')
    panel = raw.set_index(['date', 'instrument'])[['open', 'close', 'volume']]
    manifest, facts = ExecutionFactsStore(root).read(config['execution_facts_binding']['generation_id'])
    if facts_binding(manifest) != config['execution_facts_binding']:
        raise ContractError('stress execution facts binding mismatch')
    market = BacktestMarketDatasetStore(root)
    calendar = market.read_frame(config['calendar_binding']['generation_id'],
        config['calendar_binding']['checksum_sha256'], dataset_type='calendar', required_columns={'date'})
    if sorted(calendar['date'].astype(str).tolist()) != sorted(set(panel.index.get_level_values('date'))):
        raise ContractError('stress raw price calendar differs from bound calendar')
    suspension = market.read_frame(config['suspension_binding']['dataset_generation_id'],
        config['suspension_binding']['data_checksum_sha256'], dataset_type='suspension',
        required_columns={'date', 'instrument', 'suspended'})
    corporate = market.read_frame(config['corporate_action_binding']['dataset_generation_id'],
        config['corporate_action_binding']['data_checksum_sha256'], dataset_type='corporate_action',
        required_columns={'date', 'instrument'})
    weights = {}
    store = TargetWeightStore(root)
    for binding in base['target_weight_bindings']:
        day, generation = binding['decision_date'], binding['generation_id']
        directory = root / 'target_weights' / f'date={day}' / f'generation={generation}'
        for name in ('manifest.json', 'data.parquet'):
            reject_symlinks_within(root, directory / name, label='stress target weights')
        target, frame = store.read(generation, day)
        _verify_review(root, target, 'target_weights', 'target_weights_v1')
        if (target['manifest_digest_sha256'] != binding['manifest_digest_sha256']
                or target['portfolio_definition_generation_id'] != base['portfolio_definition_generation_id']
                or target['universe_snapshot_generation_id'] != config['universe_binding']['snapshot_generation_id']
                or target['decision_date'] != day):
            raise ContractError('stress target-weight lineage disagrees with baseline')
        weights[day] = frame
    return {
        'portfolio_definition': {'generation_id': base['portfolio_definition_generation_id']},
        'weight_partitions': weights, 'price_panel': panel, 'execution_facts': facts,
        'suspension_dates': {(str(row.date), str(row.instrument)) for row in
                             suspension.loc[suspension['suspended'].astype(bool)].itertuples(index=False)},
        'corporate_action_instruments': set(corporate['instrument'].astype(str)),
    }


def _same_frames(left: dict, right: dict) -> None:
    try:
        for name in ('equity_curve', 'daily_metrics', 'fills'):
            pd.testing.assert_frame_equal(left[name].reset_index(drop=True), right[name].reset_index(drop=True),
                                          check_dtype=False, check_exact=True)
    except AssertionError as exc:
        raise ContractError('baseline replay differs from its published artifacts') from exc


def _metrics(config: dict, frames: dict) -> dict:
    equity, daily, fills = (frames[name] for name in ('equity_curve', 'daily_metrics', 'fills'))
    if (equity['date'].tolist() != daily['date'].tolist() or equity.empty
            or equity['date'].duplicated().any()):
        raise ContractError('stress result calendars are invalid')
    wealth = np.r_[float(config['initial_capital']), equity['portfolio_value'].to_numpy(dtype=float)]
    returns = daily['daily_return'].to_numpy(dtype=float)
    if (not np.isfinite(wealth).all() or (wealth <= 0).any() or not np.isfinite(returns).all()
            or not np.allclose(wealth[1:] / wealth[:-1] - 1, returns, rtol=0, atol=1e-12)):
        raise ContractError('stress returns do not reconcile with capital and equity')
    requested = float((fills['target_shares'] * fills['gross_execution_price']).sum())
    filled = float((fills['filled_shares'] * fills['gross_execution_price']).sum())
    return {
        'cumulative_return': float(wealth[-1] / wealth[0] - 1),
        'max_drawdown': float(np.max(1 - wealth / np.maximum.accumulate(wealth))),
        'fill_ratio': filled / requested if requested else None,
        'unpriced_order_count': int(((fills['target_shares'] > 0) & (fills['gross_execution_price'] <= 0)).sum()),
        'requested_notional_cny': requested, 'filled_notional_cny': filled,
        'total_commission_cny': float(fills['commission_fee'].sum()),
        'total_stamp_duty_cny': float(fills['stamp_duty_fee'].sum()),
        'total_slippage_cny': float(fills['slippage_cost'].sum()),
        'total_impact_cny': float(fills['impact_cost'].sum()),
        'avg_daily_turnover': float(daily['executed_turnover'].mean()),
        'risk_budget_breach_sessions': int(daily['risk_budget_breached'].sum()),
    }


def _reviewed_config(root: Path, payload: dict, decision) -> dict:
    # Recover a partially completed batch through actual reviewed manifests.
    # Comparing content excludes review/run metadata, so an unsigned guess is
    # never a usable match. Multiple independent reviews require an explicit
    # choice rather than arbitrary catalogue ordering.
    matches = []
    for path in sorted((root / 'backtest_configs').glob('generation=*/manifest.json')):
        reject_symlinks_within(root, path, label='stress config')
        try:
            candidate = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ContractError('stress config catalogue contains unreadable input') from exc
        if _content(candidate) == _content(payload):
            candidate = BacktestConfigStore(root).read_manifest(path.parent.name.removeprefix('generation='))
            _verify_review(root, candidate, 'backtest_config_v3', 'backtest_config_v1')
            matches.append(candidate)
    if len(matches) > 1:
        raise ContractError('ambiguous reviewed stress config')
    return matches[0] if matches else BacktestConfigStore(root).publish(payload, quality_decision=decision)


def _publish_result(root: Path, manifest: dict, frames: dict, targets: list, decision) -> dict:
    try:
        BacktestResultStore(root).publish(manifest, frames, quality_decision=decision,
                                          target_weight_generations=targets)
    except ContractError:
        expected = root / 'backtest_results' / f"result={manifest['generation_id']}"
        if not expected.exists():
            raise
    restored, _, restored_frames = _verified_result(root, manifest['generation_id'])
    if restored != manifest:
        raise ContractError('stress result retry manifest mismatch')
    _same_frames(frames, restored_frames)
    return restored


def _derive(root: Path, baseline_binding: dict, grid: list, policy: dict, bindings: list) -> tuple[list, dict]:
    base, config, base_frames = _verified_result(root, baseline_binding['generation_id'])
    if _binding(base) != baseline_binding or config['contract_version'] != 3:
        raise ContractError('stress baseline binding/version mismatch')
    expected_names = list(_COST_CASES) + [f'capital_{index}' for index in range(len(grid))]
    if [binding['name'] for binding in bindings] != expected_names:
        raise ContractError('stress scenarios are missing, reordered or unexpected')
    scenarios = []
    for binding in bindings:
        name = binding['name']
        capital = grid[int(name.removeprefix('capital_'))] if name.startswith('capital_') else None
        expected_config = _variant(config, name, capital)
        result, actual_config, frames = _verified_result(root, binding['result_binding']['generation_id'])
        if (_binding(result) != binding['result_binding'] or _binding(actual_config) != binding['config_binding']
                or _content(actual_config) != _content(expected_config)
                or result['target_weight_bindings'] != base['target_weight_bindings']
                or result['portfolio_definition_generation_id'] != base['portfolio_definition_generation_id']):
            raise ContractError('stress scenario changes inputs outside its declared variant')
        metrics = _metrics(actual_config, frames)
        meets = (metrics['fill_ratio'] is not None and metrics['fill_ratio'] >= policy['minimum_fill_ratio']
                 and metrics['unpriced_order_count'] == 0
                 and metrics['risk_budget_breach_sessions'] <= policy['maximum_risk_breach_sessions']
                 and (not policy['require_positive_return'] or metrics['cumulative_return'] > 0))
        scenarios.append({**binding, 'initial_capital': actual_config['initial_capital'], 'metrics': metrics,
                          'meets_capacity_policy': bool(meets) if capital is not None else None})
    base_metrics = _metrics(config, base_frames)
    positive = base_metrics['cumulative_return'] > 0 and all(
        row['metrics']['cumulative_return'] > 0 for row in scenarios if row['name'] in _COST_CASES)
    qualified = [row['initial_capital'] for row in scenarios if row['meets_capacity_policy']]
    return scenarios, {
        'baseline_metrics': base_metrics, 'positive_base_and_stress_returns': bool(positive),
        'maximum_tested_capital_meeting_policy': max(qualified) if qualified else None,
        'tested_grid_only': True, 'market_capacity_certified': False,
    }


def run_backtest_stress(
    data_root: Path | str, baseline_generation: str, *, capital_grid: list[float], capacity_policy: dict,
    config_quality_decision, result_quality_decision, report_root: Path | str | None = None,
) -> dict:
    root = Path(data_root).resolve()
    policy, grid = _policy(capacity_policy), _grid(capital_grid)
    project = Path(__file__).resolve().parents[3]
    environment = capture_environment(project)
    base, config, frames = _verified_result(root, baseline_generation)
    replay_inputs = _inputs(root, base, config)
    engine = BacktestEngine(root)
    replay, replay_frames = engine.run(config=config, **replay_inputs)
    _same_frames(frames, replay_frames)
    if replay['summary_metrics'] != base['summary_metrics']:
        raise ContractError('baseline replay summary differs from published result')
    # Validate every scenario before publishing the first one.
    variants = [(name, _variant(config, name)) for name in _COST_CASES]
    variants += [(f'capital_{index}', _variant(config, f'capital_{index}', capital))
                 for index, capital in enumerate(grid)]
    from ..contracts.model_layer import validate_model_contract
    for _, variant in variants:
        validate_model_contract('backtest_config_v3', variant)
    bindings = []
    for name, variant in variants:
        approved = _reviewed_config(root, variant, config_quality_decision)
        manifest, artifacts = engine.run(config=approved, **replay_inputs)
        result = _publish_result(root, manifest, artifacts, base['target_weight_bindings'], result_quality_decision)
        bindings.append({'name': name, 'config_binding': _binding(approved), 'result_binding': _binding(result)})
    scenarios, conclusions = _derive(root, _binding(base), grid, policy, bindings)
    observed = capture_environment(project)
    environment_fields = ('code_fingerprint', 'environment_lock_digest_sha256', 'git_head')
    if any(observed[name] != environment[name] for name in environment_fields):
        raise ContractError('backtest stress implementation/environment changed during execution')
    payload = {
        'contract_version': 1, 'diagnostic_only': True, 'calibration_status': 'assumptions_not_market_calibrated',
        'data_root': str(root), 'baseline_binding': _binding(base), 'capital_grid': grid, 'capacity_policy': policy,
        'scenarios': scenarios, 'conclusions': conclusions,
        'execution_environment': {name: environment[name] for name in environment_fields},
    }
    return BacktestStressStore(report_root or root).publish(payload)


class BacktestStressStore:
    """Store immutable diagnostics; reads recompute every scenario and conclusion."""
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, generation: str) -> Path:
        if not isinstance(generation, str) or not re.fullmatch('[0-9a-f]{64}', generation):
            raise ContractError('invalid backtest stress generation')
        path = self.root / 'backtest_stress' / f'generation={generation}' / 'manifest.json'
        reject_symlinks_within(self.root, path, label='backtest stress report')
        return path

    def publish(self, payload: dict) -> dict:
        generation = sha256_json(payload)
        manifest = {**payload, 'generation_id': generation}
        manifest['manifest_digest_sha256'] = sha256_json(manifest)
        self._verify(manifest)
        target = self._path(generation)
        base = target.parent.parent
        base.mkdir(parents=True, exist_ok=True)
        lock_path = base / 'publication.lock'
        reject_symlinks_within(self.root, lock_path, label='backtest stress lock')
        with lock_path.open('a+') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if target.parent.exists():
                return self.read(generation)
            staging = base / ('.staging-' + uuid.uuid4().hex)
            try:
                staging.mkdir()
                (staging / 'manifest.json').write_bytes(canonical_json(manifest) + b'\n')
                fsync_tree(staging)
                os.rename(staging, target.parent)
                fsync_dir(base)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        return manifest

    def read(self, generation: str) -> dict:
        try:
            manifest = json.loads(self._path(generation).read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ContractError('backtest stress report unavailable or malformed') from exc
        if manifest.get('generation_id') != generation:
            raise ContractError('backtest stress physical identity mismatch')
        self._verify(manifest)
        return manifest

    @staticmethod
    def _verify(manifest: dict) -> None:
        validate_contract('backtest_stress.v1.json', manifest)
        payload = {key: value for key, value in manifest.items()
                   if key not in {'generation_id', 'manifest_digest_sha256'}}
        if (sha256_json(payload) != manifest['generation_id']
                or sha256_json({**payload, 'generation_id': manifest['generation_id']}) != manifest['manifest_digest_sha256']):
            raise ContractError('backtest stress manifest identity mismatch')
        policy, grid = _policy(manifest['capacity_policy']), _grid(manifest['capital_grid'])
        if grid != manifest['capital_grid']:
            raise ContractError('backtest stress capital grid is not sorted')
        root = Path(manifest['data_root'])
        base, config, _ = _verified_result(root, manifest['baseline_binding']['generation_id'])
        _inputs(root, base, config)
        bindings = [{key: row[key] for key in ('name', 'config_binding', 'result_binding')}
                    for row in manifest['scenarios']]
        scenarios, conclusions = _derive(root, manifest['baseline_binding'], grid, policy, bindings)
        if scenarios != manifest['scenarios'] or conclusions != manifest['conclusions']:
            raise ContractError('backtest stress diagnostics do not recompute from their results')
