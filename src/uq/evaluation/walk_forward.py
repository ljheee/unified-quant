"""Session-based rolling protocols over the existing governed research runner."""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from ..contracts.canonical_v2 import fsync_dir
from ..contracts.gate_contracts import canonical_json, sha256_json
from ..contracts.model_layer import ModelContractLoader
from ..contracts.paths import reject_symlinks_within
from ..errors import ContractError
from ..models.dataset import SplitValidator
from ..research_chain.contracts import research_request_schema_name
from ..research_chain.owning_contracts import BacktestMarketDatasetStore
from ..research_chain.runner import ResearchChainRunner
from .metrics import session_dates


@dataclass(frozen=True)
class WalkForwardSpec:
    train_sessions: int
    validation_sessions: int
    test_sessions: int
    mode: str = 'rolling'
    label_horizon_sessions: int = 5
    purge_sessions: int = 5
    embargo_sessions: int = 2
    warmup_sessions: int = 60
    partial_final_test: bool = False

    def validate(self) -> None:
        for field in ('train_sessions', 'validation_sessions', 'test_sessions',
                      'label_horizon_sessions', 'warmup_sessions'):
            value = getattr(self, field)
            if type(value) is not int or value < 1:
                raise ContractError(f'walk-forward {field} must be a positive integer')
        for field in ('purge_sessions', 'embargo_sessions'):
            value = getattr(self, field)
            if type(value) is not int or value < 0:
                raise ContractError(f'walk-forward {field} must be a nonnegative integer')
        if self.purge_sessions < self.label_horizon_sessions:
            raise ContractError('walk-forward purge must cover the label horizon')
        if self.warmup_sessions < 60:
            raise ContractError('Alpha158 walk-forward needs at least 60 warmup sessions')
        if self.mode not in {'rolling', 'expanding'}:
            raise ContractError('walk-forward mode must be rolling or expanding')
        if type(self.partial_final_test) is not bool:
            raise ContractError('partial_final_test must be boolean')


def build_walk_forward_plan(
    calendar_dates: list[str], spec: WalkForwardSpec, *, calendar_binding: Mapping[str, Any],
) -> dict:
    """Plan disjoint contiguous OOS folds; no split is inferred from row counts."""
    spec.validate()
    calendar = session_dates(calendar_dates, label='walk-forward calendar')
    if not calendar or calendar != sorted(set(calendar)):
        raise ContractError('walk-forward calendar must be sorted, unique and nonempty')
    gap = spec.purge_sessions + spec.embargo_sessions
    first_test = spec.warmup_sessions + spec.train_sessions + gap + spec.validation_sessions + gap
    last_usable = len(calendar) - spec.label_horizon_sessions - 1
    if first_test > last_usable:
        raise ContractError('calendar cannot cover warmup/train/validation/test and future label tail')
    folds, cursor = [], first_test
    while cursor <= last_usable:
        last = min(cursor + spec.test_sessions - 1, last_usable)
        if last - cursor + 1 < spec.test_sessions and not spec.partial_final_test:
            break
        validation_end = cursor - gap - 1
        validation_start = validation_end - spec.validation_sessions + 1
        train_end = validation_start - gap - 1
        train_start = spec.warmup_sessions if spec.mode == 'expanding' else train_end - spec.train_sessions + 1
        splits = [
            {'name': 'train', 'start_date': calendar[train_start], 'end_date': calendar[train_end]},
            {'name': 'validation', 'start_date': calendar[validation_start], 'end_date': calendar[validation_end]},
            {'name': 'test', 'start_date': calendar[cursor], 'end_date': calendar[last]},
        ]
        SplitValidator.validate_splits(splits, horizon=spec.purge_sessions,
                                       embargo_days=spec.embargo_sessions, trading_dates=calendar)
        folds.append({
            'fold_id': f'fold-{len(folds):04d}',
            'raw_history_start': calendar[train_start - spec.warmup_sessions],
            'train_start': calendar[train_start],
            'label_observation_end': calendar[last + spec.label_horizon_sessions],
            'split_policy': {'purge_trading_days': spec.purge_sessions,
                             'embargo_trading_days': spec.embargo_sessions, 'splits': splits},
            'test_dates': calendar[cursor:last + 1],
            'partial_test': last - cursor + 1 < spec.test_sessions,
        })
        cursor = last + 1
    if not folds:
        raise ContractError('calendar cannot cover a full walk-forward test fold')
    oos = [day for fold in folds for day in fold['test_dates']]
    span = (date.fromisoformat(oos[-1]) - date.fromisoformat(oos[0])).days + 1
    payload = {
        'record_version': 1, 'kind': 'walk_forward_plan', 'spec': asdict(spec),
        'calendar_binding': dict(calendar_binding), 'calendar_dates': calendar, 'folds': folds,
        'unused_test_candidate_dates': calendar[cursor:last_usable + 1],
        'qualification': {
            'oos_sessions': len(oos), 'oos_calendar_days': span,
            'required_oos_sessions': 240, 'required_oos_calendar_days': 365,
            'one_year_coverage_met': len(oos) >= 240 and span >= 365,
            'market_regimes_verified': False,
            'historical_pit_completeness_verified': False,
        },
        'capital_semantics': 'independent_fold_backtests; no_stitched_pnl',
    }
    payload['generation_id'] = sha256_json(payload)
    return payload


