"""Replay verified walk-forward targets through one continuous v3 account."""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from ..backtest.engine import BacktestEngine, BacktestResultStore
from ..backtest.execution_facts import ExecutionFactsStore, RawExecutionPriceStore, facts_binding
from ..contracts.canonical_v2 import fsync_dir
from ..contracts.gate_contracts import canonical_json, sha256_json
from ..contracts.model_layer import model_manifest_identities
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError
from ..portfolio.builder import TargetWeightStore
from ..research_chain.owning_contracts import BacktestConfigStore, BacktestMarketDatasetStore
from ..research_chain.resolver import FileResearchRunStore
from .report import _verify_review, _verified_result
from .walk_forward import WalkForwardStore, verify_walk_forward_plan


_ECONOMICS = (
    'contract_version', 'schema_version', 'cost_model', 'execution_model',
    'limit_rules', 'initial_capital', 'risk_constraints',
)


def _verified_target_weights(root: Path, generation: str, decision_date: str):
    directory = root / 'target_weights' / f'date={decision_date}' / f'generation={generation}'
    for name in ('manifest.json', 'data.parquet'):
        reject_symlinks_within(root, directory / name, label='continuous replay target weights')
    manifest, frame = TargetWeightStore(root).read(generation, decision_date)
    expected = model_manifest_identities(manifest, schema_name='target_weights')
    if (expected != (generation, manifest['manifest_digest_sha256'])
            or manifest['decision_date'] != decision_date):
        raise ContractError('continuous replay target-weight identity/date mismatch')
    _verify_review(root, manifest, 'target_weights', 'target_weights_v1')
    return manifest, frame


def _read_continuous_inputs(root: Path, config: dict, expected_dates: list[str]):
    """Read and bind every v3 market input through its owning store."""
    calendar = BacktestMarketDatasetStore(root).read_frame(
        config['calendar_binding']['generation_id'], config['calendar_binding']['checksum_sha256'],
        dataset_type='calendar', required_columns={'date'},
    )
    calendar_dates = sorted(pd.to_datetime(calendar['date']).dt.strftime('%Y-%m-%d').unique())
    if calendar_dates != expected_dates:
        raise ContractError('continuous backtest calendar differs from OOS plus next execution session')

    price_binding, raw_prices = RawExecutionPriceStore(root).read_window(
        config['start_date'], config['end_date'], config['price_source_binding'],
    )
    del price_binding
    raw_prices['date'] = pd.to_datetime(raw_prices['session_date']).dt.strftime('%Y-%m-%d')
    price_dates = sorted(raw_prices['date'].unique())
    if price_dates != expected_dates:
        raise ContractError('continuous raw prices do not cover the complete replay window')
    price_panel = raw_prices.set_index(['date', 'instrument'])[
        ['open', 'high', 'low', 'close', 'volume', 'amount']
    ]

    facts_manifest, execution_facts = ExecutionFactsStore(root).read(
        config['execution_facts_binding']['generation_id'],
    )
    if facts_binding(facts_manifest) != config['execution_facts_binding']:
        raise ContractError('continuous execution facts binding mismatch')
    if sorted(execution_facts['date'].unique()) != expected_dates:
        raise ContractError('continuous execution facts do not cover the complete replay window')

    market = BacktestMarketDatasetStore(root)
    suspension_frame = market.read_frame(
        config['suspension_binding']['dataset_generation_id'],
        config['suspension_binding']['data_checksum_sha256'], dataset_type='suspension',
        required_columns={'date', 'instrument', 'suspended'},
    )
    suspension_dates = {
        (str(row.date), str(row.instrument))
        for row in suspension_frame.loc[suspension_frame['suspended'].astype(bool)].itertuples(index=False)
    }
    corporate_frame = market.read_frame(
        config['corporate_action_binding']['dataset_generation_id'],
        config['corporate_action_binding']['data_checksum_sha256'], dataset_type='corporate_action',
        required_columns={'date', 'instrument'},
    )
    corporate_action_instruments = set(
        corporate_frame.loc[corporate_frame['date'].astype(str).isin(expected_dates), 'instrument']
        .astype(str).unique().tolist()
    )
    return price_panel, execution_facts, suspension_dates, corporate_action_instruments


