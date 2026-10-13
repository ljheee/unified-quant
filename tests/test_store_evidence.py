"""The digest inventory producer must refuse to record bytes it cannot verify.

scripts/build_store_evidence.py is the producer of the committed pilot
inventories, so its fail-closed behaviour is governance, not plumbing.
"""

import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.index_membership import IndexMembershipStore
from uq.contracts.dividend_events import DividendEventStore
from uq.contracts.gate_contracts import sha256_json
from uq.contracts.schema import load_schema
from uq.store.pit_store import CanonicalStore

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "build_store_evidence", _ROOT / "scripts" / "build_store_evidence.py"
)
module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(module)

SCHEMA_PATH = _ROOT / "config/schemas/bars_daily.v2.yaml"
SESSION = date(2026, 3, 2)


def _bars_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "instrument": ["600000.XSHG", "000001.XSHE"],
        "session_date": pd.to_datetime(["2026-03-02", "2026-03-02"]),
        "open": [10.0, 10.0],
        "high": [11.0, 11.0],
        "low": [9.0, 9.0],
        "close": [10.5, 10.0],
        "volume": [100.0, 200.0],
        "amount": [1050.0, 2100.0],
        "status": ["trading", "trading"],
        "limit_up": [11.0, 11.0],
        "limit_down": [9.0, 9.0],
        "adj_factor": [2.0, 4.0],
    })


def _bars_store(tmp_path: Path) -> Path:
    store = tmp_path / "bars-store"
    CanonicalStore(store).publish(load_schema(SCHEMA_PATH), SESSION, _bars_frame(), {}, {})
    return store


def _membership_store(tmp_path: Path) -> Path:
    store = IndexMembershipStore(tmp_path / "membership-store")
    store.publish(
        universe_id="a500",
        index_code="000510.SH",
        source="tushare:index_weight:000510.SH",
        source_fetched_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone(timedelta(hours=8))),
        snapshot_date=date(2026, 2, 27),
        publish_lag_days=2,
        trading_days=[SESSION + timedelta(days=offset) for offset in range(1, 8)],
        members=pd.DataFrame({"instrument": ["000001.XSHE", "600000.XSHG"]}),
    )
    return tmp_path / "membership-store"


def test_bars_inventory_pins_the_digests_a_reviewer_needs(tmp_path):
    store = _bars_store(tmp_path)
    inventory = module.bars_inventory(
        store, "a500", date(2026, 3, 1), date(2026, 3, 3), SCHEMA_PATH, tmp_path
    )
    assert inventory["partition_count"] == 1
    record = inventory["records"][0]
    partition = store / "canonical/bars_daily/v2/date=2026-03-02"
    assert record["manifest_sha256"] == module._sha256(partition / "manifest.json")
    assert record["data_sha256"] == module._sha256(partition / "data.parquet")
    assert record["row_count"] == 2
    assert record["generation_id"]
    assert not Path(record["manifest_path"]).is_absolute()


def test_bars_inventory_refuses_tampered_data(tmp_path):
    store = _bars_store(tmp_path)
    data = store / "canonical/bars_daily/v2/date=2026-03-02/data.parquet"
    frame = pd.read_parquet(data)
    frame.loc[0, "close"] = 99.0
    frame.to_parquet(data, index=False)
    with pytest.raises(SystemExit, match="drifted from manifest"):
        module.bars_inventory(store, "a500", date(2026, 3, 1), date(2026, 3, 3), SCHEMA_PATH, tmp_path)


def test_bars_inventory_refuses_stray_partition_directories(tmp_path):
    store = _bars_store(tmp_path)
    (store / "canonical/bars_daily/v2/date=2026-03-02.staging.deadbeef").mkdir()
    with pytest.raises(SystemExit, match="unexpected partition directory"):
        module.bars_inventory(store, "a500", date(2026, 3, 1), date(2026, 3, 3), SCHEMA_PATH, tmp_path)


def test_bars_inventory_refuses_a_manifest_the_reader_will_not_accept(tmp_path):
    # A manifest whose row_count no longer matches its bytes keeps its own
    # trust anchor (anchor = sha256(generation_id)), so the governed readback is
    # what catches it. The producer must turn that into a refusal, not a traceback.
    store = _bars_store(tmp_path)
    manifest_path = store / "canonical/bars_daily/v2/date=2026-03-02/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["row_count"] = 1
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    with pytest.raises(SystemExit, match="inventory refused"):
        module.bars_inventory(store, "a500", date(2026, 3, 1), date(2026, 3, 3), SCHEMA_PATH, tmp_path)


def test_bars_inventory_refuses_windows_without_partitions(tmp_path):
    store = _bars_store(tmp_path)
    with pytest.raises(SystemExit):
        module.bars_inventory(store, "a500", date(2026, 4, 1), date(2026, 4, 30), SCHEMA_PATH, tmp_path)