def verify_walk_forward_plan(plan: Mapping[str, Any]) -> None:
    try:
        spec = WalkForwardSpec(**plan['spec'])
        expected = build_walk_forward_plan(plan['calendar_dates'], spec,
                                          calendar_binding=plan['calendar_binding'])
        if canonical_json(expected) != canonical_json(dict(plan)):
            raise ContractError('walk-forward plan does not recompute from calendar and spec')
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError('invalid walk-forward plan') from exc


def plan_from_published_calendar(
    root: Path, generation: str, checksum: str, spec: WalkForwardSpec,
) -> dict:
    from ..contracts.gate_contracts import validate_contract
    root = Path(root)
    directory = root / 'backtest_market_data' / f'dataset={generation}'
    for name in ('manifest.json', 'data.parquet'):
        reject_symlinks_within(root, directory / name, label='walk-forward source calendar')
    manifest = BacktestMarketDatasetStore(root).read_manifest_by(generation, dataset_type='calendar')
    frame = BacktestMarketDatasetStore(root).read_frame(
        generation, checksum, dataset_type='calendar', required_columns={'date'},
    )
    validate_contract('backtest_market_dataset.v1.json', manifest)
    if sha256_json({key: value for key, value in manifest.items()
                    if key != 'manifest_digest_sha256'}) != manifest['manifest_digest_sha256']:
        raise ContractError('walk-forward calendar manifest digest mismatch')
    expected_generation = sha256_json({
        'dataset_type': 'calendar', 'window_start': manifest['window_start'],
        'window_end': manifest['window_end'], 'columns': list(frame.columns),
        'rows': frame.to_dict(orient='records'),
    })
    if generation != expected_generation:
        raise ContractError('walk-forward calendar content generation mismatch')
    # The absolute store locator is part of plan identity: relocating or
    # spelling the same root differently requires creating a new plan.
    return build_walk_forward_plan(frame['date'].tolist(), spec, calendar_binding={
        'family': 'backtest_market_dataset_calendar', 'generation_id': generation,
        'manifest_digest_sha256': manifest['manifest_digest_sha256'],
        'data_checksum_sha256': checksum,
        'store_root': str(root.absolute()),
    })


def validate_fold_requests(plan: Mapping[str, Any], requests: list[dict]) -> None:
    """Validate the entire fixed protocol before executing the first request."""
    verify_walk_forward_plan(plan)
    if len(requests) != len(plan['folds']):
        raise ContractError('walk-forward requires exactly one request for every planned fold')
    identities, reference = set(), None
    for fold, request in zip(plan['folds'], requests):
        ModelContractLoader.validate(research_request_schema_name(request), request)
        ResearchChainRunner._validate_plan(SimpleNamespace(request=request))
        identity = (request['request_content_generation_id'], request['run_id'])
        if identity in identities:
            raise ContractError('duplicate walk-forward request/run identity')
        identities.add(identity)
        if request['execution_mode'] != 'full_research_run':
            raise ContractError('walk-forward requires executable full research requests')
        if request['dataset_policy_template']['split_policy'] != fold['split_policy']:
            raise ContractError(f"{fold['fold_id']} request split policy does not match protocol")
        if (request['window_start_date'], request['window_end_date']) != (
            fold['train_start'], fold['label_observation_end']
        ):
            raise ContractError('walk-forward request window must cover train through label observation tail')
        # Fold identities and input snapshots vary; model, portfolio constraints,
        # missing-data rule and execution environment must not vary with OOS results.
        model = request['model_definition_template']
        portfolio = request['portfolio_definition_template']
        settings = {
            'model': {key: value for key, value in model.items()
                      if key not in {'template_generation_id', 'template_manifest_digest_sha256'}},
            'portfolio': {key: value for key, value in portfolio.items()
                          if key not in {'template_generation_id', 'template_manifest_digest_sha256',
                                         'universe_snapshot_generation_id'}},
            'missing_policy': request['dataset_policy_template']['missing_policy'],
            'environment': request['environment'], 'contract_version': request['contract_version'],
        }
        if reference is not None and canonical_json(settings) != canonical_json(reference):
            raise ContractError('walk-forward model/portfolio/environment settings drift across folds')
        reference = settings


