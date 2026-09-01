# Qlib Factor Adapter Specification

Status: **implemented v1.0.0; Alpha158 only; Alpha360 contract-only**

Upstream source: `specs/layers/factor-layer-spec.md`
External dependency: `pyqlib` (Alpha158/Alpha360 expression engine)

## 1. Purpose

This adapter bridges Microsoft Qlib's mature factor expression engine
(Alpha158/Alpha360) into UQ's factor governance pipeline. Qlib computes
factor values; UQ provides canonical manifests, immutable partitions,
quality gates, and accepted store publication.

The adapter does NOT replace UQ's FactorEngine. It is a **factor source
plugin** that plugs into the existing governance infrastructure.

## 2. Design Principle

```
UQ governed price data (canonical, PIT-safe)
  → Qlib expression engine (Alpha158/360 formulas)
  → UQ governance wrapper (manifest, checksum, quality)
  → UQ accepted factor partitions
```

Qlib answers "what to compute". UQ answers "how to govern it".

## 3. Non-Goals

- modifying Qlib source code;
- replacing UQ FactorEngine or FactorStore;
- computing factors from ungoverned data sources;
- bypassing UQ quality gates or manifest checks;
- online factor computation or streaming;
- factors requiring `$vwap` (not a governed column);
- factors with forward-looking references (`Ref($x, -N)` where N > 0).

## 3A. Dependency Gating

`pyqlib` is an optional extra (`[project.optional-dependencies] qlib = ["pyqlib>=0.9.7"]`).
The adapter module uses a lazy import. If pyqlib is not installed, a typed
`QlibNotInstalledError` is raised at compute time. The broad unified gate does
not install the optional extra; Qlib compute tests are skipped unless pyqlib
is present. Governance-only tests still run. A qlib-enabled environment is
required for Phase 1 compute evidence. The installed `qlib.__version__` is
recorded in every manifest.

## 3B. Compute Strategy

Qlib's expression engine requires `qlib.init(provider_uri)` to resolve
`$column` references. The adapter uses a temporary Qlib data directory:

1. Convert UQ canonical data to Qlib `.bin` format in a temp directory
   (calendar.txt, instruments/all.txt, features/<inst>/<col>.day.bin).
2. Call `qlib.init(provider_uri=tempdir)`.
3. Run Alpha158 expressions via `D.features()` over the full range.
4. Convert result back to UQ canonical format (instrument, datetime keys).
5. Clean up temp directory and re-init Qlib to previous state if needed.

Qlib `init()` is a global singleton. The adapter tracks whether it was
previously initialized and restores state after computation.

UQ FactorStore publishes per-date partitions. After computation, the
adapter slices the result into per-date frames for publication.

## 3C. VWAP Handling

Alpha158 includes factors that reference `$vwap`. UQ governed canonical
data does not include vwap. These factors are **excluded** from the
adapter's factor set. The exact exclusion list is maintained in the
factor-set definition JSON.

## 3D. Lookahead Audit

Qlib Alpha158 expressions that use `Ref($column, -N)` (future data) are
**excluded**. The adapter performs a deterministic expression-shift audit at
definition load time and raises a typed error if a forward-looking reference
is detected.

## 4. Data Flow

1. The caller supplies a UQ canonical multi-date OHLCV DataFrame.
2. Adapter converts it to a temporary Qlib file provider.
3. Qlib expression evaluation runs the reviewed Alpha158 set over the full range.
4. Adapter rejects any negative `Ref` shift before computation.
5. Adapter converts result back to UQ canonical format (instrument, datetime keys).
6. Result is sliced into per-date partitions.
7. UQ governance wrapper generates manifest, binds quality report.
8. `FactorStore.publish()` writes immutable partition per date.

The adapter writes UQ governed data to a temporary Qlib-format directory
(calendars, instruments, features as `.bin` files), calls `qlib.init()`,
evaluates expressions, then cleans up. UQ's governance remains the single
data source; Qlib's file layer is used only as a computation backend.

## 5. Factor Set Definitions

### 5.1 `alpha158-v1`

Reviewed 157-factor subset of Qlib's Alpha158 expression set. It uses only
the governed columns `$open`, `$high`, `$low`, `$close`, and `$volume`.
`VWAP0` is excluded because `$vwap` is not governed. Expressions may use
positive `Ref(..., N)` history; negative lookahead references are rejected.

### 5.2 `alpha360-v1`

Draft contract only. It has no reviewed factors and cannot be computed or
published until a future reviewed version supplies its expression set.

### 5.3 Selection Criteria

A factor is included if:
1. Its expression uses only governed columns (`$open`, `$high`, `$low`,
   `$close`, and `$volume`; no `$vwap`).
2. All operations are backward-looking (no future data leakage).
3. It produces finite values for at least 80% of the universe.
4. It does not duplicate an existing UQ factor (e.g. return_1d).

### 5.4 Name Mapping

Qlib factor names (KMID, STD20, etc.) are mapped to UQ snake_case names
via a deterministic mapping table stored in the factor-set definition JSON.
Example: `KMID` → `qlib_kmid`, `STD20` → `qlib_std20`.

### 5.5 Version Pinning

The adapter records the installed pyqlib version in each manifest as
`qlib_version`. Different Qlib versions may produce slightly different
values due to floating point ordering. A change in `qlib_version` requires
a new `factor_version` to maintain identity stability.

## 6. Governance Contract

Every published partition must have:

| Field | Source |
|---|---|
| factor_set | `alpha158` or `alpha360` |
| factor_version | semver string |
| generation_id | SHA-256 of canonical identity payload |
| manifest_digest_sha256 | SHA-256 of full manifest |
| data_checksum_sha256 | SHA-256 of Parquet artifact |
| quality_report_checksum_sha256 | Factor quality report file digest |
| implementation_fingerprint | SHA-256 of adapter module bytes |
| engine_contract.qlib_expression_set | `Alpha158` for this release |
| engine_contract.engine_version | installed pyqlib version string |

## 7. Quality Gates

Same as factor layer spec §9:
- null rate below threshold
- coverage above minimum
- no infinite values
- key reconciliation pass
- external quality review required

## 8. Identity Model

Follows factor-layer-spec §6A:
- `generation_id`: stable content identity excluding run metadata.
- `manifest_digest_sha256`: includes generation_id.
- Quality report checksum participates in generation.

## 9. Acceptance Criteria Summary

### Phase 0
- Factor-set definition schema v2, alpha158-v1, and contract-only alpha360-v1.
- Lookahead audit and deterministic Qlib-to-UQ name mapping.
- Persistent phase record and unified gate evidence on final HEAD.

### Phase 1
- Single-process Qlib computation on a synthetic multi-date panel.
- Factor manifest v2 records reviewed expressions, adapter fingerprint,
  Qlib version, and Alpha158 expression set.
- Factor quality report binding passes immutable publication/read gates.
- Same governed input produces the same generation ID.

### Phase 2
- Final HEAD gate evidence and release reconciliation.
- Full lineage remains governed by the existing factor-layer rules.

## 10. Module Placement

The adapter lives in `src/uq/factors/qlib_adapter.py` (not `src/uq/adapters/`)
because it is a factor source plugin, not a cross-cutting adapter. This keeps
it co-located with the other factor computation modules.

## 11. Implementation Plan Reference

See `specs/layers/qlib-factor-adapter-implementation-plan.md`.
