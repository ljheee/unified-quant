import copy
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore, UniverseSnapshotReader, UniverseSnapshotStore
from uq.contracts.factor_governance import FactorRegistry
from uq.errors import ContractError


ROOT = Path(__file__).resolve().parents[1]


def factor_set():
    return json.loads((ROOT / "config/factor-sets/basic-v1.json").read_text())


def manifest():
    fingerprint = "a" * 64
    return {
        "manifest_version": 1,
        "input_dataset": "bars_daily", "input_schema_version": "research-v1",
        "factor_set": "basic", "factor_version": "1.0.0", "partition_date": "2026-08-21",
        "decision_time": "2026-08-21T16:00:00+08:00", "run_visible_cutoff": "2026-08-21T16:00:00+08:00",
        "inputs": [{
            "binding": "bars", "dataset": "bars_daily", "schema_version": "research-v1",
            "partition_date": "2026-08-21", "manifest_generation_id": "b" * 64, "data_checksum_sha256": "c" * 64,
            "schema_checksum_sha256": None, "adjustment_snapshot_id": None, "effective_date_table_checksum": None,
        }],
        "factor_definitions": [
            {"name": item["name"], "version": item["version"], "implementation_fingerprint": item["implementation_fingerprint"]}
            for item in factor_set()["factors"]
        ],
        "universe_snapshot": None, "row_count": 1,
        "columns": ["instrument", "volume_ratio_20d"],
        "dtypes": {"instrument": "object", "volume_ratio_20d": "float64"},
        "data_checksum_sha256": "d" * 64, "logical_fingerprint": "e" * 64,
        "engine_version": "v0", "code_fingerprint": "f" * 64, "serialization_profile_id": "parquet-v1",
        "engine_package_provenance": {"project_version": "0.1.0", "python_version": "3.12", "dependency_lock_digest_sha256": "1" * 64},
        "run_id": "00000000-0000-4000-8000-000000000001", "created_at": "2026-08-21T16:00:00+08:00",
        "quality": {"status": "passed", "policy": "reject_all", "report_checksum_sha256": "2" * 64},
        "manifest_digest_sha256": "3" * 64, "generation_id": "4" * 64,
    }


def test_reviewed_basic_v1_registry_loads_and_manifest_matches():
    registry = FactorRegistry(ROOT)
    definition = registry.get("basic", "1.0.0")
    assert [item["name"] for item in definition.factors][0] == "volume_ratio_20d"
    registry.resolve_dependencies(definition)
    document = manifest()
    from uq.contracts.gate_contracts import factor_manifest_identities
    generation, digest = factor_manifest_identities({k:v for k,v in document.items() if k not in {"generation_id","manifest_digest_sha256"}})
    document.update(generation_id=generation, manifest_digest_sha256=digest)
    registry.validate_manifest(document)
    other = copy.deepcopy(document)
    other["run_id"] = "00000000-0000-4000-8000-000000000002"
    other["created_at"] = "2026-08-22T16:00:00+08:00"
    new_generation, new_digest = factor_manifest_identities({k:v for k,v in other.items() if k not in {"generation_id","manifest_digest_sha256"}})
    other.update(generation_id=new_generation, manifest_digest_sha256=new_digest)
    assert document["generation_id"] == other["generation_id"]
    assert document["manifest_digest_sha256"] != other["manifest_digest_sha256"]
    registry.validate_manifest(other)


@pytest.mark.parametrize("mutate,message", [
    (lambda x: x["factor_definitions"][0].update({"implementation_fingerprint": "9" * 64}), "reviewed set-version action"),
    (lambda x: x.update({"factor_set": "unknown"}), "unknown factor set/version"),
    (lambda x: x.update({"quality": {**x["quality"], "policy": "accept_with_warnings"}}), "quality policy"),
    (lambda x: x.update({"partition_date": "2026-02-31"}), "invalid factor governance calendar date"),
    (lambda x: x["factor_definitions"].pop(), "do not match reviewed factor set"),
    (lambda x: x.update({"run_visible_cutoff": "2026-08-20T16:00:00+08:00"}), "cutoff precedes decision"),
    (lambda x: x["dtypes"].pop("volume_ratio_20d"), "dtype map"),
])
def test_manifest_rejects_typed_malformed_cases(mutate, message):
    document = manifest(); mutate(document); _finalize(document)
    with pytest.raises(ContractError, match=message):
        FactorRegistry(ROOT).validate_manifest(document)


def test_manifest_identity_mutations_are_rejected():
    for field in ("generation_id", "manifest_digest_sha256"):
        document = manifest(); _finalize(document); document[field] = "9" * 64
        with pytest.raises(ContractError, match="mismatch"):
            FactorRegistry(ROOT).validate_manifest(document)
    document = manifest(); document["extra"] = True
    with pytest.raises(ContractError, match="mismatch|validation failed"):
        FactorRegistry(ROOT).validate_manifest(document)


def _finalize(document):
    from uq.contracts.gate_contracts import factor_manifest_identities
    generation, digest = factor_manifest_identities({k:v for k,v in document.items() if k not in {"generation_id","manifest_digest_sha256"}})
    document.update(generation_id=generation, manifest_digest_sha256=digest)


