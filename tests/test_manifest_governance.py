import json
from datetime import date

import pandas as pd
import pytest

from uq.contracts.schema import load_schema
from uq.errors import ContractError
from uq.factors.context import FactorContext
from uq.market.adjustment import XdxrAdjustmentDeriver
from uq.store.pit_store import CanonicalStore
from uq.store.reader import ManifestFirstReader


def bars(dates):
    return pd.DataFrame({
        "instrument": ["600000.XSHG"] * len(dates),
        "datetime": pd.to_datetime(dates),
        "open": [10.0] * len(dates),
        "high": [10.2] * len(dates),
        "low": [9.8] * len(dates),
        "close": [10.0] * len(dates),
        "volume": [10000.0] * len(dates),
        "amount": [100000.0] * len(dates),
    })


def publish(root, schema, dates):
    store = CanonicalStore(root)
    for value in dates:
        store.publish(schema, value, bars([value]), {}, {})


def test_adjustment_derivation_binds_snapshot_and_effective_dates():
    events = pd.DataFrame([{
        "year": 2026, "month": 8, "day": 21, "category": 1,
        "fenhong": 0.0, "songzhuangu": 10.0, "peigu": 0.0, "peigujia": 0.0,
    }])
    derivation = XdxrAdjustmentDeriver().derive(
        "600000.XSHG",
        events,
        pd.to_datetime(["2026-08-20", "2026-08-21"]),
        pd.Series([10.0], index=pd.to_datetime(["2026-08-21"])),
    )
    assert derivation.snapshot_id
    assert derivation.effective_date_table_checksum
    assert derivation.frame.iloc[0]["adj_factor"] == pytest.approx(2.0)
    assert derivation.frame.iloc[-1]["adj_factor"] == pytest.approx(1.0)


def test_context_reads_verified_date_range(tmp_path):
    schema = load_schema("config/schemas/bars_daily.research-v1.yaml")
    publish(tmp_path, schema, [date(2026, 8, 20), date(2026, 8, 21)])
    context = FactorContext(tmp_path, schema)
    frame = context.read_bars(
        start=date(2026, 8, 20),
        end=date(2026, 8, 21),
        fields=["instrument", "datetime", "close"],
    )
    assert frame["datetime"].tolist() == [
        pd.Timestamp("2026-08-20"), pd.Timestamp("2026-08-21")
    ]


def test_reader_rejects_regenerated_manifest_without_anchor(tmp_path):
    schema = load_schema("config/schemas/bars_daily.research-v1.yaml")
    publish(tmp_path, schema, [date(2026, 8, 21)])
    partition = tmp_path / "canonical/bars_daily/research-v1/date=2026-08-21"
    manifest = json.loads((partition / "manifest.json").read_text())
    manifest["row_count"] += 1
    (partition / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ContractError, match="generation digest mismatch|trust anchor"):
        ManifestFirstReader(tmp_path).read(schema, date(2026, 8, 21))
