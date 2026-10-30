from __future__ import annotations

import json
from datetime import date

import pytest

from uq.errors import ContractError
from uq.models.listing_snapshot import ListingDateSnapshotStore


def test_snapshot_is_content_addressed_and_idempotent(tmp_path):
    store = ListingDateSnapshotStore(tmp_path)
    dates = {'600000.XSHG': date(1999, 11, 10), '000001.XSHE': date(1991, 4, 3)}
    snapshot = store.publish(dates, source='test-security-master')
    assert store.publish(dict(reversed(list(dates.items()))), source='test-security-master') == snapshot
    stored, restored = store.read(snapshot['generation_id'])
    assert restored == dates
    assert stored == snapshot
    alias = tmp_path.parent / (tmp_path.name + '-snapshot-alias')
    alias.symlink_to(tmp_path, target_is_directory=True)
    try:
        assert ListingDateSnapshotStore(alias).read(snapshot['generation_id']) == (stored, dates)
    finally:
        alias.unlink()
    changed = store.publish({**dates, '600000.XSHG': date(2000, 1, 1)}, source='test-security-master')
    assert changed['generation_id'] != snapshot['generation_id']
    assert store.read(snapshot['generation_id'])[1] == dates


def test_tampered_snapshot_is_refused_even_on_idempotent_publish(tmp_path):
    store = ListingDateSnapshotStore(tmp_path)
    dates = {'600000.XSHG': date(1999, 11, 10)}
    snapshot = store.publish(dates, source='test')
    path = store.path_for(snapshot['generation_id'])
    snapshot['listing_dates']['600000.XSHG'] = '2000-01-01'
    path.write_text(json.dumps(snapshot))
    with pytest.raises(ContractError, match='checksum mismatch'):
        store.read(snapshot['generation_id'])
    with pytest.raises(ContractError, match='checksum mismatch'):
        store.publish(dates, source='test')


def test_snapshot_cannot_escape_store_via_generation_or_symlink(tmp_path):
    store = ListingDateSnapshotStore(tmp_path / 'store')
    with pytest.raises(ContractError, match='invalid listing snapshot generation'):
        store.read('../outside')
    store.root.mkdir()
    (tmp_path / 'outside').mkdir()
    (store.root / 'listing_date_snapshots').symlink_to(tmp_path / 'outside')
    with pytest.raises(ContractError, match='symbolic links'):
        store.publish({'600000.XSHG': date(1999, 11, 10)}, source='test')
