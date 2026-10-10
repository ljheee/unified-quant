from datetime import date

import pandas as pd
import pytest

from uq.adapters.demo import DeterministicDemoAdapter
from uq.adapters.mootdx_source import MootdxSourceAdapter
from uq.adapters.tushare_free import TushareFreeAdapter
from uq.contracts.schema import ContractError, load_schema
from uq.market.adjustment import XdxrAdjustmentDeriver
from uq.routing.router import SourceRouter
from uq.quality.gate import CrossSourceGate
from uq.store.pit_store import CanonicalStore
from uq.store.reader import ManifestFirstReader
from uq.sources.fetch import FetchStatus


def contract():
    root = "config"
    return load_schema(f"{root}/schemas/bars_daily.research-v1.yaml")


def test_mootdx_client_error_returns_typed_result():
    class Failing(MootdxSourceAdapter):
        def _client(self):
            raise RuntimeError("connection refused")

    result = Failing(("127.0.0.1", 1)).fetch("bars_daily", ["600000.XSHG"], "", "", ["close"])
    assert result.status == FetchStatus.UPSTREAM_ERROR
    assert result.retryable


def test_tushare_missing_token_is_typed_auth_failure():
    import os

    import pytest

    from uq.errors import ContractError

    saved = os.environ.pop("TUSHARE_TOKEN", None)
    try:
        adapter = TushareFreeAdapter(token_env="MISSING_UQ_TOKEN")
        with pytest.raises(ContractError, match="missing Tushare token"):
            adapter.fetch("bars_daily", ["600000.XSHG"], "", "", ["close"])
    finally:
        if saved is not None:
            os.environ["TUSHARE_TOKEN"] = saved


def test_empty_primary_result_is_not_route_complete():
    schema = load_schema("config/schemas/bars_daily.research-v1.yaml")
    from uq.contracts.config import load_dataset_contract

    config = load_dataset_contract("config/datasets/bars_daily.research-v1.yaml", schema)
    empty = pd.DataFrame(columns=["instrument", "datetime", "open", "high", "low", "close", "volume", "amount"])
    router = SourceRouter(config, {"tdx": DeterministicDemoAdapter("tdx", empty)})
    route = router.fetch(["600000.XSHG"], "2026-08-21", "2026-08-21")
    assert not route.is_complete


def test_exact_schema_rejects_extra_columns():
    schema = load_schema("config/schemas/bars_daily.research-v1.yaml")
    frame = pd.DataFrame({
        "instrument": ["600000.XSHG"],
        "datetime": pd.to_datetime(["2026-08-21"]),
        "open": [10.0], "high": [10.2], "low": [9.9], "close": [10.1],
        "volume": [100.0], "amount": [1010.0],
        "extra": [1],
    })
    with pytest.raises(ContractError, match="unexpected fields"):
        schema.validate(frame)


def test_mootdx_normalizes_named_columns_from_reordered_indexed_frame():
    raw = pd.DataFrame(
        {
            "instrument": ["600000.XSHG"],
            "amount": [102000.0],
            "vol": [100.0],
            "close": [10.2],
            "low": [9.9],
            "high": [10.3],
            "open": [10.0],
        },
        index=pd.DatetimeIndex(["2026-08-21"], name="datetime"),
    )
    normalized = MootdxSourceAdapter._normalize(raw, "2026-08-20", "2026-08-21")
    assert normalized.iloc[0]["open"] == 10.0
    assert normalized.iloc[0]["volume"] == 10000.0
    assert normalized.iloc[0]["datetime"] == pd.Timestamp("2026-08-21")


def test_mootdx_missing_required_column_is_typed_error():
    class Client:
        def bars(self, **kwargs):
            return pd.DataFrame({"bad": [1]}, index=pd.DatetimeIndex(["2026-08-21"], name="datetime"))

        def close(self):
            pass

    adapter = MootdxSourceAdapter(("127.0.0.1", 1))
    adapter._client = lambda: Client()
    result = adapter.fetch("bars_daily", ["600000.XSHG"], "", "", ["close"])
    assert result.status == FetchStatus.UPSTREAM_ERROR
    assert not result.retryable


def test_canonical_parquet_roundtrip_preserves_contract(tmp_path):
    schema = load_schema("config/schemas/bars_daily.research-v1.yaml")
    frame = pd.DataFrame({
        "instrument": ["600000.XSHG"],
        "datetime": pd.to_datetime(["2026-08-21"]),
        "open": [10.0], "high": [10.3], "low": [9.9], "close": [10.2],
        "volume": [10000.0], "amount": [102000.0],
    })
    CanonicalStore(tmp_path).publish(schema, date(2026, 8, 21), frame, {}, {})
    restored = ManifestFirstReader(tmp_path).read(schema, date(2026, 8, 21))
    assert str(restored["datetime"].dtype) == "datetime64[ns]"
    assert restored.iloc[0]["datetime"] == pd.Timestamp("2026-08-21")
    schema.validate(restored)


def test_xdxr_deriver_handles_bonus_share_event():
    events = pd.DataFrame([{
        "year": 2026, "month": 8, "day": 21, "category": 1,
        "fenhong": 0.0, "peigujia": 0.0, "songzhuangu": 10.0,
        "peigu": 0.0,
    }])
    derivation = XdxrAdjustmentDeriver().derive(
        "600000.XSHG",
        events,
        pd.to_datetime(["2026-08-20", "2026-08-21"]),
        pd.Series([10.0], index=pd.to_datetime(["2026-08-21"])),
    )
    assert derivation.version == "adj_factor.exchange_v2"
    # Multiply-basis (exchange_v2): 10送10 halves the ex-right price, so the
    # pre-event session factor is 0.5 and close * adj_factor stays continuous.
    assert derivation.frame["adj_factor"].tolist() == pytest.approx([0.5, 1.0])


