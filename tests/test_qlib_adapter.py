import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.contracts.factor_governance import FactorRegistry
from uq.errors import ContractError
from uq.factors.qlib_adapter import QlibFactorAdapter, QlibNotInstalledError
from uq.factors.store import FactorStore, factor_generation, read_factor_partition


ROOT = Path(__file__).resolve().parents[1]
DEFINITION = ROOT / "config/factor-sets/alpha158-v1.json"

def _synthetic_panel(days: int = 300, instruments: tuple[str, ...] = ("600000.XSHG", "000001.XSHE")):
    dates = pd.bdate_range("2024-01-02", periods=days)
    frames = []
    for instrument_index, instrument in enumerate(instruments):
        generator = np.random.default_rng(42 + instrument_index)
        close = np.cumprod(1 + generator.normal(0, 0.01, days)) * 10
        frames.append(
            pd.DataFrame(
                {
                    "instrument": instrument,
                    "datetime": dates,
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": np.linspace(1_000_000, 2_000_000, days),
                }
            )
        )
    return pd.concat(frames, ignore_index=True), dates, instruments


def _adapter() -> QlibFactorAdapter:
    return QlibFactorAdapter(DEFINITION)


def _publish_one(result: pd.DataFrame, root: Path, partition_date: str) -> Path:
    frame = result[result["datetime"] == partition_date].reset_index(drop=True)
    arguments = {
        "factor_set": "alpha158",
        "factor_version": "1.0.0",
        "frame": frame,
        "partition_date": date.fromisoformat(partition_date),
        "input_dataset": "bars_daily",
        "input_schema_version": "research-v1",
        "upstream_generation_id": "a" * 64,
        "upstream_data_checksum": "b" * 64,
        "quality_report_checksum": "0" * 64,
        "upstream_created_at": datetime.fromisoformat("2024-01-01T16:00:00+08:00"),
    }
    generation = factor_generation(**arguments)
    report = {
        "report_version": 1,
        "binding_type": "factor_v1",
        "bound_generation_id": generation,
        "policy": "reject_all",
        "status": "passed",
        "checks": [
            {
                "name": "null_rate",
                "threshold": 0.2,
                "observed": float(frame.drop(columns=["instrument", "datetime"]).isna().to_numpy().mean()),
                "level": "error",
                "result": "passed",
            },
            {
                "name": "coverage",
                "threshold": 0.8,
                "observed": 1.0 - float(frame.drop(columns=["instrument", "datetime"]).isna().to_numpy().mean()),
                "level": "error",
                "result": "passed",
            },
        ],
        "errors": [],
        "warnings": [],
    }
    QualityReportStore().save(root, report)
    arguments["quality_report_checksum"] = file_sha256_bytes((root / "reports" / "factor_v1" / generation / "report.json").read_bytes())
    return FactorStore(root, FactorRegistry(ROOT)).publish(**arguments)


def test_factor_set_definition_valid():
    registry = FactorRegistry(ROOT)
    definition = registry.get("alpha158", "1.0.0")
    assert definition.factor_set == "alpha158"
    assert len(definition.factors) == 157
    assert len({factor["name"] for factor in definition.factors}) == 157


def test_alpha158_factor_names_deterministic():
    adapter = _adapter()
    expected = sorted(factor["name"] for factor in adapter.definition["factors"])
    assert adapter.factor_names == expected


def test_name_mapping_deterministic():
    factors = _adapter().definition["factors"]
    qlib_names = [factor["qlib_name"] for factor in factors]
    uq_names = [factor["name"] for factor in factors]
    assert len(qlib_names) == len(set(qlib_names))
    assert uq_names == [f"qlib_{name.lower()}" for name in qlib_names]


def test_lookahead_audit_excludes_forward_looking():
    import re

    pattern = re.compile(r"Ref\s*\(\s*\$[A-Za-z_][A-Za-z0-9_]*\s*,\s*([+-]?\d+)")
    for factor in _adapter().definition["factors"]:
        assert all(int(value) > 0 for value in pattern.findall(factor["expression"]))
        assert "$vwap" not in factor["expression"].lower()


def test_vwap_factors_excluded():
    definition = json.loads(DEFINITION.read_text())
    excluded = {factor["name"] for factor in definition["excluded_factors"]}
    included = {factor["qlib_name"] for factor in definition["factors"]}
    assert "VWAP0" in excluded
    assert not excluded & included


def test_adapter_code_fingerprint_is_stable():
    assert len(QlibFactorAdapter._fingerprint()) == 64


