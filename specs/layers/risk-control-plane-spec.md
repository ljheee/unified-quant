# Risk Control Plane Specification

Status: **v0.2.1 final-CR remediated contract draft; implementation paused pending explicit activation**

Design input: `layering.md` from the one-stop-quant project.
Related specs: `specs/layers/portfolio-backtest-layer-spec.md`, `specs/layers/model-layer-spec.md`, `specs/layers/research-chain-layer-spec.md`.

Approval state: `draft-not-approved`. This document authorizes contract drafting
and review only; no schema, store, engine, publication, or backtest code may be
implemented until `contract_draft=approved` and `activation=active` in §2A.

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
must fail closed on any `critical` finding or on actions that restrict the
intended operation unless a valid, unexpired reviewed exception explicitly
permits that behavior. Restrictive actions include `block`, `block_order`,
`block_new_buy`, `de_risk`, `flatten`, and `halt_strategy`.

## 2A. Document State Machine

This specification separates three independently auditable states:

| State | Allowed value | Meaning |
|---|---|---|
| `contract_draft` | `drafting`, `ready-for-review`, `approved` | governs whether schemas and tests may be drafted |
| `activation` | `inactive`, `active` | governs whether the first implementation slice may start |
| `runtime` | `paused`, `open` | governs whether Phase 1+ code may be opened |

Normative progression is:

```text
contract_draft=approved
  AND activation=active
  -> runtime=open; Phase 0 may start
```

`activation=active` requires a checked activation condition in the
implementation plan §10 plus a dated note naming the condition and evidence.
Approval and activation may be recorded in the same commit, but they remain
separate predicates. Drafting schemas alone does not activate runtime work.

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

Ownership is explicit:

| Check class | Owning layer enforcement | Risk-plane behavior |
|---|---|---|
| canonical/factor/model quality and PIT | existing readers | no re-enforcement; bind generation identity only |
| portfolio construction constraints | `PortfolioBuilder` | recompute selected rules as independent audit; never silently repair weights |
| backtest exchange/feasibility guards | `BacktestEngine` | emit an independent decision; consumer compares native guard and risk action |
| strategy drawdown/volatility triggers | no owner today | RiskEngine owns evaluation, but Phase 3 requires an executable consumer contract |
| exception and policy approval | no owner today | Risk Control Plane owns the review artifacts and trust model |

The engine may fail before native enforcement. It must not cause native checks
to be skipped.


## 7. Core Artifact Families

Every durable artifact follows the repository identity convention:

1. `schema_version`;
2. stable `generation_id` excluding run metadata;
3. `manifest_digest_sha256` over the canonical JSON manifest;
4. upstream generation/checksum bindings;
5. producer code fingerprint;
6. serialization profile and physical-file checksums.

Closed enums are normative:

1. `policy_scope`: `strategy`, `portfolio`, `account`;
2. `rule_scope`: `strategy`, `portfolio`, `order`;
3. `rule_operator`: `greater_than`, `greater_than_or_equal`, `less_than`,
   `less_than_or_equal`;
4. `risk_action`: `allow`, `warn`, `resize`, `block`, `block_order`,
   `block_new_buy`, `de_risk`, `flatten`, `halt_strategy`, `escalate_review`;
5. `severity`: `warning`, `error`, `critical`;
6. `serialization_profile`: `json-canonical-v1`, `parquet-v1`, `ndjson-v1`.

Rule/scope compatibility is `strategy -> strategy`, `portfolio -> portfolio`,
and `order -> order`. No cross-scope policy rule is valid.

Excluded stable-identity fields are:

| Family | Excluded from stable generation |
|---|---|
| `risk_policy.v1` | `run_id`, `request_id`, `created_at` |
| `risk_state.v1` | `run_id`, `request_id`, `created_at` |
| `risk_decision.v1` | `run_id`, `request_id`, `created_at` |
| `risk_event.v1` | `run_id`, `request_id`, `created_at`, `event_sequence_number` |
| `risk_exception.v1` | `run_id`, `request_id`, `created_at` |
| `risk_review_decision.v1` | `created_at`, `request_id` |
| `risk_run.v1` | `created_at`, `run_id` |

Normative identity rules:

1. stable generation excludes `created_at`, `run_id`, `request_id`, and other
   provenance-only metadata;
2. `manifest_digest_sha256` is canonical JSON SHA-256 including
   `generation_id` and all durable binding/identity fields;
3. a top-level artifact has one JSON manifest plus one Parquet or NDJSON data
   file where rows/metrics require non-JSON storage;
