"""Dividend disclosure importer store (pendding item 6)."""

import datetime as dt
import hashlib
import json

import pandas as pd
import pytest

from uq.contracts.dividend_events import (
    EVENT_COLUMNS,
    DividendEventStore,
    normalize_dividend_event_rows,
    render_events_csv,
)
from uq.errors import ContractError

RAW_COLUMNS = [
    "ts_code",
    "end_date",
    "ann_date",
    "div_proc",
    "stk_div",
    "stk_bo_rate",
    "stk_co_rate",
    "cash_div",
    "cash_div_tax",
    "record_date",
    "ex_date",
    "pay_date",
    "div_listdate",
    "imp_ann_date",
]


def _row(**overrides):
    row = {
        "ts_code": "600000.SH",
        "end_date": "20250630",
        "ann_date": "20250829",
        "div_proc": "预案",
        "stk_div": 0.0,
        "stk_bo_rate": None,
        "stk_co_rate": None,
        "cash_div": None,
        "cash_div_tax": 0.12,
        "record_date": None,
        "ex_date": None,
        "pay_date": None,
        "div_listdate": None,
        "imp_ann_date": None,
    }
    row.update(overrides)
    return row


def _frame(*rows):
    return pd.DataFrame(list(rows), columns=RAW_COLUMNS)


def _publish(store, day=dt.date(2025, 8, 29), frame=None, **kwargs):
    return store.publish(
        ann_date=day,
        source="tushare:dividend",
        source_fetched_at=dt.datetime(2025, 9, 1, 9, tzinfo=dt.timezone(dt.timedelta(hours=8))),
        frame=frame if frame is not None else _frame(_row(**kwargs)),
    )


def test_publish_and_read_roundtrip(tmp_path):
    store = DividendEventStore(tmp_path)
    path = _publish(store, frame=_frame(_row(), _row(ts_code="000001.SZ")))
    assert path == tmp_path / "dividend_events" / "2025-08-29"
    document = store.read_manifest(dt.date(2025, 8, 29))
    assert document["event_count"] == 2
    assert document["instrument_count"] == 2
    assert document["events_artifact"]["columns"] == EVENT_COLUMNS
    assert document["field_units"]["cash_div_tax"].startswith("cash dividend")
    frame = store.read_events(dt.date(2025, 8, 29))
    assert list(frame["instrument"]) == ["000001.XSHE", "600000.XSHG"]
    assert set(frame["record_id"].str.len()) == {64}
    assert frame["cash_div_tax"].tolist() == [0.12, 0.12]
    assert frame["record_date"].isna().all()


def test_identical_rows_collapse_but_revisions_survive():
    day = dt.date(2025, 8, 29)
    frame = _frame(
        _row(cash_div=0.0, cash_div_tax=0.02),
        _row(cash_div=0.0, cash_div_tax=0.02),
        _row(cash_div=0.0, cash_div_tax=0.05),
    )
    records = normalize_dividend_event_rows(frame, ann_date=day)
    assert len(records) == 2
    assert sorted(record["cash_div_tax"] for record in records) == [0.02, 0.05]


def test_revision_content_is_not_an_event_identity():
    # The same (ts_code, end_date, div_proc, ann_date) with different figures is
    # exactly the case that defeated the naive primary key.
    day = dt.date(2025, 8, 29)
    a = normalize_dividend_event_rows(_frame(_row(cash_div_tax=0.02)), ann_date=day)
    b = normalize_dividend_event_rows(_frame(_row(cash_div_tax=0.0)), ann_date=day)
    assert a[0]["record_id"] != b[0]["record_id"]


def test_render_is_byte_stable():
    records = normalize_dividend_event_rows(_frame(_row(), _row(ts_code="000001.SZ")), ann_date=dt.date(2025, 8, 29))
    assert render_events_csv(records) == render_events_csv(records)
    assert hashlib.sha256(render_events_csv(records)).hexdigest() == hashlib.sha256(
        render_events_csv(records)
    ).hexdigest()


