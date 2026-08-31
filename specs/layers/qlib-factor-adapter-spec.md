# Qlib Factor Adapter Specification

Status: **design v1.0.0; contract drafting in progress**

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

`pyqlib` is NOT added to `pyproject.toml` at this stage. The adapter module
uses a lazy import guarded by an explicit capability check. CI and gate
runner do not require pyqlib. If pyqlib is installed in the environment,
the adapter activates; otherwise, a typed `QlibNotInstalledError` is raised
at compute time. This follows the same pattern as the model layer's Qlib
dependency gating.

## 3B. Compute Strategy

Qlib Alpha158 computes factors over an entire date range in one pass,
producing a wide DataFrame (date × instrument × N columns). UQ
FactorStore publishes per-date partitions. The adapter:

1. Converts UQ canonical data to Qlib's expected format (date-indexed
   panel with OHLCV columns).
2. Runs Alpha158 expressions over the full range.
3. Slices the result into per-date frames.
4. Publishes each date partition via FactorStore.

This means Phase 1 E2E tests must use a synthetic multi-date panel, not
a single-day snapshot.

## 3C. VWAP Handling

Alpha158 includes factors that reference `$vwap`. UQ governed canonical
data does not include vwap. These factors are **excluded** from the
adapter's factor set. The exact exclusion list is maintained in the
factor-set definition JSON.

## 3D. Lookahead Audit

Qlib Alpha158 expressions that use `Ref($column, -N)` (future data) are
**excluded**. The adapter validates each factor's expression AST before
computation and raises a typed error if a forward-looking reference is
detected. This audit is performed at factor-set definition time, not at
runtime.

## 4. Data Flow

1. UQ `FactorContext` reads governed canonical data.
2. Adapter converts to Qlib's expected DataFrame format (pandas MultiIndex).
3. Qlib expression evaluation runs Alpha158/360 over the full range.
4. Adapter validates no forward-looking references in expression AST.
5. Adapter converts result back to UQ canonical format (instrument, datetime keys).
6. Result is sliced into per-date partitions.
7. UQ governance wrapper generates manifest, binds quality report.
8. `FactorStore.publish()` writes immutable partition per date.

Qlib initialization: the adapter does NOT use `qlib.init(provider_uri=...)`.
Instead, it uses Qlib's expression evaluation directly on a pandas DataFrame
via `qlib.data.dataset.processor` internals or `Alpha158` handler's
`__call__` method, bypassing Qlib's file-based data layer entirely. This
keeps UQ's governance as the single data source.

## 5. Factor Set Definitions

### 5.1 `alpha158-v1`

Subset of Qlib's Alpha158 expression set, restricted to factors that
depend only on: open, close, high, low, volume, amount, vwap.

Excluded: factors requiring Qlib's internal `Ref` with negative lookahead
or non-governed data sources.

### 5.2 `alpha360-v1`

Analogous subset from Alpha360.

### 5.3 Selection Criteria

A factor is included if:
1. Its expression uses only governed columns (OHLCV + amount, no vwap).
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
| quality_report_checksum_sha256 | External reviewer's report digest |
| implementation_fingerprint | SHA-256 of adapter code version |
| qlib_expression_set | `Alpha158` or `Alpha360` |
| qlib_version | installed pyqlib version string |

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
- Factor set definition JSON for alpha158-v1 with included/excluded factor names.
- Lookahead audit report: list of excluded factors with reason.
- Name mapping table (Qlib name → UQ name).
- Adapter module placement and fingerprint registration.

### Phase 1
- Adapter computes factors from a small synthetic price panel.
- Output passes UQ manifest generation and identity checks.
- Quality report binding works with existing reviewer registry.
- Published partition passes FactorStore.read() fail-closed verification.

### Phase 2
- E2E: governed data → Qlib compute → governance → accepted store.
- Full lineage from accepted partition back to source price data.
- Deterministic across runs (same inputs → same generation_id).

## 10. Module Placement

The adapter lives in `src/uq/factors/qlib_adapter.py` (not `src/uq/adapters/`)
because it is a factor source plugin, not a cross-cutting adapter. This keeps
it co-located with the other factor computation modules.

## 11. Implementation Plan Reference

See `specs/layers/qlib-factor-adapter-implementation-plan.md`.
