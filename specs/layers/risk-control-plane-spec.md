# Risk Control Plane Specification

Status: **v0.1 draft; implementation paused**

Design input: `layering.md` from the one-stop-quant project.
Related specs: `specs/layers/portfolio-backtest-layer-spec.md`, `specs/layers/model-layer-spec.md`, `specs/layers/research-chain-layer-spec.md`.

## 1. Purpose

The Risk Control Plane is not an eighth pipeline layer and does not own all
risk logic. It is a cross-layer governance contract that makes risk intent,
risk state, risk decisions, risk events, and exceptions uniform across layers.

Each owning layer remains responsible for its own controls:

```text
Data -> Data quality / PIT / visibility gates
Factor -> Factor quality and factor-health gates
Model -> Model quality and future model-drift gates
Portfolio -> Target-weight constraints
Execution/Backtest -> Order feasibility and exchange-rule simulation
Risk Control Plane -> Shared policy, state, decision, event, exception, audit
```

The control plane answers four questions consistently:

1. Which reviewed risk policy applies?
2. What is the durable risk state?
3. What decision was produced for a publication or order?
4. What lifecycle transition or human intervention occurred?

It does not replace layer-owned gates. A missing canonical manifest still fails
in canonical/factor readers; a T+1 violation still fails in backtest/execution.
The control plane expresses those decisions in one auditable form where they are
cross-layer or stateful.

## 2. Position

```text
Prediction / Target Weights / Candidate Orders / Portfolio State / Market Snapshot
        |
        +--> Owning-layer validation
        |
        +--> RiskEngine.evaluate_portfolio()
        |
        +--> RiskEngine.evaluate_order()
        |
        v
Immutable risk_decision.v1 + risk_event.v1
        |
        v
Publication / Backtest / future Execution
```

A consumer may use the engine in advisory mode only for an explicitly
non-enforceable research scenario. Normative publication and execution paths
must fail closed on `block`, `halt_new_buy`, `de_risk`, or `flatten` unless a
valid, unexpired reviewed exception explicitly permits the behavior.

## 3. Non-Goals for v1

The first implementation must not attempt these capabilities:

1. live order routing;
2. real broker or exchange connectivity;
3. millisecond or intraday streaming risk checks;
4. portfolio optimization;
5. multi-asset, derivatives, margin, leverage, or short-selling risk;
6. replacing existing canonical, factor, model, portfolio, or backtest gates;
7. automatically inventing new target weights or orders;
8. becoming a generic rules engine for business workflows.

The first release is deterministic, artifact-governed, decision-time risk
governance for research and backtests.

## 4. Why Full Production Control Is Deferred

A complete real-time Risk Control Plane is intentionally deferred. It should
not be built until at least one of the following conditions is met:

1. an Execution Layer with real or paper order submission is specified;
2. live positions require continuous intraday monitoring;
3. Research Chain must coordinate more than one strategy/account;
4. drawdown or volatility limits become release gates for a tradable policy;
5. risk events must be consumed by an operational on-call/compliance workflow;
6. factor/model drift monitoring has accepted-data and monitoring contracts.

Deferral reasons:

- The current system ends at governed backtest evidence; there are no live
  orders, no execution gateway, and no continuous position stream.
- Existing backtest checks already cover limit up/down, suspension, T+1,
  volume, and cash for the first A-share research scope.
- Real-time risk requires a different latency, availability, and process model
  from the repository's immutable-artifact research pipeline.
- Premature schemas would guess broker, OMS, market-data, and position semantics
  before the Execution Layer contract exists.
- Portfolio-level drawdown/volatility actions without an execution contract could
  create unexecutable `de_risk` or `flatten` instructions.

The lightweight control-plane contracts below are stable enough to draft now;
runtime implementation remains paused.

## 5. Control Taxonomy

Risk controls are classified into three concerns:

| Concern | Meaning | Examples |
|---|---|---|
| Error prevention | Reject malformed, stale, ineligible, or inconsistent input | missing lineage, duplicate keys, unknown instrument, insufficient cash |
| Limits | Constrain exposure and activity | single-name cap, industry cap, gross exposure, turnover, order notional |
| Loss control | Change behavior after adverse or unstable conditions | drawdown de-risk, volatility de-risk, strategy halt |

A control may belong to an owning layer and still emit a shared
`risk_decision.v1` for audit. The control plane does not have to recompute a
check that the owning layer already computed.

## 6. Current-Layer Mapping

| Layer | Existing/prescribed owner | Shared risk-plane role |
|---|---|---|
| Data | canonical schemas, manifests, checksums, PIT visibility | bind data-generation identity to risk decisions |
| Factor | factor schemas, null/coverage/freshness, quarantine | bind factor-generation identity; future IC/crowding signals |
| Model | model quality reports, reviewed policies, lineage | bind model/dataset/prediction generation; future drift signals |
| Portfolio | target-weight constraints | publication pre-check and concentration/exposure decision |
| Backtest | execution simulation guards | pre-trade checks and order decision audit |
| Future execution | broker/order gateway checks | consume the same pre-trade contract with lower-latency storage |