def _fold_targets(plan: dict, attempt: dict) -> tuple[dict[str, pd.DataFrame], list[dict], list[dict]]:
    """Re-read passed fold results and their reviewed target-weight partitions."""
    weight_partitions: dict[str, pd.DataFrame] = {}
    target_sources: list[dict] = []
    fold_configs: list[dict] = []
    for fold, record in zip(plan['folds'], attempt['fold_records'], strict=True):
        root = Path(record['data_root'])
        result_store = FileResearchRunStore(root)
        research_result = result_store.read_result(
            record['result_content_generation_id'], record['result_manifest_digest_sha256'],
        )
        if (research_result['final_status'] != 'passed'
                or research_result['request_content_generation_id'] != record['request_content_generation_id']
                or research_result['run_id'] != record['run_id']):
            raise ContractError('continuous replay source is not the passed fold result')
        result_bindings = [binding for stage in research_result['stage_records']
                           for binding in stage['output_bindings']
                           if binding['output_family'] == 'backtest_result']
        if len(result_bindings) != 1 or result_bindings[0]['generation_id'] != record['backtest_generation_id']:
            raise ContractError('continuous replay attempt/backtest result lineage mismatch')
        bound_digest = result_bindings[0]['manifest_digest_sha256']
        if (record.get('backtest_manifest_digest_sha256') is not None
                and record['backtest_manifest_digest_sha256'] != bound_digest):
            raise ContractError('continuous replay attempt/backtest digest mismatch')

        backtest, config, _artifacts = _verified_result(root, record['backtest_generation_id'])
        if backtest['manifest_digest_sha256'] != bound_digest or config['contract_version'] != 3:
            raise ContractError('continuous replay requires the verified v3 fold backtest')
        fold_configs.append(config)
        bindings = backtest['target_weight_bindings']
        dates = [binding['decision_date'] for binding in bindings]
        if dates != fold['test_dates']:
            raise ContractError('continuous replay fold weights do not cover its OOS dates exactly')
        for binding in bindings:
            day = binding['decision_date']
            manifest, frame = _verified_target_weights(root, binding['generation_id'], day)
            if manifest['manifest_digest_sha256'] != binding['manifest_digest_sha256']:
                raise ContractError('continuous replay target-weight manifest binding mismatch')
            if day in weight_partitions:
                raise ContractError('continuous replay contains duplicate OOS target dates')
            weight_partitions[day] = frame
            target_sources.append({
                'decision_date': day, 'store_root': str(root.absolute()),
                'generation_id': manifest['generation_id'],
                'manifest_digest_sha256': manifest['manifest_digest_sha256'],
                'weights_checksum_sha256': manifest['weights_checksum_sha256'],
            })

    expected_oos = [day for fold in plan['folds'] for day in fold['test_dates']]
    if sorted(weight_partitions) != expected_oos or len(expected_oos) != len(set(expected_oos)):
        raise ContractError('continuous replay target weights do not cover all planned OOS dates')
    return weight_partitions, target_sources, fold_configs


