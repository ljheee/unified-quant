import copy
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.errors import ContractError
from uq.factors.raw_price import calculate_raw_price_factors, logical_fingerprint
from uq.factors.store import FactorStore, factor_generation, quarantine_rejected, read_factor_partition


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
        "checks": [
            {"name": "null_rate", "threshold": 0.5, "observed": 1.0 if status == "rejected" else 0.0, "level": "error", "result": "failed" if status == "rejected" else "passed"},
            {"name": "coverage", "threshold": 0.0, "observed": 0.0 if status == "rejected" else 1.0, "level": "error", "result": result},
        ],
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
        "upstream_created_at": datetime.fromisoformat("2026-08-20T16:00:00+08:00"),
    }
    defaults.update(overrides)
    return defaults


def local_checks(arguments):
    value_columns = [column for column in arguments["frame"].columns if column not in {"instrument", "datetime"}]
    observed = float(arguments["frame"][value_columns].isna().to_numpy().mean())
    return [
        {"name": "null_rate", "threshold": 0.5, "observed": observed, "level": "error", "result": "failed" if observed > 0.5 else "passed"},
    ]


def publish(tmp_path, **overrides):
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    arguments = kwargs(**overrides)
    generation = factor_generation(**arguments)
    report_path = root / "reports" / "factor_v1" / generation / "report.json"
    if not (root / "reports").exists():
        QualityReportStore().save(root, {**quality_document(generation), "checks": [*local_checks(arguments), *quality_document(generation)["checks"]]})
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
        {"partition_date": date(2026, 8, 22)},
        {"upstream_created_at": datetime.fromisoformat("2026-08-20T17:00:00+08:00")},
    ]
    results = []
    for index, overrides in enumerate(variants):
        partition = publish(tmp_path / f"run{index}", **overrides)
        manifest = json.loads((partition / "manifest.json").read_text())
        results.append(manifest["generation_id"])
    assert len(set(results)) == 5


def test_quality_report_checksum_changes_manifest_digest_only():
    from uq.contracts.gate_contracts import factor_manifest_identities

    document = {
        "stable": "same",
        "quality": {"report_checksum_sha256": "1" * 64},
    }
    assert factor_manifest_identities(document)[0] == factor_manifest_identities({
        **document,
        "run_id": "run-a",
        "created_at": "created-a",
    })[0]
    left_generation, left_digest = factor_manifest_identities(document)
    document["quality"]["report_checksum_sha256"] = "2" * 64
    right_generation, right_digest = factor_manifest_identities(document)
    assert left_generation == right_generation
    assert left_digest != right_digest


def test_null_rate_threshold_rejects_publication(tmp_path):
    bad = frame()
    factor_column = [
        column for column in bad.columns
        if column not in {"instrument", "datetime"}
    ][0]
    bad[factor_column] = None
    root = tmp_path / "root"
    root.mkdir()
    arguments = kwargs(frame=bad)
    QualityReportStore().save(
        root,
        {
            **quality_document(factor_generation(**arguments), status="rejected", result="passed"),
            "bound_generation_id": factor_generation(**arguments),
            "errors": ["null rate threshold failed"],
            "checks": local_checks(arguments) + [{"name": "coverage", "threshold": 0.0, "observed": 1.0, "level": "error", "result": "passed"}],
        },
    )
    with pytest.raises(ContractError, match="factor quality report rejects publication"):
        FactorStore(root, FactorRegistry(ROOT)).publish(**arguments)


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
            "upstream_created_at": "2026-08-21T16:00:00+08:00",
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


def test_freshness_violation_rejects_publication(tmp_path):
    arguments = kwargs(upstream_created_at=datetime.fromisoformat("2026-08-22T17:00:00+08:00"))
    root = tmp_path / "root"
    root.mkdir()
    QualityReportStore().save(
        root,
        quality_document(factor_generation(**arguments)),
    )
    with pytest.raises(ContractError, match="upstream partition visibility violates factor run cutoff"):
        FactorStore(root, FactorRegistry(ROOT)).publish(**arguments)


def test_quarantine_is_not_an_accepted_factor_partition(tmp_path):
    from uq.factors.store import read_quarantine_manifest

    rejected = pd.DataFrame({"instrument": ["600000.XSHG"], "datetime": [pd.Timestamp(2026, 8, 21)], "value": [1.0]})
    directory = quarantine_rejected(tmp_path, rejected, "quality rejection", operator="release-gate")
    manifest = read_quarantine_manifest(directory)
    assert manifest["reason"] == "quality rejection"
    assert manifest["operator"] == "release-gate"
    assert manifest["retention_policy"] == "manual-review; no automatic accepted promotion"
    with pytest.raises((FileNotFoundError, ContractError)):
        read_factor_partition(directory)


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