## 7. Core Artifact Families

Every durable artifact follows the repository identity convention:

1. `schema_version`;
2. stable `generation_id` excluding run metadata;
3. `manifest_digest_sha256` over the canonical JSON manifest;
4. upstream generation/checksum bindings;
5. producer code fingerprint;
6. serialization profile and physical-file checksums.

### 7.1 `risk_policy.v1`

`risk_policy.v1` is an immutable, reviewed set of rules. It is configuration,
not an evaluation result.

Required semantic fields:

- `policy_scope`: `strategy`, `portfolio`, `account`, `instrument`, or `order`;
- `rules[]`: deterministic ordered list of rule declarations;
- `rule_id`, `metric`, `operator`, `threshold`;
- `action`;
- `severity`: `warning`, `error`, or `critical`;
- `priority`: deterministic evaluation precedence;
- `cooldown_days` or analogous state window;
- `hysteresis`: exit threshold, if stateful;
- `effective_from` / `effective_to`;
- `activation_status`: `draft`, `approved`, `retired`;
- `approval_binding`: externally reviewed decision reference.

A retired or unapproved policy may be loaded for replay or audit but must not
produce an enforceable decision. Every enforceable decision must record the
approved policy generation.

### 7.2 `risk_state.v1`

`risk_state.v1` is a durable snapshot of risk-relevant state at a decision
time. It makes stateful rules such as drawdown hysteresis and cooldowns
possible.

Required semantic fields:

- `scope_type` and `scope_id`;
- `as_of_date`;
- `visible_through` decision/execution cutoff;
- input generation bindings: portfolio state, target weights, orders, market
  data, calendar, universe, model prediction where applicable;
- metric observations: NAV, gross exposure, single-name weights, industry
  weights, rolling drawdown, rolling volatility, cash, turnover;
- open halt/cooldown state;
- counters such as consecutive trigger days;
- producer code fingerprint and serialization profile.

State is derived from accepted upstream artifacts. It must not silently read
unpublished, tampered, or future data.

### 7.3 `risk_decision.v1`

`risk_decision.v1` is the immutable output of one evaluation.

Required semantic fields:

- `decision_scope`: `portfolio_publication`, `order_submission`, or a future
  explicitly enumerated scope;
- `policy_generation_id`;
- `policy_manifest_digest_sha256`;
- `state_generation_id` and digest, where state exists;
- ordered input bindings;
- `as_of_date` and visibility cutoff;
- `findings[]`, each with `rule_id`, observed value, threshold, result, severity;
- `action`;
- `constraints[]`, such as allowed weight, allowed shares, allowed notional;
- evaluation code fingerprint;
- deterministic decision digest.

Allowed first-class actions are:

| Action | Meaning |
|---|---|
| `allow` | no restriction |
| `warn` | allow but persist warning |
| `resize` | reduce requested weight, shares, or notional to a declared value |
| `block_order` | reject the candidate order |
| `block_new_buy` | preserve sell/de-risk capability but reject increases |
| `de_risk` | reduce exposure according to an executable downstream contract |
| `flatten` | request full reduction according to an executable downstream contract |
| `halt_strategy` | stop new decision/publication/activity |
| `escalate_review` | require external review before continuation |

`de_risk`, `flatten`, and `halt_strategy` are not executable until the consuming
layer defines an execution contract. They must fail closed in backtest/execution
rather than being interpreted ad hoc.

### 7.4 `risk_event.v1`

`risk_event.v1` records lifecycle changes, not every internal calculation.

Required event classes:

- `policy_activated`;
- `policy_retired`;
- `risk_triggered`;
- `risk_cleared`;
- `action_taken`;
- `exception_applied`;
- `exception_expired`;
- `escalation_opened`;
- `escalation_closed`;
- `state_snapshot_published`.

Each event binds the decision/state generations, scope, timestamp visibility,
and reason. Events are append-only.

### 7.5 `risk_exception.v1`

`risk_exception.v1` grants a narrowly scoped exception to a policy action. It
is not generated by the publisher or decision requester.

Required semantic fields:

- exception id;
- policy generation and rule id;
- scope and subject;
- permitted action/limit;
- expiration;
- reviewer, review binding, signature/trust anchor;
- reason and supporting evidence.

Exceptions must be logged as events when applied and when expired. An exception
cannot rewrite the original decision; it can only authorize a downstream
consumer to proceed despite that decision.

## 8. Governance and Trust Model

1. Risk policy activation requires an external reviewed decision.
2. The process requesting a risk decision cannot create its own approved policy.
3. The process requesting an exception cannot create its own exception.
4. Risk decisions are immutable; consumers may retry with new inputs but may not
   rewrite an existing decision.
