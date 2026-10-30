from __future__ import annotations

import json

import pytest

from uq.errors import ContractError
from uq.evaluation.label_audit import audit_label_legs
from tests.test_build_label_sets import DATES, _patch_listing_dates, _publish_bars, module


def test_raw_input_reproduces_labels_and_measures_nonzero_unexecutable_entry_leg(tmp_path, monkeypatch):
    bars = tmp_path / 'bars'
    labels = tmp_path / 'labels'
    _publish_bars(bars, open_offset=.2)
    _patch_listing_dates(monkeypatch)
    publication = tmp_path / 'publication.json'
    module.main(['--bars-root', str(bars), '--label-root', str(labels), '--start', DATES[0].date().isoformat(),
                 '--end', DATES[-1].date().isoformat(), '--combined', '--out', str(publication)])
    generation = json.loads(publication.read_text())['records'][0]['label_generation_id']
    audit = audit_label_legs(bars, labels, generation, DATES[0].date().isoformat(), DATES[-1].date().isoformat())
    assert audit['published_label_rows'] == 24
    assert audit['non_null_labels_reproduced'] == 14
    assert audit['entry_gap']['rows'] == 14
    assert audit['entry_gap']['label_minus_open_entry_close_exit']['mean'] == pytest.approx(-.009805510377790918)
    assert audit['full_open_leg']['rows'] == 12
    assert audit['delisting_visibility']['null_label_rows_before_global_five_session_tail'] == 0
    assert audit['pit_delisting_completeness_certified'] is False
    path = bars / 'canonical/bars_daily/v2' / f"date={DATES[1].date().isoformat()}" / 'data.parquet'
    with path.open('ab') as stream:
        stream.write(b'tampered')
    with pytest.raises(ContractError, match='checksum'):
        audit_label_legs(bars, labels, generation, DATES[0].date().isoformat(), DATES[-1].date().isoformat())
