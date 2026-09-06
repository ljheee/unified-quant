# Risk Control Plane Implementation Plan

Status: **v0.1 draft; all runtime phases paused pending spec approval**

Source spec: `specs/layers/risk-control-plane-spec.md`

## 0. Plan Purpose

This plan describes a small, artifact-governed Risk Control Plane. It is not
authorization to implement production execution, real-time streaming, broker
connectivity, or a generic rules engine.

The current deliverable is design only. Runtime phases remain paused until the
source spec is approved and at least one activation condition in its §4 is met.

## 1. Scope

The first implementable scope, when activated, covers:

1. versioned risk policy, state, decision, event, and exception contracts;
2. deterministic portfolio-publication evaluation;
3. deterministic backtest pre-trade evaluation;
4. explicit actions and fail-closed consumer integration;
5. external policy/exception review binding;
6. lineage, checksums, golden vectors, and negative fixtures.

It does not implement factor/model drift monitors, real-time portfolio risk, or
execution routing in this release.

## 2. Execution Rules

1. No runtime phase starts until Phase 0 exit criteria pass.
2. Owning-layer checks are not removed when a shared risk decision is added.
3. RiskEngine must not mutate accepted upstream artifacts.
4. All durable artifacts use stable generation identity and manifest digest.
5. Enforceable paths fail closed on missing, expired, unapproved, or tampered
   policy/exception evidence, and on missing/tampered state when the evaluated
   rule or scope requires state.
6. Unsupported `de_risk`, `flatten`, or `halt_strategy` actions fail closed;
   they are not informally mapped to sell-all behavior.
7. Every phase has focused tests, persisted fixtures/golden vectors where
   applicable, and evidence paths.

Gate command:

```bash
uv sync --locked --extra dev --extra real && uv run --no-sync python -m pytest
```

Qlib-dependent phases add:

```bash
UQ_GATE_EXTRAS=qlib bash scripts/run_gate.sh
```

## 3. Dependency Graph

```text
Phase 0 Contract Foundation
  -> Phase 1 Portfolio Risk Decision
  -> Phase 2 Backtest Pre-Trade Decision
  -> Phase 3 Stateful Loss-Control Actions
  -> Phase 4 Research Chain Integration
  -> Phase 5 Release Reconciliation
```

No parallel implementation tracks.

## 4. Phase 0 — Contract Foundation

Goal: freeze the machine contracts before any engine is wired into publication
or backtest.

Deliverables:

1. `config/schemas/contracts/risk_policy.v1.json`;
2. `config/schemas/contracts/risk_state.v1.json`;
3. `config/schemas/contracts/risk_decision.v1.json`;
4. `config/schemas/contracts/risk_event.v1.json`;
5. `config/schemas/contracts/risk_exception.v1.json`;
6. a separate `risk_review_decision.v1` contract or explicitly versioned
   extension for external policy/exception review;
7. typed loaders for each family;
8. stable generation and manifest-digest helpers;
9. representative and negative fixtures per family;
10. deterministic golden decision vectors with empty/simple state;
11. action enum, scope enum, severity enum, and finding schema;
12. contract tests for identity sensitivity and checksum verification.

Entry criteria:

- Source spec is approved;
- activation condition is documented in the phase record.

Exit criteria:

- Every schema validates representative fixtures and rejects negative fixtures;
- stable generation excludes run metadata and changes when any semantic binding
  changes;
- canonical JSON is deterministic;
- no runtime publication/backtest behavior changes in this phase;
- focused tests and full suite pass.

## 5. Phase 1 — Portfolio Risk Decision

Goal: produce governed portfolio-publication decisions.

Deliverables:

1. `RiskPolicyStore` and `RiskStateStore` with accepted/read-only semantics;
2. `RiskDecisionStore` and `RiskEventStore`;
3. `RiskEngine.evaluate_portfolio()`;
4. portfolio state computation from accepted target weights, definitions,
   universe, industry, price/NAV, and prior state inputs;
5. decision persistence with policy/state/input lineage;
6. first rules:
   - `portfolio_gross_exposure_limit`;
   - `portfolio_single_name_limit`;
   - `portfolio_top_n_limit`;
   - `portfolio_industry_limit`;
   - `portfolio_turnover_limit`;
   - `portfolio_cash_reserve_limit`;
