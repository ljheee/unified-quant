from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd

from ..errors import ContractError


@dataclass(frozen=True)
class FieldCoverage:
    source: str
    owner: str | None
    validated_by: list[str]
    compared_rows: int
    missing_primary_rows: int
    missing_secondary_rows: int
    mismatched_rows: int

@dataclass(frozen=True)
class QualityReport:
    accepted: bool
    policy: str
    frame: pd.DataFrame
    lineage: dict[str, FieldCoverage]
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    key: list[str] = field(default_factory=list)
    required_fields: set[str] = field(default_factory=set)
    minimum_coverage: float = 0.0
    input_fingerprints: dict[str, str] = field(default_factory=dict)

    @property
    def checksum(self) -> str:
        payload = {
            "accepted": self.accepted,
            "policy": self.policy,
            "key": self.key,
            "required_fields": sorted(self.required_fields),
            "minimum_coverage": self.minimum_coverage,
            "input_fingerprints": self.input_fingerprints,
            "conflicts": self.conflicts,
            "lineage": {name: vars(item) for name, item in self.lineage.items()},
        }
        raw = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(raw).hexdigest()


class CrossSourceGate:
    """Merge primary rows with owned/complement fields and structured validation."""

    def __init__(
        self,
        primary_source: str,
        cross_validation: dict[str, dict[str, Any]],
        owners: dict[str, str] | None = None,
        minimum_coverage: float = 0.0,
        policy: str = "reject_all",
    ) -> None:
        if policy not in {"reject_all", "accept_with_warnings"}:
            raise ValueError(f"unsupported policy: {policy}")
        self.primary_source = primary_source
        self.cross_validation = cross_validation
        self.owners = dict(owners or {})
        self.minimum_coverage = float(minimum_coverage)
        self.policy = policy

    def merge(self, frames: dict[str, pd.DataFrame], key: list[str], required_fields: set[str] | None = None) -> QualityReport:
        if self.primary_source not in frames:
            raise ContractError(f"primary source unavailable: {self.primary_source}")
        primary = frames[self.primary_source]
        indexed = {
            source: frame.set_index(key).sort_index()
            for source, frame in frames.items()
            if source != self.primary_source
        }
        accepted = primary.sort_values(key).reset_index(drop=True)
        primary_indexed = accepted.set_index(key)
        conflicts: list[dict[str, Any]] = []
        errors: list[str] = []
        coverage: dict[str, FieldCoverage] = {}
        input_fingerprints = {
            source: hashlib.sha256(frame.to_csv(index=True).encode()).hexdigest()
            for source, frame in frames.items()
        }

        # Owned/complement fields must come from their declared provider.
        for field_name, owner in self.owners.items():
            if field_name in accepted.columns:
                continue
            if owner not in indexed:
                errors.append(f"required owned field {field_name} missing from owner {owner}")
                continue
            if field_name not in indexed[owner].columns:
                errors.append(f"required owned field {field_name} missing from owner {owner}")
                continue
            values = indexed[owner][field_name].reindex(primary_indexed.index)
            if values.isna().any() and (required_fields and field_name in required_fields):
                errors.append(f"owned field {field_name} has missing values")
            accepted[field_name] = values.to_numpy()
            coverage[field_name] = FieldCoverage(
                source=owner, owner=owner, validated_by=[], compared_rows=0,
                missing_primary_rows=0, missing_secondary_rows=int(values.isna().sum()), mismatched_rows=0,
            )

        for field_name, rules in self.cross_validation.items():
            comparator = str(rules.get("compare_with", ""))
            if field_name not in accepted.columns or comparator not in indexed or field_name not in indexed[comparator].columns:
                continue
            secondary = indexed[comparator][field_name].reindex(primary_indexed.index)
            primary_values = primary_indexed[field_name]
            valid = secondary.notna() & primary_values.notna()
            absolute = (primary_values - secondary).abs()
            relative = absolute / primary_values.abs().replace(0, np.nan)
            tolerance_pct = float(rules.get("max_abs_diff_pct", rules.get("max_rel_diff_pct", 0)))
            mismatch = valid & ((relative > tolerance_pct) if "max_rel_diff_pct" in rules else (absolute > primary_values.abs() * tolerance_pct))
            for index_value in primary_indexed.index[mismatch]:
                key_value = index_value if isinstance(index_value, tuple) else (index_value,)
                conflicts.append({
                    "key": dict(zip(key, key_value)),
                    "field": field_name,
                    "primary": float(primary_values.loc[index_value]),
                    "secondary": float(secondary.loc[index_value]),
                    "tolerance_pct": tolerance_pct,
                })
            compared_rows = int(valid.sum())
            total_rows = len(primary_indexed)
            meets_coverage = total_rows > 0 and compared_rows / total_rows >= self.minimum_coverage
            coverage[field_name] = FieldCoverage(
                source=self.primary_source, owner=None,
                validated_by=[comparator] if meets_coverage and not mismatch.any() else [],
                compared_rows=compared_rows,
                missing_primary_rows=int((~primary_values.notna()).sum()),
                missing_secondary_rows=int((~secondary.notna()).sum()),
                mismatched_rows=int(mismatch.sum()),
            )

        for column in accepted.columns:
            if column not in key and column not in coverage:
                coverage[column] = FieldCoverage(
                    source=self.primary_source, owner=None, validated_by=[],
                    compared_rows=0, missing_primary_rows=0, missing_secondary_rows=0, mismatched_rows=0,
                )

        if conflicts and self.policy == "reject_all":
            errors.append(f"cross-source quality rejected {len(conflicts)} values")
        accepted_flag = not errors
        return QualityReport(
            accepted=accepted_flag,
            policy=self.policy,
            frame=accepted.reset_index(drop=True),
            lineage=coverage,
            conflicts=conflicts,
            errors=errors,
            key=list(key),
            required_fields=set(required_fields or ()),
            minimum_coverage=self.minimum_coverage,
            input_fingerprints=input_fingerprints,
        )
