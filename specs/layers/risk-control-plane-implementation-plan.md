# Risk Control Plane Implementation Plan

Status: **v0.2.4 final-CR remediated plan; Phases 0–2 exited with preserved local gate evidence**

Source spec: `specs/layers/risk-control-plane-spec.md`

## 0. Plan Purpose

This plan turns the Risk Control Plane into a bounded, contract-first slice. It
does not authorize production execution, real-time streaming, broker
connectivity, or a generic rules engine.

The source spec is approved. Activation condition 5 is recorded in
`evidence/risk/activation.json`. Phase 0 and Phase 1 are exited with local
gate evidence. Phase 2 is exited with preserved local gate evidence. Phase 3
remains gated on Phase 2 exit and its own acceptance expansion.

## 1. Scope

The first activated scope covers:

1. six machine contracts:
   - `risk_policy.v1`;
   - `risk_state.v1`;
   - `risk_decision.v1`;
   - `risk_event.v1`;
   - `risk_exception.v1`;
   - `risk_review_decision.v1`;
2. optional immutable `risk_run.v1` index;
3. deterministic portfolio-publication evaluation;
4. deterministic backtest pre-trade evaluation;
5. explicit action, finding, exception, event, and lineage semantics;
6. independent risk ledger integration, without changing released
   `backtest_result.v1`.

It does not implement factor/model drift monitors, real-time portfolio risk, or
execution routing.

## 2. Execution Rules

1. No runtime phase starts until Phase 0 exit criteria pass.
2. Phase 0 is contract-only; it must not modify portfolio, backtest, research,
   model, factor, or canonical runtime behavior.
3. Owning-layer checks are not removed when a shared risk decision is added.
4. `RiskEngine` must not mutate accepted upstream artifacts.
5. Stable identity excludes run metadata. `manifest_digest_sha256` is canonical
   JSON SHA-256 including stable generation and durable bindings.
6. Enforceable paths fail closed on missing, expired, unapproved, or tampered
   policy/review/exception evidence, and on missing/tampered state when the
   rule or scope requires state.
7. `de_risk`, `flatten`, and `halt_strategy` fail closed without an explicit
   consumer contract; they are not informally mapped to sell-all.
8. Phase 2 uses an independent `risk_run.v1` + decision/event ledger. Released
   `backtest_result.v1` is not modified; a `v2` result migration is out of scope.
9. Research Chain integration must introduce `research_run_request.v2`; the
   released request schema and canonical stage plan digest are not mutated.
10. Every phase must record focused test IDs, fixture paths, evidence paths,
    and phase status before it is declared complete.

Gate commands:

```bash
uv sync --locked --extra dev --extra real && uv run --no-sync python -m pytest
UQ_GATE_EXTRAS=qlib bash scripts/run_gate.sh
```

Phase 1+ and any phase touching backtest integration must run both commands.

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

Entry criteria:

- Source spec status is `contract_draft=approved`;
- activation condition is checked with a dated note and evidence path;
- the activation note explicitly permits contract-only work.

Deliverables:

1. `config/schemas/contracts/risk_policy.v1.json`;
2. `config/schemas/contracts/risk_state.v1.json`;
3. `config/schemas/contracts/risk_decision.v1.json`;
4. `config/schemas/contracts/risk_event.v1.json`;
5. `config/schemas/contracts/risk_exception.v1.json`;
6. `config/schemas/contracts/risk_review_decision.v1.json`;
7. `config/schemas/contracts/risk_review_trust_anchor.v1.json`;
8. `config/schemas/contracts/risk_run.v1.json`;
8a. `config/schemas/contracts/risk_de_risk_contract.v1.json`;
9. `config/risk-review-trust-anchor.v1.json` registry;
10. `src/uq/risk/contracts.py` typed loaders and identity helpers;
11. per-family representative/negative fixtures under
    `config/schemas/fixtures/risk/`;
12. deterministic decision golden vectors under `evidence/risk/phase-0/`;
13. documented serialization profiles, storage paths, canonical JSON rules,
    excluded identity fields, failure taxonomy, and rule/scope compatibility;