def test_quality_checksum_includes_inputs_and_policy():
    frame = pd.DataFrame({
        "instrument": ["600000.XSHG"], "datetime": pd.to_datetime(["2026-08-21"]),
        "open": [10.0], "high": [10.2], "low": [9.9], "close": [10.1],
        "volume": [100.0], "amount": [1010.0],
    })
    gate = CrossSourceGate("tdx", {}, minimum_coverage=0.5)
    first = gate.merge({"tdx": frame}, ["instrument", "datetime"])
    second = gate.merge({"tdx": frame}, ["instrument", "datetime"])
    changed = gate.merge({"tdx": frame.assign(close=[10.2])}, ["instrument", "datetime"])
    assert first.checksum == second.checksum
    assert first.checksum != changed.checksum
    assert first.input_fingerprints["tdx"]


def _gate_frames(close_p=10.1, close_s=10.1, vol_p=100.0, vol_s=100.0):
    primary = pd.DataFrame({
        "instrument": ["600000.XSHG"], "datetime": pd.to_datetime(["2026-08-21"]),
        "close": [close_p], "volume": [vol_p],
    })
    secondary = pd.DataFrame({
        "instrument": ["600000.XSHG"], "datetime": pd.to_datetime(["2026-08-21"]),
        "close": [close_s], "volume": [vol_s],
    })
    rules = {
        "close": {"compare_with": "tushare", "max_abs_diff_pct": 0.001},
        "volume": {"compare_with": "tushare", "max_rel_diff_pct": 0.01},
    }
    return primary, secondary, rules


def test_quality_gate_fails_closed_when_comparator_cannot_run():
    # cr.md H2 regression: a configured comparison that cannot run used to be
    # skipped silently, publishing unvalidated fields.
    primary, secondary, rules = _gate_frames()
    gate = CrossSourceGate("tdx", rules)
    # comparator source absent entirely
    report = gate.merge({"tdx": primary}, ["instrument", "datetime"])
    assert not report.accepted
    assert any("comparator tushare unavailable" in e for e in report.errors)
    # comparator present but missing the field
    report = gate.merge(
        {"tdx": primary, "tushare": secondary.drop(columns=["volume"])},
        ["instrument", "datetime"],
    )
    assert not report.accepted
    assert any("lacks field volume" in e for e in report.errors)


def test_quality_gate_zero_primary_relative_diff_is_a_conflict():
    # cr.md H2 regression: primary volume 0 vs comparator 500 produced a NaN
    # relative difference and used to pass the max_rel_diff_pct rule.
    primary, secondary, rules = _gate_frames(vol_p=0.0, vol_s=500.0)
    gate = CrossSourceGate("tdx", rules)
    report = gate.merge({"tdx": primary, "tushare": secondary}, ["instrument", "datetime"])
    assert not report.accepted
    assert any(c["field"] == "volume" for c in report.conflicts)
    # zero vs zero stays clean
    primary, secondary, rules = _gate_frames(vol_p=0.0, vol_s=0.0)
    report = CrossSourceGate("tdx", rules).merge(
        {"tdx": primary, "tushare": secondary}, ["instrument", "datetime"]
    )
    assert report.accepted


def test_quality_gate_rejects_zero_overlap_and_enforces_minimum_coverage():
    # cr.md H2 regression: zero comparable rows and under-coverage used to be
    # accepted while minimum_coverage only fed a lineage label.
    primary, secondary, rules = _gate_frames()
    disjoint = secondary.copy()
    disjoint["datetime"] = pd.to_datetime(["2026-08-22"])
    gate = CrossSourceGate("tdx", rules)
    report = gate.merge({"tdx": primary, "tushare": disjoint}, ["instrument", "datetime"])
    assert not report.accepted
    assert any("compared zero rows" in e for e in report.errors)
    # minimum_coverage enforced: 1 of 2 rows comparable < 0.9
    primary2 = pd.concat([primary, primary.assign(datetime=pd.to_datetime(["2026-08-22"]))])
    gate = CrossSourceGate("tdx", rules, minimum_coverage=0.9)
    report = gate.merge({"tdx": primary2, "tushare": secondary}, ["instrument", "datetime"])
    assert not report.accepted
    assert any("below minimum" in e for e in report.errors)


def test_quality_gate_rejects_duplicate_keys_and_empty_primary():
    # cr.md H2 regression: duplicate comparator keys escaped as a bare
    # ValueError from reindex, and an empty primary was accepted vacuously.
    primary, secondary, rules = _gate_frames()
    from uq.errors import ContractError as GateContractError
    dup = pd.concat([secondary, secondary])
    gate = CrossSourceGate("tdx", rules)
    with pytest.raises(GateContractError, match="duplicate keys"):
        gate.merge({"tdx": primary, "tushare": dup}, ["instrument", "datetime"])
    with pytest.raises(GateContractError, match="duplicate keys in primary"):
        gate.merge({"tdx": pd.concat([primary, primary]), "tushare": secondary}, ["instrument", "datetime"])
    empty = primary.iloc[0:0]
    report = gate.merge({"tdx": empty, "tushare": secondary}, ["instrument", "datetime"])
    assert not report.accepted
    assert any("primary frame is empty" in e for e in report.errors)
