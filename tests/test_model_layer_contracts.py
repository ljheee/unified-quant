from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from uq.contracts.gate_contracts import validate_contract
from uq.contracts.model_layer import (
    AcceptedFactorIndexContract,
    ModelContractLoader,
    model_manifest_identities,
    sha256_json,
)
from uq.errors import ContractError

ROOT = Path(__file__).resolve().parents[1]


def base_document(schema_name: str) -> dict:
    digest = "0" * 64
    common = {
        "contract_version": 1,
        "run_id": "00000000-0000-4000-8000-000000000001",
        "created_at": "2026-01-30T07:00:00+00:00",
        "manifest_digest_sha256": digest,
        "generation_id": digest,
    }
    if schema_name == "label_set":
        binding = {
            "binding": "adjusted_price", "dataset": "bars_adjusted", "schema_version": "adjusted-v1",
            "partition_date": "2026-01-29", "generation_id": digest, "data_checksum_sha256": digest,
            "visible_cutoff": "2026-01-29T15:00:00+08:00",
        }
        common.update({
            "name": "return_5d", "semantic_version": "1.0.0", "primary_key": ["instrument", "decision_date"],
            "decision_time_convention": "trading_close_asia_shanghai", "horizon_trading_days": 5,
            "formula_sha256": digest, "adjustment_basis": "governed_adjusted_close",
            "upstream_adjusted_price_bindings": [binding], "eligibility": {"rules": {}},
            "terminal_return_policy": None, "null_policy": {"insufficient_future": "null"},
            "row_count": 2, "columns": ["instrument", "decision_date", "label"],
            "dtypes": {"instrument": "string", "decision_date": "date32-backed datetime", "label": "float64"},
            "data_checksum_sha256": digest, "logical_fingerprint": digest,
            "code_fingerprint": digest, "serialization_profile_id": "uq-parquet-v1",
        })
    elif schema_name == "model_dataset":
        common.update({
            "dataset_name": "research_slice", "semantic_version": "1.0.0", "ordered_features": ["volume_ratio_20d"],
            "factor_set": "basic", "factor_version": "1.0.0", "factor_generation_ids": [digest],
            "label_set_name": "return_5d", "label_generation_id": digest, "universe_snapshot_generation_id": digest,
            "split_policy": {"purge_trading_days": 5, "embargo_trading_days": 2, "splits": [{"name": "train", "start_date": "2026-01-05", "end_date": "2026-01-12"}, {"name": "validation", "start_date": "2026-01-23", "end_date": "2026-01-26"}]},
            "missing_policy": "fail_closed", "row_count": 10,
            "input_feature_schema_generation_id": None,
            "input_feature_schema_manifest_digest_sha256": None,
            "input_feature_schema_path": None,
            "data_checksum_sha256": digest,
            "logical_fingerprint": digest, "code_fingerprint": digest, "serialization_profile_id": "uq-parquet-v1",
            "quality_report_checksum_sha256": digest,
        })
    elif schema_name == "model_definition":
        common.update({
            "model_set": "baseline", "model_version": "1.0.0", "status": "reviewed",
            "algorithm": "regularized_linear", "hyperparameters": {"alpha": 1.0},
            "seed_policy": {"base_seed": 7, "derivation": "fixed"}, "feature_schema_generation_id": digest,
            "compatible_dataset_versions": ["1.0.0"], "metrics": [{"name": "ic", "direction": "maximize"}],
            "selection_rule": "maximum validation ic", "quality_policy": "reject_all",
            "serializer_version": "json-numpy-v1", "code_fingerprint": digest,
            "model_run_content_generation_id": digest,
        })
    elif schema_name == "model_artifact":
        common.update({
            "artifact_filename": "artifact.bin", "artifact_checksum_sha256": digest, "byte_size": 128,
            "runtime_name": "qlib", "runtime_version": "0.9.6", "runtime_import_path": "qlib",
            "model_run_content_generation_id": digest, "serialization_profile_id": "uq-artifact-v1",
        })
    return common


