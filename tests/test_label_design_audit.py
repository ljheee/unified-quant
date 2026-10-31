from __future__ import annotations

import json
import shutil
from datetime import date

import pandas as pd
import pytest

from uq.errors import ContractError
from uq.evaluation.label_audit import audit_label_legs
from uq.research_chain.owning_contracts import BacktestMarketDatasetStore
from tests.test_build_label_sets import DATES, _patch_listing_dates, _publish_bars, module


def _publish_governed_calendar(root):
    sessions = [day.date().isoformat() for day in DATES]
    frame = pd.DataFrame({"date": sessions, "instrument": ["CALENDAR"] * len(sessions)})
    return BacktestMarketDatasetStore(root).publish(
        frame,
        dataset_type="calendar",
        window_start=date.fromisoformat(sessions[0]),
        window_end=date.fromisoformat(sessions[-1]),
        sources={"fixture": "independent-governed-calendar"},
        producer="test-label-audit",
    )


def test_raw_input_reproduces_labels_and_measures_nonzero_unexecutable_entry_leg(tmp_path, monkeypatch):
    bars = tmp_path / 'bars'
    labels = tmp_path / 'labels'
    _publish_bars(bars, open_offset=.2)
    _patch_listing_dates(monkeypatch)
    publication = tmp_path / 'publication.json'
    module.main(['--bars-root', str(bars), '--label-root', str(labels), '--start', DATES[0].date().isoformat(),
                 '--end', DATES[-1].date().isoformat(), '--combined', '--out', str(publication)])
    generation = json.loads(publication.read_text())['records'][0]['label_generation_id']
    calendar = _publish_governed_calendar(bars)
    audit = audit_label_legs(
        bars, labels, generation, DATES[0].date().isoformat(), DATES[-1].date().isoformat(),
        calendar_generation_id=calendar['generation_id'],
        calendar_checksum_sha256=calendar['data_checksum_sha256'],
    )
    assert audit['published_label_rows'] == 24
    assert audit['non_null_labels_reproduced'] == 14
    assert audit['entry_gap']['rows'] == 14
    assert audit['entry_gap']['label_minus_open_entry_close_exit']['mean'] == pytest.approx(-.009805510377790918)
    assert audit['full_open_leg']['rows'] == 12
    assert audit['delisting_visibility']['null_label_rows_before_global_five_session_tail'] == 0
    assert audit['pit_delisting_completeness_certified'] is False
    assert audit['record_version'] == 2
    assert audit['raw_sessions_match_governed_calendar'] is True
    assert audit['calendar_binding']['session_set_sha256']
    path = bars / 'canonical/bars_daily/v2' / f"date={DATES[1].date().isoformat()}" / 'data.parquet'
    with path.open('ab') as stream:
        stream.write(b'tampered')
    with pytest.raises(ContractError, match='checksum'):
        audit_label_legs(
            bars, labels, generation, DATES[0].date().isoformat(), DATES[-1].date().isoformat(),
            calendar_generation_id=calendar['generation_id'],
            calendar_checksum_sha256=calendar['data_checksum_sha256'],
        )


def test_audit_rejects_an_interior_raw_partition_missing_from_the_governed_calendar(tmp_path, monkeypatch):
    bars = tmp_path / 'bars'
    labels = tmp_path / 'labels'
    _publish_bars(bars, open_offset=.2)
    _patch_listing_dates(monkeypatch)
    publication = tmp_path / 'publication.json'
    module.main(['--bars-root', str(bars), '--label-root', str(labels), '--start', DATES[0].date().isoformat(),
                 '--end', DATES[-1].date().isoformat(), '--combined', '--out', str(publication)])
    generation = json.loads(publication.read_text())['records'][0]['label_generation_id']
    calendar = _publish_governed_calendar(bars)
    missing_day = DATES[4].date().isoformat()
    shutil.rmtree(bars / 'canonical/bars_daily/v2' / f'date={missing_day}')

    with pytest.raises(ContractError, match='exactly match the governed calendar'):
        audit_label_legs(
            bars, labels, generation, DATES[0].date().isoformat(), DATES[-1].date().isoformat(),
            calendar_generation_id=calendar['generation_id'],
            calendar_checksum_sha256=calendar['data_checksum_sha256'],
        )


def test_audit_rejects_a_non_null_label_whose_instrument_grid_skips_a_governed_session(
    tmp_path, monkeypatch
):
    bars = tmp_path / 'bars'
    labels = tmp_path / 'labels'
    instrument = '600000.XSHG'
    missing_day = DATES[4].date().isoformat()
    _publish_bars(
        bars,
        instruments=(instrument,),
        open_offset=.2,
        skip_pairs={(instrument, missing_day)},
    )
    _patch_listing_dates(monkeypatch)
    publication = tmp_path / 'publication.json'
    module.main([
        '--bars-root', str(bars), '--label-root', str(labels),
        '--start', DATES[0].date().isoformat(), '--end', DATES[-1].date().isoformat(),
        '--instrument', instrument, '--out', str(publication),
    ])
    generation = json.loads(publication.read_text())['records'][0]['label_generation_id']
    calendar = _publish_governed_calendar(bars)

    with pytest.raises(ContractError, match='incomplete governed horizon'):
        audit_label_legs(
            bars, labels, generation, DATES[0].date().isoformat(), DATES[-1].date().isoformat(),
            calendar_generation_id=calendar['generation_id'],
            calendar_checksum_sha256=calendar['data_checksum_sha256'],
        )