14. `industry_membership.v1` prerequisite declaration or explicit disabling of
    `portfolio_industry_limit`.

Exit criteria:

- Every schema validates representative fixtures and rejects negative fixtures;
- stable generation excludes run metadata and changes when any semantic binding
  changes;
- manifest digest verification rejects payload tampering;
- policy/exception review verification covers signature, trust anchor, subject
  digest, type, expiration, and failure taxonomy;
- canonical JSON is deterministic;
- no runtime publication/backtest behavior changes;
- focused tests and full suite pass;
- a gate report generated at the final implementation commit is preserved under
  the phase evidence directory and indexed in the phase record.

## 5. Phase 1 — Portfolio Risk Decision

Deliverables:

1. `RiskPolicyStore`, `RiskStateStore`, `RiskDecisionStore`,
   `RiskEventStore`, and `RiskRunStore`;
2. `RiskEngine.evaluate_portfolio()`;
3. portfolio state computation from accepted target weights, definitions,
   universe, optional industry membership, price/NAV, and prior state;
4. finding aggregation, action ranking, and deterministic `resize` formulas;
5. first rules:
   - `portfolio_gross_exposure_limit`;
   - `portfolio_single_name_limit`;
   - `portfolio_top_n_limit`;
   - `portfolio_industry_limit`, only when industry contract exists;
   - `portfolio_turnover_limit`;
   - `portfolio_cash_reserve_limit`;
6. portfolio publication adapter.

Boundary:

- `PortfolioBuilder` remains the construction-time owner;
- the adapter recompute is an independent audit/gate;
- the adapter may reject publication, but it does not silently repair weights;
- `resize` is applied only through an explicit, typed portfolio policy.

Acceptance:

- Missing/tampered target weights, definition, universe, policy, or required
  state fails closed;
- identical inputs produce identical decision digest;
- policy/state/input changes change identity;
- overlapping policies fail closed;
- concentration violations produce deterministic `block` or `resize`;
- events and immutable decisions are readable;
- unsupported actions fail closed.

## 6. Phase 2 — Backtest Pre-Trade Risk Decision

Deliverables:

1. `RiskEngine.evaluate_order()`;
2. order context adapter for candidate orders, holdings, cash, prices, volume,
   calendar, suspension, and instrument metadata;
3. independent `risk_run.v1` ledger binding the backtest configuration and
   ordered decisions/events;
4. integration into BacktestEngine before order application;
5. first rules:
   - `order_notional_limit`;
   - `order_participation_limit`;
   - `order_insufficient_cash`;
   - `order_instrument_halt`;
   - `order_limit_price`;
   - `order_t1_sellable_quantity`;
6. comparison behavior between native BacktestEngine guards and shared risk
   decisions.

Boundary:

- Native backtest checks remain executable;
- risk decisions are additional, not replacements;
- `backtest_result.v1` is unchanged;
- the risk ledger is separately checksummed and read-verified;
- a mismatch between native and risk outcomes is surfaced, not hidden.

Acceptance:

- No order is applied without a valid decision when the adapter is enabled;
- duplicate, stale, halted, over-limit, limit-price, and T+1 violations are
  deterministic;
- sell-before-buy and cash settlement semantics remain unchanged;
- tampering with decision, event, run, order, or input lineage rejects the
  risk-run read;
- risk-rejected candidate orders appear in the risk ledger without violating
  the frozen v1 fills schema;
- released backtest result schema/read tests remain green.

## 7. Phase 3 — Stateful Loss-Control Actions

Entry criteria:

- A new `risk_de_risk_contract.v1` schema and source-spec section define the
  consumer;
- external review explicitly approves the de-risk transition;
- no implementation may infer it from strategy intent.

Deliverables:

1. rolling drawdown and volatility state;
2. hysteresis, cooldown, and consecutive-trigger counters;
3. `strategy_drawdown_trigger` and `strategy_volatility_trigger`;
4. `halt_strategy`, `de_risk`, and `flatten` transition semantics;
5. external exception grants and expiry.

Acceptance:

