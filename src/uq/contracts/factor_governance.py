from __future__ import annotations

from dataclasses import dataclass
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any


from .gate_contracts import validate_contract
from ..errors import ContractError


@dataclass(frozen=True)
class FactorSetDefinition:
    document: dict[str, Any]

    @property
    def factor_set(self) -> str:
        return self.document["factor_set"]

    @property
    def factor_version(self) -> str:
        return self.document["factor_version"]

    @property
    def factors(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.document["factors"])


class FactorRegistry:
    """Typed in-memory view over reviewed factor-set definition files."""

    def __init__(self, root: Path) -> None:
        self.root = root / "config" / "factor-sets"
        self._definitions: dict[tuple[str, str], FactorSetDefinition] = {}
        if self.root.exists():
            for path in sorted(self.root.glob("*.json")):
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                    _validate_factor_set(document)
                except Exception as exc:
                    raise ContractError(f"invalid factor-set definition: {path.name}") from exc
                key = (document["factor_set"], document["factor_version"])
                if key in self._definitions:
                    raise ContractError(f"duplicate factor-set version: {key}")
                self._definitions[key] = FactorSetDefinition(document)

    def get(self, factor_set: str, factor_version: str) -> FactorSetDefinition:
        definition = self._definitions.get((factor_set, factor_version))
        if definition is None:
            raise ContractError(f"unknown factor set/version: {factor_set}/{factor_version}")
        if definition.document["status"] != "reviewed":
            raise ContractError("factor set definition is not reviewed")
        return definition

    def resolve_dependencies(self, definition: FactorSetDefinition, *, seen: set[tuple[str, str]] | None = None) -> None:
        key = (definition.factor_set, definition.factor_version)
        seen = set() if seen is None else seen
        if key in seen:
            raise ContractError(f"cyclic factor-set dependency: {key}")
        seen.add(key)
        for dependency in definition.document["dependencies"]:
            if "/" not in dependency:
                raise ContractError(f"invalid factor-set dependency: {dependency}")
            factor_set, factor_version = dependency.split("/", 1)
            resolved = self.get(factor_set, factor_version)
            self.resolve_dependencies(resolved, seen=seen)

    @staticmethod
    def validate_identities(manifest: dict[str, Any]) -> None:
        from .gate_contracts import factor_manifest_identities

        unsigned = {
            key: value for key, value in manifest.items()
            if key not in {"generation_id", "manifest_digest_sha256"}
        }
        expected_generation, expected_digest = factor_manifest_identities(unsigned)
        if manifest["generation_id"] != expected_generation:
            raise ContractError("factor manifest generation mismatch")
        if manifest["manifest_digest_sha256"] != expected_digest:
            raise ContractError("factor manifest digest mismatch")

    def validate_manifest(self, manifest: dict[str, Any]) -> None:
        self.validate_identities(manifest)
        _validate_calendar_dates(manifest["partition_date"], *(item["partition_date"] for item in manifest["inputs"]))
        decision_time = datetime.fromisoformat(manifest["decision_time"])
        cutoff = datetime.fromisoformat(manifest["run_visible_cutoff"])
        if cutoff < decision_time:
            raise ContractError("run visible cutoff precedes decision time")
        for item in manifest["inputs"]:
            input_created_at = datetime.fromisoformat(item["upstream_created_at"])
            if input_created_at > cutoff:
                raise ContractError("upstream partition visibility violates factor run cutoff")
        definition = self.get(manifest["factor_set"], manifest["factor_version"])
        if manifest["quality"]["policy"] != definition.document["quality_policy"]:
            raise ContractError("factor manifest quality policy does not match reviewed definition")
        expected_factors = {
            item["name"]: item for item in definition.factors
        }
        actual_factors = {item["name"]: item for item in manifest["factor_definitions"]}
        if set(actual_factors) != set(expected_factors):
            raise ContractError("factor definitions do not match reviewed factor set")
        for name, actual in actual_factors.items():
            expected = expected_factors[name]
            if (
                actual["version"] != expected["version"]
                or actual["implementation_fingerprint"] != expected["implementation_fingerprint"]
            ):
                raise ContractError(f"changed factor implementation requires reviewed set-version action: {name}")
        if not definition.document["required_columns"]:
            raise ContractError("reviewed factor set has no required columns")
        columns = manifest["columns"]
        dtypes = manifest["dtypes"]
        if set(dtypes) != set(columns):
            raise ContractError("factor dtype map does not exactly match columns")
        if len(columns) < 2:
            raise ContractError("factor output must contain instrument and value columns")
        if manifest["row_count"] < 0:
            raise ContractError("negative factor row count")


def _validate_factor_set(document: dict[str, Any]) -> None:
    schema_path = Path(__file__).resolve().parents[3] / "config/schemas/factor-sets/factor_set.v1.json"
    from .gate_contracts import validate_contract_path

    validate_contract_path(schema_path, document)


def _validate_calendar_dates(*values: str) -> None:
    for value in values:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ContractError(f"invalid factor governance calendar date: {value}") from exc
