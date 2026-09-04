from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from uq.contracts.gate_contracts import canonical_json, sha256_bytes
from uq.contracts.model_layer import (
    ModelContractLoader,
    sha256_json,
    research_contract_identities,
    research_stage_plan_sha256,
)
from uq.errors import ContractError
from uq.research_chain.contracts import validate_provider_config_ref, validate_research_layout
from uq.research_chain.owning_contracts import FeatureSchemaStore, LabelStore
from uq.models.features import FeatureSchemaBuilder
from uq.models.labels import LabelBuilder
from uq.portfolio.builder import PortfolioDefinitionBinding
from uq.contracts.artifacts import UniverseSnapshotStore
from uq.contracts.factor_governance import FactorRegistry
from uq.contracts.gate_contracts import factor_manifest_identities
from uq.contracts.canonical_v2 import file_sha256_bytes
from uq.factors.store import FactorStore
from uq.contracts.model_layer import bind_reviewed_quality_decision, create_reviewed_quality_decision, model_manifest_identities
from tests.review_key import REVIEWER_PRIVATE_KEY

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "evidence/research-chain/phase-0/fixtures"
GOLDEN_DIR = ROOT / "evidence/research-chain/phase-0/golden-vectors"
FAMILIES = [
    "model_definition_template",
    "portfolio_definition_template",
    "research_run_request",
    "research_run_state",
    "research_run_result",
    "quality_decision",
]


def load_fixture(family: str, kind: str) -> dict:
    path = FIXTURE_DIR / f"{family}-{kind}.json"
    assert path.is_file(), f"missing persisted fixture: {path}"
    return json.loads(path.read_text())


def valid_documents() -> dict[str, dict]:
    return {family: load_fixture(family, "valid") for family in FAMILIES}


@pytest.mark.parametrize("family", FAMILIES)
def test_valid_and_negative_fixtures_are_persisted(family: str) -> None:
    ModelContractLoader.validate(family, load_fixture(family, "valid"))
    with pytest.raises(ContractError):
        ModelContractLoader.validate(family, load_fixture(family, "negative-schema"))
    with pytest.raises(ContractError):
        ModelContractLoader.validate(family, load_fixture(family, "negative-semantic"))


def test_request_identity_is_stable_under_run_metadata_and_key_reorder() -> None:
    request = valid_documents()["research_run_request"]
    reordered = {key: request[key] for key in sorted(request)}
    first_generation, first_digest = research_contract_identities(
        request, schema_name="research_run_request"
    )
    reordered_generation, reordered_digest = research_contract_identities(
        reordered, schema_name="research_run_request"
    )
    assert first_generation == reordered_generation == request["request_content_generation_id"]
    assert first_digest == reordered_digest == request["manifest_digest_sha256"]

    metadata_changed = copy.deepcopy(request)
    metadata_changed["run_id"] = "00000000-0000-4000-8000-000000000002"
    metadata_changed["created_at"] = "2026-01-31T07:00:00+00:00"
    metadata_changed["request_content_generation_id"] = "0" * 64
    metadata_changed["manifest_digest_sha256"] = "0" * 64
    generation, digest = research_contract_identities(
        metadata_changed, schema_name="research_run_request"
    )
    assert generation == first_generation
    assert digest != first_digest


def test_result_identity_is_sensitive_to_governed_content() -> None:
    result = valid_documents()["research_run_result"]
    baseline_generation, _ = research_contract_identities(
        result, schema_name="research_run_result"
    )
    assert baseline_generation == result["result_content_generation_id"]

    def changed_generation(mutate) -> str:
        changed = copy.deepcopy(result)
        mutate(changed)
        changed["result_content_generation_id"] = "0" * 64
        changed["manifest_digest_sha256"] = "0" * 64
        generation, _ = research_contract_identities(changed, schema_name="research_run_result")
        return generation

    assert changed_generation(
        lambda doc: doc["stage_records"][0].__setitem__("status", "failed")
    ) != baseline_generation
    assert changed_generation(
        lambda doc: doc["runner_identity"].__setitem__("environment_profile", "linux-x86")
    ) != baseline_generation
    stable = changed_generation(
        lambda doc: doc.__setitem__("request_manifest_digest_sha256", "1" * 64)
    )
    assert stable == baseline_generation
    assert changed_generation(
        lambda doc: doc.__setitem__("request_content_generation_id", "2" * 64)
    ) != baseline_generation
    assert changed_generation(
        lambda doc: doc["stage_records"][0]["output_bindings"][0].__setitem__(
            "quality_decision_checksum_sha256", "3" * 64
        )
    ) != baseline_generation
    metadata_changed = copy.deepcopy(result)
    metadata_changed["request_manifest_digest_sha256"] = "1" * 64
    _, metadata_digest = research_contract_identities(
        metadata_changed, schema_name="research_run_result"
    )
    _, baseline_digest = research_contract_identities(result, schema_name="research_run_result")
    assert metadata_digest != baseline_digest