- Trigger entry, hysteresis exit, cooldown, and replay are deterministic;
- transition events are append-only;
- late visibility creates a new state generation, never rewrites history;
- expired exceptions restore enforcement;
- missing de-risk contract fails closed;
- same inputs and accepted state produce the same state sequence.

## 8. Phase 4 — Research Chain Integration

Entry criteria:

- `research_run_request.v2` schema exists;
- new stage-plan version is externally reviewed;
- release compatibility with `research_run_request.v1` is documented.

Deliverables:

1. required risk policy binding in request v2; policy may permit no active
   state rules, but the enforceable decision reference itself is required;
2. a new risk review stage or explicit binding into portfolio/backtest stages;
3. fail-closed stage behavior on enforceable decisions;
4. result evidence index entries for risk runs/decisions/events;
5. compatibility test proving request v1 remains valid.

Acceptance:

- Request v1 does not change its stage plan digest;
- request v2 cannot omit a required enforceable decision;
- lineage resolves policy, state, run, decision, and input generations;
- rejected decisions stop the affected stage;
- the orchestrator cannot sign or approve policies/exceptions.

## 9. Phase 5 — Release Reconciliation

Deliverables:

1. final local unified gate on the release implementation commit;
2. remote matrix evidence, because deterministic Parquet/NumPy behavior is
   environment-sensitive;
3. phase/release records with acceptance status and evidence paths;
4. reconciliation of the already-registered architecture boundary;
5. spec/plan status reconciliation.

Exit criteria:

- All security/lineage/fail-closed acceptance rows pass with no deferral;
- deferred non-security items have explicit release-scope limitations;
- evidence is committed and bound to the implementation HEAD.

## 10. Activation Checklist

Before opening Phase 0, check one condition and record a dated activation note:

- [ ] Execution Layer spec has started;
- [ ] Research Chain needs cross-strategy/account risk binding;
- [ ] drawdown/volatility trigger becomes a release gate;
- [ ] live/paper positions require persistent risk state;
- [x] policy/exception audit becomes a compliance requirement.

Activation note: condition 5 is active for contract-only Phase 0 work. See
`evidence/risk/activation.json`; approver is repository-owner, dated
2026-09-06. No Execution Layer, live position, or real-time path is activated.

## 11. Acceptance Matrix

Status meaning: `pending` means the exact ID is planned but not implemented;
it must become `passed` with evidence before the phase exits.

