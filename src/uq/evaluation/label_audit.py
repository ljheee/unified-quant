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
from ..research_chain.owning_contracts import BacktestMarketDatasetStore, LabelStore
from ..store.reader import ManifestFirstReader
from .metrics import session_dates


def _summary(series: pd.Series) -> dict:
    return {
        'mean': float(series.mean()), 'median': float(series.median()),
        'p95_absolute': float(series.abs().quantile(.95)),
    }


def _governed_calendar_window(
    bars_root: Path,
    *,
    calendar_generation_id: str,
    calendar_checksum_sha256: str,
    start: str,
    end: str,
) -> tuple[dict, list[str]]:
    if (not isinstance(calendar_generation_id, str)
            or len(calendar_generation_id) != 64
            or not isinstance(calendar_checksum_sha256, str)
            or len(calendar_checksum_sha256) != 64):
        raise ContractError('label audit requires a governed calendar generation and checksum')
    store = BacktestMarketDatasetStore(bars_root)
    manifest = store.read_manifest_by(calendar_generation_id, dataset_type='calendar')
    if manifest.get('data_checksum_sha256') != calendar_checksum_sha256:
        raise ContractError('label audit calendar checksum does not match its manifest')
    expected_manifest_digest = sha256_json({
        key: value for key, value in manifest.items() if key != 'manifest_digest_sha256'
    })
    if manifest.get('manifest_digest_sha256') != expected_manifest_digest:
        raise ContractError('label audit calendar manifest digest mismatch')
    frame = store.read_frame(
        calendar_generation_id,
        calendar_checksum_sha256,
        dataset_type='calendar',
        required_columns={'date', 'instrument'},
    )
    if frame.empty or frame['instrument'].astype(str).tolist() != ['CALENDAR'] * len(frame):
        raise ContractError('label audit calendar must use one CALENDAR row per session')
    calendar_dates = session_dates(frame['date'].tolist(), label='label audit calendar')
    if calendar_dates != sorted(set(calendar_dates)):
        raise ContractError('label audit calendar sessions must be sorted and unique')
    if (calendar_dates[0] != manifest.get('window_start')
            or calendar_dates[-1] != manifest.get('window_end')):
        raise ContractError('label audit calendar rows do not match the declared window')
    ordered = frame.sort_values(['date', 'instrument'], kind='mergesort').reset_index(drop=True)
    expected_generation = sha256_json({
        'dataset_type': 'calendar',
        'window_start': manifest['window_start'],
        'window_end': manifest['window_end'],
        'columns': list(ordered.columns),
        'rows': ordered.to_dict(orient='records'),
    })
    if expected_generation != calendar_generation_id:
        raise ContractError('label audit calendar generation does not match its rows')
    if not manifest['window_start'] <= start <= end <= manifest['window_end']:
        raise ContractError('label audit window is outside the governed calendar')
    window_dates = [day for day in calendar_dates if start <= day <= end]
    if not window_dates or window_dates[0] != start or window_dates[-1] != end:
        raise ContractError('label audit calendar does not contain both window endpoints')
    return manifest, calendar_dates