def test_result_identity_is_stable_under_run_metadata_and_physical_paths() -> None:
    result = valid_documents()["research_run_result"]
    changed = copy.deepcopy(result)
    changed["run_id"] = "00000000-0000-4000-8000-000000000002"
    changed["created_at"] = "2026-01-31T07:00:00+00:00"
    for binding in changed["stage_records"][0]["output_bindings"]:
        binding["physical_path"] = "research_runs/other/manifest.json"
    changed["result_content_generation_id"] = "0" * 64
    changed["manifest_digest_sha256"] = "0" * 64
    generation, _ = research_contract_identities(changed, schema_name="research_run_result")
    assert generation == result["result_content_generation_id"]


def test_normative_stage_order_is_enforced() -> None:
    state = valid_documents()["research_run_state"]
    ModelContractLoader.validate("research_run_state", state)

    reordered = copy.deepcopy(state)
    second = copy.deepcopy(state["stage_records"][0])
    second["stage"] = "factor_computation"
    second["output_bindings"][0]["output_family"] = "factor_manifest"
    reordered["stage_records"].append(second)
    reordered["stage_records"].reverse()
    generation, digest = research_contract_identities(
        reordered, schema_name="research_run_state"
    )
    reordered["state_content_generation_id"] = generation
    reordered["manifest_digest_sha256"] = digest
    with pytest.raises(ContractError, match="not in normative order"):
        ModelContractLoader.validate("research_run_state", reordered)

    result = valid_documents()["research_run_result"]
    incomplete = copy.deepcopy(result)
    incomplete["stage_records"] = incomplete["stage_records"][:-1]
    generation, digest = research_contract_identities(
        incomplete, schema_name="research_run_result"
    )
    incomplete["result_content_generation_id"] = generation
    incomplete["manifest_digest_sha256"] = digest
    with pytest.raises(ContractError):
        ModelContractLoader.validate("research_run_result", incomplete)


def test_quality_decision_binds_owning_report_checksum() -> None:
    decision = valid_documents()["quality_decision"]
    ModelContractLoader.validate("quality_decision", decision)
    assert decision["decision_checksum_sha256"] == decision["owning_report"]["report_checksum_sha256"]

    wrong = copy.deepcopy(decision)
    wrong["decision_checksum_sha256"] = "0" * 64
    with pytest.raises(ContractError, match="quality decision checksum mismatch"):
        ModelContractLoader.validate("quality_decision", wrong)


