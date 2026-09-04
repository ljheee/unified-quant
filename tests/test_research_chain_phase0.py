from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from uq.contracts.gate_contracts import canonical_json
from uq.contracts.model_layer import (
    ModelContractLoader,
    research_contract_identities,
    research_stage_plan_sha256,
)
from uq.errors import ContractError
from uq.research_chain.contracts import validate_provider_config_ref, validate_research_layout

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
        validate_provider_config_ref(config_ref, trust_root=ROOT / "config", registered_names=set())


def test_provider_config_accepts_registered_relative_path(tmp_path: Path) -> None:
    root = tmp_path / "trust"
    (root / "providers").mkdir(parents=True)
    path = root / "providers/default.json"
    path.write_text("{}")
    result = validate_provider_config_ref(
        "providers/default.json", trust_root=root, registered_names={"providers/default.json"}
    )
    assert result.provider_id == "providers/default.json"
    assert result.config_path == str(path.resolve())


def test_provider_config_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "trust"
    (root / "providers").mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (root / "providers/escape.json").symlink_to(outside)
    with pytest.raises(ContractError, match="escapes trust root"):
        validate_provider_config_ref(
            "providers/escape.json", trust_root=root, registered_names={"providers/escape.json"}
        )
