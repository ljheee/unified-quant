from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import pytest

from uq.contracts.artifacts import QualityReportStore
from uq.contracts.canonical_v2 import CanonicalV2Store, file_sha256_bytes
from uq.contracts.gate_contracts import canonical_json, sha256_bytes
from uq.contracts.schema import load_schema
from uq.contracts.model_layer import (
    bind_reviewed_quality_decision,
    create_reviewed_quality_decision,
    research_contract_identities,
)
from uq.errors import ContractError
from uq.research_chain.owning_contracts import AdjustedPriceDatasetStore
from uq.research_chain.resolver import (
    FileResearchRunStore,
    ResearchChainRequestResolver,
    ResearchResolutionError,
    build_dry_run_state,
)
from tests.review_key import REVIEWER_PRIVATE_KEY

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evidence/research-chain/phase-0/fixtures/research_run_request-valid.json"

_CHECKS = {
    "label_set_v1": (
        "reject_all",
        [
            {"name": "key_uniqueness", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "null_rate", "threshold": 0.1, "observed": 0.0, "level": "error", "result": "passed"},
        ],
    ),
    "feature_preprocessing_v1": (
        "cross_sectional_stateless_v1",
        [
            {"name": "key_reconciliation", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "row_count_reconciliation", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "output_readback", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
        ],
    ),
    "backtest_config_v1": (
        "reject_all",
        [
            {"name": "calendar_period_valid", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "cost_parameters_non_negative", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
            {"name": "price_source_bound", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
        ],
    ),
}


class FakeOwningStore:
    def __init__(self, manifest: Mapping[str, Any]) -> None:
        self.manifest = dict(manifest)
        self.read_calls = 0

    def read_manifest(self, generation_id: str) -> dict[str, Any]:
        self.read_calls += 1
        if generation_id != self.manifest["generation_id"]:
            raise ContractError(f"unpublished owning artifact: {generation_id}")
        return self.manifest


class RecordingProvider:
    def __init__(self, behavior: str = "passed") -> None:
        self.behavior = behavior

    def resolve(self, *, binding_type: str, subject_generation_id: str, subject_manifest_digest_sha256: str | None, **_: Any) -> Mapping[str, Any]:
        if self.behavior == "unreachable":
            raise ContractError("quality provider is unavailable")
        if self.behavior == "invalid_config":
            raise ContractError("provider configuration is not registered")
        if self.behavior == "untrusted_key":
            raise ContractError("quality decision trust anchor mismatch")
        if binding_type == "factor_v1":
            report = {
                "report_version": 1,
                "binding_type": "factor_v1",
                "bound_generation_id": "a" * 64,
                "policy": "reject_all",
                "status": "passed",
                "checks": [
                    {"name": "null_rate", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}
                ],
                "errors": [],
                "warnings": [],
            }
            return {
                "contract_version": 1,
                "schema_version": "1.0.0",
                "binding_type": "factor_v1",
                "subject_generation_id": "a" * 64,
                "subject_manifest_digest_sha256": None,
                "owning_report": report,
                "decision_checksum_sha256": sha256_bytes(canonical_json(report)),
                "provider_id": "external-model-quality-reviewer-v1",
                "trust_anchor_id": "factor-review-key-v1",
            }
        policy, checks = _CHECKS[binding_type]
        unsigned = create_reviewed_quality_decision(
            binding_type=binding_type,
            policy=policy,
            status="passed",
            checks=checks,
            errors=[],
            warnings=[],
            producer_code_fingerprint="0" * 64,
            private_key_pem=REVIEWER_PRIVATE_KEY,
        )
        subject_digest = subject_manifest_digest_sha256
        report, checksum = bind_reviewed_quality_decision(
            unsigned,
            binding_type=binding_type,
            subject_generation_id=subject_generation_id,
            subject_content_sha256=subject_digest,
        )
        return {
            "contract_version": 1,
            "schema_version": "1.0.0",
            "binding_type": binding_type,
            "subject_generation_id": subject_generation_id,
            "subject_manifest_digest_sha256": subject_digest,
            "owning_report": report,
            "decision_checksum_sha256": checksum,
            "provider_id": "external-model-quality-reviewer-v1",
            "trust_anchor_id": report["key_id"],
        }


def _upstream_manifests() -> dict[str, dict[str, str]]:
    return {
        "factor_partition": {
            "generation_id": "a" * 64,
            "manifest_digest_sha256": "b" * 64,
            "data_checksum_sha256": "c" * 64,
        },
        "universe_snapshot": {
            "generation_id": "d" * 64,
            "manifest_digest_sha256": "e" * 64,
            "members_artifact": {"checksum_sha256": "f" * 64},
        },
        "adjusted_price_dataset": {
            "generation_id": "1" * 64,
            "manifest_digest_sha256": "2" * 64,
            "data_checksum_sha256": "3" * 64,
        },
        "label_set": {
            "generation_id": "b" * 64,
            "manifest_digest_sha256": "c" * 64,
            "data_checksum_sha256": "d" * 64,
        },
        "feature_preprocessing": {
            "generation_id": "e" * 64,
            "manifest_digest_sha256": "f" * 64,
            "output_frame_sha256": "1" * 64,
        },
        "backtest_config": {
            "generation_id": "f" * 64,
            "manifest_digest_sha256": "0" * 64,
            "price_source_binding": {"data_checksum_sha256": "2" * 64},
        },
    }


def _request(manifests: dict[str, dict[str, str]]) -> dict[str, Any]:
    request = json.loads(FIXTURE.read_text())
    request["factor_binding"].update({
        "generation_id": manifests["factor_partition"]["generation_id"],
        "manifest_digest_sha256": manifests["factor_partition"]["manifest_digest_sha256"],
    })
    request["universe_snapshot_binding"].update({
        "generation_id": manifests["universe_snapshot"]["generation_id"],
        "manifest_digest_sha256": manifests["universe_snapshot"]["manifest_digest_sha256"],
    })
    request["adjusted_price_binding"].update({
        "generation_id": manifests["adjusted_price_dataset"]["generation_id"],
        "data_checksum_sha256": manifests["adjusted_price_dataset"]["data_checksum_sha256"],
    })
    request["label_binding"].update({
        "generation_id": manifests["label_set"]["generation_id"],
        "manifest_digest_sha256": manifests["label_set"]["manifest_digest_sha256"],
    })
    request["feature_preprocessing_binding"].update({
        "generation_id": manifests["feature_preprocessing"]["generation_id"],
        "manifest_digest_sha256": manifests["feature_preprocessing"]["manifest_digest_sha256"],
    })
    request["backtest_config_binding"].update({
        "generation_id": manifests["backtest_config"]["generation_id"],
        "manifest_digest_sha256": manifests["backtest_config"]["manifest_digest_sha256"],
    })
    request["request_content_generation_id"] = "0" * 64
    request["manifest_digest_sha256"] = "0" * 64
    generation, digest = research_contract_identities(request, schema_name="research_run_request")
    request["request_content_generation_id"] = generation
    request["manifest_digest_sha256"] = digest
    return request


def _resolver(
    manifests: dict[str, dict[str, str]], *, omit: str | None = None
) -> tuple[ResearchChainRequestResolver, dict[str, FakeOwningStore]]:
    stores = {
        output_family: FakeOwningStore(manifest)
        for output_family, manifest in manifests.items()
        if output_family != omit
    }
    return ResearchChainRequestResolver(stores), stores


def test_request_resolves_all_upstreams_and_is_deterministic() -> None:
    manifests = _upstream_manifests()
    resolver, stores = _resolver(manifests)
    first = resolver.resolve(
        _request(manifests), quality_provider=RecordingProvider(), provider_config_ref="provider.json"
    )
    second = resolver.resolve(
        _request(manifests), quality_provider=RecordingProvider(), provider_config_ref="provider.json"
    )
    assert first.resolved_execution_plan_sha256 == second.resolved_execution_plan_sha256
    assert [binding.stage for binding in first.stage_bindings] == [
        "factor_computation", "factor_computation", "dataset_preparation",
        "dataset_preparation", "dataset_preparation", "backtest_execution",
    ]
    assert all(binding.data_checksum_sha256 for binding in first.stage_bindings)
    assert sum(store.read_calls for store in stores.values()) == 12


def test_dry_run_state_contains_no_downstream_outputs(tmp_path: Path) -> None:
    manifests = _upstream_manifests()
    resolver, stores = _resolver(manifests)
    request = _request(manifests)
    plan = resolver.resolve(
        request, quality_provider=RecordingProvider(), provider_config_ref="provider.json"
    )
    reads_before = sum(store.read_calls for store in stores.values())
    state = build_dry_run_state(
        plan,
        runner_identity={
            "code_fingerprint": "0" * 64,
            "environment_profile": "test",
            "lock_digest_sha256": "0" * 64,
        },
        created_at="2026-01-30T07:00:00+00:00",
    )
    store = FileResearchRunStore(tmp_path)
    published_request = store.publish_request(request, path_policy="strict_v1")
    published_state = store.publish_state(state, stage="resolve_request")
    assert published_request.manifest_path.is_file()
    assert published_state.manifest_path.is_file()
    assert store.read_request(
        request["request_content_generation_id"], request["manifest_digest_sha256"]
    )["run_id"] == request["run_id"]
    assert store.read_state(
        request["request_content_generation_id"], request["run_id"],
        "resolve_request", state["manifest_digest_sha256"],
    )["intent"] == "dry_run"
    assert sum(store.read_calls for store in stores.values()) == reads_before
    for record in state["stage_records"][1:]:
        assert not record["output_bindings"]
    assert all(
        binding["output_family"] == "research_run_request"
        for record in state["stage_records"]
        for binding in record["output_bindings"]
    )


@pytest.mark.parametrize("behavior,reason", [
    ("unreachable", "quality_decision_missing"),
    ("invalid_config", "quality_decision_missing"),
    ("untrusted_key", "quality_decision_rejected"),
])
def test_provider_failures_are_typed(behavior: str, reason: str) -> None:
    manifests = _upstream_manifests()
    resolver, _ = _resolver(manifests)
    with pytest.raises(ResearchResolutionError) as exc_info:
        resolver.resolve(
            _request(manifests), quality_provider=RecordingProvider(behavior),
            provider_config_ref="provider.json",
        )
    assert exc_info.value.reason == reason


@pytest.mark.parametrize("mutation,reason", [
    (
        lambda manifests, request: request["factor_binding"].__setitem__(
            "generation_id", "9" * 64
        ),
        "input_unresolved",
    ),
    (
        lambda manifests, request: manifests["factor_partition"].__setitem__(
            "manifest_digest_sha256", "9" * 64
        ),
        "input_tampered",
    ),
    (
        lambda manifests, request: request.pop("research_name"),
        "request_invalid",
    ),
    (
        lambda manifests, request: request.__setitem__("stage_plan_sha256", "9" * 64),
        "request_invalid",
    ),
    (
        lambda manifests, request: request["factor_binding"].__setitem__("family", "not_factor"),
        "request_invalid",
    ),
    (
        lambda manifests, request: manifests["adjusted_price_dataset"].pop("data_checksum_sha256"),
        "lineage_mismatch",
    ),
])
def test_request_failures_are_typed(
    mutation, reason: str
) -> None:
    manifests = _upstream_manifests()
    request = _request(manifests)
    mutation(manifests, request)
    request["request_content_generation_id"] = "0" * 64
    request["manifest_digest_sha256"] = "0" * 64
    try:
        generation, digest = research_contract_identities(request, schema_name="research_run_request")
        request["request_content_generation_id"] = generation
        request["manifest_digest_sha256"] = digest
    except (ContractError, KeyError, TypeError):
        pass
    resolver, _ = _resolver(manifests)
    with pytest.raises(ResearchResolutionError) as exc_info:
        resolver.resolve(
            request, quality_provider=RecordingProvider(), provider_config_ref="provider.json"
        )
    assert exc_info.value.reason == reason


def test_adjusted_price_store_reads_published_manifest(tmp_path: Path) -> None:
    schema = load_schema("config/schemas/bars_daily.research-v1.yaml")
    frame = pd.DataFrame({
        "instrument": ["600000.XSHG"],
        "datetime": pd.to_datetime(["2026-08-21"]),
        "open": [10.0], "high": [10.2], "low": [9.8], "close": [10.0],
        "volume": [10000.0], "amount": [100000.0],
    })
    store = CanonicalV2Store(tmp_path)
    generation = store.prepare_generation(schema, date(2026, 8, 21), frame, {}, {})
    report_directory = QualityReportStore().save(tmp_path, {
        "report_version": 1,
        "binding_type": "canonical_v2",
        "bound_generation_id": generation,
        "policy": "reject_all",
        "status": "passed",
        "checks": [
            {"name": "coverage", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}
        ],
        "errors": [],
        "warnings": [],
    })
    checksum = file_sha256_bytes((report_directory / "report.json").read_bytes())
    data_path = store.publish(
        schema, date(2026, 8, 21), frame, {}, {}, quality_checksum=checksum
    )

    manifest = AdjustedPriceDatasetStore(tmp_path).read_manifest(generation)

    assert manifest["generation_id"] == generation
    assert manifest["data_checksum_sha256"] == file_sha256_bytes(data_path.read_bytes())
    assert manifest["quality_report_checksum"] == checksum


def test_adjusted_price_store_rejects_missing_quality_report(tmp_path: Path) -> None:
    schema = load_schema("config/schemas/bars_daily.research-v1.yaml")
    frame = pd.DataFrame({
        "instrument": ["600000.XSHG"],
        "datetime": pd.to_datetime(["2026-08-21"]),
        "open": [10.0], "high": [10.2], "low": [9.8], "close": [10.0],
        "volume": [10000.0], "amount": [100000.0],
    })
    store = CanonicalV2Store(tmp_path)
    generation = store.prepare_generation(schema, date(2026, 8, 21), frame, {}, {})
    report_directory = QualityReportStore().save(tmp_path, {
        "report_version": 1,
        "binding_type": "canonical_v2",
        "bound_generation_id": generation,
        "policy": "reject_all",
        "status": "passed",
        "checks": [
            {"name": "coverage", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}
        ],
        "errors": [],
        "warnings": [],
    })
    checksum = file_sha256_bytes((report_directory / "report.json").read_bytes())
    store.publish(schema, date(2026, 8, 21), frame, {}, {}, quality_checksum=checksum)
    report_path = tmp_path / "reports" / "canonical_v2" / generation / "report.json"
    report_path.unlink()

    with pytest.raises(ContractError, match="adjusted price quality report is missing"):
        AdjustedPriceDatasetStore(tmp_path).read_manifest(generation)


def test_adjusted_price_store_rejects_tampered_data(tmp_path: Path) -> None:
    schema = load_schema("config/schemas/bars_daily.research-v1.yaml")
    frame = pd.DataFrame({
        "instrument": ["600000.XSHG"],
        "datetime": pd.to_datetime(["2026-08-21"]),
        "open": [10.0], "high": [10.2], "low": [9.8], "close": [10.0],
        "volume": [10000.0], "amount": [100000.0],
    })
    store = CanonicalV2Store(tmp_path)
    generation = store.prepare_generation(schema, date(2026, 8, 21), frame, {}, {})
    report_directory = QualityReportStore().save(tmp_path, {
        "report_version": 1,
        "binding_type": "canonical_v2",
        "bound_generation_id": generation,
        "policy": "reject_all",
        "status": "passed",
        "checks": [
            {"name": "coverage", "threshold": 0, "observed": 0, "level": "error", "result": "passed"}
        ],
        "errors": [],
        "warnings": [],
    })
    checksum = file_sha256_bytes((report_directory / "report.json").read_bytes())
    partition = store.publish(
        schema, date(2026, 8, 21), frame, {}, {}, quality_checksum=checksum
    )
    partition.write_bytes(b"tampered")
    with pytest.raises(ContractError, match="tampered adjusted price data checksum"):
        AdjustedPriceDatasetStore(tmp_path).read_manifest(generation)


def test_failed_layout_validation_does_not_create_directories(tmp_path: Path) -> None:
    manifests = _upstream_manifests()
    request = _request(manifests)
    store = FileResearchRunStore(tmp_path)
    wrong_path = (
        Path("research_runs") / "requests"
        / f"request={request['request_content_generation_id']}"
        / "run=00000000-0000-4000-8000-000000000002" / "manifest.json"
    )
    with pytest.raises(ContractError, match="does not match request layout"):
        store._atomic_write(wrong_path, request)
    assert not (tmp_path / "research_runs").exists()


def test_request_readback_rejects_tampered_manifest(tmp_path: Path) -> None:
    manifests = _upstream_manifests()
    request = _request(manifests)
    store = FileResearchRunStore(tmp_path)
    store.publish_request(request, path_policy="strict_v1")
    manifest_path = next((tmp_path / "research_runs" / "requests").rglob("manifest.json"))
    document = json.loads(manifest_path.read_text())
    document["research_name"] = "tampered"
    manifest_path.write_text(json.dumps(document))
    with pytest.raises(ContractError, match="stable content identity mismatch"):
        store.read_request(
            request["request_content_generation_id"], request["manifest_digest_sha256"]
        )


def test_missing_store_and_overwrite_are_fail_closed(tmp_path: Path) -> None:
    manifests = _upstream_manifests()
    resolver, _ = _resolver(manifests, omit="factor_partition")
    with pytest.raises(ResearchResolutionError) as exc_info:
        resolver.resolve(
            _request(manifests), quality_provider=RecordingProvider(),
            provider_config_ref="provider.json",
        )
    assert exc_info.value.reason == "request_invalid"

    request = _request(manifests)
    store = FileResearchRunStore(tmp_path)
    store.publish_request(request, path_policy="strict_v1")
    with pytest.raises(ContractError, match="immutable research manifest already exists"):
        store.publish_request(request, path_policy="strict_v1")
    with pytest.raises(ContractError, match="Phase 5"):
        store.publish_result({}, path_policy="strict_v1")