def test_quality_decision_supports_factor_wrapper() -> None:
    owning_report = {
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
    decision = {
        "contract_version": 1,
        "schema_version": "1.0.0",
        "binding_type": "factor_v1",
        "subject_generation_id": "a" * 64,
        "subject_manifest_digest_sha256": None,
        "owning_report": owning_report,
        "decision_checksum_sha256": sha256_bytes(canonical_json(owning_report)),
        "provider_id": "external-model-quality-reviewer-v1",
        "trust_anchor_id": "factor-review-key-v1",
    }
    ModelContractLoader.validate("quality_decision", decision)
    assert decision["decision_checksum_sha256"] == sha256_bytes(canonical_json(owning_report))


def test_quality_decision_rejects_malformed_signature() -> None:
    decision = copy.deepcopy(valid_documents()["quality_decision"])
    decision["owning_report"]["review_signature_sha256"] = "0" * 128
    with pytest.raises(ContractError, match="model quality report canonical checksum mismatch"):
        ModelContractLoader.validate("quality_decision", decision)


def test_golden_vectors_are_persisted_and_fail_closed() -> None:
    golden_path = GOLDEN_DIR / "identity-golden-vectors.json"
    assert golden_path.is_file()
    golden = json.loads(golden_path.read_text())
    documents = valid_documents()
    for family in FAMILIES:
        document = documents[family]
        if family == "quality_decision":
            key = f"{family}.decision_checksum_sha256"
            assert golden[key] == document["decision_checksum_sha256"]
        else:
            generation, digest = research_contract_identities(document, schema_name=family)
            assert golden[f"{family}.generation_id"] == generation
            assert golden[f"{family}.manifest_digest_sha256"] == digest
    request = documents["research_run_request"]
    metadata_changed = copy.deepcopy(request)
    metadata_changed.update(
        run_id="00000000-0000-4000-8000-000000000002",
        created_at="2026-01-31T07:00:00+00:00",
        request_content_generation_id="0" * 64,
        manifest_digest_sha256="0" * 64,
    )
    generation, digest = research_contract_identities(
        metadata_changed, schema_name="research_run_request"
    )
    assert golden["research_run_request.stable_under_run_metadata"] == generation
    assert golden["research_run_request.digest_under_run_metadata"] == digest
    assert golden["stage_plan_sha256"] == research_stage_plan_sha256()


def test_stage_plan_sha256_is_stable_and_sensitive() -> None:
    expected = research_stage_plan_sha256()
    assert expected == golden_vectors()["stage_plan_sha256"]
    different_payload = json.dumps(
        {"schema_version": "v1", "stage_plan": ["resolve_request"]},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert expected != hashlib.sha256(different_payload.encode()).hexdigest()





REQUEST_GENERATION = "a" * 64
RESULT_GENERATION = "b" * 64
RUN_ID = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("kind", ["request", "state", "result"])
def test_research_layout_accepts_frozen_paths(tmp_path: Path, kind: str) -> None:
    relative = {
        "request": f"research_runs/requests/request={REQUEST_GENERATION}/run={RUN_ID}/manifest.json",
        "state": f"research_runs/states/request={REQUEST_GENERATION}/run={RUN_ID}/stage=00/manifest.json",
        "result": f"research_runs/results/request={REQUEST_GENERATION}/run={RUN_ID}/result={RESULT_GENERATION}/manifest.json",
    }[kind]
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    kwargs = {"stage": "resolve_request"} if kind == "state" else {"result_generation_id": RESULT_GENERATION} if kind == "result" else {}
    assert validate_research_layout(path, data_root=tmp_path, kind=kind, request_generation_id=REQUEST_GENERATION, run_id=RUN_ID, **kwargs) == path


def test_research_layout_rejects_traversal_missing_parent_and_overwrite(tmp_path: Path) -> None:
    kwargs = {"kind": "request", "request_generation_id": REQUEST_GENERATION, "run_id": RUN_ID}
    with pytest.raises(ContractError, match="must not traverse"):
        validate_research_layout("../escape/manifest.json", data_root=tmp_path, **kwargs)
    with pytest.raises(ContractError, match="escapes data root"):
        validate_research_layout(Path('/tmp/outside/manifest.json'), data_root=tmp_path, **kwargs)
    missing_parent = tmp_path / "research_runs/requests" / f"request={REQUEST_GENERATION}" / f"run={RUN_ID}" / "manifest.json"
    with pytest.raises(ContractError, match="parent is missing or symlinked"):
        validate_research_layout(missing_parent, data_root=tmp_path, **kwargs)
    existing = tmp_path / "research_runs/requests" / f"request={REQUEST_GENERATION}" / f"run={RUN_ID}" / "manifest.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("{}")
    with pytest.raises(ContractError, match="overwrite is rejected"):
        validate_research_layout(existing, data_root=tmp_path, **kwargs)


def test_research_layout_rejects_symlink_escape(tmp_path: Path) -> None:
    (tmp_path / "research_runs").symlink_to(tmp_path / "missing")
    target = tmp_path / "research_runs/requests" / f"request={REQUEST_GENERATION}" / f"run={RUN_ID}" / "manifest.json"
    with pytest.raises(ContractError, match="parent is missing or symlinked"):
        validate_research_layout(
            target, data_root=tmp_path, kind="request",
            request_generation_id=REQUEST_GENERATION, run_id=RUN_ID,
        )





def test_provider_config_rejects_malformed_or_untrusted_content(tmp_path: Path) -> None:
    root = tmp_path / "trust"
    path = root / "provider.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not-json")
    with pytest.raises(ContractError, match="unavailable or malformed"):
        validate_provider_config_ref(
            "provider.json", trust_root=root, registered_names={"provider.json"},
            allowed_trust_anchor_ids={"review-key-v1"},
        )
    path.write_text(json.dumps({
        "provider_id": "external-reviewer",
        "trust_anchor_id": "unknown-key",
        "supported_binding_types": ["model_dataset_v1"],
    }))
    with pytest.raises(ContractError, match="unregistered trust anchor"):
        validate_provider_config_ref(
            "provider.json", trust_root=root, registered_names={"provider.json"},
            allowed_trust_anchor_ids={"review-key-v1"},
        )

def golden_vectors() -> dict:
    return json.loads((GOLDEN_DIR / "identity-golden-vectors.json").read_text())


@pytest.mark.parametrize(
    ("config_ref", "message"),
    [
        ("../outside.json", "not traverse"),
        ("foo/../../outside.json", "not traverse"),
        ("/absolute/config.json", "must be relative"),
        ("windows\\config.json", "backslashes"),
        (".hidden/config.json", "hidden segments"),
            ],
)
def test_provider_config_rejects_unsafe_references(config_ref: str, message: str) -> None:
    with pytest.raises(ContractError, match=message):
        validate_provider_config_ref(
            config_ref,
            trust_root=ROOT / "config",
            registered_names=set(),
            allowed_trust_anchor_ids=set(),
        )


def test_provider_config_accepts_registered_relative_path(tmp_path: Path) -> None:
    root = tmp_path / "trust"
    (root / "providers").mkdir(parents=True)
    path = root / "providers/default.json"
    path.write_text("{}")
    path.write_text(json.dumps({
        "provider_id": "external-reviewer",
        "trust_anchor_id": "review-key-v1",
        "supported_binding_types": ["model_dataset_v1"],
    }))
    result = validate_provider_config_ref(
        "providers/default.json",
        trust_root=root,
        registered_names={"providers/default.json"},
        allowed_trust_anchor_ids={"review-key-v1"},
    )
    assert result.provider_id == "external-reviewer"
    assert result.config_path == str(path.resolve())


def test_provider_config_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "trust"
    (root / "providers").mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (root / "providers/escape.json").symlink_to(outside)
    with pytest.raises(ContractError, match="escapes trust root"):
        validate_provider_config_ref(
            "providers/escape.json",
            trust_root=root,
            registered_names={"providers/escape.json"},
            allowed_trust_anchor_ids={"review-key-v1"},
        )


def test_stage_ledger_rejects_gaps_and_terminal_regression() -> None:
    state = copy.deepcopy(valid_documents()["research_run_state"])
    state["stage_records"].append({
        "stage": "model_training", "status": "passed", "output_bindings": [],
        "failure_reason": None,
    })
    state["state_content_generation_id"] = "0" * 64
    state["manifest_digest_sha256"] = "0" * 64
    state["state_content_generation_id"], state["manifest_digest_sha256"] = research_contract_identities(
        state, schema_name="research_run_state"
    )
    with pytest.raises(ContractError, match="contain gaps"):
        ModelContractLoader.validate("research_run_state", state)

    failed = copy.deepcopy(valid_documents()["research_run_state"])
    failed["stage_records"][0]["status"] = "failed"
    failed["stage_records"][0]["failure_reason"] = None
    failed["final_status"] = "failed"
    failed["state_content_generation_id"], failed["manifest_digest_sha256"] = research_contract_identities(
        failed, schema_name="research_run_state"
    )
    with pytest.raises(ContractError, match="failure reason"):
        ModelContractLoader.validate("research_run_state", failed)


def test_stage_ledger_rejects_failed_without_reason_and_later_progress() -> None:
    result = copy.deepcopy(valid_documents()["research_run_result"])
    failed = copy.deepcopy(result)
    failed["stage_records"][0]["status"] = "failed"
    failed["stage_records"][0]["failure_reason"] = None
    with pytest.raises(ContractError, match="failure reason"):
        ModelContractLoader.validate("research_run_result", failed)

    later = copy.deepcopy(result)
    later["stage_records"][0]["status"] = "blocked"
    later["stage_records"][0]["failure_reason"] = "input_tampered"
    with pytest.raises(ContractError, match="later progress"):
        ModelContractLoader.validate("research_run_result", later)


def test_provider_config_rejects_unregistered_reference_and_binding(tmp_path: Path) -> None:
    root = tmp_path / "trust"
    root.mkdir()
    path = root / "provider.json"
    path.write_text(json.dumps({
        "provider_id": "external-reviewer", "trust_anchor_id": "review-key-v1",
        "supported_binding_types": ["model_dataset_v1"],
    }))
    with pytest.raises(ContractError, match="not registered"):
        validate_provider_config_ref(
            "provider.json", trust_root=root, registered_names=set(),
            allowed_trust_anchor_ids={"review-key-v1"},
        )
    path.write_text(json.dumps({
        "provider_id": "external-reviewer", "trust_anchor_id": "review-key-v1",
        "supported_binding_types": ["not_a_binding"],
    }))
    with pytest.raises(ContractError, match="unsupported quality binding"):
        validate_provider_config_ref(
            "provider.json", trust_root=root, registered_names={"provider.json"},
            allowed_trust_anchor_ids={"review-key-v1"},
        )


def test_research_layout_resolves_relative_paths_under_data_root(tmp_path: Path) -> None:
    path = tmp_path / "research_runs/requests" / f"request={REQUEST_GENERATION}" / f"run={RUN_ID}" / "manifest.json"
    path.parent.mkdir(parents=True)
    resolved = validate_research_layout(
        path.relative_to(tmp_path), data_root=tmp_path, kind="request",
        request_generation_id=REQUEST_GENERATION, run_id=RUN_ID,
    )
    assert resolved == path
    with pytest.raises(ContractError, match="does not match request layout"):
        validate_research_layout(
            "other/manifest.json", data_root=tmp_path, kind="request",
            request_generation_id=REQUEST_GENERATION, run_id=RUN_ID,
        )


def test_owning_layer_read_boundaries(tmp_path: Path) -> None:
    frame = _adjusted_frame()
    label_manifest = LabelBuilder(name="return_5d", semantic_version="1.0.0").build(
        frame, upstream_bindings=[_binding(frame)]
    )
    directory = tmp_path / "label_sets" / f"generation={label_manifest['generation_id']}"
    directory.mkdir(parents=True)
    frame[["instrument", "datetime"]].rename(columns={"datetime": "decision_date"}).assign(label=pd.Series([None] * len(_adjusted_frame()), dtype="float64")).to_parquet(directory / "data.parquet", index=False)
    (directory / "manifest.json").write_text(json.dumps(label_manifest))
    manifest, stored = LabelStore(tmp_path).read_frame(label_manifest["generation_id"])
    assert manifest["generation_id"] == label_manifest["generation_id"]
    assert len(stored) == len(frame)

    schema = FeatureSchemaBuilder().build(frame, source_factor_set="basic", source_factor_version="1.0.0")
    schema_directory = tmp_path / "feature_schemas" / f"generation={schema['generation_id']}"
    schema_directory.mkdir(parents=True)
    (schema_directory / "manifest.json").write_text(json.dumps(schema))
    assert FeatureSchemaStore(tmp_path).read_schema(schema["generation_id"])["generation_id"] == schema["generation_id"]


def test_factor_store_read_manifest_boundary(tmp_path: Path) -> None:
    manifest = json.loads((ROOT / "config/schemas/fixtures/factor-manifest-v1-valid.json").read_text())
    manifest["factor_version"] = "1.0.0"
    manifest["factor_definitions"][0]["version"] = "1.0.0"
    manifest["data_checksum_sha256"] = file_sha256_bytes(b"partition-bytes")
    generation, digest = factor_manifest_identities({
        key: value for key, value in manifest.items()
        if key not in {"generation_id", "manifest_digest_sha256"}
    })
    manifest["generation_id"] = generation
    manifest["manifest_digest_sha256"] = digest
    partition = (
        tmp_path / "factors" / "dataset=bars_daily" / "schema_version=research-v1" /
        "factor_set=basic" / "factor_version=1.0.0" / "date=2026-08-21"
    )
    partition.mkdir(parents=True)
    (partition / "manifest.json").write_text(json.dumps(manifest))
    (partition / "data.parquet").write_bytes(b"partition-bytes")
    store = FactorStore(tmp_path, FactorRegistry(ROOT))
    assert store.read_manifest(generation)["generation_id"] == generation
    with pytest.raises(ContractError):
        store.read_partition(generation)


def _adjusted_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=10)
    rows = []
    for instrument_index in range(2):
        for day in dates:
            rows.append({
                "instrument": f"INST{instrument_index:04d}", "datetime": day,
                "close": 10.0 + instrument_index, "adj_factor": 1.0 + 0.01 * instrument_index,
                "limit_up": False, "limit_down": False, "delisted": False, "suspended": False,
                "listing_date": pd.Timestamp("2020-01-01", tz="UTC"),
            })
    return pd.DataFrame(rows)