@pytest.mark.parametrize("schema_name", ["label_set", "model_dataset", "model_definition", "model_artifact"])
def test_representative_contracts_validate_and_stable_identity_ignores_run_metadata(schema_name: str) -> None:
    document = base_document(schema_name)
    validate_contract(f"{schema_name}.v1.json", document)
    first = model_manifest_identities(document, schema_name=schema_name)
    changed = copy.deepcopy(document)
    changed["run_id"] = "00000000-0000-4000-8000-000000000002"
    changed["created_at"] = "2026-01-31T08:00:00+08:00"
    second = model_manifest_identities(changed, schema_name=schema_name)
    assert first[0] == second[0]
    assert first[0] != first[1]


def test_typed_loader_rejects_absent_malformed_and_tampered_documents(tmp_path: Path) -> None:
    loader = ModelContractLoader()
    absent = tmp_path / "absent.json"
    with pytest.raises(ContractError, match="missing"):
        loader.load("model_definition", absent)
    malformed = tmp_path / "malformed.json"; malformed.write_text("{")
    with pytest.raises(ContractError, match="malformed"):
        loader.load("model_definition", malformed)
    document = base_document("model_definition")
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(document))
    with pytest.raises(ContractError, match="stable generation mismatch"):
        loader.load("model_definition", tampered)


def test_accepted_index_contract_is_query_only_and_fails_closed() -> None:
    query = {
        "contract_version": 1,
        "filters": {"factor_set": "basic", "factor_version": "1.0.0"},
        "ordering": ["factor_set", "partition_date"], "visibility": "accepted_only",
        "pagination": {"limit": 10},
    }
    contract = AcceptedFactorIndexContract()
    assert contract.list(query) == [] and contract.index(query) == []
    unverified = {**query, "filters": {**query["filters"], "generation_id": "1" * 64}}
    with pytest.raises(ContractError, match="not verified"):
        contract.list(unverified)


def _quality_report() -> dict:
    digest = "0" * 64
    return {
        "report_version": 1, "binding_type": "model_artifact_v1", "bound_generation_id": "2" * 64,
        "policy": "reject_all", "status": "passed",
        "checks": [{"name": "artifact_readback", "threshold": 1, "observed": 1, "level": "error", "result": "passed"}],
        "errors": [], "warnings": [], "producer_code_fingerprint": digest, "report_checksum_sha256": "",
    }


def test_quality_report_checksum_and_binding_are_enforced() -> None:
    from uq.contracts.model_layer import ModelContractLoader
    from uq.contracts.gate_contracts import validate_contract as validate_v1
    report = _quality_report()
    report["report_checksum_sha256"] = sha256_json({key: value for key, value in report.items() if key != "report_checksum_sha256"})
    validate_v1("model_quality_report.v1.json", report)
    tampered = {**report, "bound_generation_id": "3" * 64}
    with pytest.raises(ContractError, match="checksum mismatch"):
        ModelContractLoader.validate("model_quality_report.v1", tampered)

