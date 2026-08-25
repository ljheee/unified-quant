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
            "split_policy": {"purge_trading_days": 5, "embargo_trading_days": 2, "splits": [{"name": "train", "start_date": "2026-01-01", "end_date": "2026-01-28"}]},
            "missing_policy": "fail_closed", "row_count": 10, "data_checksum_sha256": digest,
            "logical_fingerprint": digest, "code_fingerprint": digest, "serialization_profile_id": "uq-parquet-v1",
        })
    elif schema_name == "model_definition":
        common.update({
            "model_set": "baseline", "model_version": "1.0.0", "status": "reviewed",
            "algorithm": "regularized_linear", "hyperparameters": {"alpha": 1.0},
            "seed_policy": {"base_seed": 7, "derivation": "fixed"}, "feature_schema_generation_id": digest,
            "compatible_dataset_versions": ["1.0.0"], "metrics": [{"name": "ic", "direction": "maximize"}],
            "selection_rule": "maximum validation ic", "quality_policy": "reject_all",
            "serializer_version": "joblib-v1", "code_fingerprint": digest,
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
    report = _quality_report()
    report["report_checksum_sha256"] = sha256_json({key: value for key, value in report.items() if key != "report_checksum_sha256"})
    ModelContractLoader.validate("model_quality_report", report)
    tampered = {**report, "bound_generation_id": "3" * 64}
    with pytest.raises(ContractError, match="checksum mismatch"):
        ModelContractLoader.validate("model_quality_report", tampered)
