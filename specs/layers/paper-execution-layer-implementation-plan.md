# Paper Execution Layer Implementation Plan

Status: **v0.1 all phases paused pending Phase 0 exit**

Source spec: `specs/layers/paper-execution-layer-spec.md`

## 0. Plan Purpose

This is a bounded contract-first plan for deterministic paper execution. It does
not authorize live broker connectivity, credentials, market-data streaming,
algorithmic execution, or a change to released backtest semantics.

The first deliverable is Phase 0 only. Runtime phases remain paused until all
paper execution contracts, fixtures, golden vectors, typed loaders, and gate
evidence exist and are independently reviewed.

## 1. Scope

### 1.1 First Release

- Four durable families:
  - `execution_config.v1`;
  - `order_plan.v1`;
  - `execution_result.v1`;
  - `paper_portfolio_state.v1`.
- Deterministic paper order planning and one-session replay execution.
- Strict sell-before-buy, T+1 inventory, board lots, price limits, suspension,
  corporate-action exclusion, and cash feasibility.
- Reviewed quality reports and reviewed risk decisions as hard publication gates.
- Immutable stores with accepted-reader validation and typed failures.

### 1.2 Explicit Non-Goals

- No broker adapter implementation.
- No network call, credential lookup, secret store, or live order submission.
- No real-time daemon or websocket runtime.
- No partial fills, child orders, market orders, shorting, margin, or derivatives.
- No modification of the frozen `backtest_result.v1` publication contract.

## 2. Execution Rules

1. Phase 0 is contract-only. Runtime source directories must not change in
   Phase 0 except for additive schema registration if explicitly tested.
2. No runtime phase starts before Phase 0 exit criteria pass.
3. `mode` is `paper`; `market` is `cn_a`; order type is `limit`; fill policy is
   all-or-none for the first release.
4. Missing mandatory inputs, tampered manifests, invalid reviews, rejected risk
   decisions, and reconciliation failures fail closed.
5. Stable generation excludes run-only metadata and quality report binding.
   `manifest_digest_sha256` covers the published manifest and is verified on
   read.
6. Paper artifacts are separate from backtest artifacts. No backtest result may
   be relabeled as paper execution evidence.
7. Every phase must list exact test IDs, fixture paths, evidence paths, and
   status in a phase record before it is marked complete.
8. The runner may create paper artifacts, but it may never generate, sign, or
   approve quality reviews or risk decisions.

Gate commands:

```bash
uv sync --locked --extra dev --extra real && uv run --no-sync python -m pytest
UQ_GATE_EXTRAS=qlib bash scripts/run_gate.sh
```

Any runtime phase must run both commands. Contract-only Phase 0 may run the
unified gate with the repository's configured extras.

## 3. Dependency Graph

```text
Phase 0 Contract Foundation
  -> Phase 1 Paper Order Planning
  -> Phase 2 Paper Execution Simulation
  -> Phase 3 State Evolution and Reconciliation
  -> Phase 4 Governance Integration
  -> Phase 5 Release Reconciliation
```

No parallel implementation tracks.

## 4. Phase 0 — Contract Foundation

Entry criteria:

- Source spec is reviewed and its status permits contract-only work.
- No runtime phase is opened.

Deliverables:

1. `config/schemas/contracts/execution_config.v1.json`;
2. `config/schemas/contracts/order_plan.v1.json`;
3. `config/schemas/contracts/execution_result.v1.json`;
4. `config/schemas/contracts/paper_portfolio_state.v1.json`;
5. typed loaders and identity helpers for all four families;
6. representative and at least two distinct negative fixtures per family under
   `config/schemas/fixtures/paper-execution/`;
7. deterministic identity golden vectors under
   `evidence/paper-execution/phase-0/`;
8. explicit storage layout, serialization profiles, canonical JSON identity
   rules, excluded identity fields, and failure taxonomy;
9. additions to `model_quality_report.v2` binding types and review registry for
   the four paper families;
10. architecture registration for the paper execution boundary;
11. phase record and evidence index.

Exit criteria:

- Every representative fixture passes schema and typed-loader validation.
- Every negative fixture fails with a typed contract error.
- Identity golden vectors prove key-order stability and semantic-field
  sensitivity.
- Storage paths reject traversal and symlink traversal.
- Quality report binding types and trust-anchor governance tests pass.
- Full test suite and unified gate pass at the final implementation commit.
- The gate report, lockfile digest, evidence index, and phase record are
  preserved at that commit.

## 5. Phase 1 — Paper Order Planning

Deliverables:

