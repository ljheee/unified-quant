from datetime import date
from pathlib import Path
import pandas as pd
import pytest
from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.store.pit_store import CanonicalStore

ROOT = Path(__file__).resolve().parents[1]

def rows():
    return pd.DataFrame({
        "instrument": ["600000.XSHG"], "datetime": pd.to_datetime(["2026-08-21"]),
        "open": [10.0], "high": [10.1], "low": [9.9], "close": [10.0],
        "volume": [1.0], "amount": [10.0],
    })

def test_publication_is_manifest_first_and_immutable(tmp_path):
    schema = load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml")
    store = CanonicalStore(tmp_path)
    partition = store.publish(schema, date(2026,8,21), rows(), {}, {})
    assert partition.exists()
    assert (partition.parent / partition.parent.name / "manifest.json").exists() or (partition.parent / "manifest.json").exists()

    with pytest.raises(ContractError, match="immutable partition already published"):
        store.publish(schema, date(2026,8,21), rows(), {}, {})

def test_invalid_frame_is_not_published(tmp_path):
    schema = load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml")
    store = CanonicalStore(tmp_path)
    bad = rows()
    bad.loc[0, "low"] = 11.0
    with pytest.raises(Exception, match="invariant failed: high_low"):
        store.publish(schema, date(2026,8,21), bad, {}, {})
    assert not any(tmp_path.rglob("data.parquet"))

def test_reader_rejects_bare_parquet_and_tampering(tmp_path):
    from uq.store.reader import ManifestFirstReader
    schema = load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml")
    store = CanonicalStore(tmp_path)
    reader = ManifestFirstReader(tmp_path)
    with pytest.raises(ContractError, match="unpublished"):
        reader.read(schema, date(2026,8,21))

    store.publish(schema, date(2026,8,21), rows(), {}, {})
    partition = tmp_path / "canonical" / "bars_daily" / "research-v1" / "date=2026-08-21"
    data = partition / "data.parquet"
    data.write_bytes(b"tampered")
    with pytest.raises(ContractError, match="checksum mismatch"):
        reader.read(schema, date(2026,8,21))

def test_factor_context_reads_published_data_only(tmp_path):
    from uq.factors.context import FactorContext
    from datetime import date as Date
    schema = load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml")
    store = CanonicalStore(tmp_path)
    context = FactorContext(tmp_path, schema)
    with pytest.raises(ContractError):
        context.read_bars(Date(2026,8,21))

def test_qlib_exporter_creates_lineage_snapshot(tmp_path):
    from datetime import date as Date
    from uq.exporters.qlib import QlibExporter
    from uq.factors.context import FactorContext
    schema = load_schema(ROOT / "config/schemas/bars_daily.research-v1.yaml")
    CanonicalStore(tmp_path).publish(schema, Date(2026,8,21), rows(), {}, {})
    exporter = QlibExporter(tmp_path, tmp_path / "qlib")
    snapshot = exporter.export_partition_snapshot(schema, Date(2026,8,21))
    manifest = (snapshot / "manifest.json").read_text()
    assert "exporter_version" in manifest and "checksum_sha256" in manifest
