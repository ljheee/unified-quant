from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

_DTYPE_ALIASES = {"string": "object", "float64": "float64", "int64": "int64", "bool": "bool"}
_COMPATIBLE_DTYPES = {
    "date32": {"datetime64[ns]", "datetime64[us]", "object"},
}
_FIELD_RULE_KEYS = {"dtype", "nullable", "unit", "adjustment", "timezone", "meaning", "resolution", "pattern", "enum", "default", "zero_policy", "semantics"}
_SCHEMA_KEYS = {"dataset", "version", "status", "primary_key", "sort_key", "fields", "invariants", "compatibility", "migration"}


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class Schema:
    dataset: str
    version: str
    primary_key: tuple[str, ...]
    sort_key: tuple[str, ...]
    fields: dict[str, dict[str, Any]]
    invariants: dict[str, str]
    compatibility: str = "exact"
    source_path: Path | None = None

    @property
    def name(self) -> str:
        return f"{self.dataset}.{self.version}"

    def validate(self, frame: pd.DataFrame) -> None:
        if self.compatibility == "exact":
            extra = [column for column in frame.columns if column not in self.fields]
            if extra:
                raise ContractError(f"{self.name} has unexpected fields: {extra}")
        missing = [field for field in self.fields if field not in frame.columns]
        if missing:
            raise ContractError(f"{self.name} missing fields: {missing}")
        if frame.duplicated(list(self.primary_key)).any():
            raise ContractError(f"{self.name} duplicate primary key")

        for field, rules in self.fields.items():
            series = frame[field]
            dtype = rules["dtype"]
            if dtype == "string":
                invalid = series.notna() & ~series.map(lambda value: isinstance(value, str))
                if invalid.any():
                    raise ContractError(f"field {field} must contain strings")
                if pattern := rules.get("pattern"):
                    bad = series.notna() & ~series.str.fullmatch(pattern)
                    if bad.any():
                        raise ContractError(f"field {field} violates pattern")
                if enum := rules.get("enum"):
                    bad = series.notna() & ~series.isin(enum)
                    if bad.any():
                        raise ContractError(f"field {field} contains values outside enum")
            elif dtype == "date32":
                accepted = {"datetime64[ns]", "datetime64[us]"}
                if str(series.dtype) not in accepted:
                    raise ContractError(f"field {field} dtype {series.dtype}, expected date32-backed datetime")
                normalized = series
                if not normalized.eq(normalized.dt.normalize()).all():
                    raise ContractError(f"field {field} must contain day-resolution dates")
            elif str(series.dtype) != _DTYPE_ALIASES[dtype]:
                raise ContractError(f"field {field} dtype {series.dtype}, expected {dtype}")
            if not rules.get("nullable", True) and series.isna().any():
                raise ContractError(f"non-nullable field {field} contains null")

        self._validate_invariants(frame)

    def _validate_invariants(self, frame: pd.DataFrame) -> None:
        for name, expression in self.invariants.items():
            result = pd.eval(expression, resolvers=[frame], engine="python")
            if not bool(result.all()):
                raise ContractError(f"invariant failed: {name}: {expression}")


def load_schema(path: str | Path) -> Schema:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    unknown = set(payload) - _SCHEMA_KEYS
    if unknown:
        raise ContractError(f"unknown schema keys: {sorted(unknown)}")
    for field, rules in payload["fields"].items():
        unknown_rules = set(rules) - _FIELD_RULE_KEYS
        if unknown_rules:
            raise ContractError(f"unknown field rules for {field}: {sorted(unknown_rules)}")
        if "dtype" not in rules or "nullable" not in rules:
            raise ContractError(f"field {field} requires dtype and nullable")
    schema = Schema(
        dataset=payload["dataset"],
        version=str(payload["version"]),
        primary_key=tuple(payload["primary_key"]),
        sort_key=tuple(payload.get("sort_key", payload["primary_key"])),
        source_path=Path(path).resolve(),
        fields=payload["fields"],
        invariants=payload.get("invariants", {}),
        compatibility=str(payload.get("compatibility", "exact")),
    )
    _validate_references(schema)
    return schema


def _validate_references(schema: Schema) -> None:
    if schema.compatibility not in {"exact", "additive_nullable"}:
        raise ContractError(f"unsupported compatibility: {schema.compatibility}")
    unknown_keys = set(schema.primary_key + schema.sort_key) - set(schema.fields)
    if unknown_keys:
        raise ContractError(f"keys reference unknown fields: {sorted(unknown_keys)}")
    for name, expression in schema.invariants.items():
        for field in schema.fields:
            if _uses_identifier(expression, field) and field not in frame_columns(schema):
                raise ContractError(f"invariant {name} references unknown field {field}")


def frame_columns(schema: Schema) -> set[str]:
    return set(schema.fields)


def _uses_identifier(expression: str, identifier: str) -> bool:
    import re
    return re.search(rf"\b{re.escape(identifier)}\b", expression) is not None