def _extended_document(schema_name: str) -> dict:
    digest = "1" * 64
    common = {
        "contract_version": 1,
        "run_id": "00000000-0000-4000-8000-00000000000a",
        "created_at": "2026-02-01T07:00:00+08:00",
        "manifest_digest_sha256": digest,
        "generation_id": digest,
    }
    if schema_name == "model_run":
        common.update({
            "run_content_generation_id": digest,
            "model_definition_generation_id": digest,
            "model_dataset_generation_id": digest,
            "qlib_export_generation_id": digest,
            "init_receipt_generation_id": digest,
            "code_fingerprint": digest,
            "environment_lock_sha256": digest,
            "determinism_controls": {"threads": 1, "random_seed": 7},
            "reproducibility_mode": "logical_fingerprint",
            "logical_tolerance": 0.0,
        })
    elif schema_name == "qlib_dataset_export":
        common["serialization_profile_id"] = "uq-v1"
        common.update({
            "export_layout": {"root": "exports/run"},
            "files": [{"path": "calendars/day.txt", "checksum_sha256": digest, "byte_size": 4}],
            "provider_uri_sha256": digest,
            "calendar_checksum_sha256": digest,
            "instruments_checksum_sha256": digest,
            "feature_mapping_checksum_sha256": digest,
            "exporter_fingerprint": digest,
            "empty_cache_precondition": True,
        })
    elif schema_name == "qlib_init_receipt":
        common.update({
            "resolved_provider_uri_sha256": digest,
            "export_generation_id": digest,
            "export_manifest_digest_sha256": digest,
            "file_list_checksum_sha256": digest,
            "calendar_checksum_sha256": digest,
            "instruments_checksum_sha256": digest,
            "feature_mapping_checksum_sha256": digest,
            "qlib_import_path": "qlib",
            "qlib_version": "0.9.6",
            "cache_root": ".cache/qlib",
            "cache_diff_checksum_sha256": digest,
            "no_ungoverned_source_assertion": True,
        })
    elif schema_name == "prediction_set":
        common["serialization_profile_id"] = "uq-v1"
        common.update({
            "prediction_set_name": "daily",
            "model_artifact_generation_id": digest,
            "model_artifact_checksum_sha256": digest,
            "input_dataset_generation_id": digest,
            "model_run_generation_id": digest,
            "decision_date": "2026-02-02",
            "visible_cutoff": "2026-02-02T15:00:00+08:00",
            "score_semantics": {"column": "score", "unit": "rank", "direction": "higher_better", "ranking_scope": "universe", "tie_policy": "instrument", "normalization": "none"},
            "declared_output_columns": ["instrument", "score"],
            "actual_output_columns": ["instrument", "score"],
            "eligibility_policy": "reviewed-v1",
            "eligibility_status": "passed",
            "row_count": 2,
            "data_checksum_sha256": digest,
            "column_set_exact_match": True,
        })
    common["quality_report_checksum_sha256"] = "0" * 64
    return common


@pytest.mark.parametrize("schema_name", ["model_run", "qlib_dataset_export", "qlib_init_receipt", "prediction_set"])
def test_remaining_manifest_families_have_representative_and_negative_coverage(schema_name: str) -> None:
    document = _extended_document(schema_name)
    validate_contract(f"{schema_name}.v1.json", document)
    generation, digest = model_manifest_identities(document, schema_name=schema_name)
    assert generation != digest
    invalid = copy.deepcopy(document)
    if schema_name == "qlib_dataset_export":
        invalid["files"][0]["path"] = "/absolute/path"
    elif schema_name == "qlib_init_receipt":
        invalid["no_ungoverned_source_assertion"] = False
    elif schema_name == "prediction_set":
        invalid["decision_date"] = "not-a-date"
    else:
        invalid["determinism_controls"]["threads"] = 0
    with pytest.raises(ContractError):
        validate_contract(f"{schema_name}.v1.json", invalid)


def test_loader_rejects_nonfinite_invalid_formats_and_unapproved_root(tmp_path: Path) -> None:
    loader = ModelContractLoader(accepted_root=tmp_path / "accepted")
    accepted = tmp_path / "accepted"; accepted.mkdir()
    document = base_document("model_definition")
    generation, digest = model_manifest_identities(document, schema_name="model_definition")
    document["generation_id"] = generation; document["manifest_digest_sha256"] = digest
    path = accepted / "definition.json"; path.write_text(json.dumps(document))
    assert loader.load("model_definition", path)["generation_id"] == generation

    outside = tmp_path / "outside.json"; outside.write_text(json.dumps(document))
    with pytest.raises(ContractError, match="outside approved root"):
        loader.load("model_definition", outside)

    nan_document = copy.deepcopy(document)
    if "hyperparameters" in nan_document:
        pass
    raw_nan = json.dumps(document).replace('"baseline"', "NaN")
    (accepted / "nan.json").write_text(raw_nan)
    with pytest.raises(ContractError, match="non-finite"):
        loader.load("model_definition", accepted / "nan.json")

    prediction = _extended_document("prediction_set")
    prediction["decision_date"] = "2026-02-30"
    with pytest.raises(ContractError):
        validate_contract("prediction_set.v1.json", prediction)


