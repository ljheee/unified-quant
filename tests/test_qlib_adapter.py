import json
import sys
import tempfile
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
from uq.factors.store import FactorStore, _validate_factor_frame, factor_generation, read_factor_partition


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
                    "volume": 1_000_000 * np.exp(generator.normal(0, 0.02, days)),
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
    definition = FactorRegistry(ROOT).get("alpha158", "1.0.0")
    report = {
        "report_version": 1,
        "binding_type": "factor_v1",
        "bound_generation_id": generation,
        "policy": "reject_all",
        "status": "passed",
        "checks": _validate_factor_frame(frame, definition)["checks"],
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


def test_forward_looking_expression_rejected():
    definition = json.loads(DEFINITION.read_text())
    definition["factors"][0]["expression"] = "Ref($close, - 1)"
    with pytest.raises(ContractError, match="forward-looking qlib expression"):
        QlibFactorAdapter._validate_document(definition)


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


def test_foreign_definition_path_rejected(tmp_path):
    outside = tmp_path / "alpha158-v1.json"
    outside.write_text(DEFINITION.read_text(), encoding="utf-8")
    with pytest.raises(ContractError, match="outside the reviewed registry"):
        QlibFactorAdapter(outside)


def test_alpha360_contract_only_not_computable():
    definition = json.loads((ROOT / "config/factor-sets/alpha360-v1.json").read_text())
    definition["status"] = "reviewed"
    assert not definition["factors"]
    with pytest.raises(ContractError, match="no reviewed factors"):
        QlibFactorAdapter._validate_document(definition)


def test_engine_version_mismatch_rejected(monkeypatch):
    qlib = pytest.importorskip("qlib")

    monkeypatch.setattr(qlib, "__version__", "0.0.0-test", raising=False)
    with pytest.raises(ContractError, match="not reviewed"):
        _adapter()._validate_engine_version()


def test_incomplete_qlib_panel_rejected():
    panel, dates, instruments = _synthetic_panel(days=70)
    incomplete = panel.drop(index=0)
    with pytest.raises(ContractError, match="not a complete instrument/date panel"):
        _adapter()._validate_request(
            incomplete,
            list(instruments),
            str(dates[-1].date()),
            str(dates[-1].date()),
        )


def test_minimum_qlib_history_rejected():
    panel, dates, instruments = _synthetic_panel(days=59)
    with pytest.raises(ContractError, match="shorter than reviewed minimum_history"):
        _adapter()._validate_request(
            panel,
            list(instruments),
            str(dates[-1].date()),
            str(dates[-1].date()),
        )


def test_null_and_nonfinite_qlib_inputs_rejected():
    panel, dates, instruments = _synthetic_panel(days=70)
    with pytest.raises(ContractError, match="null required values"):
        null_panel = panel.copy()
        null_panel.loc[null_panel.index[0], "close"] = None
        _adapter()._validate_request(
            null_panel,
            list(instruments),
            str(dates[-1].date()),
            str(dates[-1].date()),
        )
    with pytest.raises(ContractError, match="non-finite"):
        infinite_panel = panel.copy()
        infinite_panel.loc[infinite_panel.index[0], "close"] = np.inf
        _adapter()._validate_request(
            infinite_panel,
            list(instruments),
            str(dates[-1].date()),
            str(dates[-1].date()),
        )


def test_nonfinite_qlib_result_rejected():
    pytest.importorskip("qlib")
    panel, dates, instruments = _synthetic_panel(days=70)
    result = _adapter().compute(
        panel,
        instruments=list(instruments),
        start_date=str(dates[-1].date()),
        end_date=str(dates[-1].date()),
    )
    result.loc[result.index[0], result.columns[-1]] = np.inf
    with pytest.raises(ContractError, match="non-finite"):
        _adapter()._reconcile_result(
            result,
            panel,
            list(instruments),
            str(dates[-1].date()),
            str(dates[-1].date()),
        )


@pytest.mark.parametrize("days", [70])
def test_qlib_process_isolation_and_temp_cleanup(monkeypatch, days):
    pytest.importorskip("qlib")
    import qlib.data

    captured: dict[str, Path] = {}

    class CapturingTemporaryDirectory(tempfile.TemporaryDirectory):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["root"] = Path(self.name)

    monkeypatch.setattr(
        "uq.factors.qlib_adapter.tempfile.TemporaryDirectory",
        CapturingTemporaryDirectory,
    )
    import subprocess

    original_run = subprocess.run
    commands: list[list[str]] = []

    def capturing_run(command, *args, **kwargs):
        commands.append(list(command))
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(
        "uq.factors.qlib_adapter.subprocess.run",
        capturing_run,
    )
    panel, dates, instruments = _synthetic_panel(days=days)
    result = _adapter().compute(
        panel,
        instruments=list(instruments),
        start_date=str(dates[-1].date()),
        end_date=str(dates[-1].date()),
    )
    assert result.columns.tolist() == ["instrument", "datetime", *_adapter().factor_names]
    assert commands == [[sys.executable, "-m", "uq.factors.qlib_adapter", str(captured["root"] / "payload.json")]]
    assert not captured["root"].exists()
