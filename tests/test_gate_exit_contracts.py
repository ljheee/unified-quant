import copy
import json
from pathlib import Path

import pytest

from uq.contracts.gate_contracts import (
    validate_contract_path,
    adjustment_snapshot_generation,
    canonical_v2_identities,
    sha256_bytes,
    validate_contract,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "config/schemas/fixtures"


def test_canonical_v2_identity_excludes_run_metadata():
    fixture = json.loads((FIXTURES / "canonical-v2-golden.json").read_text())
    first = copy.deepcopy(fixture["without_digests"])
    second = copy.deepcopy(first)
    second.update(fixture["same_content_different_run"])
    generation_one, digest_one = canonical_v2_identities(first)
    generation_two, digest_two = canonical_v2_identities(second)
    assert generation_one == generation_two
    assert digest_one != digest_two

    third = copy.deepcopy(first)
    third["quality_report_checksum"] = "9" * 64
    generation_three, digest_three = canonical_v2_identities(third)
    assert generation_one == generation_three
    assert digest_one != digest_three
    validate_contract("canonical_manifest.v2.json", {
        **first, "generation_id": generation_one, "manifest_digest_sha256": digest_one,
        "trust_anchor_sha256": sha256_bytes(generation_one.encode("ascii")),
    })


def test_canonical_migration_contract_and_negative_fields():
    valid = {
        "migration_version": 1, "action": "republish_v2",
        "source_dataset": "bars_daily", "source_schema_version": "v1",
        "source_partition_path": "canonical/bars_daily/v1/date=2026-08-21",
        "source_partition_date": "2026-08-21",
        "source_data_checksum_sha256": "a" * 64,
        "source_schema_checksum_sha256": "b" * 64,
        "source_legacy_generation_id": "c" * 64,
        "source_manifest_digest_sha256": "d" * 64,
        "target_dataset": "bars_daily", "target_schema_version": "v2",
        "target_partition_path": "canonical/bars_daily/v2/date=2026-08-21",
        "target_content_generation_id": "e" * 64,
        "target_manifest_digest_sha256": "f" * 64,
        "migration_algorithm_version": "canonical-migration.v1",
        "decision_time": "2026-08-23T00:00:00Z",
        "run_visible_cutoff": "2026-08-23T01:00:00Z", "operator": "reviewer",
        "approval_reference": "gate-plan-v0.7", "mapping_checksum_sha256": "1" * 64,
    }
    validate_contract("canonical_migration.v1.json", valid)
    for field in ("source_data_checksum_sha256", "target_content_generation_id", "approval_reference"):
        invalid = dict(valid); invalid.pop(field)
        with pytest.raises(Exception):
            validate_contract("canonical_migration.v1.json", invalid)


def test_adjustment_snapshot_generation_is_stable():
    payload = json.loads((FIXTURES / "adjustment-snapshot-v1-valid.json").read_text())
    payload["generation_id"] = adjustment_snapshot_generation(payload)
    validate_contract("adjustment_snapshot.v1.json", payload)
    assert payload["generation_id"] == adjustment_snapshot_generation(payload)


def test_factor_manifest_contract():
    manifest = json.loads((FIXTURES / "factor-manifest-v1-valid.json").read_text())
    validate_contract_path(ROOT / "config/schemas/manifests/factor_manifest.v1.json", manifest)
    assert manifest["inputs"][0]["upstream_created_at"] <= manifest["run_visible_cutoff"]
    invalid = dict(manifest); invalid["extra"] = True
    with pytest.raises(Exception):
        validate_contract("factor_manifest.v1.json", invalid)


def test_factor_set_definition_contract():
    from uq.contracts.factor_governance import _validate_factor_set

    valid = json.loads((ROOT / "config/factor-sets/basic-v1.json").read_text())
    _validate_factor_set(valid)
    for case in (
        {"status": "maybe"},
        {"quality_policy": "maybe"},
        {"dependencies": ["basic/1.0"]},
        {"factors": [{**valid["factors"][0], "minimum_history": 0}]},
        {"factors": [{**valid["factors"][0], "implementation_fingerprint": "short"}]},
    ):
        with pytest.raises(Exception):
            _validate_factor_set({**valid, **case})


def test_universe_and_quality_contracts():
    universe = {
        "universe_version": 1, "universe_id": "research-whitelist",
        "source": "config/universe/research-whitelist.txt",
        "snapshot_time": "2026-08-23T00:00:00Z", "visibility_time": "2026-08-23T00:00:00Z",
        "valid_from": "2026-08-21", "valid_to": None,
        "members_artifact": {"path": "members.csv", "checksum_sha256": "a" * 64},
        "membership_evidence": "static research whitelist; not PIT index membership",
        "generation_id": "b" * 64,
    }
    quality = {
        "report_version": 1, "binding_type": "factor_v1", "bound_generation_id": "c" * 64,
        "policy": "reject_all", "status": "passed",
        "checks": [{"name":"duplicate_keys","threshold":0,"observed":0,"level":"error","result":"passed"}],
        "errors": [], "warnings": [],
    }
    validate_contract("universe_snapshot.v1.json", universe)
    validate_contract("quality_report.v1.json", quality)


def test_all_contract_negative_fixtures():
    cases = json.loads((FIXTURES / "contract-negative-cases.json").read_text())
    provenance = json.loads((FIXTURES / "adjustment/golden-provenance.v1.json").read_text())
    valid_payloads = {
        "factor_manifest.v1.json": json.loads((FIXTURES / "factor-manifest-v1-valid.json").read_text()),
        "canonical_manifest.v2.json": _canonical_v2_valid(),
        "canonical_migration.v1.json": _migration_valid(),
        "adjustment_snapshot.v1.json": json.loads((FIXTURES / "adjustment-snapshot-v1-valid.json").read_text()),
        "factor_manifest.v1.json": json.loads((FIXTURES / "factor-manifest-v1-valid.json").read_text()),
        "universe_snapshot.v1.json": _universe_valid(),
        "quality_report.v1.json": _quality_valid(),
        "adjustment_golden_provenance.v1.json": provenance,
    }
    for schema_name, invalid_cases in cases.items():
        base = valid_payloads[schema_name]
        if schema_name == "adjustment_snapshot.v1.json":
            base = copy.deepcopy(base)
            base.pop("generation_id", None)
            base["generation_id"] = adjustment_snapshot_generation(base)
        for case in invalid_cases:
            payload = copy.deepcopy(base)
            if "patch_cases" in case:
                index = case["patch_cases"][0]
                payload["cases"][index]["source"].update(case["replace_source"])
            if "remove_case_field" in case:
                index, field = case["remove_case_field"]
                payload["cases"][index].pop(field, None)
            if "remove" in case:
                for field in case["remove"]:
                    payload.pop(field, None)
            if "replace" in case:
                payload.update(case["replace"])
                continue
            if "patch" in case:
                for field, value in case["patch"].items():
                    if isinstance(value, dict) and isinstance(payload.get(field), dict):
                        payload[field].update(value)
                    else:
                        payload[field] = value
            try:
                validate_contract(schema_name, payload)
            except Exception:
                pass
            else:
                raise AssertionError(f"{schema_name} unexpectedly accepted case {case.get('case')}")


def _canonical_v2_valid():
    fixture = json.loads((FIXTURES / "canonical-v2-golden.json").read_text())
    manifest = copy.deepcopy(fixture["without_digests"])
    generation_id, manifest_digest = canonical_v2_identities(manifest)
    return {
        **manifest,
        "generation_id": generation_id,
        "manifest_digest_sha256": manifest_digest,
        "trust_anchor_sha256": sha256_bytes(generation_id.encode("ascii")),
    }


def _migration_valid():
    return {
        "migration_version": 1, "action": "republish_v2",
        "source_dataset": "bars_daily", "source_schema_version": "v1",
        "source_partition_path": "canonical/bars_daily/v1/date=2026-08-21",
        "source_partition_date": "2026-08-21",
        "source_data_checksum_sha256": "a" * 64,
        "source_schema_checksum_sha256": "b" * 64,
        "source_legacy_generation_id": "c" * 64,
        "source_manifest_digest_sha256": "d" * 64,
        "target_dataset": "bars_daily", "target_schema_version": "v2",
        "target_partition_path": "canonical/bars_daily/v2/date=2026-08-21",
        "target_content_generation_id": "e" * 64,
        "target_manifest_digest_sha256": "f" * 64,
        "migration_algorithm_version": "canonical-migration.v1",
        "decision_time": "2026-08-23T00:00:00Z",
        "run_visible_cutoff": "2026-08-23T01:00:00Z",
        "operator": "reviewer", "approval_reference": "gate-plan-v0.7",
        "mapping_checksum_sha256": "1" * 64,
    }


def _universe_valid():
    return {
        "universe_version": 1, "universe_id": "research-whitelist",
        "source": "config/universe/research-whitelist.txt",
        "snapshot_time": "2026-08-23T00:00:00Z", "visibility_time": "2026-08-23T00:00:00Z",
        "valid_from": "2026-08-21", "valid_to": None,
        "members_artifact": {"path": "members.csv", "checksum_sha256": "a" * 64},
        "membership_evidence": "static research whitelist; not PIT index membership",
        "generation_id": "b" * 64,
    }


def _quality_valid():
    return {
        "report_version": 1, "binding_type": "factor_v1", "bound_generation_id": "c" * 64,
        "policy": "reject_all", "status": "passed",
        "checks": [{"name":"duplicate_keys","threshold":0,"observed":0,"level":"error","result":"passed"}],
        "errors": [], "warnings": [],
    }


def test_phase_1_test_migration_contract_is_preserved():
    payload = json.loads((ROOT / "config/schemas/fixtures/test-migrations/adjustment-v1.json").read_text())
    assert payload["migration_version"] == 1
    replacements = {item["removed_test_id"]: item for item in payload["removed_or_replaced"]}
    assert "test_adjustment_provider.py::test_cash_only_formula_matches_provider_factor_ratios" in replacements
    replacement = replacements["test_adjustment_provider.py::test_cash_only_formula_matches_provider_factor_ratios"]
    assert replacement["status"] == "replaced"
    assert Path(ROOT / replacement["replacement_test_id"].split("::", 1)[0]).is_file()
    assert "test_exchange_formula_golden_cases" in (ROOT / replacement["replacement_test_id"].split("::", 1)[0]).read_text()


def test_adjustment_golden_provenance_contract():
    fixture_path = FIXTURES / "adjustment/golden-provenance.v1.json"
    provenance = json.loads(fixture_path.read_text())
    validate_contract("adjustment_golden_provenance.v1.json", provenance)
    assert provenance["certification"] == "pending-authoritative-source"
    assert all(case["source"]["kind"] != "exchange-reference" or case["source"]["locator"] for case in provenance["cases"])


def test_authoritative_golden_certification_requires_published_source():
    fixture_path = FIXTURES / "adjustment/golden-provenance.v1.json"
    provenance = json.loads(fixture_path.read_text())
    certified = copy.deepcopy(provenance)
    certified["certification"] = "certified-authoritative-source"
    with pytest.raises(Exception):
        validate_contract("adjustment_golden_provenance.v1.json", certified)


def test_adjustment_golden_provenance_v2_official_cases():
    fixture_path = FIXTURES / "adjustment/golden-provenance.v2.json"
    provenance = json.loads(fixture_path.read_text())
    validate_contract("adjustment_golden_provenance.v2.json", provenance)
    assert provenance["provenance_version"] == 2
    assert provenance["certification"] == "pending-authoritative-source"

    event_types = {case["event_type"] for case in provenance["cases"]}
    assert event_types == {"cash", "cash_bonus_transfer", "bonus_transfer", "rights"}
    for case in provenance["cases"]:
        for evidence_name in ("event_evidence", "pre_close_evidence", "reference_price_evidence"):
            evidence = case[evidence_name]
            assert len(evidence["file_sha256"]) == 64
            assert evidence["quoted_locator"]
        if case["case_id"] == "szse-300866-2024-cash-transfer":
            assert case["pre_close_evidence"]["kind"] == "exchange-reference"
            assert "szse.cn/api/report/ShowReport/data" in case["pre_close_evidence"]["locator"]
            assert case["reference_price_evidence"]["kind"] == "derived-formula"
        if case["instrument"] in {"000725.XSHE", "003026.XSHE"}:
            assert case["pre_close_evidence"]["kind"] == "exchange-reference"
            assert "szse.cn/api/report/ShowReport/data" in case["pre_close_evidence"]["locator"]
        if case["event_type"] == "rights":
            assert case["inputs"]["rights_per_ten"] == 3.0
            assert case["expected_ex_right_price"] == pytest.approx(11.1669230769)
            assert case["reference_price_evidence"]["kind"] == "derived-formula"


def test_adjustment_golden_provenance_v2_rejects_uncertified_reference_sources():
    fixture_path = FIXTURES / "adjustment/golden-provenance.v2.json"
    provenance = json.loads(fixture_path.read_text())
    certified = copy.deepcopy(provenance)
    certified["certification"] = "certified-authoritative-source"
    with pytest.raises(Exception):
        validate_contract("adjustment_golden_provenance.v2.json", certified)


def test_adjustment_golden_provenance_v2_certification_accepts_direct_official_references():
    fixture_path = FIXTURES / "adjustment/golden-provenance.v2.json"
    provenance = copy.deepcopy(json.loads(fixture_path.read_text()))
    provenance["certification"] = "certified-authoritative-source"
    for case in provenance["cases"]:
        case["pre_close_evidence"]["kind"] = "exchange-reference"
        case["reference_price_evidence"]["kind"] = "exchange-reference"
    validate_contract("adjustment_golden_provenance.v2.json", provenance)