def test_accepted_query_cursor_arity_is_validated() -> None:
    contract = AcceptedFactorIndexContract()
    query = {
        "contract_version": 1,
        "filters": {},
        "ordering": ["factor_set"],
        "visibility": "accepted_only",
        "pagination": {"limit": 10, "after_sort_key": ["a", "b"]},
    }
    with pytest.raises(ContractError, match="cursor"):
        contract.list(query)


EVIDENCE_DIR = ROOT / "evidence" / "phase-0"


def test_all_families_have_valid_and_negative_fixtures_on_disk() -> None:
    fixture_dir = EVIDENCE_DIR / "fixtures"
    for family in ["label_set","model_dataset","model_definition","model_run","model_artifact","qlib_dataset_export","qlib_init_receipt","prediction_set","feature_preprocessing"]:
        valid_path = fixture_dir / f"{family}-valid.json"
        negative_path = fixture_dir / f"{family}-negative.json"
        assert valid_path.is_file(), f"missing valid fixture: {valid_path}"
        assert negative_path.is_file(), f"missing negative fixture: {negative_path}"
        valid_doc = json.loads(valid_path.read_text())
        ModelContractLoader.validate(family, valid_doc)


def test_golden_vectors_cover_all_manifest_families() -> None:
    golden_path = EVIDENCE_DIR / "golden-vectors" / "identity-golden-vectors.json"
    vectors = json.loads(golden_path.read_text())
    expected = {"label_set","model_dataset","model_definition","model_run","model_artifact","qlib_dataset_export","qlib_init_receipt","prediction_set","feature_preprocessing"}
    assert set(vectors.keys()) == expected
    for family, entry in vectors.items():
        assert entry["run_metadata_change_stable"] is True
        assert entry["changed_run_generation_matches"] is True


def test_cross_manifest_binding_resolver_passes_and_fails() -> None:
    from uq.contracts.model_layer import resolve_bindings
    digest = "0" * 64
    dataset = {
        "generation_id": "a" * 64,
        "label_generation_id": "b" * 64,
        "label_set_name": "return_5d",
        "factor_generation_ids": [digest],
        "universe_snapshot_generation_id": digest,
    }
    with pytest.raises(ContractError, match="invalid factor manifest content"):
        resolve_bindings({
            "model_dataset": dataset,
            "label_set": {"generation_id": "b" * 64, "name": "return_5d"},
            "universe_snapshot": {"generation_id": digest},
            "factor_manifests": {digest: {"generation_id": digest}},
        })
    wrong_label = {"generation_id": "c" * 64, "name": "return_5d"}
    with pytest.raises(ContractError, match="mismatch"):
        resolve_bindings({"model_dataset": dataset, "label_set": wrong_label})
    with pytest.raises(ContractError, match="missing"):
        resolve_bindings({"model_dataset": dataset})


def test_accepted_index_response_schema_validates() -> None:
    from uq.contracts.gate_contracts import validate_contract
    response = {
        "contract_version": 1,
        "entries": [{
            "factor_set": "basic", "factor_version": "1.0.0",
            "partition_date": "2026-01-30",
            "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64,
            "data_checksum_sha256": "0" * 64, "quality_status": "passed",
            "universe_snapshot_generation_id": None,
        }],
        "next_cursor": None,
        "has_more": False,
    }
    validate_contract("accepted_factor_index_response.v1.json", response)


def test_quality_report_and_response_fixtures_exist_and_validate() -> None:
    fixture_dir = EVIDENCE_DIR / "fixtures"
    for name in ("model_quality_report", "accepted_factor_index_response"):
        valid_path = fixture_dir / f"{name}-valid.json"
        negative_path = fixture_dir / f"{name}-negative.json"
        assert valid_path.is_file() and negative_path.is_file()
        from uq.contracts.gate_contracts import validate_contract as validate_schema
        schema_name = "model_quality_report.v1.json" if name == "model_quality_report" else f"{name}.v1.json"
        validate_schema(schema_name, json.loads(valid_path.read_text()))
