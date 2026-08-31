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
- online factor computation or streaming.

## 4. Data Flow

1. UQ `FactorContext` reads governed canonical data.
2. Adapter converts to Qlib's expected DataFrame format.
3. Qlib `Alpha158` / `Alpha360` handler computes factors.
4. Adapter converts result back to UQ canonical format.
5. UQ governance wrapper generates manifest, binds quality report.
6. `FactorStore.publish()` writes immutable partition.

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
1. Its expression uses only governed columns (OHLCV + amount).
2. All operations are backward-looking (no future data leakage).
3. It produces finite values for at least 80% of the universe.
4. It does not duplicate an existing UQ factor (e.g. return_1d).

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
- Factor set definition JSON for alpha158-v1 with all included factor names.
- Adapter can enumerate Qlib Alpha158 factor names.
- Deterministic expression-to-UQ-factor-name mapping.
- Governance wrapper schemas validated.

### Phase 1
- Adapter computes factors from a small synthetic price panel.
- Output passes UQ manifest generation and identity checks.
- Quality report binding works with existing reviewer registry.
- Published partition passes FactorStore.read() fail-closed verification.

### Phase 2
- E2E: governed data → Qlib compute → governance → accepted store.
- Full lineage from accepted partition back to source price data.
- Deterministic across runs (same inputs → same generation_id).

## 10. Implementation Plan Reference

See `specs/layers/qlib-factor-adapter-implementation-plan.md`.