class WalkForwardStore:
    """Immutable protocol and attempt records, outside quality-approval families."""
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _publish(self, path: Path, document: dict, *, identical_ok: bool) -> Path:
        reject_symlinks_within(self.root, path, label='walk-forward evidence')
        payload = canonical_json(document) + b'\n'
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.walk-forward-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
                fsync_dir(path.parent)
            except FileExistsError:
                if not identical_ok or path.read_bytes() != payload:
                    raise ContractError('immutable walk-forward evidence conflict')
            return path
        finally:
            Path(temporary).unlink(missing_ok=True)

    def publish_plan(self, plan: dict) -> Path:
        verify_walk_forward_plan(plan)
        return self._publish(self.root / 'walk_forward/plans' / f"plan={plan['generation_id']}.json",
                             plan, identical_ok=True)

    def read_plan(self, generation: str) -> dict:
        import re
        if not isinstance(generation, str) or not re.fullmatch('[0-9a-f]{64}', generation):
            raise ContractError('invalid walk-forward plan generation')
        path = self.root / 'walk_forward/plans' / f'plan={generation}.json'
        reject_symlinks_within(self.root, path, label='walk-forward plan')
        try:
            plan = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ContractError('walk-forward plan unavailable or malformed') from exc
        verify_walk_forward_plan(plan)
        if plan['generation_id'] != generation:
            raise ContractError('walk-forward plan path identity mismatch')
        return plan

    def publish_attempt(
        self, plan: dict, records: list[dict], *, status: str, error_type: str | None,
        failed_fold: dict | None = None,
    ) -> Path:
        verify_walk_forward_plan(plan)
        if status not in {'passed', 'failed'}:
            raise ContractError('invalid walk-forward attempt status')
        payload = {
            'record_version': 1, 'kind': 'walk_forward_attempt', 'plan_generation_id': plan['generation_id'],
            'publication_scope': 'research_evidence_no_quality_authorization',
            'capital_semantics': plan['capital_semantics'], 'status': status, 'fold_records': records,
            'qualification': plan['qualification'], 'error_type': error_type,
            'failed_fold': failed_fold,
        }
        payload['checksum_sha256'] = sha256_json(payload)
        return self._publish(self.root / 'walk_forward/attempts' / f"plan={plan['generation_id']}"
                             / f'attempt={uuid.uuid4()}.json', payload, identical_ok=False)

    def read_attempt(self, path: Path | str) -> dict:
        path = Path(path)
        reject_symlinks_within(self.root, path, label='walk-forward attempt')
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            checksum = payload['checksum_sha256']
            expected = sha256_json({key: value for key, value in payload.items() if key != 'checksum_sha256'})
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ContractError('walk-forward attempt unavailable or malformed') from exc
        if checksum != expected:
            raise ContractError('walk-forward attempt checksum mismatch')
        if set(payload) != {
            'record_version', 'kind', 'plan_generation_id', 'publication_scope', 'capital_semantics',
            'status', 'fold_records', 'qualification', 'error_type', 'failed_fold', 'checksum_sha256',
        } or type(payload['record_version']) is not int or payload['record_version'] != 1 or payload['kind'] != 'walk_forward_attempt':
            raise ContractError('walk-forward attempt fields/version mismatch')
        plan = self.read_plan(payload['plan_generation_id'])
        if (payload['publication_scope'] != 'research_evidence_no_quality_authorization'
                or payload['capital_semantics'] != plan['capital_semantics']
                or payload['qualification'] != plan['qualification']):
            raise ContractError('walk-forward attempt qualification/scope disagrees with plan')
        records = payload['fold_records']
        if not isinstance(records, list) or len(records) > len(plan['folds']):
            raise ContractError('walk-forward attempt has invalid fold records')
        if payload['status'] not in {'passed', 'failed'}:
            raise ContractError('invalid walk-forward attempt status')
        if payload['status'] == 'passed' and (
            len(records) != len(plan['folds']) or payload['error_type'] is not None or payload['failed_fold'] is not None
        ):
            raise ContractError('walk-forward passed attempt is incomplete')
        if payload['status'] == 'failed' and (
            not isinstance(payload['error_type'], str) or not payload['error_type']
            or not isinstance(payload['failed_fold'], dict)
        ):
            raise ContractError('walk-forward failed attempt has no failure context')
        for fold, record in zip(plan['folds'], records):
            if (not isinstance(record, dict) or record.get('fold_id') != fold['fold_id']
                    or record.get('status') != 'passed' or record.get('test_dates') != fold['test_dates']):
                raise ContractError('walk-forward attempt folds do not match planned OOS sequence')
        return payload