| ID | Phase | Test ID | Fixture/evidence | Status |
|---|---:|---|---|---|
| RCP0a | 0 | `test_all_risk_contract_fixtures_are_valid` | `config/schemas/fixtures/risk/*-valid.json` | implemented |
| RCP0b | 0 | `test_all_risk_contract_negative_fixtures_fail` | `config/schemas/fixtures/risk/*-negative.json` | implemented |
| RCP0c | 0 | `test_risk_generation_excludes_run_metadata` | `evidence/risk/phase-0/golden/` | implemented |
| RCP0d | 0 | `test_risk_identity_changes_on_each_binding` | `evidence/risk/phase-0/golden/` | implemented |
| RCP0e | 0 | `test_risk_manifest_digest_rejects_tampering` | `config/schemas/fixtures/risk/*-negative.json` | implemented |
| RCP0f | 0 | `test_risk_review_decision_requires_external_trust_anchor` | `config/schemas/fixtures/risk/risk_review_decision-*` | implemented |
| RCP0g | 0 | `test_risk_rule_scope_compatibility_matrix` | policy and decision fixtures | implemented |
| RCP0h | 0 | `test_risk_artifact_bytes_require_checksum_match` and `test_risk_file_path_rejects_escape` | `config/schemas/fixtures/risk/risk_state-*` | implemented |
| RCP0i | 0 | `test_risk_event_sequence_rejects_duplicate_or_stale_entries` | `config/schemas/fixtures/risk/risk_event-*` | implemented |
| RCP0j | 0 | `test_industry_limit_requires_membership_contract` | `config/schemas/fixtures/risk/risk_policy-*` | implemented |
| RCP0k | 0 | `test_enforceable_de_risk_contract_fails_closed` | `config/schemas/fixtures/risk/risk_de_risk_contract-*` | implemented |
| RCP0l | 0 | `test_phase0_golden_identities_are_persisted` | `evidence/risk/phase-0/golden/risk-contract-identities.json` | implemented |
| RCP0m | 0 | `test_approved_policy_binds_verified_external_review_decision` | `config/schemas/fixtures/risk/risk_policy-*` | implemented |
| RCP1a | 1 | `test_risk_portfolio_missing_inputs_fail_closed` | `tests/test_risk_control_plane_phase1.py` | implemented |
| RCP1b | 1 | `test_risk_portfolio_decision_is_deterministic` | `evidence/risk/phase-1/golden/portfolio-decision.json` | implemented |
| RCP1c | 1 | `test_risk_portfolio_action_ranking_is_deterministic` | `evidence/risk/phase-1/golden/portfolio-decision.json` | implemented |
| RCP1d | 1 | `test_risk_portfolio_resize_formula_is_exact` | `tests/test_risk_control_plane_phase1.py` | implemented |
| RCP1e | 1 | `test_risk_portfolio_overlapping_policies_fail_closed` | `tests/test_risk_control_plane_phase1.py` | implemented |
| RCP1f | 1 | `test_risk_portfolio_publication_gate_rejects_block` | `evidence/risk/phase-1/gate-reports/` | implemented |
| RCP1g | 1 | `test_risk_industry_limit_disabled_without_contract` | `tests/test_risk_control_plane_phase1.py` | implemented |
| RCP2a | 2 | `test_risk_order_missing_context_fails_closed` | `evidence/risk/phase-2/fixtures/` | implemented |
| RCP2b | 2 | `test_risk_order_rules_are_deterministic` | `evidence/risk/phase-2/golden/` | implemented |
| RCP2c | 2 | `test_risk_t1_sellable_quantity_is_enforced` | `evidence/risk/phase-2/` | implemented |
| RCP2d | 2 | `test_risk_rejected_candidate_is_recorded_in_risk_ledger` | `evidence/risk/phase-2/` | implemented |
| RCP2e | 2 | `test_risk_run_tampering_rejects_read` | `evidence/risk/phase-2/` | implemented |
| RCP2f | 2 | `test_backtest_result_v1_contract_remains_frozen` | existing backtest contract tests | implemented |
| RCP2g | 2 | `test_risk_rejected_candidate_is_not_added_to_frozen_fills` | `evidence/risk/phase-2/frozen-fills.json` | implemented |
| RCP2h | 2 | `test_duplicate_order_id_fails_closed` | `tests/test_risk_control_plane_phase2.py` | implemented |
| RCP3a | 3 | `test_risk_state_transition_is_deterministic` | `evidence/risk/phase-3/golden/` | pending |
| RCP3b | 3 | `test_risk_hysteresis_and_cooldown_are_exact` | `evidence/risk/phase-3/golden/` | pending |
| RCP3c | 3 | `test_risk_late_visibility_creates_new_generation` | `evidence/risk/phase-3/` | pending |
| RCP3d | 3 | `test_risk_exception_expiry_restores_enforcement` | `evidence/risk/phase-3/` | pending |
| RCP3e | 3 | `test_risk_missing_de_risk_contract_fails_closed` | `config/schemas/fixtures/risk/de-risk-negative.json` | pending |
| RCP4a | 4 | `test_research_request_v1_stage_plan_is_frozen` | existing research chain tests | pending |
| RCP4b | 4 | `test_research_request_v2_requires_risk_decision` | `evidence/risk/phase-4/` | pending |
| RCP4c | 4 | `test_research_stage_stops_on_rejected_decision` | `evidence/risk/phase-4/` | pending |
| RCP4d | 4 | `test_research_runner_cannot_sign_risk_reviews` | `evidence/risk/phase-4/` | pending |
| RCP5a | 5 | `scripts/run_gate.sh` | `evidence/risk/release/` | pending |
| RCP5b | 5 | remote unified gate | `evidence/risk/release/remote-matrix/` | pending |

The plan is not executable until every relevant phase's rows are expanded or
marked with an approved non-security deferral.