4. file names and physical paths must be derived from `scope_type`, `scope_id`,
   date, and generation using path-safe segments;
5. physical files are checksummed in `files[]`; a file may not be accepted when
   its byte checksum or row count mismatches the manifest;
6. `serialization_profile` is an explicit enum, not a free string;
7. schema semantics are frozen within a released `.v1`; changes require v2.

Normative storage root is:

```text
data/risk/
  policies/<generation>/manifest.json
  reviews/<review_type>/<generation>/manifest.json
  states/<scope_type>/<scope_id>/<as_of_date>/<generation>/
    manifest.json
    state.parquet
  decisions/<scope_type>/<scope_id>/<as_of_date>/<generation>/
    manifest.json
    findings.json
  events/<scope_type>/<scope_id>/manifest.json
    events.ndjson
  exceptions/<generation>/manifest.json
```

The first implementation must include per-family fixtures and negative
fixtures for missing file, checksum mismatch, wrong path generation, extra
fields, unknown action, unapproved policy, and tampered lineage.


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

State semantics are normative:

1. state is append-only by `(scope_type, scope_id, as_of_date, generation)`;
2. `as_of_date` is the risk evaluation date, not the publication timestamp;
3. `visible_through` is the latest upstream decision/execution time included;
4. late-arriving data never rewrites prior state; it creates a new generation
   with `supersedes_state_generation_id`;
5. cooldown duration is measured in governed trading days from the trigger
   `as_of_date`;
6. exit is evaluated only after `hysteresis.exit_below` remains true for the
   declared number of consecutive governed sessions;
7. state requirement is declared per rule: `stateful=true` requires state;
   stateless rules may evaluate without a prior state snapshot;
8. multiple decisions on one date append multiple states/decisions; readers use
   explicit generation IDs, never "latest" unless the API contract defines
   deterministic ordering and rejects ties.

### 7.3 `risk_decision.v1`

`risk_decision.v1` is the immutable output of one evaluation.

Required semantic fields:

- `decision_scope`: `portfolio_publication`, `order_submission`, or a future
  explicitly enumerated scope;
- `policy_generation_id`;
- `policy_manifest_digest_sha256`;
- optional `state_generation_id` and digest;
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
| `block` | reject the evaluated portfolio publication or request |
| `block_order` | reject the candidate order |
| `block_new_buy` | preserve sell/de-risk capability but reject increases |
| `de_risk` | reduce exposure according to an executable downstream contract |
| `flatten` | request full reduction according to an executable downstream contract |
| `halt_strategy` | stop new decision/publication/activity |
| `escalate_review` | require external review before continuation |

`de_risk`, `flatten`, and `halt_strategy` are not executable until the consuming
layer defines an execution contract. They must fail closed in backtest/execution
rather than being interpreted ad hoc.

### 7.3A Finding Aggregation

Evaluation is deterministic and ordered:

1. each rule produces at most one finding;
2. findings are sorted by ascending rule `priority`, then `rule_id`;
3. `failure_action_rank` is `allow=0`, `warn=1`, `escalate_review=2`,
   `resize=3`, `block=4`, `block_order=4`, `block_new_buy=4`,
   `de_risk=5`, `flatten=6`, `halt_strategy=7`;
4. the final action is the finding with the highest rank; ties use lower rule
   priority and then lexical `rule_id`;
5. error findings with no bound threshold are `critical` and final;
6. numeric comparisons use tolerance `round(observed, 12) >
   round(threshold, 12)` for limit violations; equality variants compare
   rounded values directly;
7. multiple active policies for one scope must have disjoint effective ranges;
   overlapping generations fail closed;
8. all restrictive resize findings are normalized to a single
   `allowed_fraction`, the minimum over every such finding, before conflict
   resolution;
9. portfolio resize uses `allowed_weight = floor(requested_weight *
   allowed_fraction / 1e-8) * 1e-8`; `allowed_fraction` is the minimum of
   `rule.weight_limit / requested_weight` and
   `rule.total_notional_limit / current_total_notional`; both request and
   limit are positive;
10. order shares resize uses
    `allowed_shares = min(requested_shares, floor(rule.notional_limit /
    (execution_price * (1 + slippage_bps / 10000)) / board_lot) * board_lot)`;
    execution price and board lot are required context and must be positive;
11. order notional resize uses `allowed_notional = rule.notional_limit`;
12. an action may not be downgraded by a lower-priority finding;
13. exceptions can authorize a consumer to proceed, but never rewrite the
    original immutable decision.


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

### 7.6 Optional `risk_run.v1`

