from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from uq.adapters.demo import DeterministicDemoAdapter
from uq.contracts.config import load_dataset_contract
from uq.contracts.schema import load_schema
from uq.quality.gate import CrossSourceGate
from uq.routing.router import SourceRouter
from uq.store.pit_store import CanonicalStore

ROOT = Path(__file__).resolve().parents[1]


def production_rows() -> pd.DataFrame:
    rows = canonical_rows().rename(columns={"datetime": "session_date"})
    rows["status"] = "trading"
    rows["limit_up"] = [11.0, 11.2]
    rows["limit_down"] = [9.0, 9.2]
    return rows


def canonical_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "instrument": ["600000.XSHG", "600000.XSHG"],
            "datetime": pd.to_datetime(["2026-08-20", "2026-08-21"]),
            "open": [10.0, 10.2],
            "high": [10.5, 10.8],
            "low": [9.9, 10.1],
            "close": [10.3, 10.6],
            "volume": [1_000_000.0, 1_200_000.0],
            "amount": [10_300_000.0, 12_720_000.0],
        }
    )


@pytest.fixture()
def schema():
    return load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml")


@pytest.fixture()
def contract():
    return load_dataset_contract(ROOT / "config/datasets/bars_daily.research-v1.yaml", load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml"))


def test_schema_accepts_canonical_rows(schema):
    frame = canonical_rows()
    schema.validate(frame)
    assert schema.name == "bars_daily.research-v1"


def test_schema_rejects_bad_ohlc_and_duplicate(schema):
    bad = canonical_rows()
    bad.loc[0, "low"] = 11.0
    with pytest.raises(ValueError, match="invariant"):
        schema.validate(bad)

    duplicate = pd.concat([canonical_rows(), canonical_rows()], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate primary key"):
        schema.validate(duplicate)


def test_router_only_fetches_eligible_declared_fields(contract):
    router = SourceRouter(
        contract,
        {
            "tdx": DeterministicDemoAdapter(
                "tdx",
                canonical_rows(),
                schema_version=contract.schema_version,
            )
        },
        trading_dates=lambda start, end: {date(2026, 8, 20), date(2026, 8, 21)},
    )
    result = router.fetch(
        instruments=["600000.XSHG"],
        start="2026-08-20",
        end="2026-08-21",
    )
    assert result.selected_sources == ["tdx"]
    assert result.is_complete
    assert set(result.frames["tdx"].columns) == set(contract.required_fields)


def test_router_without_calendar_cannot_claim_complete(contract):
    tdx = DeterministicDemoAdapter("tdx", canonical_rows())
    result = SourceRouter(contract, {"tdx": tdx}).fetch(
        instruments=["600000.XSHG"],
        start="2026-08-20",
        end="2026-08-21",
    )
    assert result.coverage_status == "unverified"


def test_cross_source_gate_records_lineage(contract):
    rows = canonical_rows()
    tdx = DeterministicDemoAdapter("tdx", rows)
    tushare = DeterministicDemoAdapter("tushare", rows)
    gate = CrossSourceGate(contract.primary_source, contract.cross_validation)
    result = gate.merge({"tdx": tdx.fetch("bars_daily", ["600000.XSHG"], "2026-08-20", "2026-08-21", list(rows.columns)).rows, "tushare": tushare.fetch("bars_daily", ["600000.XSHG"], "2026-08-20", "2026-08-21", list(rows.columns)).rows}, ["instrument", "datetime"])
    assert result.conflicts == []
    assert result.lineage["close"].validated_by == ["tushare"]


def test_cross_source_gate_rejects_conflict(contract):
    rows = canonical_rows()
    tdx = DeterministicDemoAdapter("tdx", rows)
    tushare = DeterministicDemoAdapter("tushare", rows, close_delta_pct=0.05)
    gate = CrossSourceGate(contract.primary_source, contract.cross_validation)
    report = gate.merge({"tdx": rows.copy(), "tushare": tushare.fetch("bars_daily", ["600000.XSHG"], "2026-08-20", "2026-08-21", list(rows.columns)).rows}, ["instrument", "datetime"])
    assert not report.accepted
    assert len(report.conflicts) == 2
    assert report.errors


def test_store_publishes_atomic_partition(schema, tmp_path):
    store = CanonicalStore(tmp_path)
    path = store.publish(
        schema=schema,
        partition_date=date(2026, 8, 21),
        frame=canonical_rows(),
        lineage={"close": {"source": "tdx", "validated_by": []}},
        source_versions={"demo": "test"},
    )
    manifest_path = path.parent / "manifest.json"
    assert path.exists()
    assert manifest_path.exists()
    restored = pd.read_parquet(path)
    schema.validate(restored)
    assert len(restored) == 2
