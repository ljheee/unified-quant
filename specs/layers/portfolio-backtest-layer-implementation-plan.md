# Portfolio and Backtest Layer Implementation Plan

Status: **gated implementation order v1.2; phases 0–3 exited and released; Phase 0 test IDs normalized. Final local gate evidence binds implementation commit 196de96.**

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

- Final HEAD local gate evidence with bound commit hash
- Updated spec/plan status markers
- Release record with bound commit hash and evidence index
- Remote six-cell CI matrix result (if available) recorded as supplementary

**Entry criteria**: Phase 2 exits.

**Exit criteria**: All phases exited; local gate passed on final
implementation commit; release record committed.

### Acceptance Matrix

| ID | Phase | Test ID | Status |
|---|---|---|---|
| PB0a | 0 | test_portfolio_backtest_contracts.py::TestPortfolioDefinitionSchema::test_valid_fixture | passed |
| PB0b | 0 | test_portfolio_backtest_contracts.py::TestPortfolioDefinitionSchema::test_negative_fixture_1 | passed |
| PB0b2 | 0 | test_portfolio_backtest_contracts.py::TestPortfolioDefinitionSchema::test_negative_fixture_2 | passed |
| PB0c | 0 | test_portfolio_backtest_contracts.py::TestTargetWeightsSchema::test_valid_fixture | passed |
| PB0d | 0 | test_portfolio_backtest_contracts.py::TestTargetWeightsSchema::test_negative_fixture_1 | passed |
| PB0d2 | 0 | test_portfolio_backtest_contracts.py::TestTargetWeightsSchema::test_negative_fixture_2 | passed |
| PB0e | 0 | test_portfolio_backtest_contracts.py::TestBacktestConfigSchema::test_valid_fixture | passed |
| PB0f | 0 | test_portfolio_backtest_contracts.py::TestBacktestConfigSchema::test_negative_fixture_1 | passed |
| PB0f2 | 0 | test_portfolio_backtest_contracts.py::TestBacktestConfigSchema::test_negative_fixture_2 | passed |
| PB0g | 0 | test_portfolio_backtest_contracts.py::TestBacktestResultSchema::test_valid_fixture | passed |
| PB0h | 0 | test_portfolio_backtest_contracts.py::TestBacktestResultSchema::test_negative_fixture_1 | passed |
| PB0h2 | 0 | test_portfolio_backtest_contracts.py::TestBacktestResultSchema::test_negative_fixture_2 | passed |
| PB0i | 0 | test_golden_vectors_deterministic_and_persisted | passed |
| PB1a | 1 | test_e2e_publish_read | passed |
| PB1b | 1 | test_single_position_cap | passed |
| PB1c | 1 | test_industry_cap_scaling | passed |
| PB1d | 1 | test_turnover_cap_interpolation | passed |
| PB1e | 1 | test_tampered_data_rejects_read | passed |
| PB1f | 1 | test_missing_quality_report_rejects_read | passed |
| PB1g | 1 | test_wrong_reviewer_signature_rejects | passed |
| PB1h | 1 | test_prediction_lineage_mismatch_rejected | passed |
| PB1i | 1 | test_insufficient_universe | passed |
| PB1j | 1 | test_cash_reserve_violation_rejected | passed |
| PB1k | 1 | test_overwrite_rejection | passed |
| PB2a | 2 | test_deterministic_pnl | passed |
| PB2b | 2 | test_costs_reduce_pnl | passed |
| PB2c | 2 | test_limit_up_blocks_buy | passed |
| PB2d | 2 | test_limit_down_blocks_sell | passed |
| PB2e | 2 | test_board_lot_rounding | passed |
| PB2f | 2 | test_suspension_skip_recorded | passed |
| PB2g | 2 | test_volume_guard_skips_fill | passed |
| PB2h | 2 | test_corporate_action_rejected | passed |
| PB2i | 2 | test_t1_sellable_quantity_enforced | passed |
| PB2j | 2 | test_insufficient_cash_skip_recorded | passed |
| PB3a | 3 | scripts/run_gate.sh | passed |

## 3. CI Matrix Note

This layer is pure Python (pandas + numpy). Release requires a successful
local gate on final HEAD. The remote six-cell matrix is inherited from the
repository's existing CI workflow as a conservative regression check; if it
passes, it strengthens evidence. A local gate is sufficient to declare this
layer's phase exit, provided all focused tests pass.

## 4. Dependency Graph

```text
Phase 0 ──> Phase 1 ──> Phase 2 ──> Phase 3
```

No parallel tracks in the first release.

## 5. Architecture Registration

This portfolio/backtest layer is registered as a downstream consumer of the
model layer in the UQ architecture. Its boundary is:

```
Model Layer (prediction_set.v1)
  -> Portfolio Layer (portfolio_definition.v1, target_weights.v1)
  -> Backtest Layer (backtest_config.v1, backtest_result.v1)
```

It does not replace Qlib's internal backtest for research exploration; it
provides a governed, manifest-verified simulation path.