`risk_run.v1` is an immutable index for one evaluation session. It prevents the
released `backtest_result.v1` schema from being silently extended.

Required fields:

- `run_scope`: `portfolio_publication` or `order_submission`;
- `risk_policy_generation_id`;
- `risk_state_generation_id`, if stateful;
- ordered `decision_bindings[]`;
- ordered `event_bindings[]`;
- consumer binding, such as portfolio definition, target weights, backtest
  configuration, or future execution request;
- `consumer_artifact_generation_id`;
- producer code fingerprint and serialization profile.

The current backtest result contract remains v1 and unchanged. A Phase 2
integration may bind `risk_run.v1` to the backtest configuration and ordered
orders; modifying `backtest_result.v1` is deferred to an explicit v2 migration.

## 8. Governance and Trust Model

1. Risk policy activation requires an external reviewed decision.
2. The process requesting a risk decision cannot create its own approved policy.
3. The process requesting an exception cannot create its own exception.
4. Risk decisions are immutable; consumers may retry with new inputs but may not
   rewrite an existing decision.
5. Missing or expired policy, malformed policy, or tampered evidence fails
   closed for enforceable scopes. Missing state fails closed only for a
   stateful rule or an evaluation scope that declares state required.
6. Exceptions are narrow, time-bounded, and bound to a specific rule/action.
7. Risk events are append-only and must not be used as mutable state.

The implementation may initially reuse the repository's reviewed-quality trust
anchor pattern, but it must not silently extend a released schema version.
A new risk review contract should be introduced instead.

### 8A. External Risk Review Contract

Policy and exception approval must use a new `risk_review_decision.v1`
family. The model quality review v2 contract must not be silently reused or
extended.

`risk_review_decision.v1` is an immutable manifest with these required fields:

| Field | Meaning |
|---|---|
| `schema_version` | always `1` in this contract |
| `review_type` | `risk_policy_activation` or `risk_exception_grant` |
| `subject_generation_id` | generation of the reviewed policy or exception manifest |
| `subject_manifest_digest_sha256` | digest of that exact manifest |
| `review_status` | `approved` or `rejected` only |
| `reviewer` | stable reviewer identity from a reviewed registry |
| `key_id` | trust-anchor key identifier |
| `subject_content_sha256` | canonical JSON content digest reviewed by the reviewer |
| `review_signature_sha256` | detached signature over the canonical review payload |
| `reviewed_at_utc` | ISO-8601 UTC time |
| `policy` | `reject_all` for activation; exceptions require explicit permit scope |
| `errors` / `warnings` | reviewer findings |

The review decision does not itself establish trust. A separate
`risk_review_trust_anchor.v1` registry binds `key_id`, public-key fingerprint,
validity interval, reviewer, and accepted `review_type`. Verification is:

```text
verify registry -> verify signature -> verify subject generation/digest
  -> verify time interval -> verify review_type compatibility
  -> verify no errors -> activation_status=approved / exception grant valid
```

Failure taxonomy is normative: `missing`, `schema_invalid`, `tampered`,
`untrusted_key`, `expired`, `wrong_subject`, `wrong_review_type`, `rejected`,
and `reviewer_mismatch`. Any failure fails closed.

Signing is normative:

- algorithm: Ed25519;
- signature input: canonical JSON over exactly `review_type`,
  `subject_generation_id`, `subject_manifest_digest_sha256`, `review_status`,
  `reviewer`, `key_id`, `policy`, `errors`, and `warnings`;
- signature output: 128 lowercase hexadecimal characters in
  `review_signature_sha256`;
- `subject_content_sha256` must equal the canonical JSON SHA-256 of the
  subject manifest before its run-metadata fields; it provides stable content
  review, while `subject_manifest_digest_sha256` binds the exact durable
  manifest;
- verification must check both digests independently.

`risk_de_risk_contract.v1` is declared now as a required Phase 0 contract
deliverable so its version and schema identity are frozen before any Phase 3
runtime work.

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
| `portfolio_industry_limit` | portfolio | industry weight | block | requires `industry_membership.v1`; it is a Phase 0 prerequisite and disabled otherwise |
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
5. stateful rules reject missing state while stateless rules may evaluate without it;
6. decision changes when policy, state, or any bound input changes;
7. deterministic golden decision vectors;
8. event ordering and state transitions;
9. exception expiry and wrong reviewer rejection;
10. portfolio decision integration;
11. order decision integration;
12. no mutation of accepted upstream artifacts;
13. publication/execution refuses unsupported critical actions.

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
