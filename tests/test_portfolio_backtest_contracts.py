"""Phase 0 contract tests for portfolio and backtest layers."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from uq.contracts.model_layer import ModelContractLoader, model_manifest_identities
from uq.errors import ContractError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evidence/portfolio-backtest/phase-0/fixtures"
GOLDEN_DIR = ROOT / "evidence/portfolio-backtest/phase-0/golden-vectors"

# Quality checksum PARTICIPATES in stable generation for these four families.
FAMILY_EXCLUDES = {
    "portfolio_definition": set(),
    "target_weights": {"logical_fingerprint"},
    "backtest_config": set(),
    "backtest_result": set(),
}


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / f"{name}.json") as f:
        return json.load(f)


def _make_valid(schema_name: str, fixture_name: str) -> dict:
    payload = _load_fixture(fixture_name)
    payload["quality_report_checksum_sha256"] = "0" * 64
    exclude = FAMILY_EXCLUDES[schema_name]
    gen, digest = model_manifest_identities(
        {**payload, "generation_id": "0" * 64, "manifest_digest_sha256": "0" * 64},
        schema_name=schema_name,
        exclude_fields=exclude,
    )
    payload["generation_id"] = gen
    payload["manifest_digest_sha256"] = digest
    return payload


class TestPortfolioDefinitionSchema:
    def test_valid_fixture(self):
        ModelContractLoader.validate("portfolio_definition", _make_valid("portfolio_definition", "valid_portfolio_definition"))

    def test_negative_fixture_1(self):
        with pytest.raises(ContractError):
            ModelContractLoader.validate("portfolio_definition", _load_fixture("negative_portfolio_definition"))

    def test_negative_fixture_2(self):
        with pytest.raises(ContractError):
            ModelContractLoader.validate("portfolio_definition", _load_fixture("negative_portfolio_definition_2"))


class TestTargetWeightsSchema:
    def test_valid_fixture(self):
        ModelContractLoader.validate("target_weights", _make_valid("target_weights", "valid_target_weights"))

    def test_negative_fixture_1(self):
        with pytest.raises(ContractError):
            ModelContractLoader.validate("target_weights", _load_fixture("negative_target_weights"))

    def test_negative_fixture_2(self):
        with pytest.raises(ContractError):
            ModelContractLoader.validate("target_weights", _load_fixture("negative_target_weights_2"))


class TestBacktestConfigSchema:
    def test_valid_fixture(self):
        ModelContractLoader.validate("backtest_config", _make_valid("backtest_config", "valid_backtest_config"))

    def test_negative_fixture_1(self):
        with pytest.raises(ContractError):
            ModelContractLoader.validate("backtest_config", _load_fixture("negative_backtest_config"))

    def test_negative_fixture_2(self):
        with pytest.raises(ContractError):
            ModelContractLoader.validate("backtest_config", _load_fixture("negative_backtest_config_2"))


class TestBacktestResultSchema:
    def test_valid_fixture(self):
        ModelContractLoader.validate("backtest_result", _make_valid("backtest_result", "valid_backtest_result"))

    def test_negative_fixture_1(self):
        with pytest.raises(ContractError):
            ModelContractLoader.validate("backtest_result", _load_fixture("negative_backtest_result"))

    def test_negative_fixture_2(self):
        with pytest.raises(ContractError):
            ModelContractLoader.validate("backtest_result", _load_fixture("negative_backtest_result_2"))


class TestGoldenVectors:
    """Persist deterministic identity golden vectors to disk."""

    FAMILIES = [
        ("portfolio_definition", "valid_portfolio_definition"),
        ("target_weights", "valid_target_weights"),
        ("backtest_config", "valid_backtest_config"),
        ("backtest_result", "valid_backtest_result"),
    ]

    def test_golden_vectors_deterministic_and_persisted(self):
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        vectors = {}
        for family, fixture in self.FAMILIES:
            payload = _make_valid(family, fixture)
            exclude = FAMILY_EXCLUDES[family]
            gen_a, digest_a = model_manifest_identities(
                copy.deepcopy(payload), schema_name=family, exclude_fields=exclude
            )
            gen_b, digest_b = model_manifest_identities(
                copy.deepcopy(payload), schema_name=family, exclude_fields=exclude
            )
            assert gen_a == gen_b and digest_a == digest_b
            vectors[f"{family}.v1"] = {"generation_id": gen_a, "manifest_digest_sha256": digest_a}

        golden_path = GOLDEN_DIR / "identity-golden-vectors.json"
        existing = json.loads(golden_path.read_text()) if golden_path.exists() else None
        if existing is not None:
            # Deterministic: same values every run
            for key in existing:
                assert vectors[key] == existing[key], f"golden vector drift for {key}"
        else:
            golden_path.write_text(json.dumps(vectors, indent=2, sort_keys=True) + "\n")

    def test_quality_checksum_participates_in_generation(self):
        base = _make_valid("portfolio_definition", "valid_portfolio_definition")
        modified = copy.deepcopy(base)
        modified["quality_report_checksum_sha256"] = "b" * 64

        gen_base, _ = model_manifest_identities(
            copy.deepcopy(base), schema_name="portfolio_definition",
            exclude_fields=FAMILY_EXCLUDES["portfolio_definition"],
        )
        gen_mod, _ = model_manifest_identities(
            modified, schema_name="portfolio_definition",
            exclude_fields=FAMILY_EXCLUDES["portfolio_definition"],
        )
        assert gen_base != gen_mod  # quality changes stable generation


class TestRunMetadataExcludedFromGeneration:
    def test_run_id_and_created_at_do_not_change_generation(self):
        base = _make_valid("portfolio_definition", "valid_portfolio_definition")
        modified = copy.deepcopy(base)
        modified["run_id"] = "99999999-9999-9999-9999-999999999999"
        modified["created_at"] = "2027-12-31T23:59:59Z"

        gen_a, _ = model_manifest_identities(
            copy.deepcopy(base), schema_name="portfolio_definition",
            exclude_fields=FAMILY_EXCLUDES["portfolio_definition"],
        )
        gen_b, _ = model_manifest_identities(
            modified, schema_name="portfolio_definition",
            exclude_fields=FAMILY_EXCLUDES["portfolio_definition"],
        )
        assert gen_a == gen_b
