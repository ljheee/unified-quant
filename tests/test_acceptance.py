from datetime import date
from pathlib import Path
import pandas as pd
import pytest
from uq.adapters.demo import DeterministicDemoAdapter
from uq.contracts.config import load_dataset_contract
from uq.contracts.schema import load_schema
from uq.errors import CapabilityGapError, ContractError
from uq.quality.gate import CrossSourceGate
from uq.routing.router import SourceRouter
from uq.store.pit_store import CanonicalStore
from uq.store.reader import ManifestFirstReader

ROOT = Path(__file__).resolve().parents[1]

def research():
    schema = load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml")
    contract = load_dataset_contract(ROOT / "config/datasets/bars_daily.research-v1.yaml", schema)
    return schema, contract

def rows():
    return pd.DataFrame({
        "instrument": ["600000.XSHG"], "datetime": pd.to_datetime(["2026-08-21"]),
        "open": [10.0], "high": [10.1], "low": [9.9], "close": [10.0],
        "volume": [1.0], "amount": [10.0],
    })

def test_no_primary_source_fails_without_partial():
    _, contract = research()
    router = SourceRouter(contract, {"tushare": DeterministicDemoAdapter("tushare", rows())})
    with pytest.raises(CapabilityGapError):
        router.fetch(["600000.XSHG"], "2026-08-20", "2026-08-21")

def test_two_complete_sources_are_interchangeable():
    _, contract = research()
    rows_a = rows()
    rows_b = rows()
    trading_dates = lambda start, end: {date(2026, 8, 21)}
    a = SourceRouter(contract, {"tdx": DeterministicDemoAdapter("tdx", rows_a)})
    b = SourceRouter(contract, {"tdx": DeterministicDemoAdapter("tdx", rows_b)})
    ra = a.fetch(["600000.XSHG"], "2026-08-21", "2026-08-21")
    rb = b.fetch(["600000.XSHG"], "2026-08-21", "2026-08-21")
    assert ra.coverage_status == "unverified"
    assert rb.coverage_status == "unverified"
    pd.testing.assert_frame_equal(ra.frames["tdx"], rb.frames["tdx"])

def test_quality_rejects_conflicts_and_reports_lineage():
    _, contract = research()
    bad = DeterministicDemoAdapter("tushare", rows(), close_delta_pct=.1)
    gate = CrossSourceGate(contract.primary_source, contract.cross_validation)
    report = gate.merge({"tdx": rows(), "tushare": bad.fetch("bars_daily", ["600000.XSHG"], "", "", list(rows().columns)).rows}, ["instrument", "datetime"])
    assert not report.accepted
    assert report.conflicts and report.lineage["close"].validated_by == []

def test_publication_failure_leaves_no_artifact(tmp_path):
    schema, _ = research()
    store = CanonicalStore(tmp_path)
    bad = rows(); bad.loc[0, "low"] = 99.0
    with pytest.raises(Exception, match="invariant failed"):
        store.publish(schema, date(2026,8,21), bad, {}, {})
    assert list(tmp_path.rglob("data.parquet")) == []

def test_reader_rejects_tampered_data(tmp_path):
    schema, _ = research()
    CanonicalStore(tmp_path).publish(schema, date(2026,8,21), rows(), {}, {})
    partition = tmp_path / "canonical/bars_daily/research-v1/date=2026-08-21"
    (partition / "data.parquet").write_bytes(b"bad")
    with pytest.raises(ContractError, match="checksum mismatch"):
        ManifestFirstReader(tmp_path).read(schema, date(2026,8,21))
