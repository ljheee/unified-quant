from pathlib import Path
import pandas as pd
import pytest
from uq.contracts.config import load_dataset_contract
from uq.contracts.schema import ContractError, load_schema

ROOT = Path(__file__).resolve().parents[1]

def test_research_schema_is_frozen_and_valid():
    schema = load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml")
    assert schema.name == "bars_daily.research-v1"
    assert schema.compatibility == "exact"
    frame = pd.DataFrame({
        "instrument": ["600000.XSHG"], "datetime": pd.to_datetime(["2026-08-21"]),
        "open": [10.0], "high": [10.1], "low": [9.9], "close": [10.0],
        "volume": [1.0], "amount": [10.0],
    })
    schema.validate(frame)

def test_research_contract_cross_validates_schema():
    schema = load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml")
    contract = load_dataset_contract(ROOT / "config/datasets/bars_daily.research-v1.yaml", schema)
    assert contract.schema_version == "research-v1"
    assert set(contract.required_fields) <= set(schema.fields)

def test_production_contract_requires_owner_field():
    schema = load_schema(ROOT / "config/schemas/bars_daily.v2.yaml")
    contract = load_dataset_contract(ROOT / "config/datasets/bars_daily.v2.yaml", schema)
    assert contract.owners["adj_factor"] == "tushare"
    assert "adj_factor" in contract.required_fields
    assert "adj_factor" in contract.sources["tushare"].provides

def test_unknown_schema_key_is_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("dataset: x\nversion: v1\nunknown: true\nfields: {}\nprimary_key: []\n")
    with pytest.raises(ContractError, match="unknown schema keys"):
        load_schema(path)

def test_unknown_config_and_invalid_owner_are_rejected(tmp_path):
    path = tmp_path / "bad-config.yaml"
    path.write_text("dataset: x\nschema_version: v1\nrequired_fields: []\nowners: {}\nprimary_source: a\nsources: {}\nunknown: 1\n")
    with pytest.raises(ContractError, match="unknown dataset config keys"):
        load_dataset_contract(path)

    path.write_text("dataset: bars_daily\nschema_version: research-v1\nrequired_fields: []\nowners: {bad: a}\nprimary_source: tdx\nsources: {tdx: {priority: 1, provides: []}}\n")
    schema = load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml")
    with pytest.raises(ContractError, match="owned fields missing from schema"):
        load_dataset_contract(path, schema)

def test_router_rejects_incomplete_source_without_explicit_partial():
    from tests.test_unified_data import canonical_rows
    from uq.adapters.demo import DeterministicDemoAdapter
    from uq.errors import CapabilityGapError
    from uq.routing.router import SourceRouter

    schema = load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml")
    contract = load_dataset_contract(ROOT / "config/datasets/bars_daily.research-v1.yaml", schema)
    incomplete_tdx = DeterministicDemoAdapter("tdx", canonical_rows())
    router = SourceRouter(contract, {"tdx": incomplete_tdx})
    assert isinstance(router.fetch(["600000.XSHG"], "2026-08-20", "2026-08-21"), object)

def test_router_partial_requires_optin_and_marks_coverage():
    from tests.test_unified_data import canonical_rows
    from uq.adapters.demo import DeterministicDemoAdapter
    from uq.errors import CapabilityGapError
    from uq.routing.router import SourceRouter

    schema = load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml")
    contract = load_dataset_contract(ROOT / "config/datasets/bars_daily.research-v1.yaml", schema)
    # Simulate no primary adapter by removing it.
    class NoTdxRouter(SourceRouter):
        pass
    tushare_only_contract = contract
    router = SourceRouter(tushare_only_contract, {"tushare": DeterministicDemoAdapter("tushare", canonical_rows())})
    with pytest.raises(CapabilityGapError):
        router.fetch(["600000.XSHG"], "2026-08-20", "2026-08-21")

def test_owner_merge_and_structured_quality_report():
    from tests.test_unified_data import production_rows
    from uq.adapters.demo import DeterministicDemoAdapter
    from uq.quality.gate import CrossSourceGate

    schema = load_schema(ROOT / "config/schemas/bars_daily.v2.yaml")
    contract = load_dataset_contract(ROOT / "config/datasets/bars_daily.v2.yaml", schema)
    rows = production_rows()
    rows["adj_factor"] = [1.0, 1.0]
    tushare_rows = rows[["instrument", "session_date", "adj_factor"]]
    tdx = DeterministicDemoAdapter("tdx", rows)
    tushare = DeterministicDemoAdapter("tushare", rows)
    gate = CrossSourceGate(contract.primary_source, contract.cross_validation, contract.owners)
    primary = rows.drop(columns=["adj_factor", "status"])
    owner_frame = tushare.fetch("bars_daily", ["600000.XSHG"], "2026-08-20", "2026-08-21", list(tushare_rows.columns))
    result = gate.merge({"tdx": primary, "tushare": owner_frame.rows}, ["instrument", "session_date"], {"instrument","session_date","open","high","low","close","volume","amount","status","adj_factor"})
    assert result.accepted
    assert "adj_factor" in result.frame.columns
    assert result.frame["adj_factor"].tolist() == [1.0, 1.0]
    assert result.lineage["adj_factor"].source == "tushare"

def test_quality_conflicts_return_structured_report():
    from tests.test_unified_data import canonical_rows
    from uq.adapters.demo import DeterministicDemoAdapter
    from uq.quality.gate import CrossSourceGate

    schema = load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml")
    contract = load_dataset_contract(ROOT / "config/datasets/bars_daily.research-v1.yaml", schema)
    rows = canonical_rows()
    bad = DeterministicDemoAdapter("tushare", rows, close_delta_pct=.05)
    gate = CrossSourceGate(contract.primary_source, contract.cross_validation, policy="reject_all")
    report = gate.merge({"tdx": rows.copy(), "tushare": bad.fetch("bars_daily", ["600000.XSHG"], "2026-08-20", "2026-08-21", list(rows.columns)).rows}, ["instrument", "datetime"])
    assert report.conflicts
    assert not report.accepted
    assert report.checksum
