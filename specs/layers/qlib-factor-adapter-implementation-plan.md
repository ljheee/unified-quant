# Qlib Factor Adapter Implementation Plan

Status: **implementation complete; local gate passed at implementing HEAD; final release evidence pending marker + remote gate.**

Source spec: `specs/layers/qlib-factor-adapter-spec.md`

## 0. Scope

This plan implements the adapter that runs a curated subset of Qlib's Alpha158
factor expressions against UQ governed price data and publishes results
through the existing factor governance pipeline. Alpha360 is deferred to
a future version.

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

- `config/schemas/factor-sets/factor_set.v2.json` supporting reviewed expressions
- `config/factor-sets/alpha158-v1.json` reviewed 157-factor definition
- `config/factor-sets/alpha360-v1.json` draft, contract-only definition
- Lookahead audit result in the definition JSON
- Qlib-to-UQ name mapping table
- Factor name enumeration test (deterministic ordering)
- Adapter code fingerprint registration
- Machine-readable phase record

**Entry criteria**: none (first phase).

**Exit criteria**: factor set definition validates, factor names are
deterministic, gate passes on final HEAD.

### Phase 1: Computation and Governance

**Deliverables**

- `src/uq/factors/qlib_adapter.py` with lazy pyqlib import gating
- `QlibFactorAdapter.compute()` accepting UQ canonical DataFrame (multi-date panel)
- Expression-shift lookahead validation before computation
- Per-date partition slicing after full-range computation
- Governance wrapper: manifest v2 generation, quality binding, Qlib version recording
- `config/schemas/manifests/factor_manifest.v2.json`
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
| QA0a | 0 | tests/test_qlib_adapter.py::test_alpha158_factor_names_deterministic | passed |
| QA0b | 0 | tests/test_qlib_adapter.py::test_factor_set_definition_valid | passed |
| QA0c | 0 | tests/test_qlib_adapter.py::test_lookahead_audit_excludes_forward_looking | passed |
| QA0d | 0 | tests/test_qlib_adapter.py::test_name_mapping_deterministic | passed |
| QA1a | 1 | tests/test_qlib_adapter.py::test_adapter_computes_alpha158_factors | passed |
| QA1b | 1 | tests/test_qlib_adapter.py::test_governance_manifest_generation | passed |
| QA1c | 1 | tests/test_qlib_adapter.py::test_quality_report_binding | passed |
| QA1d | 1 | tests/test_qlib_adapter.py::test_e2e_publish_read | passed |
| QA1e | 1 | tests/test_qlib_adapter.py::test_deterministic_generation_id | passed |
| QA1f | 1 | tests/test_qlib_adapter.py::test_qlib_import_guard_raises_without_qlib | passed |
| QA1g | 1 | tests/test_qlib_adapter.py::test_forward_looking_expression_rejected | passed |
| QA1h | 1 | tests/test_qlib_adapter.py::test_per_date_partition_slicing | passed |
| QA1i | 1 | tests/test_qlib_adapter.py::test_vwap_factors_excluded | passed |
| QA1j | 1 | tests/test_qlib_adapter.py::test_tampered_partition_rejects_read | passed |
| QA2a | 2 | scripts/run_gate.sh | pending-final-head-gate |
