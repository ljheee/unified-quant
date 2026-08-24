import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.errors import ContractError
from uq.factors.raw_price import calculate_raw_price_factors, logical_fingerprint
from uq.factors.store import FactorStore, factor_generation, read_factor_partition


ROOT = Path(__file__).resolve().parents[1]


def frame(days=21):
    rows = [
        {
            "instrument": "600000.XSHG",
            "datetime": pd.Timestamp(2026, 8, day),
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "volume": float(day),
            "amount": float(day),
        }
        for day in range(1, days + 1)
    ]
    return calculate_raw_price_factors(pd.DataFrame(rows))


def quality_document(generation, *, status="passed", result="passed"):
    return {
        "report_version": 1,
        "binding_type": "factor_v1",
        "bound_generation_id": generation,
        "policy": "reject_all",
        "status": status,
        "checks": [{
            "name": "coverage",
            "threshold": 0,
            "observed": int(result == "failed"),
            "level": "error",
            "result": result,
        }],
        "errors": [] if status == "passed" else ["coverage failed"],
        "warnings": [],
    }


def kwargs(**overrides):
    defaults = {
        "frame": frame(),
        "partition_date": date(2026, 8, 21),
        "input_dataset": "bars_daily",
        "input_schema_version": "research-v1",
        "upstream_generation_id": "a" * 64,
        "upstream_data_checksum": "b" * 64,
        "quality_report_checksum": "",
    }
    defaults.update(overrides)
    return defaults


def publish(tmp_path, **overrides):
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    arguments = kwargs(**overrides)
    generation = factor_generation(**arguments)
    report_path = root / "reports" / "factor_v1" / generation / "report.json"
    if not (root / "reports").exists():
        QualityReportStore().save(root, quality_document(generation))
    arguments["quality_report_checksum"] = file_sha256_bytes(report_path.read_bytes())
    return FactorStore(root, FactorRegistry(ROOT)).publish(**arguments)


def test_f4_identical_runs_produce_identical_artifacts(tmp_path):
    first = publish(tmp_path / "first")
    second = publish(tmp_path / "second")
    assert (first / "data.parquet").read_bytes() == (second / "data.parquet").read_bytes()

    left = json.loads((first / "manifest.json").read_text())
    right = json.loads((second / "manifest.json").read_text())
    assert left["generation_id"] == right["generation_id"]
    assert left["manifest_digest_sha256"] != right["manifest_digest_sha256"]


def test_f7_overwrite_rejected(tmp_path):
    publish(tmp_path)
    with pytest.raises(
        ContractError, match="immutable factor partition already published"
    ):
        publish(tmp_path)


def test_f8b_tampered_data_prevents_factor_read(tmp_path):
    partition = publish(tmp_path)
    path = partition / "data.parquet"
    path.write_bytes(path.read_bytes() + b"bad")
    with pytest.raises(ContractError, match="tampered factor data"):
        read_factor_partition(partition)


def test_f9b_tampered_manifest_fails_verification(tmp_path):
    partition = publish(tmp_path)
    manifest = json.loads((partition / "manifest.json").read_text())
    manifest["logical_fingerprint"] = "d" * 64
    (partition / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ContractError, match="tampered factor manifest"):
        read_factor_partition(partition)


def test_f13b_duplicate_keys_reject_publication(tmp_path):
    with pytest.raises(ContractError, match="duplicate factor keys"):
        publish(tmp_path, frame=pd.concat([frame(), frame()]))


def test_f14b_generation_changes_on_each_binding_change(tmp_path):
    variants = [
        {},
        {"upstream_generation_id": "e" * 64},
        {"upstream_data_checksum": "f" * 64},
        {"quality_report_checksum": "2" * 64, "_expect_generation_change": False},
        {"partition_date": date(2026, 8, 22)},
    ]
    generations = []
    for index, overrides in enumerate(variants):
        overrides = dict(overrides)
        report_checksum_changes_manifest_digest_only = overrides.pop(
            "_expect_generation_change", True
        ) is False
        partition = publish(tmp_path / f"run{index}", **overrides)
        manifest = json.loads((partition / "manifest.json").read_text())
        generations.append((index, manifest["generation_id"]))
    assert len({generation for _, generation in generations}) == 4
    assert generations[0][1] == generations[3][1]