def audit_label_legs(
    bars_root: Path | str,
    label_root: Path | str,
    label_generation: str,
    start: str,
    end: str,
    *,
    calendar_generation_id: str,
    calendar_checksum_sha256: str,
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
    if first > last or first.isoformat() != start or last.isoformat() != end:
        raise ContractError('label audit window is reversed')
    calendar_manifest, calendar_dates = _governed_calendar_window(
        bars_root,
        calendar_generation_id=calendar_generation_id,
        calendar_checksum_sha256=calendar_checksum_sha256,
        start=start,
        end=end,
    )
    label, labels = LabelStore(label_root).read_frame(label_generation)
    if (label['horizon_trading_days'] != 5 or label['contract_version'] != 1
            or label['adjustment_basis'] != 'governed_adjusted_close'):
        raise ContractError('label audit requires the published raw-close x factor five-day label')
    label_dates = pd.to_datetime(labels['decision_date']).dt.strftime('%Y-%m-%d')
    labels = labels.loc[label_dates.between(start, end)].copy()
    labels['decision_date'] = pd.to_datetime(labels['decision_date']).dt.strftime('%Y-%m-%d')
    base = bars_root / 'canonical/bars_daily/v2'
    paths = sorted(path for path in base.glob('date=*/manifest.json')
                   if start <= path.parent.name.removeprefix('date=') <= end)
    window_dates = [day for day in calendar_dates if start <= day <= end]
    raw_dates = [path.parent.name.removeprefix('date=') for path in paths]
    if raw_dates != window_dates:
        raise ContractError('label audit raw partitions do not exactly match the governed calendar')
    if len(window_dates) < 7:
        raise ContractError('label audit needs at least seven published sessions for both execution legs')
    schema = load_schema(Path(__file__).resolve().parents[3] / 'config/schemas/bars_daily.v2.yaml')
    reader = ManifestFirstReader(bars_root)
    bars, members = [], []
    for path in paths:
        session = date.fromisoformat(path.parent.name.removeprefix('date='))
        manifest = json.loads(path.read_text(encoding='utf-8'))
        frame = reader.read(schema, session)
        bars.append(frame)
        members.append({'date': session.isoformat(), 'generation_id': manifest['generation_id'],
                        'data_checksum_sha256': manifest['data_checksum_sha256'],
                        'manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    prices = pd.concat(bars, ignore_index=True).sort_values(['instrument', 'session_date']).reset_index(drop=True)
    if not (prices['adj_factor'].gt(0) & prices['open'].gt(0) & prices['close'].gt(0)).all():
        raise ContractError('label audit requires positive raw prices and adjustment factors')
    adjusted_close = prices['close'] * prices['adj_factor']
    adjusted_open = prices['open'] * prices['adj_factor']
    price_dates = pd.to_datetime(prices['session_date']).dt.strftime('%Y-%m-%d')
    prices['date'] = price_dates
    if not set(prices['date']) <= set(calendar_dates):
        raise ContractError('label audit raw bars contain sessions outside the governed calendar')
    prices['adjusted_close'] = adjusted_close
    prices['adjusted_open'] = adjusted_open
    if prices.duplicated(['instrument', 'date']).any():
        raise ContractError('label audit raw bars have duplicate instrument/session keys')

    calendar_index = {day: index for index, day in enumerate(calendar_dates)}
    date_shifts = {
        (day, offset): (
            calendar_dates[index + offset]
            if index + offset < len(calendar_dates)
            else None
        )
        for index, day in enumerate(calendar_dates)
        for offset in (1, 5, 6)
    }
    derived = prices[['instrument', 'date', 'adjusted_close']].copy()
    for offset, source_column, target_column in (
        (1, 'adjusted_open', 'next_open'),
        (5, 'adjusted_close', 'exit_close'),
        (6, 'adjusted_open', 'exit_open'),
    ):
        target_date_column = f'_session_plus_{offset}'
        derived[target_date_column] = derived['date'].map(
            lambda day: date_shifts[(day, offset)]
        )
        target = prices[['instrument', 'date', source_column]].rename(
            columns={'date': target_date_column, source_column: target_column}
        )
        derived = derived.merge(
            target,
            on=['instrument', target_date_column],
            how='left',
            validate='many_to_one',
        )
    derived['expected_label'] = derived['exit_close'] / derived['adjusted_close'] - 1

    left = labels[['instrument', 'decision_date', 'label']].copy()
    left['date'] = pd.to_datetime(left['decision_date']).dt.strftime('%Y-%m-%d')
    left = left.loc[left['date'].between(start, end)].copy()
    joined = left.merge(derived[['instrument', 'date', 'expected_label', 'next_open', 'exit_close', 'exit_open']],
                        on=['instrument', 'date'], how='left', validate='one_to_one')
    if joined['expected_label'].isna().sum() > labels['label'].isna().sum():
        raise ContractError('label audit source does not cover published non-null labels')
    actual = joined.loc[joined['label'].notna()].copy()
    instrument_dates = {
        str(instrument): set(group['date'].astype(str))
        for instrument, group in prices.groupby('instrument', sort=False)
    }
    for row in actual.itertuples(index=False):
        position = calendar_index.get(row.date)
        if position is None or position + 5 >= len(calendar_dates):
            raise ContractError('published non-null label lacks five governed future sessions')
        required_sessions = calendar_dates[position:position + 6]
        observed_sessions = instrument_dates.get(str(row.instrument), set())
        if not set(required_sessions) <= observed_sessions:
            raise ContractError(
                f'published non-null label has an incomplete governed horizon for {row.instrument} on {row.date}'
            )
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
        'record_version': 2, 'scope': 'read_only_label_leg_diagnostic',
        'bars_root': str(bars_root), 'label_root': str(label_root),
        'calendar_binding': {
            'generation_id': calendar_manifest['generation_id'],
            'manifest_digest_sha256': calendar_manifest['manifest_digest_sha256'],
            'data_checksum_sha256': calendar_manifest['data_checksum_sha256'],
            'window_start': calendar_manifest['window_start'],
            'window_end': calendar_manifest['window_end'],
            'window_session_count': len(window_dates),
            'session_set_sha256': sha256_json(window_dates),
        },
        'label_binding': {'generation_id': label_generation,
            'manifest_digest_sha256': label['manifest_digest_sha256'],
            'data_checksum_sha256': label['data_checksum_sha256']},
        'window': {'start': start, 'end': end},
        'raw_partition_count': len(members), 'raw_partition_set_sha256': sha256_json(members),
        'raw_sessions_match_governed_calendar': True,
        'raw_manifest_versions': sorted({
            json.loads(path.read_text(encoding='utf-8'))['manifest_version'] for path in paths
        }),
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