1. `ExecutionConfigStore` for `execution_config.v1`.
2. `PaperOrderPlanner` consuming accepted target weights, prior paper state,
   governed market inputs, calendar, suspension, corporate actions, and risk
   decision.
3. Deterministic cash-residual order ordering.
4. Board-lot, sell-before-buy, T+1, and cash-budget sizing.
5. `OrderPlanStore` with immutable publication and accepted readback.
6. Typed errors for missing bindings, unavailable sellable quantity, invalid
   policy values, path traversal, and target-state inconsistency.

Acceptance:

- Identical inputs produce identical plan generation and payload bytes.
- Sell orders precede buy orders by configured sequence.
- T+1-sellable quantity cannot be exceeded.
- Buy quantities are board-lot-normalized downward and cash-feasible.
- Rejected risk decisions stop before order-plan publication.
- Every target instrument is represented by an order or an explicit unfilled
  reconciliation reason.
- Plan readback rejects payload, manifest, identity, and upstream-binding
  tampering.

## 6. Phase 2 — Paper Execution Simulation

Deliverables:

1. `PaperExecutionEngine`.
2. Deterministic limit-price fill/rejection logic.
3. Trading-status, suspension, corporate-action, limit-up, and limit-down guards.
4. All-or-none fills and explicit fee calculation.
5. `ExecutionResultStore` and accepted readback.
6. Rejection taxonomy exactly matching the source spec.

Acceptance:

- A `trading` instrument at a permitted limit price fills at the configured
  basis and quantity.
- Non-`trading`, suspended, and corporate-action-excluded instruments fail with
  the configured typed reason.
- Limit-up blocks buys; limit-down blocks sells.
- Missing mandatory market data produces `no_market_data`, not zero valuation.
- Fees match the configured formulas and precision.
- All-or-none cash feasibility is enforced after planned sells and fees.
- Result readback rejects tampering and incomplete lineage.

## 7. Phase 3 — State Evolution and Reconciliation

Deliverables:

1. `PaperPortfolioStateStore`.
2. Deterministic next-state construction from prior state and result.
3. Cash, holdings, average cost, buy-locked quantity, and sellable quantity
   evolution.
4. Aggregate target/order/fill/rejection/cash/value reconciliation.
5. Floating-point tolerance and exact currency rounding rules.
6. Cross-manifest result→plan→target→state resolution.

Acceptance:

- Closing cash is non-negative and equals opening cash plus signed net cash.
- Filled buys increase quantity and buy-locked quantity.
- Filled sells reduce sellable quantity first and never sell T+1-locked shares.
- Average cost is deterministic and unaffected by key order.
- Rejected and unfilled quantities reconcile exactly within tolerance.
- Empty and fully rejected executions can publish valid state.
- State readback rejects row, dtype, checksum, binding, and identity tampering.

## 8. Phase 4 — Governance Integration

Deliverables:

1. Quality-report publication gate for all four families.
2. Risk Control Plane decision verification before order-plan publication.
3. Optional deterministic `allow_with_actions` consumption contract.
4. Runtime-mode guard confirming paper execution cannot enter a broker path.
5. Readback governance tests for missing, tampered, forged, expired, and
  mismatched reports and decisions.

Acceptance:

- A publisher-generated passed report cannot enable publication.
- Reviewed report family, generation, digest, and checks must match exactly.
- `reject` risk decisions block plan, result, and state publication.
- Research/prod trust-anchor behavior remains unchanged.
- No production test-key anchor is accepted when
  `UQ_RUNTIME_MODE=production`.

## 9. Phase 5 — Release Reconciliation

Deliverables:

1. Final implementation commit.
2. Local unified gate report.
3. Remote CI evidence for the declared environment matrix.
4. `evidence/paper-execution/release/release-record.json`.
5. `evidence/paper-execution/release/phase-record.json`.
6. `evidence/paper-execution/release/evidence-index.json`.
7. Architecture and source-spec status reconciliation.

Acceptance:

- All phase records are complete and cross-linked.
- No acceptance row remains `TBD` or unimplemented.
- Local and remote evidence bind the final implementation commit.
- No broker, live, network, credential, or streaming path exists.
- Full test suite and both gate commands pass.
- The release marker is immutable and append-only.

## 10. Immediate Next Actions

1. Implement Phase 0 contracts, fixtures, golden vectors, typed loaders, and
   evidence.
2. Run the unified gate at the final Phase 0 commit.
3. Preserve and index the successful report and lockfile digest.
4. Conduct a separate review of the Phase 0 exit evidence.
5. Only after that review passes, open Phase 1 runtime work.
