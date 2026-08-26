# Portfolio and Backtest Layer Implementation Plan

Status: **gated implementation order v1.0; all phases paused pending phase 0 exit.**

Source spec: `specs/layers/portfolio-backtest-layer-spec.md`

## 0. Scope

This plan implements the first local-research portfolio and backtest slice:

- one top-N equal-weight portfolio scheme with single-position, industry,
  and turnover constraints (score_proportional deferred to v0.2);
- one daily-frequency T+1-open backtest engine;
- fixed commission + stamp duty + slippage cost model;
- immutable target-weight and backtest-result artifacts;
- fail-closed lineage and quality gates inherited from the model layer.

It does not implement production execution, multi-strategy orchestration,
or live monitoring.

## 1. Execution Rules

1. `scripts/run_gate.sh` is the mechanical gate.
2. Every phase exit requires focused tests plus a successful unified gate.
3. Contract drafting may occur during plan preparation; runtime publication
   or reads require phase entry criteria.
4. A semantic schema change requires a new schema version.
5. Every acceptance ID must map to concrete test IDs before phase declaration.
6. Each phase has a machine-readable record under
   `evidence/portfolio-backtest/phase-N/phase-record.json`.

## 2. Phases

### Phase 0: Contracts

**Deliverables**

- `config/schemas/contracts/portfolio_definition.v1.json`
- `config/schemas/contracts/target_weights.v1.json`
- `config/schemas/contracts/backtest_config.v1.json`
- `config/schemas/contracts/backtest_result.v1.json`
- `evidence/portfolio-backtest/phase-0/fixtures/`: valid and negative fixture files for each family
- `evidence/portfolio-backtest/phase-0/golden-vectors/`: deterministic identity golden vectors
- Typed loader registration in `ModelContractLoader`
- Machine-readable phase record

**Entry criteria**: none (first phase).

**Exit criteria**: all schemas validate representative fixtures, reject
negative fixtures, golden vectors are deterministic, gate passes on final HEAD.

### Phase 1: Portfolio Runtime

**Deliverables**

- `PortfolioBuilder.build()` accepting prediction frame + definition
- Weight scheme registry (`top_n_equal_weight`; `score_proportional` deferred)
- Constraint pipeline (single cap, industry cap, turnover cap, cash reserve)
- `TargetWeightStore.publish()` / `.read()` with manifest-first verification
- External quality report binding at publication

**Entry criteria**: Phase 0 exits.

**Exit criteria**: E2E test publishes predictions -> builds weights -> reads
weights back with checksum and generation verification. Negative tests reject
tampered manifests and invalid quality reports. Single-position cap,
industry cap, and turnover cap tests pass with expected clipped, scaled,
and interpolated values.

### Phase 2: Backtest Runtime

**Deliverables**

- `BacktestEngine.run()` consuming weight partitions + price panel
- Daily T+1-open execution simulation
- Cost model (commission, stamp duty, slippage)
- Limit-up/down and suspension guards
- `BacktestResultStore.publish()` / `.read()`
- Summary metrics (Sharpe, max drawdown, annualized return, turnover)

**Entry criteria**: Phase 1 exits.

**Exit criteria**: E2E test runs weights through backtest engine against a
small synthetic price panel, produces equity curve with expected PnL given
deterministic inputs. Commission, stamp duty, and slippage reduce PnL by
exact expected amounts. Limit-up blocks buy fill; limit-down blocks sell
fill. Board-lot rounding floors share count to multiples of 100.

### Phase 3: Release Reconciliation

**Deliverables**

- Final HEAD gate evidence (local + remote CI six-cell matrix)
- Updated spec/plan status markers
- Release record with bound commit hash and evidence index

**Entry criteria**: Phase 2 exits.

**Exit criteria**: All phases exited; remote CI matrix passed on final
implementation commit; release marker committed.

### Acceptance Matrix

| ID | Phase | Test ID | Status |
|---|---|---|---|
| PB0a | 0 | test_portfolio_definition_valid_fixture | pending |
| PB0b | 0 | test_portfolio_definition_negative_fixture | pending |
| PB0c | 0 | test_target_weights_valid_fixture | pending |
| PB0d | 0 | test_target_weights_negative_fixture | pending |
| PB0e | 0 | test_backtest_config_valid_fixture | pending |
| PB0f | 0 | test_backtest_config_negative_fixture | pending |
| PB0g | 0 | test_backtest_result_valid_fixture | pending |
| PB0h | 0 | test_backtest_result_negative_fixture | pending |
| PB0i | 0 | test_golden_vectors_deterministic | pending |
| PB1a | 1 | test_portfolio_e2e_publish_read | blocked-by: PB0* |
| PB1b | 1 | test_single_position_cap | blocked-by: PB0* |
| PB1c | 1 | test_industry_cap_scaling | blocked-by: PB0* |
| PB1d | 1 | test_turnover_cap_interpolation | blocked-by: PB0* |
| PB1e | 1 | test_tampered_manifest_rejects_read | blocked-by: PB0* |
| PB1f | 1 | test_missing_quality_report_rejects_publish | blocked-by: PB0* |
| PB1g | 1 | test_wrong_reviewer_signature_rejects_read | blocked-by: PB0* |
| PB1h | 1 | test_prediction_lineage_mismatch_rejects_build | blocked-by: PB0* |
| PB1i | 1 | test_instrument_outside_universe_rejected | blocked-by: PB0* |
| PB1j | 1 | test_cash_reserve_violation_rejected | blocked-by: PB0* |
| PB1k | 1 | test_overwrite_rejection | blocked-by: PB0* |
| PB2a | 2 | test_backtest_deterministic_pnl | blocked-by: PB1* |
| PB2b | 2 | test_commission_stamp_slippage_costs | blocked-by: PB1* |
| PB2c | 2 | test_limit_up_blocks_buy | blocked-by: PB1* |
| PB2d | 2 | test_limit_down_blocks_sell | blocked-by: PB1* |
| PB2e | 2 | test_board_lot_rounding | blocked-by: PB1* |
| PB2f | 2 | test_suspension_skip_recorded | blocked-by: PB1* |
| PB2g | 2 | test_volume_guard_skips_fill | blocked-by: PB1* |
| PB2h | 2 | test_config_result_lineage_mismatch_rejected | blocked-by: PB1* |
| PB2i | 2 | test_t1_sellable_quantity_enforced | blocked-by: PB1* |
| PB3a | 3 | test_final_head_local_gate | blocked-by: PB2* |

## 3. CI Matrix Note

The portfolio/backtest layers are pure Python (pandas + numpy) with no C
extension dependencies. The six-cell OS × Python matrix inherited from the
factor/model layers is retained as a conservative regression check but is not
a hard requirement unique to this layer; a single-platform gate is sufficient
to validate portfolio/backtest correctness.

## 4. Dependency Graph

```text
Phase 0 ──> Phase 1 ──> Phase 2 ──> Phase 3
```

No parallel tracks in the first release.
