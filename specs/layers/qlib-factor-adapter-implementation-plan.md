# Qlib Factor Adapter Implementation Plan

Status: **gated implementation order v1.0; all phases paused pending phase 0 exit.**

Source spec: `specs/layers/qlib-factor-adapter-spec.md`

## 0. Scope

This plan implements the adapter that runs Qlib's Alpha158 factor expressions
against UQ governed price data and publishes results through the existing
factor governance pipeline.

It does NOT:
- modify Qlib source code;
- redefine factor formulas;
- bypass any existing governance gate.

## 1. Execution Rules

1. `scripts/run_gate.sh` is the mechanical gate.
2. Every phase exit requires focused tests plus a successful unified gate.
3. Contract drafting may occur during plan preparation.
4. Every acceptance ID must map to concrete test IDs before phase declaration.
5. Each phase has a machine-readable record under
   `evidence/qlib-adapter/phase-N/phase-record.json`.

## 2. Phases

### Phase 0: Contracts

**Deliverables**

- `config/factor-sets/alpha158-v1.json` factor set definition
- Factor name enumeration test (deterministic ordering)
- Adapter code fingerprint registration
- Machine-readable phase record

**Entry criteria**: none (first phase).

**Exit criteria**: factor set definition validates, factor names are
deterministic, gate passes on final HEAD.

### Phase 1: Computation and Governance

**Deliverables**

- `QlibFactorAdapter.compute()` accepting UQ canonical DataFrame
- Format conversion UQ ↔ Qlib DataFrame
- Alpha158 expression execution via Qlib
- Output conversion to UQ canonical format
- Governance wrapper: manifest generation, quality binding
- FactorStore.publish() integration

**Entry criteria**: Phase 0 exits.

**Exit criteria**: E2E test computes factors from synthetic data, passes
governance, publishes to accepted store, and reads back with identity checks.

### Phase 2: Release Reconciliation

**Deliverables**

- Final HEAD gate evidence
- Updated spec/plan status markers
- Release record

**Entry criteria**: Phase 1 exits.

**Exit criteria**: All phases exited; gate passed on final HEAD.

## 3. Dependency Graph

```text
Phase 0 ──> Phase 1 ──> Phase 2
```

## 4. Acceptance Matrix

| ID | Phase | Test ID | Status |
|---|---|---|---|
| QA0a | 0 | test_alpha158_factor_names_deterministic | pending |
| QA0b | 0 | test_factor_set_definition_valid | pending |
| QA1a | 1 | test_adapter_computes_alpha158_factors | blocked-by: QA0* |
| QA1b | 1 | test_governance_manifest_generation | blocked-by: QA0* |
| QA1c | 1 | test_quality_report_binding | blocked-by: QA0* |
| QA1d | 1 | test_e2e_publish_read | blocked-by: QA0* |
| QA1e | 1 | test_deterministic_generation_id | blocked-by: QA0* |
| QA2a | 2 | test_final_head_gate | blocked-by: QA1* |
