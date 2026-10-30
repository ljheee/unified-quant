"""Read-only audit of the published close label versus executable entry/exit legs."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from ..contracts.gate_contracts import sha256_json
from ..contracts.schema import load_schema
from ..errors import ContractError
from ..research_chain.owning_contracts import LabelStore
from ..store.reader import ManifestFirstReader


def _summary(series: pd.Series) -> dict:
    return {
        'mean': float(series.mean()), 'median': float(series.median()),
        'p95_absolute': float(series.abs().quantile(.95)),
    }


def audit_label_legs(
    bars_root: Path | str, label_root: Path | str, label_generation: str, start: str, end: str,
) -> dict:
    """Recompute every non-null label from its raw bars, then measure entry gaps.

    Uses the legacy manifest-first reader that the label publisher used. This
    verifies schema, manifest anchor and bytes, but does not turn old canonical
    v1 partitions into quality-signed v2 inputs. No source store is modified.
    """
    bars_root, label_root = Path(bars_root), Path(label_root)
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise ContractError('label audit requires ISO window dates') from exc
    if first > last:
        raise ContractError('label audit window is reversed')
    label, labels = LabelStore(label_root).read_frame(label_generation)
    if (label['horizon_trading_days'] != 5 or label['contract_version'] != 1
            or label['adjustment_basis'] != 'governed_adjusted_close'):
        raise ContractError('label audit requires the published raw-close x factor five-day label')
    base = bars_root / 'canonical/bars_daily/v2'
    paths = sorted(path for path in base.glob('date=*/manifest.json')
                   if start <= path.parent.name.removeprefix('date=') <= end)
    if not paths or paths[0].parent.name != f'date={start}' or paths[-1].parent.name != f'date={end}':
        raise ContractError('label audit raw window endpoints are missing')
    if len(paths) < 7:
        raise ContractError('label audit needs at least seven published sessions for both execution legs')
    schema = load_schema(Path(__file__).resolve().parents[3] / 'config/schemas/bars_daily.v2.yaml')
    reader = ManifestFirstReader(bars_root)
    bars, members = [], []
    for path in paths:
        session = date.fromisoformat(path.parent.name.removeprefix('date='))
        manifest = json.loads(path.read_text())
        frame = reader.read(schema, session)
        bars.append(frame)
        members.append({'date': session.isoformat(), 'generation_id': manifest['generation_id'],
                        'data_checksum_sha256': manifest['data_checksum_sha256'],
                        'manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    prices = pd.concat(bars, ignore_index=True).sort_values(['instrument', 'session_date']).reset_index(drop=True)
    if not (prices['adj_factor'].gt(0) & prices['open'].gt(0) & prices['close'].gt(0)).all():
        raise ContractError('label audit requires positive raw prices and adjustment factors')
    groups = prices['instrument']
    adjusted_close = prices['close'] * prices['adj_factor']
    adjusted_open = prices['open'] * prices['adj_factor']
    prices['expected_label'] = adjusted_close.groupby(groups).shift(-5) / adjusted_close - 1
    prices['next_open'] = adjusted_open.groupby(groups).shift(-1)
    prices['exit_close'] = adjusted_close.groupby(groups).shift(-5)
    prices['exit_open'] = adjusted_open.groupby(groups).shift(-6)
    price_dates = pd.to_datetime(prices['session_date']).dt.strftime('%Y-%m-%d')
    prices['date'] = price_dates
    left = labels[['instrument', 'decision_date', 'label']].copy()
    left['date'] = pd.to_datetime(left['decision_date']).dt.strftime('%Y-%m-%d')
    joined = left.merge(prices[['instrument', 'date', 'expected_label', 'next_open', 'exit_close', 'exit_open']],
                        on=['instrument', 'date'], how='left', validate='one_to_one')
    if joined['expected_label'].isna().sum() > labels['label'].isna().sum():
        raise ContractError('label audit source does not cover published non-null labels')
    actual = joined.loc[joined['label'].notna()].copy()
    if (actual['expected_label'].isna().any()
            or not np.allclose(actual['label'], actual['expected_label'], rtol=0, atol=1e-10)):
        raise ContractError('published labels do not reproduce from bound raw bars')
    entry = actual.loc[actual['next_open'].notna()].copy()
    entry['entry_to_close_exit'] = entry['exit_close'] / entry['next_open'] - 1
    entry['unexecutable_entry_leg'] = entry['label'] - entry['entry_to_close_exit']
    full = entry.loc[entry['exit_open'].notna()].copy()
    if entry.empty or full.empty:
        raise ContractError('label audit has no complete executable entry/exit observations')
    full['open_to_open'] = full['exit_open'] / full['next_open'] - 1
    full['close_label_minus_open_to_open'] = full['label'] - full['open_to_open']
    for column in ('unexecutable_entry_leg', 'close_label_minus_open_to_open'):
        if not np.isfinite((entry if column == 'unexecutable_entry_leg' else full)[column]).all():
            raise ContractError('label audit return arithmetic is not finite')
    payload = {
        'record_version': 1, 'scope': 'read_only_label_leg_diagnostic',
        'bars_root': str(bars_root), 'label_root': str(label_root),
        'label_binding': {'generation_id': label_generation,
            'manifest_digest_sha256': label['manifest_digest_sha256'],
            'data_checksum_sha256': label['data_checksum_sha256']},
        'window': {'start': start, 'end': end},
        'raw_partition_count': len(members), 'raw_partition_set_sha256': sha256_json(members),
        'raw_manifest_versions': sorted({json.loads(path.read_text())['manifest_version'] for path in paths}),
        'reader': 'ManifestFirstReader: manifest anchor, schema and byte checksum; old v1 quality report not reverified',
        'published_label_rows': len(labels), 'non_null_labels_reproduced': len(actual),
        'overlap_lags_sessions': [1, 2, 3, 4],
        'entry_gap': {'rows': len(entry), 'label_minus_open_entry_close_exit': _summary(entry['unexecutable_entry_leg']),
            'return_sign_flip_rows': int(((entry['label'] > 0) != (entry['entry_to_close_exit'] > 0)).sum())},
        'full_open_leg': {'rows': len(full), 'label_minus_open_to_open': _summary(full['close_label_minus_open_to_open']),
            'return_sign_flip_rows': int(((full['label'] > 0) != (full['open_to_open'] > 0)).sum())},
        'delisting_visibility': {
            'raw_status_counts': {str(key): int(value) for key, value in prices['status'].value_counts().items()},
            'instruments_last_seen_before_window_end': int((prices.groupby('instrument')['date'].max() < end).sum()),
            'null_label_rows_before_global_five_session_tail': int((joined['label'].isna()
                & (joined['date'] <= sorted(prices['date'].unique())[-6])).sum()),
            'legacy_delisted_flag_source': 'hardcoded_false; status_nontrading_sets_suspended',
        },
        'pit_delisting_completeness_certified': False,
    }
    payload['audit_checksum_sha256'] = sha256_json(payload)
    return payload