def test_qlib_import_guard_raises_without_qlib(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "qlib", None)
    panel, dates, instruments = _synthetic_panel(days=70)
    with pytest.raises(QlibNotInstalledError):
        _adapter().compute(
            panel,
            instruments=list(instruments),
            start_date=str(dates[0].date()),
            end_date=str(dates[-1].date()),
        )


def test_forward_looking_expression_rejected(tmp_path):
    definition = json.loads(DEFINITION.read_text())
    definition["factors"][0]["expression"] = "Ref($close, -1)"
    path = tmp_path / "forward.json"
    path.write_text(json.dumps(definition))
    with pytest.raises(ContractError, match="forward-looking qlib expression"):
        QlibFactorAdapter(path)


@pytest.mark.parametrize("days", [300])
def test_adapter_computes_alpha158_factors(days):
    pytest.importorskip("qlib")
    panel, dates, instruments = _synthetic_panel(days=days)
    result = _adapter().compute(
        panel,
        instruments=list(instruments),
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
    )
    assert result.columns.tolist() == ["instrument", "datetime", *_adapter().factor_names]
    assert len(result) == days * len(instruments)
    assert result["instrument"].isin(instruments).all()


def test_per_date_partition_slicing():
    pytest.importorskip("qlib")
    panel, dates, instruments = _synthetic_panel(days=70)
    result = _adapter().compute(
        panel,
        instruments=list(instruments),
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
    )
    partitions = _adapter().partition_frames(result)
    assert [partition_date for partition_date, _ in partitions] == [str(value.date()) for value in dates]
    assert sum(len(frame) for _, frame in partitions) == len(result)


def test_governance_manifest_generation(tmp_path):
    pytest.importorskip("qlib")
    panel, dates, instruments = _synthetic_panel()
    result = _adapter().compute(
        panel,
        instruments=list(instruments),
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
    )
    partition = _publish_one(result, tmp_path, str(dates[-1].date()))
    manifest = json.loads((partition / "manifest.json").read_text())
    FactorRegistry(ROOT).validate_manifest(manifest)
    assert manifest["manifest_version"] == 2
    assert manifest["engine_contract"]["engine_name"] == "qlib"
    assert manifest["engine_contract"]["engine_version"]
    assert manifest["engine_contract"]["qlib_expression_set"] == "Alpha158"


def test_quality_report_binding(tmp_path):
    pytest.importorskip("qlib")
    panel, dates, instruments = _synthetic_panel()
    result = _adapter().compute(
        panel,
        instruments=list(instruments),
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
    )
    partition = _publish_one(result, tmp_path, str(dates[-1].date()))
    manifest = json.loads((partition / "manifest.json").read_text())
    report = QualityReportStore().read(tmp_path, manifest["generation_id"], binding_type="factor_v1")
    assert report["status"] == "passed"
    assert manifest["quality"]["report_checksum_sha256"]


def test_e2e_publish_read(tmp_path):
    pytest.importorskip("qlib")
    panel, dates, instruments = _synthetic_panel()
    result = _adapter().compute(
        panel,
        instruments=list(instruments),
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
    )
    partition = _publish_one(result, tmp_path, str(dates[-1].date()))
    frame = read_factor_partition(partition)
    assert frame.columns.tolist() == ["instrument", "datetime", *_adapter().factor_names]


def test_deterministic_generation_id(tmp_path):
    pytest.importorskip("qlib")
    panel, dates, instruments = _synthetic_panel()
    result = _adapter().compute(
        panel,
        instruments=list(instruments),
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
    )
    generations = []
    for root in [tmp_path / "one", tmp_path / "two"]:
        partition = _publish_one(result, root, str(dates[-1].date()))
        generations.append(json.loads((partition / "manifest.json").read_text())["generation_id"])
    assert generations[0] == generations[1]


def test_tampered_partition_rejects_read(tmp_path):
    pytest.importorskip("qlib")
    panel, dates, instruments = _synthetic_panel()
    result = _adapter().compute(
        panel,
        instruments=list(instruments),
        start_date=str(dates[0].date()),
        end_date=str(dates[-1].date()),
    )
    partition = _publish_one(result, tmp_path, str(dates[-1].date()))
    data_path = partition / "data.parquet"
    data_path.write_bytes(data_path.read_bytes() + b"tampered")
    with pytest.raises(ContractError, match="tampered factor data"):
        read_factor_partition(partition)
