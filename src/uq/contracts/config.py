from __future__ import annotations

from pathlib import Path

import yaml

from .capabilities import DatasetContract, SourceCapability
from .schema import ContractError, Schema

_ROW_POLICY_KEYS = {
    "verified_only",
    "allow_unverified",
    "lifecycle_auxiliary_sources",
    "allow_unknown_missing",
    "raw_capture",
}
_CONFIG_KEYS = {"dataset", "schema_version", "required_fields", "owners", "primary_source", "row_policy", "cross_validation", "sources"}
_SOURCE_KEYS = {"priority", "fallback", "provides", "coverage", "authentication", "latency_class", "quota", "reliability", "correction_window", "revision_support"}


def load_dataset_contract(path: str | Path, schema: Schema | None = None) -> DatasetContract:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    unknown = set(payload) - _CONFIG_KEYS
    if unknown:
        raise ContractError(f"unknown dataset config keys: {sorted(unknown)}")
    row_policy = dict(payload.get("row_policy", {}))
    unknown_row_policy = set(row_policy) - _ROW_POLICY_KEYS
    if unknown_row_policy:
        raise ContractError(f"unknown row policy keys: {sorted(unknown_row_policy)}")
    verified_only = bool(row_policy.get("verified_only", False))
    allow_unverified = bool(row_policy.get("allow_unverified", not verified_only))
    if verified_only and allow_unverified:
        raise ContractError("row policy cannot set both verified_only and allow_unverified")

    sources: dict[str, SourceCapability] = {}
    for name, values in payload["sources"].items():
        unknown_source = set(values) - _SOURCE_KEYS
        if unknown_source:
            raise ContractError(f"unknown source keys for {name}: {sorted(unknown_source)}")
        if "priority" not in values or "provides" not in values:
            raise ContractError(f"source {name} requires priority and provides")
        sources[name] = SourceCapability(
            source_name=name,
            dataset=payload["dataset"],
            schema_version=str(payload["schema_version"]),
            priority=int(values["priority"]),
            provides=frozenset(values["provides"]),
            fallback=bool(values.get("fallback", False)),
            coverage=values.get("coverage"),
            authentication=str(values.get("authentication", "none")),
            latency_class=str(values.get("latency_class", "end_of_day")),
            quota=values.get("quota"),
            reliability=str(values.get("reliability", "unknown")),
            correction_window=values.get("correction_window"),
            revision_support=bool(values.get("revision_support", False)),
        )

    contract = DatasetContract(
        dataset=payload["dataset"],
        schema_version=str(payload["schema_version"]),
        required_fields=tuple(payload["required_fields"]),
        owners=dict(payload.get("owners", {})),
        primary_source=payload["primary_source"],
        row_policy={
            "verified_only": verified_only,
            "allow_unverified": allow_unverified,
            "lifecycle_auxiliary_sources": tuple(row_policy.get("lifecycle_auxiliary_sources", ())),
            "allow_unknown_missing": bool(row_policy.get("allow_unknown_missing", False)),
            "raw_capture": bool(row_policy.get("raw_capture", True)),
        },
        cross_validation=dict(payload.get("cross_validation", {})),
        sources=sources,
    )
    if schema is not None:
        _validate_against_schema(contract, schema)
    return contract


def _validate_against_schema(contract: DatasetContract, schema: Schema) -> None:
    if contract.dataset != schema.dataset or contract.schema_version != schema.version:
        raise ContractError(f"contract {contract.dataset}.{contract.schema_version} does not match schema {schema.name}")
    unknown_fields = set(contract.required_fields) - set(schema.fields)
    if unknown_fields:
        raise ContractError(f"required fields missing from schema: {sorted(unknown_fields)}")
    unknown_owners = set(contract.owners) - set(schema.fields)
    if unknown_owners:
        raise ContractError(f"owned fields missing from schema: {sorted(unknown_owners)}")
    for source_name, capability in contract.sources.items():
        missing = set(capability.provides) - set(schema.fields)
        if missing:
            raise ContractError(f"source {source_name} provides unknown schema fields: {sorted(missing)}")
    for field, rule in contract.cross_validation.items():
        if field not in schema.fields:
            raise ContractError(f"cross-validation field missing from schema: {field}")
        if rule.get("compare_with") not in contract.sources:
            raise ContractError(f"cross-validation comparator is not a declared source: {field}")