def test_warning_policy_allows_bound_warning_report(tmp_path):
    from uq.contracts.gate_contracts import factor_manifest_identities

    root = tmp_path / "root"
    root.mkdir()
    arguments = kwargs()
    generation = factor_generation(**arguments)
    report = quality_document(generation, status="warning")
    report["policy"] = "accept_with_warnings"
    report["checks"][0]["level"] = "warning"
    QualityReportStore().save(root, report)
    arguments["quality_report_checksum"] = file_sha256_bytes(
        (root / "reports/factor_v1" / generation / "report.json").read_bytes()
    )
    manifest = _manifest_without_identities_for_test(arguments)
    assert manifest["generation_id"] == generation
    with pytest.raises(ContractError, match="factor quality report rejects publication"):
        FactorStore(root, FactorRegistry(ROOT)).publish(**arguments)


def _manifest_without_identities_for_test(arguments):
    from uq.contracts.gate_contracts import factor_manifest_identities
    from uq.factors.store import _manifest_without_identities, serialize_factor_frame
    from uq.contracts.factor_governance import FactorRegistry

    definition = FactorRegistry(ROOT).get("basic", "1.0.0")
    _, checksum = serialize_factor_frame(arguments["frame"])
    unsigned = _manifest_without_identities(
        local_quality={"status": "passed", "policy": "reject_all"},
        frame=arguments["frame"],
        partition_date=arguments["partition_date"],
        input_dataset=arguments["input_dataset"],
        input_schema_version=arguments["input_schema_version"],
        upstream_generation_id=arguments["upstream_generation_id"],
        upstream_data_checksum=arguments["upstream_data_checksum"],
        quality_report_checksum=arguments["quality_report_checksum"],
        adjustment_snapshot_id=arguments.get("adjustment_snapshot_id"),
        effective_date_table_checksum=arguments.get("effective_date_table_checksum"),
        upstream_created_at=arguments["upstream_created_at"],
        definition=definition,
        data_checksum=checksum,
    )
    generation, digest = factor_manifest_identities(unsigned)
    return {**unsigned, "generation_id": generation, "manifest_digest_sha256": digest}


def test_warning_policy_publishes_and_reads_bound_warning_report(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    arguments = kwargs()
    definition = FactorRegistry(ROOT).get("basic", "1.0.0")
    document = copy.deepcopy(definition.document)
    document["quality_policy"] = "accept_with_warnings"

    class WarningRegistry(FactorRegistry):
        def get(self, factor_set, factor_version):
            return type(definition)(document)

    monkeypatch.setattr("uq.contracts.factor_governance.FactorRegistry.get", WarningRegistry.get)
    generation = factor_generation(**arguments)
    report = quality_document(generation)
    report["policy"] = "accept_with_warnings"
    report["status"] = "passed"
    QualityReportStore().save(root, {**report, "checks": [*local_checks(arguments), *report["checks"]]})
    arguments["quality_report_checksum"] = file_sha256_bytes(
        (root / "reports/factor_v1" / generation / "report.json").read_bytes()
    )
    store = FactorStore(root, WarningRegistry(ROOT))
    partition = store.publish(**arguments)
    frame = read_factor_partition(partition)
    assert len(frame) == len(arguments["frame"])


def test_coverage_threshold_rejects_publication(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    arguments = kwargs(frame=frame(days=2))
    QualityReportStore().save(
        root,
        {
            **quality_document(factor_generation(**arguments)),
            "status": "rejected",
            "errors": ["coverage threshold failed"],
            "checks": local_checks(arguments) + [{"name": "coverage", "threshold": 1.0, "observed": 0.5, "level": "error", "result": "failed"}],
        },
    )
    with pytest.raises(ContractError, match="factor quality report rejects publication"):
        FactorStore(root, FactorRegistry(ROOT)).publish(**arguments)


def test_local_and_bound_quality_checks_must_agree(tmp_path):
    bad = frame()
    bad[bad.columns[-1]] = None
    root = tmp_path / "root"
    root.mkdir()
    arguments = kwargs(frame=bad)
    generation = factor_generation(**arguments)
    local = [check for check in local_checks(arguments) if check["name"] != "null_rate"]
    local.append({"name": "null_rate", "threshold": 0.5, "observed": 1.0, "level": "error", "result": "passed"})
    report = quality_document(generation)
    QualityReportStore().save(root, {**report, "checks": [*local, *report["checks"]]})
    with pytest.raises(ContractError, match="factor quality report rejects publication"):
        FactorStore(root, FactorRegistry(ROOT)).publish(**arguments)


def test_quarantine_tampered_data_and_manifest_fail_closed(tmp_path):
    from uq.factors.store import read_quarantine_manifest

    rejected = pd.DataFrame({"instrument": ["A"], "value": [1.0]})
    directory = quarantine_rejected(tmp_path, rejected, "bad")
    path = directory / "rejected.parquet"
    original = path.read_bytes()
    path.write_bytes(original + b"bad")
    with pytest.raises(ContractError, match="tampered quarantine data"):
        read_quarantine_manifest(directory)
    path.write_bytes(original)

    manifest_path = directory / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_path.write_bytes(manifest_bytes.replace(b"bad", b"evil"))
    with pytest.raises(ContractError, match="tampered quarantine manifest"):
        read_quarantine_manifest(directory)