def test_membership_inventory_pins_snapshots_and_visible_dates(tmp_path):
    store = _membership_store(tmp_path)
    inventory = module.membership_inventory(store, "a500", tmp_path)
    assert inventory["snapshot_count"] == 1
    record = inventory["records"][0]
    snapshot = store / "index_membership/a500/2026-02-27"
    assert record["manifest_sha256"] == module._sha256(snapshot / "manifest.json")
    assert record["members_sha256"] == module._sha256(snapshot / "members.csv")
    assert inventory["earliest_visible_date"] == record["visible_from"]


def test_membership_inventory_refuses_tampered_members(tmp_path):
    store = _membership_store(tmp_path)
    members = store / "index_membership/a500/2026-02-27/members.csv"
    members.write_text("instrument\n000009.XSHE\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="drifted from manifest"):
        module.membership_inventory(store, "a500", tmp_path)


def _dividend_store(tmp_path: Path, *, ann_date=date(2026, 2, 26)) -> Path:
    store = DividendEventStore(tmp_path / "dividend-store")
    store.publish(
        ann_date=ann_date,
        source="tushare:dividend",
        source_fetched_at=datetime(2026, 3, 8, 9, 0, tzinfo=timezone(timedelta(hours=8))),
        frame=pd.DataFrame(
            [
                {
                    "ts_code": "600000.SH",
                    "end_date": "20251231",
                    "ann_date": ann_date.strftime("%Y%m%d"),
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
            ]
        ),
    )
    return tmp_path / "dividend-store"


def test_dividend_inventory_pins_partitions(tmp_path):
    store = _dividend_store(tmp_path)
    inventory = module.dividend_inventory(store, date(2026, 2, 1), date(2026, 2, 28), tmp_path)
    assert inventory["partition_count"] == 1
    assert inventory["event_count"] == 1
    record = inventory["records"][0]
    partition = store / "dividend_events/2026-02-26"
    assert record["manifest_sha256"] == module._sha256(partition / "manifest.json")
    assert record["events_sha256"] == module._sha256(partition / "events.csv")
    assert not Path(record["manifest_path"]).is_absolute()


def test_dividend_inventory_refuses_tampered_events(tmp_path):
    store = _dividend_store(tmp_path)
    events = store / "dividend_events/2026-02-26/events.csv"
    events.write_bytes(events.read_bytes().replace(b"0.12", b"0.99"))
    with pytest.raises(SystemExit, match="drifted from manifest"):
        module.dividend_inventory(store, None, None, tmp_path)


def test_dividend_inventory_refuses_instrument_count_drift(tmp_path):
    # The manifest's instrument_count is publisher-computed: an untrusted
    # publisher can ship a self-consistent document (valid schema, valid
    # generation id) whose count still disagrees with the verified events.
    # The inventory recomputes it instead of trusting the document.
    store = _dividend_store(tmp_path)
    manifest_path = store / "dividend_events/2026-02-26/manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["instrument_count"] = document["instrument_count"] + 1
    document["generation_id"] = sha256_json(
        {key: value for key, value in document.items() if key != "generation_id"}
    )
    manifest_path.write_text(json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="instrument count drift"):
        module.dividend_inventory(store, None, None, tmp_path)


def test_dividend_inventory_refuses_windows_without_partitions(tmp_path):
    store = _dividend_store(tmp_path)
    with pytest.raises(SystemExit):
        module.dividend_inventory(store, date(2026, 5, 1), date(2026, 5, 31), tmp_path)


def test_index_refuses_to_record_itself(tmp_path):
    (tmp_path / "evidence" / "phase-0").mkdir(parents=True)
    out = tmp_path / "evidence/phase-0/evidence-index.json"
    with pytest.raises(SystemExit, match="cannot record itself"):
        module.evidence_index(
            "layer", ["evidence/phase-0/evidence-index.json"], "archived", "note", out, tmp_path
        )


def test_index_refuses_paths_it_cannot_verify(tmp_path):
    out = tmp_path / "evidence/phase-0/evidence-index.json"
    with pytest.raises(SystemExit, match="repo-relative"):
        module.evidence_index("layer", ["/etc/passwd"], "archived", "note", out, tmp_path)
    with pytest.raises(SystemExit, match="repo-relative"):
        module.evidence_index("layer", ["../outside.json"], "archived", "note", out, tmp_path)
    with pytest.raises(SystemExit, match="missing evidence record"):
        module.evidence_index("layer", ["evidence/gone.json"], "archived", "note", out, tmp_path)


def test_index_records_are_sorted_and_refuse_duplicates(tmp_path):
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence/b.json").write_text("b\n", encoding="utf-8")
    (tmp_path / "evidence/a.json").write_text("a\n", encoding="utf-8")
    out = tmp_path / "evidence/index.json"
    payload = module.evidence_index(
        "layer",
        ["evidence/b.json", "evidence/a.json"],
        "archived",
        "note",
        out,
        tmp_path,
    )
    assert [entry["path"] for entry in payload["records"]] == ["evidence/a.json", "evidence/b.json"]
    for entry in payload["records"]:
        assert entry["sha256"] == module._sha256(tmp_path / entry["path"])
    with pytest.raises(SystemExit, match="duplicate evidence record"):
        module.evidence_index(
            "layer", ["evidence/a.json", "evidence/a.json"], "archived", "note", out, tmp_path
        )