def test_unimplemented_stage_is_kept_apart_from_implementation():
    day = dt.date(2025, 8, 29)
    frame = _frame(
        _row(div_proc="预案"),
        _row(div_proc="实施", record_date="20250916", ex_date="20250917", imp_ann_date="20250911"),
    )
    records = normalize_dividend_event_rows(frame, ann_date=day)
    implemented = [record for record in records if record["div_proc"] == "实施"]
    assert len(records) == 2
    assert implemented[0]["ex_date"] == "2025-09-17"
    assert implemented[0]["imp_ann_date"] == "2025-09-11"


def test_pre_disclosure_stage_is_a_disclosure_record():
    records = normalize_dividend_event_rows(_frame(_row(div_proc="预披露")), ann_date=dt.date(2025, 8, 29))
    assert records[0]["div_proc"] == "预披露"


@pytest.mark.parametrize(
    "mutate, message",
    [
        ({"cash_div": -0.1}, "negative"),
        ({"stk_div": "-1"}, "negative"),
        ({"cash_div": "abc"}, "not numeric"),
        ({"div_proc": "取消"}, "unknown dividend disclosure stage"),
        ({"ex_date": "20250828"}, "precedes ann_date"),
        ({"imp_ann_date": "20250801"}, "precedes ann_date"),
        ({"ann_date": "20250828"}, "does not match partition"),
        ({"end_date": None}, "no report period"),
        ({"ts_code": "600000.HK"}, "unsupported Tushare instrument"),
        ({"ex_date": "2025/13/01"}, "not a date"),
    ],
)
def test_invalid_rows_fail_closed(mutate, message):
    with pytest.raises(ContractError, match=message):
        normalize_dividend_event_rows(_frame(_row(**mutate)), ann_date=dt.date(2025, 8, 29))


def test_empty_partition_is_refused(tmp_path):
    with pytest.raises(ContractError, match="empty dividend disclosure partition"):
        _publish(tmp_path and DividendEventStore(tmp_path), frame=_frame())


def test_republish_is_refused(tmp_path):
    store = DividendEventStore(tmp_path)
    _publish(store)
    with pytest.raises(ContractError, match="already published"):
        _publish(store)


def test_tampered_bytes_are_refused(tmp_path):
    store = DividendEventStore(tmp_path)
    _publish(store)
    artifact = store._partition(dt.date(2025, 8, 29)) / "events.csv"
    artifact.write_bytes(artifact.read_bytes().replace(b"0.12", b"0.99"))
    with pytest.raises(ContractError, match="tampered dividend events bytes"):
        store.read_events(dt.date(2025, 8, 29))


def test_manifest_generation_mismatch_is_refused(tmp_path):
    store = DividendEventStore(tmp_path)
    _publish(store)
    manifest = store._partition(dt.date(2025, 8, 29)) / "manifest.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["event_count"] = document["event_count"] + 1
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ContractError, match="generation mismatch"):
        store.read_manifest(dt.date(2025, 8, 29))


def test_as_of_read_is_point_in_time(tmp_path):
    store = DividendEventStore(tmp_path)
    _publish(store, day=dt.date(2025, 8, 29), frame=_frame(_row()))
    _publish(store, day=dt.date(2025, 9, 30), frame=_frame(_row(ann_date="20250930", end_date="20250630")))
    assert len(store.as_of(dt.date(2025, 9, 1))) == 1
    both = store.as_of(dt.date(2025, 9, 30))
    assert len(both) == 2
    assert list(both["ann_date"]) == ["2025-08-29", "2025-09-30"]
    assert len(store.as_of(dt.date(2025, 8, 1))) == 0


def test_partition_listing_ignores_foreign_directories(tmp_path):
    store = DividendEventStore(tmp_path)
    _publish(store)
    (tmp_path / "dividend_events" / "not-a-date").mkdir()
    assert store.list_partitions() == [dt.date(2025, 8, 29)]