def _binding(frame: pd.DataFrame) -> dict:
    ordered = frame.sort_values(["instrument", "datetime"], kind="mergesort")
    checksum = sha256_json({"rows": [
        [str(row[0]), pd.Timestamp(row[1]).isoformat(), float(row[2]), float(row[3]), bool(row[4]), str(pd.Timestamp(row[5]).date())]
        for row in ordered[["instrument", "datetime", "close", "adj_factor", "suspended", "listing_date"]].itertuples(index=False)
    ]})
    return {
        "binding": "adjusted_price", "dataset": "bars_adjusted", "schema_version": "adjusted-v1",
        "partition_date": "2026-01-15", "generation_id": "0" * 64,
        "data_checksum_sha256": checksum, "visible_cutoff": "2026-01-15T15:00:00+08:00",
    }



def test_portfolio_definition_binding() -> None:
    fixture = json.loads((ROOT / "evidence/portfolio-backtest/phase-0/fixtures/valid_portfolio_definition.json").read_text())
    fixture["quality_report_checksum_sha256"] = "0" * 64
    fixture["generation_id"], fixture["manifest_digest_sha256"] = model_manifest_identities(
        {**fixture, "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
        schema_name="portfolio_definition",
    )
    checks = [
        {"name": "weight_scheme_valid", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
        {"name": "constraints_within_bounds", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
        {"name": "universe_binding_resolved", "threshold": 0, "observed": 0, "level": "error", "result": "passed"},
    ]
    decision = create_reviewed_quality_decision(
        binding_type="portfolio_definition_v1", policy="reject_all", status="passed",
        checks=checks, errors=[], warnings=[], producer_code_fingerprint="0" * 64,
        private_key_pem=REVIEWER_PRIVATE_KEY,
    )
    report, _ = bind_reviewed_quality_decision(
        decision, binding_type="portfolio_definition_v1",
        subject_generation_id=fixture["generation_id"],
    )
    wrapped = {
        "contract_version": 1, "schema_version": "1.0.0",
        "binding_type": "portfolio_definition_v1",
        "subject_generation_id": fixture["generation_id"],
        "subject_manifest_digest_sha256": None,
        "owning_report": report,
        "decision_checksum_sha256": report["report_checksum_sha256"],
        "provider_id": "external-model-quality-reviewer-v1",
        "trust_anchor_id": report["key_id"],
    }
    definition, bound_report = PortfolioDefinitionBinding.bind(
        definition=fixture, quality_decision=wrapped
    )
    assert report["bound_generation_id"] == definition["generation_id"]


def test_universe_snapshot_store_read_boundaries(tmp_path: Path) -> None:
    base = {
        "universe_version": 1, "universe_id": "research-core",
        "source": "config/universe/research-whitelist.txt",
        "snapshot_time": "2026-01-05T00:00:00Z", "visibility_time": "2026-01-05T00:00:00Z",
        "valid_from": "2026-01-05", "valid_to": None,
        "members_artifact": {"path": "members.csv", "checksum_sha256": "0" * 64},
        "membership_evidence": "static research whitelist; not PIT index membership",
    }
    members = pd.DataFrame({"instrument": ["A", "B"]})
    UniverseSnapshotStore(tmp_path).save(tmp_path, base, members)
    manifest = json.loads(next((tmp_path / "universes").rglob("manifest.json")).read_text())
    store = UniverseSnapshotStore(tmp_path)
    assert store.read_manifest(manifest["generation_id"])["universe_id"] == "research-core"
    stored = store.read_members(manifest["generation_id"], universe_id="research-core")
    assert stored["instrument"].tolist() == ["A", "B"]