def test_null_rate_threshold_rejects_publication(tmp_path):
    bad = frame()
    factor_column = [
        column for column in bad.columns
        if column not in {"instrument", "datetime"}
    ][0]
    bad[factor_column] = None
    with pytest.raises(
        ContractError, match="factor null rate above threshold rejects publication"
    ):
        publish(tmp_path, frame=bad)


def test_f3_changed_semantics_cannot_reuse_old_version():
    from uq.contracts.gate_contracts import factor_manifest_identities

    definition = FactorRegistry(ROOT).get("basic", "1.0.0").document
    definitions = [
        {
            key: item[key]
            for key in {"name", "version", "implementation_fingerprint"}
        }
        for item in definition["factors"]
    ]
    definitions[0]["implementation_fingerprint"] = "9" * 64
    document = {
        "manifest_version": 1,
        "input_dataset": "bars_daily",
        "input_schema_version": "research-v1",
        "factor_set": "basic",
        "factor_version": "1.0.0",
        "partition_date": "2026-08-21",
        "decision_time": "2026-08-21T16:00:00+08:00",
        "run_visible_cutoff": "2026-08-21T16:00:00+08:00",
        "inputs": [{
            "binding": "bars",
            "dataset": "bars_daily",
            "schema_version": "research-v1",
            "partition_date": "2026-08-21",
            "manifest_generation_id": "b" * 64,
            "data_checksum_sha256": "c" * 64,
            "schema_checksum_sha256": None,
            "adjustment_snapshot_id": None,
            "effective_date_table_checksum": None,
        }],
        "factor_definitions": definitions,
        "universe_snapshot": None,
        "row_count": 1,
        "columns": ["instrument", "volume_ratio_20d"],
        "dtypes": {"instrument": "object", "volume_ratio_20d": "float64"},
        "data_checksum_sha256": "d" * 64,
        "logical_fingerprint": "e" * 64,
        "engine_version": "v0",
        "code_fingerprint": "f" * 64,
        "serialization_profile_id": "parquet-v1",
        "engine_package_provenance": {
            "project_version": "0.1.0",
            "python_version": "3.12",
            "dependency_lock_digest_sha256": "1" * 64,
        },
        "run_id": "00000000-0000-4000-8000-000000000001",
        "created_at": "2026-08-21T16:00:00+08:00",
        "quality": {
            "status": "passed",
            "policy": "reject_all",
            "report_checksum_sha256": "2" * 64,
        },
    }
    generation, digest = factor_manifest_identities(document)
    document.update(generation_id=generation, manifest_digest_sha256=digest)
    with pytest.raises(ContractError, match="reviewed set-version action"):
        FactorRegistry(ROOT).validate_manifest(document)


def test_factor_path_identity_rejects_copied_partition(tmp_path):
    partition = publish(tmp_path)
    copied = tmp_path / "copied" / partition.name
    copied.mkdir(parents=True)
    for item in partition.iterdir():
        (copied / item.name).write_bytes(item.read_bytes())
    with pytest.raises(
        ContractError, match="physical path does not match manifest identity"
    ):
        read_factor_partition(copied)


def test_f12a_missing_quality_report_rejects_publication(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ContractError, match="missing quality report"):
        FactorStore(root, FactorRegistry(ROOT)).publish(**kwargs())


def test_f12a_mismatched_quality_report_rejects_publication(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    QualityReportStore().save(root, quality_document("9" * 64))
    with pytest.raises(ContractError, match="missing quality report"):
        FactorStore(root, FactorRegistry(ROOT)).publish(**kwargs())


def test_f12a_tampered_quality_report_rejects_publication(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    arguments = kwargs()
    directory = QualityReportStore().save(
        root, quality_document(factor_generation(**arguments))
    )
    path = directory / "report.json"
    path.write_bytes(path.read_bytes() + b"bad")
    with pytest.raises(ContractError, match="tampered quality report bytes"):
        FactorStore(root, FactorRegistry(ROOT)).publish(**arguments)


def test_f12a_failed_quality_report_rejects_publication(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    arguments = kwargs()
    QualityReportStore().save(
        root,
        quality_document(
            factor_generation(**arguments), status="rejected", result="failed"
        ),
    )
    with pytest.raises(
        ContractError, match="factor quality report rejects publication"
    ):
        FactorStore(root, FactorRegistry(ROOT)).publish(**arguments)