class WalkForwardContinuousStore:
    """Immutable receipt for one shared-capital walk-forward replay."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def publish(self, receipt: dict) -> Path:
        payload = {key: value for key, value in receipt.items() if key != 'checksum_sha256'}
        if receipt.get('checksum_sha256') != sha256_json(payload):
            raise ContractError('continuous replay receipt checksum mismatch')
        directory = self.root / 'walk_forward' / 'continuous_backtests'
        reject_symlinks_within(self.root, directory, label='continuous walk-forward receipt')
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"result={receipt['backtest_result_generation_id']}.json"
        reject_symlinks_within(self.root, path, label='continuous walk-forward receipt')
        encoded = canonical_json(receipt) + b'\n'
        descriptor, temporary = tempfile.mkstemp(prefix='.continuous-backtest-', dir=directory)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
                fsync_dir(directory)
            except FileExistsError:
                if path.read_bytes() != encoded:
                    raise ContractError('continuous walk-forward receipt identity conflict')
            return path
        finally:
            Path(temporary).unlink(missing_ok=True)

    def read(self, result_generation: str) -> dict:
        path = self.root / 'walk_forward' / 'continuous_backtests' / f'result={result_generation}.json'
        reject_symlinks_within(self.root, path, label='continuous walk-forward receipt')
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ContractError('continuous walk-forward receipt unavailable or malformed') from exc
        required = {
            'record_version', 'kind', 'publication_scope', 'plan_generation_id', 'walk_forward_root',
            'attempt_path', 'attempt_checksum_sha256', 'backtest_root', 'backtest_config_generation_id',
            'backtest_result_generation_id', 'backtest_result_manifest_digest_sha256',
            'portfolio_sequence_generation_id', 'window', 'target_weight_sources', 'capital_semantics',
            'terminal_liquidation', 'trading_acceptance', 'checksum_sha256',
        }
        version = record.get('record_version')
        if version == 2:
            required |= {
                'terminal_liquidation_attempted', 'terminal_liquidation_decision_date',
                'terminal_liquidation_execution_date', 'terminal_remaining_holdings_value',
                'terminal_liquidation_unfilled_statuses',
            }
        if (set(record) != required or type(version) is not int or version not in {1, 2}
                or record.get('kind') != 'walk_forward_continuous_backtest'
                or record.get('publication_scope') != 'evidence_only_no_quality_authorization'
                or record.get('backtest_result_generation_id') != result_generation
                or record.get('checksum_sha256') != sha256_json({
                    key: value for key, value in record.items() if key != 'checksum_sha256'
                })
                or record.get('capital_semantics') != 'single_backtest_engine_run_continuous_cash_and_holdings'
                or (version == 1 and record.get('terminal_liquidation') is not False)
                or (version == 2 and record.get('terminal_liquidation_attempted') is not True)
                or record.get('trading_acceptance') != 'not_certified_by_continuous_replay'):
            raise ContractError('continuous walk-forward receipt identity or semantics mismatch')
        if any(not isinstance(record[key], str) or not Path(record[key]).is_absolute()
               for key in ('walk_forward_root', 'attempt_path', 'backtest_root')):
            raise ContractError('continuous walk-forward receipt paths must be absolute')
        attempt = WalkForwardStore(record['walk_forward_root']).read_attempt(record['attempt_path'])
        if (attempt['checksum_sha256'] != record['attempt_checksum_sha256']
                or attempt['plan_generation_id'] != record['plan_generation_id']):
            raise ContractError('continuous walk-forward receipt attempt binding mismatch')
        plan = WalkForwardStore(record['walk_forward_root']).read_plan(record['plan_generation_id'])
        expected_dates = [day for fold in plan['folds'] for day in fold['test_dates']]
        calendar_dates = plan['calendar_dates']
        first_index = calendar_dates.index(expected_dates[0])
        last_index = calendar_dates.index(expected_dates[-1])
        trailing_sessions = 1 if version == 1 else 2
        expected_replay_dates = calendar_dates[first_index:last_index + 1 + trailing_sessions]
        if len(expected_replay_dates) != len(expected_dates) + trailing_sessions:
            raise ContractError('continuous walk-forward receipt lacks required post-OOS sessions')
        expected_window = {
            'start_date': expected_replay_dates[0], 'end_date': expected_replay_dates[-1],
            'oos_decision_sessions': len(expected_dates), 'replay_sessions': len(expected_replay_dates),
        }
        if version == 2:
            expected_window['terminal_liquidation_decision_date'] = expected_replay_dates[-2]
            expected_window['terminal_liquidation_execution_date'] = expected_replay_dates[-1]
        if record['window'] != expected_window:
            raise ContractError('continuous walk-forward receipt window mismatch')
        sources = record['target_weight_sources']
        if (not isinstance(sources, list)
                or [source.get('decision_date') for source in sources] != expected_dates):
            raise ContractError('continuous walk-forward receipt target dates mismatch')
        _weights, expected_sources, _fold_configs = _fold_targets(plan, attempt)
        if sources != expected_sources:
            raise ContractError('continuous walk-forward receipt sources differ from fold results')
        target_bindings = []
        for source in sources:
            if not isinstance(source.get('store_root'), str) or not Path(source['store_root']).is_absolute():
                raise ContractError('continuous replay target-weight store paths must be absolute')
            source_root = Path(source['store_root'])
            manifest, _frame = _verified_target_weights(
                source_root,
                source['generation_id'], source['decision_date'],
            )
            expected_source = {
                'generation_id': manifest['generation_id'],
                'manifest_digest_sha256': manifest['manifest_digest_sha256'],
                'weights_checksum_sha256': manifest['weights_checksum_sha256'],
            }
            if any(source.get(key) != value for key, value in expected_source.items()):
                raise ContractError('continuous walk-forward receipt source-weight binding mismatch')
            target_bindings.append({
                'decision_date': source['decision_date'], 'generation_id': source['generation_id'],
                'manifest_digest_sha256': source['manifest_digest_sha256'],
            })
        sequence_payload = {
            'kind': 'walk_forward_continuous_target_sequence',
            'plan_generation_id': plan['generation_id'], 'attempt_checksum_sha256': attempt['checksum_sha256'],
            'target_weight_sources': sources,
        }
        if version == 2:
            sequence_payload['terminal_liquidation'] = {
                'decision_date': expected_replay_dates[-2],
                'execution_date': expected_replay_dates[-1],
                'policy': 'force_flat_at_final_session_open_under_v3_execution_guards',
            }
        expected_sequence = sha256_json(sequence_payload)
        if expected_sequence != record['portfolio_sequence_generation_id']:
            raise ContractError('continuous walk-forward receipt sequence identity mismatch')
        result, config, artifacts = _verified_result(Path(record['backtest_root']), result_generation)
        if (result['manifest_digest_sha256'] != record['backtest_result_manifest_digest_sha256']
                or config['generation_id'] != record['backtest_config_generation_id']
                or (config['start_date'], config['end_date'])
                != (expected_replay_dates[0], expected_replay_dates[-1])
                or result['backtest_config_generation_id'] != record['backtest_config_generation_id']
                or result['portfolio_definition_generation_id'] != record['portfolio_sequence_generation_id']
                or result['target_weight_bindings'] != target_bindings):
            raise ContractError('continuous walk-forward receipt/result binding mismatch')
        if version == 2:
            remaining = float(artifacts['equity_curve']['holdings_value'].iloc[-1])
            unfilled = sorted(set(
                artifacts['fills'].loc[
                    (artifacts['fills']['date'] == expected_replay_dates[-1])
                    & (artifacts['fills']['status'] != 'filled'), 'status'
                ].astype(str).tolist()
            ))
            completed = remaining <= 1e-8
            if (type(record['terminal_liquidation']) is not bool
                    or record['terminal_liquidation'] != completed
                    or record['terminal_liquidation_decision_date'] != expected_replay_dates[-2]
                    or record['terminal_liquidation_execution_date'] != expected_replay_dates[-1]
                    or type(record['terminal_remaining_holdings_value']) not in {int, float}
                    or not math.isfinite(float(record['terminal_remaining_holdings_value']))
                    or float(record['terminal_remaining_holdings_value']) < 0
                    or abs(float(record['terminal_remaining_holdings_value']) - remaining) > 1e-8
                    or record['terminal_liquidation_unfilled_statuses'] != unfilled):
                raise ContractError('continuous walk-forward terminal liquidation does not recompute')
        baseline = {key: config.get(key) for key in _ECONOMICS}
        if any(canonical_json({key: fold_config.get(key) for key in _ECONOMICS}) != canonical_json(baseline)
               for fold_config in _fold_configs):
            raise ContractError('continuous walk-forward receipt economics differ from fold configs')
        return record


def run_continuous_walk_forward_backtest(
    *, walk_forward_root: Path | str, attempt_path: Path | str,
    backtest_root: Path | str, backtest_config_generation_id: str,
    quality_decision: dict[str, Any],
) -> dict:
    """Replay every verified fold's OOS targets in one reviewed v3 backtest.

    The supplied config is a separately reviewed v3 config covering the OOS
    decision sessions, the final OOS target's execution session, and one more
    session for terminal liquidation under T+1. It must use the same cost,
    execution, capital and risk settings as every fold. Liquidation is attempted
    at the last session's open and remains subject to all v3 execution guards;
    the receipt reports whether holdings actually reached zero.
    """
    walk_root, replay_root = Path(walk_forward_root), Path(backtest_root)
    walk_store = WalkForwardStore(walk_root)
    attempt = walk_store.read_attempt(attempt_path)
    if attempt['status'] != 'passed':
        raise ContractError('continuous replay requires a fully passed walk-forward attempt')
    plan = walk_store.read_plan(attempt['plan_generation_id'])
    verify_walk_forward_plan(plan)

    weight_partitions, target_sources, fold_configs = _fold_targets(plan, attempt)
    if not fold_configs:
        raise ContractError('continuous replay requires at least one verified fold')

    config = BacktestConfigStore(replay_root).read_manifest(backtest_config_generation_id)
    if config['contract_version'] != 3:
        raise ContractError('continuous walk-forward replay requires backtest_config_v3')
    _verify_review(replay_root, config, 'backtest_config_v3', 'backtest_config_v1')
    baseline = {key: config.get(key) for key in _ECONOMICS}
    if any(canonical_json({key: fold_config.get(key) for key in _ECONOMICS}) != canonical_json(baseline)
           for fold_config in fold_configs):
        raise ContractError('continuous replay economics differ from a walk-forward fold')

    oos_dates = [day for fold in plan['folds'] for day in fold['test_dates']]
    calendar_dates = plan['calendar_dates']
    first_index, last_index = calendar_dates.index(oos_dates[0]), calendar_dates.index(oos_dates[-1])
    if oos_dates != calendar_dates[first_index:last_index + 1]:
        raise ContractError('continuous replay OOS dates must be a contiguous calendar sequence')
    if last_index + 2 >= len(calendar_dates):
        raise ContractError(
            'walk-forward calendar requires one target execution session and one terminal liquidation session'
        )
    replay_dates = calendar_dates[first_index:last_index + 3]
    if (config['start_date'], config['end_date']) != (replay_dates[0], replay_dates[-1]):
        raise ContractError('continuous v3 config must cover OOS sessions plus two post-OOS sessions')
    price_panel, facts, suspension_dates, corporate_actions = _read_continuous_inputs(
        replay_root, config, replay_dates,
    )

    sequence_generation = sha256_json({
        'kind': 'walk_forward_continuous_target_sequence',
        'plan_generation_id': plan['generation_id'], 'attempt_checksum_sha256': attempt['checksum_sha256'],
        'target_weight_sources': target_sources,
        'terminal_liquidation': {
            'decision_date': replay_dates[-2], 'execution_date': replay_dates[-1],
            'policy': 'force_flat_at_final_session_open_under_v3_execution_guards',
        },
    })
    receipt_store = WalkForwardContinuousStore(walk_root)
    receipt_dir = walk_root / 'walk_forward' / 'continuous_backtests'
    if receipt_dir.is_dir():
        for candidate in receipt_dir.glob('result=*.json'):
            reject_symlinks_within(walk_root, candidate, label='continuous walk-forward receipt')
            try:
                previous = json.loads(candidate.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if (previous.get('plan_generation_id') == plan['generation_id']
                    and previous.get('attempt_checksum_sha256') == attempt['checksum_sha256']
                    and previous.get('portfolio_sequence_generation_id') == sequence_generation
                    and previous.get('backtest_config_generation_id') == config['generation_id']):
                verified = receipt_store.read(candidate.stem.removeprefix('result='))
                previous_result, _previous_config, _previous_artifacts = _verified_result(
                    replay_root, verified['backtest_result_generation_id'],
                )
                return {
                    'status': 'continuous_backtest_reused',
                    'backtest_result_generation_id': previous_result['generation_id'],
                    'backtest_result_manifest_digest_sha256': previous_result['manifest_digest_sha256'],
                    'portfolio_sequence_generation_id': sequence_generation,
                    'receipt_path': str(candidate), 'receipt_checksum_sha256': verified['checksum_sha256'],
                    'capital_semantics': verified['capital_semantics'],
                    'terminal_liquidation': verified['terminal_liquidation'],
                    'trading_acceptance': verified['trading_acceptance'],
                    'summary_metrics': previous_result['summary_metrics'],
                }
    manifest, artifacts = BacktestEngine(replay_root).run(
        config=config, portfolio_definition={'generation_id': sequence_generation},
        weight_partitions=weight_partitions, price_panel=price_panel, execution_facts=facts,
        suspension_dates=suspension_dates, corporate_action_instruments=corporate_actions,
        terminal_liquidation=True,
    )
    target_bindings = [{
        'decision_date': source['decision_date'], 'generation_id': source['generation_id'],
        'manifest_digest_sha256': source['manifest_digest_sha256'],
    } for source in target_sources]
    path = BacktestResultStore(replay_root).publish(
        manifest, artifacts, quality_decision=quality_decision,
        target_weight_generations=target_bindings,
    )
    result_generation = path.name.removeprefix('result=')
    result, _result_config, restored = _verified_result(replay_root, result_generation)
    if result['target_weight_bindings'] != target_bindings:
        raise ContractError('continuous backtest readback lost its target-weight lineage')
    final_holdings_value = float(restored['equity_curve']['holdings_value'].iloc[-1])
    terminal_unfilled_statuses = sorted(set(
        restored['fills'].loc[
            (restored['fills']['date'] == replay_dates[-1])
            & (restored['fills']['status'] != 'filled'), 'status'
        ].astype(str).tolist()
    ))
    liquidation_completed = final_holdings_value <= 1e-8
    receipt = {
        'record_version': 2, 'kind': 'walk_forward_continuous_backtest',
        'publication_scope': 'evidence_only_no_quality_authorization',
        'plan_generation_id': plan['generation_id'], 'walk_forward_root': str(walk_root.absolute()),
        'attempt_path': str(Path(attempt_path).absolute()), 'attempt_checksum_sha256': attempt['checksum_sha256'],
        'backtest_root': str(replay_root.absolute()),
        'backtest_config_generation_id': config['generation_id'],
        'backtest_result_generation_id': result_generation,
        'backtest_result_manifest_digest_sha256': result['manifest_digest_sha256'],
        'portfolio_sequence_generation_id': sequence_generation,
        'window': {'start_date': replay_dates[0], 'end_date': replay_dates[-1],
                   'oos_decision_sessions': len(oos_dates), 'replay_sessions': len(replay_dates),
                   'terminal_liquidation_decision_date': replay_dates[-2],
                   'terminal_liquidation_execution_date': replay_dates[-1]},
        'target_weight_sources': target_sources,
        'capital_semantics': 'single_backtest_engine_run_continuous_cash_and_holdings',
        'terminal_liquidation_attempted': True,
        'terminal_liquidation': liquidation_completed,
        'terminal_liquidation_decision_date': replay_dates[-2],
        'terminal_liquidation_execution_date': replay_dates[-1],
        'terminal_remaining_holdings_value': final_holdings_value,
        'terminal_liquidation_unfilled_statuses': terminal_unfilled_statuses,
        'trading_acceptance': 'not_certified_by_continuous_replay',
    }
    receipt['checksum_sha256'] = sha256_json(receipt)
    receipt_path = receipt_store.publish(receipt)
    receipt_store.read(result_generation)
    return {
        'status': 'continuous_backtest_published', 'backtest_result_generation_id': result_generation,
        'backtest_result_manifest_digest_sha256': result['manifest_digest_sha256'],
        'portfolio_sequence_generation_id': sequence_generation,
        'receipt_path': str(receipt_path), 'receipt_checksum_sha256': receipt['checksum_sha256'],
        'capital_semantics': receipt['capital_semantics'],
        'terminal_liquidation': receipt['terminal_liquidation'],
        'trading_acceptance': receipt['trading_acceptance'],
        'summary_metrics': result['summary_metrics'],
    }