def universe_document(valid_from="2026-08-20"):
    return {
        "universe_version": 1, "universe_id": "research-whitelist", "source": "reviewed fixture",
        "snapshot_time": "2026-08-21T16:00:00+08:00", "visibility_time": "2026-08-21T16:00:00+08:00",
        "valid_from": valid_from, "valid_to": None,
        "members_artifact": {"path": "members.csv", "checksum_sha256": "0" * 64},
        "membership_evidence": "governed research whitelist",
    }


def test_universe_artifact_five_acceptance_paths(tmp_path):
    members = pd.DataFrame({"instrument": ["600000.XSHG"]})
    directory = UniverseSnapshotStore().save(tmp_path, universe_document(), members)
    generation_id = directory.name
    reader = UniverseSnapshotReader(tmp_path)
    assert reader.read("research-whitelist", generation_id, requested_valid_from=date(2026,8,20)).shape[0] == 1

    with pytest.raises(ContractError, match="missing or malformed universe snapshot"):
        reader.read("research-whitelist", "f" * 64, requested_valid_from=date(2026,8,20))
    (directory / "members.csv").write_text("instrument\n000001.XSHE\n")
    with pytest.raises(ContractError, match="tampered universe membership bytes"):
        reader.read("research-whitelist", generation_id, requested_valid_from=date(2026,8,20))

    outside = UniverseSnapshotStore().save(
        tmp_path / "outside", universe_document("2026-08-22"), pd.DataFrame({"instrument": ["600000.XSHG"]}))
    with pytest.raises(ContractError, match="outside PIT validity"):
        UniverseSnapshotReader(tmp_path / "outside").read(
            "research-whitelist", outside.name, requested_valid_from=date(2026,8,20))


def quality_report(generation="4" * 64):
    return {
        "report_version": 1, "binding_type": "factor_v1", "bound_generation_id": generation,
        "policy": "reject_all", "status": "passed",
        "checks": [{"name":"duplicate_keys","threshold":0,"observed":0,"level":"error","result":"passed"}],
        "errors": [], "warnings": [],
    }


def test_quality_report_missing_wrong_binding_and_tamper(tmp_path):
    store = QualityReportStore()
    directory = store.save(tmp_path, quality_report())
    assert store.read(tmp_path, "4" * 64, binding_type="factor_v1")["status"] == "passed"
    with pytest.raises(ContractError, match="missing quality report"):
        store.read(tmp_path, "5" * 64, binding_type="factor_v1")

    wrong = quality_report("6" * 64); wrong["binding_type"] = "canonical_v2"
    store.save(tmp_path, wrong)
    assert store.read(tmp_path, "6" * 64, binding_type="canonical_v2")["binding_type"] == "canonical_v2"
    wrong_taxonomy = quality_report("6" * 64); wrong_taxonomy["checks"][0]["name"] = "made_up"
    with pytest.raises(ContractError, match="unknown quality check taxonomy"):
        store.save(tmp_path, wrong_taxonomy)

    path = directory / "report.json"
    original = path.read_bytes()
    path.write_bytes(original.replace(b"passed", b"warning"))
    with pytest.raises(ContractError, match="tampered quality report bytes"):
        store.read(tmp_path, "4" * 64, binding_type="factor_v1")


def test_universe_rejects_invalid_calendar_and_traversal(tmp_path):
    bad = universe_document("2026-02-31")
    with pytest.raises(ContractError, match="is not a 'date'|invalid universe calendar date"):
        UniverseSnapshotStore().save(tmp_path, bad, pd.DataFrame({"instrument":["600000.XSHG"]}))

    bad_path = universe_document()
    bad_path["members_artifact"]["path"] = "../members.csv"
    with pytest.raises(ContractError, match="validation failed|path must be a filename"):
        UniverseSnapshotStore().save(tmp_path, bad_path, pd.DataFrame({"instrument":["600000.XSHG"]}))


def test_universe_same_root_pit_reuse_is_rejected(tmp_path):
    directory = UniverseSnapshotStore().save(tmp_path, universe_document("2026-08-22"), pd.DataFrame({"instrument":["600000.XSHG"]}))
    reader = UniverseSnapshotReader(tmp_path)
    with pytest.raises(ContractError, match="outside PIT validity"):
        reader.read("research-whitelist", directory.name, requested_valid_from=date(2026,8,21))


def test_quality_reader_rejects_wrong_bound_generation(tmp_path):
    store = QualityReportStore()
    store.save(tmp_path, quality_report("4" * 64))
    with pytest.raises(ContractError, match="missing quality report"):
        store.read(tmp_path, "7" * 64, binding_type="factor_v1")


def test_quality_reader_rejects_existing_wrong_bound_report(tmp_path):
    store = QualityReportStore()
    store.save(tmp_path, quality_report("4" * 64))
    directory = tmp_path / "reports" / "factor_v1" / ("4" * 64)
    path = directory / "report.json"
    document = __import__("json").loads(path.read_text())
    document["bound_generation_id"] = "8" * 64
    content = __import__("json").dumps(document, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(content)
    (directory / "report.sha256").write_text(__import__("hashlib").sha256(content).hexdigest() + "\n")
    with pytest.raises(ContractError, match="bound to another run"):
        store.read(tmp_path, "4" * 64, binding_type="factor_v1")
