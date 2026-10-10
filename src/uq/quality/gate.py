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
        # cr.md H2: duplicate keys make reindex() raise a bare ValueError
        # downstream, escaping the ContractError taxonomy; reject them here.
        for source, frame in indexed.items():
            if frame.index.has_duplicates:
                raise ContractError(f"duplicate keys in source frame: {source}")
        accepted = primary.sort_values(key).reset_index(drop=True)
        primary_indexed = accepted.set_index(key)
        if primary_indexed.index.has_duplicates:
            raise ContractError(f"duplicate keys in primary source: {self.primary_source}")
        conflicts: list[dict[str, Any]] = []
        errors: list[str] = []
        coverage: dict[str, FieldCoverage] = {}
        # cr.md H2: an empty primary validates vacuously and would publish an
        # empty partition; refuse it at the gate (daily.py adds its own
        # instrument-context message on top of this).
        if accepted.empty:
            errors.append("primary frame is empty; refusing an empty publication")
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
            if field_name not in accepted.columns:
                continue  # rule targets a field the primary does not carry
            # cr.md H2: a configured comparison that cannot run must not be
            # skipped silently — the contract's validation intent is unmet.
            if comparator not in indexed:
                errors.append(f"cross-validation comparator {comparator} unavailable for {field_name}")
                continue
            if field_name not in indexed[comparator].columns:
                errors.append(f"cross-validation comparator {comparator} lacks field {field_name}")
                continue
            secondary = indexed[comparator][field_name].reindex(primary_indexed.index)
            primary_values = primary_indexed[field_name]
            valid = secondary.notna() & primary_values.notna()
            absolute = (primary_values - secondary).abs()
            tolerance_pct = float(rules.get("max_abs_diff_pct", rules.get("max_rel_diff_pct", 0)))
            if "max_rel_diff_pct" in rules:
                # cr.md H2: a zero primary makes the relative difference NaN and
                # used to excuse any nonzero comparator value; zero-vs-nonzero
                # is an unbounded relative divergence and must conflict.
                relative = absolute / primary_values.abs().replace(0, np.nan)
                mismatch = valid & ((relative > tolerance_pct) | ((primary_values.abs() == 0) & (absolute > 0)))
            else:
                mismatch = valid & (absolute > primary_values.abs() * tolerance_pct)
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
            coverage_ratio = compared_rows / total_rows if total_rows > 0 else 0.0
            meets_coverage = total_rows > 0 and coverage_ratio >= self.minimum_coverage
            # cr.md H2: a rule that compared zero rows validated nothing, and a
            # declared minimum_coverage must actually reject under-coverage.
            if total_rows > 0 and compared_rows == 0:
                errors.append(f"cross-validation for {field_name} compared zero rows")
            elif self.minimum_coverage > 0 and not meets_coverage:
                errors.append(
                    f"cross-validation coverage for {field_name} is {coverage_ratio:.4f}, below minimum {self.minimum_coverage}"
                )
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