7. an optional adapter invoked by portfolio publication; the owning Portfolio
   Layer remains responsible for final enforcement.

Acceptance:

- Missing or tampered target weights/definition/universe fails closed;
- identical inputs produce identical decision digest;
- policy change changes decision identity;
- concentration violations produce deterministic `block`/`resize`;
- state, when required, is immutable and readable;
- unsupported portfolio action fails closed.

## 6. Phase 2 — Backtest Pre-Trade Risk Decision

Goal: make order-risk decisions shared and auditable without changing the
backtest execution model.

Deliverables:

1. `RiskEngine.evaluate_order()`;
2. order context adapter for candidate orders, holdings, cash, prices, volume,
   calendar, suspension, and instrument metadata;
3. integration into the existing BacktestEngine before order application;
4. first rules:
   - `order_notional_limit`;
   - `order_participation_limit`;
   - `order_insufficient_cash`;
   - `order_instrument_halt`;
   - `order_limit_price`;
   - `order_t1_sellable_quantity`;
5. risk decision/event evidence in the backtest result or a bound risk ledger;
6. preserve existing fill ledger statuses and add explicit risk-decision links.

Acceptance:

- A backtest order cannot be applied without a valid decision when the adapter
  is enabled;
- duplicate, stale, halted, or over-limit orders produce deterministic actions;
- sellable T+1 quantity cannot be exceeded;
- cash reserve and sell-before-buy semantics remain unchanged;
- tampering with the risk decision or order lineage rejects the backtest read;
- no silent skip: rejected orders are recorded in both risk and execution ledgers.

## 7. Phase 3 — Stateful Loss-Control Actions

Goal: introduce drawdown/volatility state only after executable semantics are
defined.

Deliverables:

1. rolling drawdown and volatility state computation;
2. hysteresis, cooldown, and consecutive-trigger counters;
3. `strategy_drawdown_trigger` and `strategy_volatility_trigger`;
4. explicit de-risk contract accepted by the backtest/portfolio consumer;
5. `halt_strategy`, `de_risk`, and `flatten` event transitions;
6. reviewed exception support.

Entry criteria:

- The downstream consumer defines exactly how de-risking changes weights or
  orders;
- the trigger is an explicitly accepted research/backtest gate, not live
  execution.

Acceptance:

- Trigger/entry, hysteresis/exit, and cooldown are deterministic;
- state transitions produce events;
- exception expiry restores enforcement;
- flatten/de-risk cannot execute without a declared consumer contract;
- replaying the same ledger produces the same state sequence.

## 8. Phase 4 — Research Chain Integration

Goal: expose risk decisions as governed stage evidence without bypassing owning
layers.

Deliverables:

1. research stage plan extension for risk policy/state/decision bindings;
2. resolver support for reviewed policy and exception references;
3. fail-closed stage behavior on enforceable risk decisions;
4. result evidence index entries for decisions/events;
5. release-only documentation updates.

Acceptance:

- A research run cannot omit a required enforceable decision;
- decision lineage resolves to policy, state, and input generations;
- rejected decision stops the affected stage;
- the orchestrator does not sign or approve policies/exceptions.

## 9. Phase 5 — Release Reconciliation

Deliverables:

1. final unified local gate on the release implementation commit;
2. remote matrix evidence, if the risk slice introduces environment-sensitive
   behavior;
3. phase/release records with acceptance status and evidence paths;
4. architecture registration;
5. spec/plan status reconciliation.

Exit criteria:

- All security/lineage/fail-closed acceptance rows pass with no deferral;
- any deferred non-security item has an explicit release-scope limitation;
- release evidence is committed and bound to the implementation HEAD.

## 10. Activation Checklist

Before opening Phase 0, confirm one or more of the following:

- [ ] Execution Layer spec has started;
- [ ] Research Chain needs cross-strategy/account risk binding;
- [ ] drawdown/volatility trigger becomes a release gate;
- [ ] live/paper positions require persistent risk state;
- [ ] policy/exception audit becomes a compliance requirement.

Until then, this plan remains a draft and must not be marked implemented.
