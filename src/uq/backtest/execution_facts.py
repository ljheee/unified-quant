"""Immutable, source-declared execution facts; config review binds their identity."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts.canonical_v2 import fsync_dir, fsync_tree, read_canonical_v2
from ..contracts.gate_contracts import canonical_json, sha256_json, validate_contract
from ..contracts.paths import reject_symlinks_within
from ..contracts.schema import load_schema
from ..errors import ContractError
from ..execution.set_binding_resolver import set_digest_sha256, verify_partition_set_binding

FACT_COLUMNS = [
    'date', 'instrument', 'visible_at', 'board', 'is_st', 'suspended', 'has_price_limit',
    'limit_up', 'limit_down', 'limit_reference_price', 'raw_previous_close',
    'buy_lot', 'min_buy_shares', 'sell_lot', 'industry',
]


def normalize_execution_facts(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or set(frame.columns) != set(FACT_COLUMNS):
        raise ContractError('execution facts must be nonempty with exactly the v1 columns')
    result = frame[FACT_COLUMNS].copy()
    for value in result['date']:
        try:
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError as exc:
            raise ContractError('execution facts require ISO session dates') from exc
    if not result['instrument'].map(lambda value: isinstance(value, str) and bool(
        re.fullmatch(r'[0-9]{6}\.(XSHG|XSHE|XBSE)', value))).all():
        raise ContractError('execution facts require canonical instrument identifiers')
    if result.duplicated(['date', 'instrument']).any():
        raise ContractError('duplicate execution fact keys')
    if not result['board'].map(lambda value: isinstance(value, str) and value in
                               {'main', 'star', 'chinext', 'bse'}).all():
        raise ContractError('unknown execution board')
    for column in ('is_st', 'suspended', 'has_price_limit'):
        if not result[column].map(lambda value: isinstance(value, (bool, np.bool_))).all():
            raise ContractError(f'execution fact {column} must be boolean')
        result[column] = result[column].astype(bool)
    normalized_times = []
    for day, value in zip(result['date'], result['visible_at']):
        try:
            timestamp = pd.Timestamp(value)
            if pd.isna(timestamp) or timestamp.tzinfo is None:
                raise ValueError
            if timestamp.tz_convert('UTC') > pd.Timestamp(day + 'T09:30:00', tz='Asia/Shanghai').tz_convert('UTC'):
                raise ValueError
            normalized_times.append(timestamp.tz_convert('UTC').isoformat())
        except (ValueError, TypeError) as exc:
            raise ContractError('execution facts must be visible by the session open with explicit timezone') from exc
    result['visible_at'] = normalized_times
    for column in ('limit_reference_price', 'raw_previous_close'):
        if (not pd.api.types.is_numeric_dtype(result[column])
                or pd.api.types.is_bool_dtype(result[column])):
            raise ContractError(f'{column} must be numeric')
        values = result[column].to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ContractError(f'{column} must be finite and positive')
        result[column] = values
    for column in ('limit_up', 'limit_down'):
        if not result[column].map(lambda value: value is None or
                                  (isinstance(value, (int, float, np.number))
                                   and not isinstance(value, (bool, np.bool_)))).all():
            raise ContractError('price bands must be numeric or null')
        result[column] = pd.to_numeric(result[column], errors='raise').astype(float)
        present = result['has_price_limit']
        if not np.isfinite(result.loc[present, column]).all() or (result.loc[present, column] <= 0).any():
            raise ContractError('limited sessions require positive finite published price bands')
        if result.loc[~present, column].notna().any():
            raise ContractError('unlimited sessions must explicitly have null price bands')
    limited = result.loc[result['has_price_limit']]
    if ((limited['limit_down'] > limited['limit_reference_price']).any()
            or (limited['limit_up'] < limited['limit_reference_price']).any()):
        raise ContractError('published limit bands do not contain their reference price')
    for column in ('buy_lot', 'min_buy_shares', 'sell_lot'):
        if not result[column].map(lambda value: isinstance(value, (int, np.integer))
                                 and not isinstance(value, (bool, np.bool_))
                                 and 0 < value <= np.iinfo(np.int64).max).all():
            raise ContractError(f'execution fact {column} must contain positive integers')
        result[column] = result[column].astype('int64')
    if (result['min_buy_shares'] % result['buy_lot'] != 0).any():
        raise ContractError('minimum buy quantity must be a multiple of the buy lot')
    result['industry'] = result['industry'].map(
        lambda value: None if value is pd.NA or value is None or
        (isinstance(value, (float, np.floating)) and np.isnan(value)) else value)
    if not result['industry'].map(lambda value: value is None or (isinstance(value, str) and bool(value.strip()))).all():
        raise ContractError('industry must be a nonempty string or explicit null')
    return result.sort_values(['date', 'instrument'], kind='mergesort').reset_index(drop=True)


def execution_facts_fingerprint(frame: pd.DataFrame) -> str:
    normalized = normalize_execution_facts(frame)
    rows = normalized.astype(object).where(pd.notna(normalized), None).to_dict(orient='records')
    return sha256_json({'profile': 'execution_facts_v1', 'columns': FACT_COLUMNS, 'rows': rows})


class ExecutionFactsStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def directory_for(self, generation: str) -> Path:
        if not isinstance(generation, str) or not re.fullmatch('[0-9a-f]{64}', generation):
            raise ContractError('invalid execution facts generation')
        path = self.root / 'backtest_execution_facts' / f'generation={generation}'
        reject_symlinks_within(self.root, path, label='execution facts')
        return path

    def publish(self, frame: pd.DataFrame, *, source_id: str, source_approval_ref: str) -> dict:
        if not all(isinstance(value, str) and value.strip() for value in (source_id, source_approval_ref)):
            raise ContractError('execution facts require source identity and approval reference')
        frame = normalize_execution_facts(frame)
        base = self.root / 'backtest_execution_facts'
        reject_symlinks_within(self.root, base, label='execution facts')
        base.mkdir(parents=True, exist_ok=True)
        lock_path = base / 'publication.lock'
        reject_symlinks_within(self.root, lock_path, label='execution facts lock')
        with lock_path.open('a+') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            staging = base / f'.staging-{uuid.uuid4().hex}'
            try:
                staging.mkdir()
                frame.to_parquet(staging / 'data.parquet', index=False)
                payload = {
                    'contract_version': 1, 'source_id': source_id, 'source_approval_ref': source_approval_ref,
                    'window_start': frame['date'].min(), 'window_end': frame['date'].max(),
                    'row_count': len(frame), 'columns': FACT_COLUMNS,
                    'data_checksum_sha256': hashlib.sha256((staging / 'data.parquet').read_bytes()).hexdigest(),
                    'logical_checksum_sha256': execution_facts_fingerprint(frame),
                    'price_basis': 'raw_unadjusted_cny',
                }
                generation = sha256_json(payload)
                manifest = {**payload, 'generation_id': generation}
                manifest['manifest_digest_sha256'] = sha256_json(manifest)
                validate_contract('backtest_execution_facts.v1.json', manifest)
                (staging / 'manifest.json').write_bytes(canonical_json(manifest) + b'\n')
                target = self.directory_for(generation)
                if target.exists():
                    existing, _ = self.read(generation)
                    if existing != manifest:
                        raise ContractError('execution facts identity conflict')
                    return existing
                self._read(staging, generation)
                fsync_tree(staging)
                os.rename(staging, target)
                fsync_dir(base)
                return manifest
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def read(self, generation: str) -> tuple[dict, pd.DataFrame]:
        return self._read(self.directory_for(generation), generation)

    def _read(self, directory: Path, generation: str) -> tuple[dict, pd.DataFrame]:
        for name in ('manifest.json', 'data.parquet'):
            reject_symlinks_within(self.root, directory / name, label='execution facts')
        try:
            manifest = json.loads((directory / 'manifest.json').read_text())
        except (OSError, ValueError) as exc:
            raise ContractError('execution facts unavailable or malformed') from exc
        validate_contract('backtest_execution_facts.v1.json', manifest)
        payload = {key: value for key, value in manifest.items()
                   if key not in {'generation_id', 'manifest_digest_sha256'}}
        if (manifest['generation_id'] != generation or sha256_json(payload) != generation
                or sha256_json({**payload, 'generation_id': generation}) != manifest['manifest_digest_sha256']):
            raise ContractError('execution facts manifest identity mismatch')
        try:
            checksum = hashlib.sha256((directory / 'data.parquet').read_bytes()).hexdigest()
        except OSError as exc:
            raise ContractError('execution facts payload unavailable') from exc
        if checksum != manifest['data_checksum_sha256']:
            raise ContractError('execution facts data checksum mismatch')
        frame = normalize_execution_facts(pd.read_parquet(directory / 'data.parquet'))
        if (len(frame) != manifest['row_count'] or execution_facts_fingerprint(frame) != manifest['logical_checksum_sha256']
                or frame['date'].min() != manifest['window_start'] or frame['date'].max() != manifest['window_end']):
            raise ContractError('execution facts content mismatch')
        return manifest, frame


def facts_binding(manifest: dict) -> dict:
    return {key: manifest[key] for key in ('generation_id', 'manifest_digest_sha256',
                                          'data_checksum_sha256', 'logical_checksum_sha256')}


class RawExecutionPriceStore:
    """Read raw bars through the owning canonical-v2 reader, never adjusted prices."""
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.schema = load_schema(Path(__file__).resolve().parents[3] / 'config/schemas/bars_daily.v2.yaml')

    def read_window(self, start: str, end: str, binding: dict | None = None) -> tuple[dict, pd.DataFrame]:
        try:
            begin, finish = date.fromisoformat(start), date.fromisoformat(end)
        except (ValueError, TypeError) as exc:
            raise ContractError('raw execution price window requires ISO dates') from exc
        if begin > finish:
            raise ContractError('raw execution price window is reversed')
        base = self.root / 'canonical/bars_daily/v2'
        reject_symlinks_within(self.root, base, label='raw execution prices')
        triples, frames = [], []
        for child in sorted(base.glob('date=*')):
            try:
                day = date.fromisoformat(child.name.removeprefix('date='))
            except ValueError as exc:
                raise ContractError('malformed raw price partition date') from exc
            if not begin <= day <= finish:
                continue
            for name in ('manifest.json', 'data.parquet'):
                reject_symlinks_within(self.root, child / name, label='raw execution prices')
            try:
                manifest = json.loads((child / 'manifest.json').read_text())
                validate_contract('canonical_manifest.v2.json', manifest)
                reject_symlinks_within(self.root,
                    self.root / 'reports/canonical_v2' / manifest['generation_id'] / 'report.json',
                    label='raw execution price quality report')
                frame = read_canonical_v2(self.root, self.schema, day,
                    expected_anchor=hashlib.sha256(manifest['generation_id'].encode('ascii')).hexdigest())
            except ContractError:
                raise
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ContractError('raw execution price partition unavailable or malformed') from exc
            if (pd.to_datetime(frame['session_date']).dt.strftime('%Y-%m-%d') != day.isoformat()).any():
                raise ContractError('raw execution prices contain rows outside their session partition')
            frames.append(frame)
            triples.append({'session_or_decision_date': day.isoformat(), 'generation_id': manifest['generation_id'],
                            'data_checksum_sha256': manifest['data_checksum_sha256']})
        if not frames:
            raise ContractError('no raw canonical prices in execution window')
        expected = {'family': 'bars_daily_v2', 'window_start': start, 'window_end': end,
                    'set_digest_sha256': set_digest_sha256(triples), 'member_count': len(triples)}
        if binding is not None:
            verify_partition_set_binding(binding, triples, family='bars_daily_v2')
            if binding != expected:
                raise ContractError('raw execution price binding/window mismatch')
        return expected, pd.concat(frames, ignore_index=True)