5. Missing policy, expired policy, malformed policy, missing state, or
   tampered state fails closed for enforceable scopes.
6. Exceptions are narrow, time-bounded, and bound to a specific rule/action.
7. Risk events are append-only and must not be used as mutable state.

The implementation may initially reuse the repository's reviewed-quality trust
anchor pattern, but it must not silently extend a released schema version.
A new risk review contract should be introduced instead.

## 9. Evaluation Contracts

### 9.1 Portfolio Publication Evaluation

Input:

- candidate target weights;
- portfolio definition;
- prediction/universe lineage;
- prior target weights, if turnover is governed;
- industry membership, if industry limits are enabled;
- accepted price/NAV inputs;
- risk policy and latest risk state.

Output:

- `risk_decision.v1` with scope `portfolio_publication`;
- optional updated `risk_state.v1`.

Normative checks include:

- gross stock weight/exposure;
- cash reserve;
- single-instrument concentration;
- top-N concentration;
- industry concentration;
- target turnover;
- required lineage resolution;
- halt/cooldown state.

The engine must not silently repair target weights. A `resize` action must be
explicitly consumed by the owning layer or rejected if unsupported.

### 9.2 Pre-Trade Order Evaluation

Input:

- candidate order;
- current holdings and cash;
- target weights or decision context;
- governed price, volume, calendar, suspension, instrument metadata;
- risk policy and latest risk state.

Output:

- `risk_decision.v1` with scope `order_submission`;
- optional updated `risk_state.v1`.

Normative checks include:

- instrument eligibility;
- suspension/zero-volume status;
- limit up/down;
- board lot;
- T+1 sellable quantity;
- available cash;
- max order notional/shares;
- participation cap;
- duplicate/stale order;
- strategy/instrument halt.

The engine returns a decision and constraints. It does not submit or mutate an
order. The execution adapter is responsible for applying the declared action or
failing closed.

## 10. Initial Rule Catalog

The catalog is the scope boundary for the first implementation. Rule IDs are
normative and must not be reinterpreted after release.

| Rule ID | Scope | Metric | First action | Notes |
|---|---|---|---|---|
| `portfolio_gross_exposure_limit` | portfolio | gross stock exposure | resize | coexists with cash reserve |
| `portfolio_single_name_limit` | portfolio | instrument weight | resize | portfolio already enforces construction cap |
| `portfolio_top_n_limit` | portfolio | top-N concentration | block | first version may reject rather than optimize |
| `portfolio_industry_limit` | portfolio | industry weight | block | requires governed industry membership |
| `portfolio_turnover_limit` | portfolio | one-sided target turnover | resize | prior target weights required |
| `portfolio_cash_reserve_limit` | portfolio | cash reserve | block | fails closed on missing weights |
| `order_notional_limit` | order | absolute order notional | resize | uses declared execution price basis |
| `order_participation_limit` | order | shares / PIT volume proxy | resize | decision-day volume, not future intraday volume |
| `order_insufficient_cash` | order | cash after sell settlement | block_order | sells are settled before buys in current backtest |
| `order_instrument_halt` | order | halt/suspension state | block_order | governed source required |
| `order_limit_price` | order | execution open vs prior close | block_order | preserves existing A-share scope |
| `order_t1_sellable_quantity` | order | sellable shares | resize | reduces sell to eligible quantity |
| `strategy_drawdown_trigger` | strategy | rolling drawdown | de_risk | requires execution/rebalance contract before runtime |
| `strategy_volatility_trigger` | strategy | rolling volatility | de_risk | requires execution/rebalance contract before runtime |

Rules requiring `de_risk`, `flatten`, or continuous state are not part of the
first executable slice unless their downstream action is also specified.

## 11. Determinism and Testing

For the same:

- policy generation;
- state generation;
- ordered input generations and checksums;
- evaluation code fingerprint;
- declared visibility cutoff,

the engine must produce the same decision digest.

Required test classes:

1. valid policy/state/decision schema fixtures;
2. negative fixtures for malformed or conflicting contracts;
3. missing/tampered input fails closed;
4. policy missing, unapproved, retired, or expired fails closed;
5. decision changes when policy, state, or any bound input changes;
6. deterministic golden decision vectors;
7. event ordering and state transitions;
8. exception expiry and wrong reviewer rejection;
9. portfolio decision integration;
10. order decision integration;
11. no mutation of accepted upstream artifacts;
12. publication/execution refuses unsupported critical actions.

## 12. Deferred Real-Time Design Placeholder

A future production control plane should introduce, after the Execution Layer:

1. a stateful low-latency limit cache;
2. an execution risk gateway;
3. broker/exchange rejection reconciliation;
4. intraday position and P&L streams;
5. kill-switch ownership and recovery procedures;
6. alerting and on-call ownership;
7. explicit availability targets and degraded-mode policy.

Those requirements are deliberately not normative in this research-first spec.
