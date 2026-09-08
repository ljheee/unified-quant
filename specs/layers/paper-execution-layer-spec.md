# Paper Execution Layer Specification

Status: **v0.1.2 review-remediated contract draft**
Governance: contract-first, immutable artifacts, fail-closed accepted reads.
Runtime mode: paper execution is a deterministic simulation mode, not a broker integration.

## 1. Purpose

The Paper Execution Layer converts a governed target-weight publication and an
executable portfolio state into deterministic paper orders, fills, rejections,
cash movements, and a new portfolio state. It exists to exercise the order
lifecycle, risk gates, and state reconciliation without fabricating historical
backtest returns or connecting to a broker.

The first slice is offline and replayable. It may consume governed historical or
delayed market data as a paper market, but it must label that market source and
must never claim live-market execution.

## 2. Scope

### 2.1 In Scope

- Deterministic paper order planning from target weights and executable state.
- One decision date mapped to one execution session.
- Order lifecycle is planned before execution and is reconciled only through the
  persisted terminal states `filled`, `rejected`, `cancelled`, and `expired`;
  transient submission is never a durable paper artifact state.
- Deterministic paper fill policy with explicit price, quantity, cash, fee, and
  quantity-lot semantics.
- A-share style sell-before-buy, T+1 sellable inventory, price limits, suspension,
  and board-lot handling.
- Persistent, checksummed order plans, execution results, and paper portfolio
  state manifests.
- Risk Control Plane pre-trade decisions as hard input gates; post-trade risk
  integration is explicitly deferred.
- Reconciliation of target deltas, orders, fills, rejected quantities, fees, and
  resulting holdings/cash.

### 2.2 Out of Scope

- Live or production broker connectivity.
- Real-time market-data subscriptions, websocket sessions, or daemon execution.
- Market making, hidden order types, algorithmic child-order scheduling, and
  crossing networks.
- Automatic order repair or retry beyond the deterministic policy declared in the
  execution configuration.
- Portfolio optimization or model inference.
- Secret management and credential storage.
- Tax, financing, margin, short selling, index rebalancing specials, and options.
- Changing the historical backtest layer's accepted simulation semantics.

A future `BrokerAdapter` may implement the same order/result/state contract, but
it is a separate runtime boundary and is not part of the first paper slice.

## 3. Layer Boundary

```text
TargetWeightStore(target_weights.v1)
  + PaperPortfolioStateStore(paper_portfolio_state.v1)
  + Governed paper market inputs
  + Risk Control Plane decision
        |
        v
ExecutionConfigStore(execution_config.v1)
        |
        v
PaperOrderPlanner
  -> OrderPlanStore(order_plan.v1)
        |
        v
PaperExecutionEngine
  -> ExecutionResultStore(execution_result.v1)
        |
        v
PaperPortfolioStateStore(next paper_portfolio_state.v1)
```

The execution layer does not decide target weights. It may reject the execution
run, but it must not silently revise investment intent. Cash residual from board
lots, rejected orders, and fees is explicitly represented in the resulting state
and reconciliation ledger.

## 4. Required Artifacts

All manifests are JSON documents governed by the same identity, checksum, and
quality-report rules as other durable layers.

### 4.1 `execution_config.v1`

- `schema_version`
- `execution_id`: path-safe stable execution name.
- `mode`: `paper` in the first release.
- `state_mode`: `initial` or `continuation`; `initial` requires an explicit
  `initial_state` object and must not bind a prior published paper state.
- `market`: `cn_a` in the first release.
- `decision_date`
- `execution_date`
- `target_weights_binding`: family, generation ID, manifest digest, and decision date.
- `input_state_binding`: prior `paper_portfolio_state.v1` generation, digest, and
  as-of date; required when `state_mode=continuation` and null when `initial`.
- `initial_state`: for `state_mode=initial`, a checksummed initial holdings list,
  initial cash, valuation price basis, and provenance note; absent otherwise.
- `market_data_binding`: dataset family, schema version, generation ID, manifest
  digest, visibility timestamp, price basis, and exact required columns.
- `calendar_binding`
- `suspension_binding`
- `corporate_action_binding`
- `risk_decision_binding`
- `lot_size`
- `fee_policy`: explicit rates and minimums for commission, stamp tax, and transfer fee.
- `price_policy`: order pricing basis, limit tolerance, and allowed execution window.
- `quantity_policy`: sell-before-buy, T+1, board lot, minimum order quantity, and rejection behavior.
- `serialization_profiles`
- `quality_report_binding`: reviewed external quality report identity and canonical
  checksum; absent from stable-content identity but included in durable publication.

`decision_date` must be a governed trading day on or before `execution_date`.
For forward paper execution, ordinary decision cutoff is the configured market
close on the decision date. If replay data is deliberately delayed or synthetic,
the configuration must set `data_delay_policy` and `data_delay_reason`; the
result cannot be labeled live-paper.

### 4.2 `order_plan.v1`

The order plan is the complete intended order set after quantity normalization
but before fills. Its payload is a Parquet file with fixed columns:

| Column | Dtype | Semantics |
|---|---|---|
| `instrument` | string | canonical instrument ID |
| `side` | string enum | `buy`, `sell` |
| `requested_quantity` | int64 | board-lot-normalized share quantity |
| `limit_price` | float64 nullable | governed order price in CNY; null only for `no_market_data` |
| `order_type` | string enum | `limit` in the first release |
| `reason` | string enum | `target_rebalance`, `cash_residual`, `risk_release` |
| `target_delta_shares` | int64 | exact target change before normalization |
| `sellable_quantity` | int64 | T+1-sellable input quantity used for sizing |
| `previous_quantity` | int64 | prior executable quantity |
| `target_quantity` | int64 | intended post-fill quantity before guards |
| `order_state` | string enum | `planned`; durable submission is out of scope |
| `plan_sequence` | int64 | deterministic execution sequence |

The manifest records row count, column schema, checksum, code fingerprint,
logical fingerprint, upstream bindings, and an ordered cash-residual order list.
Unfilled target deltas are represented by absent or reduced orders and must be
reconciled in the execution result; they must not disappear.

### 4.3 `execution_result.v1`

The execution result is the immutable post-simulation ledger. Its payload is a
Parquet file with fixed columns:

| Column | Dtype | Semantics |
|---|---|---|
| `instrument` | string | canonical instrument ID; non-empty for every order-derived event |
| `side` | string enum | `buy`, `sell`, `none` |
| `order_state` | string enum | `filled`, `rejected`, `cancelled`, `expired` |
| `reject_reason` | string enum | `risk_blocked`, `suspended`, `limit_up`, `limit_down`, `insufficient_cash`, `t1_not_sellable`, `no_market_data`, `lot_size`, `quantity`, `price`, `cancelled`, `none` |
| `filled_quantity` | int64 | executed share quantity |
| `requested_quantity` | int64 | submitted quantity |
| `price` | float64 | gross execution price; 0 when not filled |
| `gross_amount` | float64 | price × filled quantity |
| `fee_amount` | float64 | configured fee charged on this event |
| `net_amount` | float64 | signed cash movement after fees |
| `plan_sequence` | int64 | originating deterministic sequence |
| `decision_date` | datetime64[ns] | originating decision date |
| `execution_date` | datetime64[ns] | simulated execution session |

The result manifest also binds the exact order plan, target weights, prior
paper state, market-data inputs, risk decision, and execution code
fingerprint. It must not bind the next paper state; the next state binds the
result after publication. It publishes aggregate reconciliation fields:

- target instrument count and total stock weight;
- planned buy/sell quantity and notional;
- filled buy/sell quantity, gross amount, fee amount, and net cash movement;
- rejected quantity grouped by reason;
- unfilled target quantity grouped by reason;
- opening and closing cash;
- opening and closing portfolio value;
- declared floating-point reconciliation tolerance.

No rows, zero fills, and complete rejection are valid results when every input
and reconciliation invariant holds.

### 4.4 `paper_portfolio_state.v1`

The state snapshot is a Parquet payload plus a JSON manifest. It records:

- `state_date`: as-of execution close.
- `cash`: non-negative closing cash in CNY.
- `holdings`: one row per instrument with quantity, average cost, buy-locked
  quantity, and sellable quantity.
- `target_weights_binding`
- `previous_state_binding`
- `execution_result_binding`
- `risk_decision_binding`
- `market_data_binding`
- row count, column schema, checksums, logical fingerprint, and serialization profile.

`buy_locked_quantity` is inventory bought during the current A-share session and
therefore not sellable that session. `sellable_quantity` equals quantity minus
buy-locked quantity after applying the T+1 rule and any governed lifecycle
restriction.

## 5. Deterministic Execution Policy

### 5.1 Price and Market Data

The first release supports a single limit price per instrument per paper session.
The governed paper market input must provide raw `open`, `high`, `low`, `close`,
`volume`, and explicit `status`, `limit_up`, and `limit_down` values where the
source contract supports them. Missing mandatory market data is a typed
`no_market_data` rejection; it is not treated as zero value or an open-ended
skip.

Price basis is explicitly configured and one of:

- `decision_close_estimate`: uses the decision-date close as the paper limit and
  documents that it is a simulation approximation.
- `execution_open_replay`: uses the replay execution-session open as the paper
  fill price.

`execution_open_replay` consumes execution-day information and is permitted only
for replay and delayed paper testing, never for a strict decision-time PIT claim.

### 5.2 Limits and Trading Status

An instrument is untradeable when its governed status is not `trading`, when the
suspension binding marks it suspended, or when the corporate-action binding
declares it excluded for the execution session. The result records the exact
reason.

A buy is rejected at `limit_up` when the configured order price is at or above
the governed limit-up price. A sell is rejected at `limit_down` when the
configured order price is at or below the governed limit-down price. A configured
absolute or relative limit tolerance may permit execution only when both sides
of the comparison satisfy the declared tolerance.

### 5.3 Quantity and T+1

Sells are planned before buys. For a continuation run, sellable quantity is
derived per execution session, never copied as a mutable balance:

```text
sellable_quantity =
    previous_quantity
    if previous_state_date < execution_date
    else previous_quantity - previous_buy_locked_quantity
```

A same-session buy is added to `buy_locked_quantity` in the execution-close
state and is not sellable until the next governed session. Board lots round buys
down and sells down to the configured lot size. The last board lot may never be
sold if the configured minimum holding rule is `preserve_one_lot`; otherwise
fractional residual is cash-settled only when the explicit contract permits it.

Insufficient cash is evaluated after all planned sells and their fees. A buy that
cannot be fully funded at its declared price is either rejected or rounded down
by the configured policy; the exact policy is a required configuration field.
Partial fills are not supported in the first release.

### 5.4 Valuation and NAV

All portfolio valuations use raw governed prices in CNY. For order planning, the
decision-date valuation is:

```text
decision_nav = initial_or_prior_cash
             + Σ(previous_quantity × decision_close_price)
```

A target quantity is computed from the governed target weight and this
decision-date NAV:

```text
target_quantity = floor(target_weight × decision_nav / order_price)
```

For result reconciliation and next-state publication, execution-date close is
the valuation price. A missing execution-date close for any holding that remains
in the prior/initial state, including an untraded holding in a zero-order or
sell-only run, fails closed; it cannot be valued as zero. Opening value is
reconstructed from the prior state and the same decision-date valuation rule. Closing cash equals
prior cash plus all signed net cash movements. `closing_portfolio_value` is the
result's deterministic projection of the next state before that state exists.

### 5.5 Fees

Fees are deterministic functions of gross amount and the fee policy. The first
release supports explicit buy commission, sell commission, sell stamp tax, and
transfer fee with optional minimum commission. Fee values are rounded to the
configured currency precision. Fees belong to their event and are included in
that event's signed net cash movement.

### 5.6 Cost Basis

Buy fees increase the instrument's invested cost basis. The deterministic
average-cost formula after a buy is:

```text
new_average_cost =
    (previous_quantity * previous_average_cost
     + filled_quantity * gross_price
     + buy_fee)
    / (previous_quantity + filled_quantity)
```

Sell fees are cash costs and do not change the surviving position's average
cost. A sell reduces quantity at the existing average cost and does not imply a
change in that average cost. When quantity reaches zero, average cost is reset to
zero. Corporate actions are excluded by the first release; therefore no
corporate-action cost-basis adjustment is defined in `v1`.

### 5.7 Risk Gate

A paper execution run must receive an immutable `risk_decision.v1` reviewed by
the Risk Control Plane governance rules. The first release accepts only the
existing v1 actions `allow` and `warn` as executable; both preserve the bound
target weights unchanged. `resize` is not consumed by paper v1 because it would
change investment intent. Paper v1 deliberately treats `block_new_buy` as a
whole-run fail-closed block rather than decomposing it into sell-only execution;
this avoids changing investment intent through an execution-layer side effect.
All `block`, `block_order`, `block_new_buy`, `de_risk`, `flatten`,
`halt_strategy`, and `escalate_review` decisions block publication of the order
plan, result, and next state. A future
`risk_decision.v2` must explicitly define action payloads and effective-weight
semantics before any automatic action consumption. Missing, expired, tampered,
or mismatched risk input fails closed before any artifact publication.

## 6. Identity, Storage, and Readback

Each manifest has three distinct identities:

1. `generation_id` is the SHA-256 of the semantic content excluding run-only
   metadata, quality report binding, and `manifest_digest_sha256`.
2. `subject_content_sha256` supplied to an external quality review is the same
   SHA-256 value as `generation_id`; it is not the final manifest digest.
3. `manifest_digest_sha256` is the SHA-256 of the canonical published manifest
   after the reviewed quality report binding is inserted, excluding only the
   `manifest_digest_sha256` field itself. This final digest is verified on
   durable readback.

The reviewed report therefore binds the stable semantic subject, while the final
manifest digest provides tamper evidence for the complete published document.
This avoids a circular report→manifest→report dependency.

The paper root is an explicitly configured path segment under the environment's
immutable artifact root. It must not resolve to the repository source tree in
production and must reject path traversal and symlink traversal. Storage layout
is root-separated by artifact family:

```text
<root>/execution-configs/<execution_id>/<generation>/manifest.json
<root>/order-plans/<execution_id>/<generation>/data.parquet
<root>/order-plans/<execution_id>/<generation>/manifest.json
<root>/execution-results/<execution_id>/<generation>/data.parquet
<root>/execution-results/<execution_id>/<generation>/manifest.json
<root>/paper-states/<execution_id>/<generation>/data.parquet
<root>/paper-states/<execution_id>/<generation>/manifest.json
```

Publication must create a partition atomically, reject overwrite, and preserve
manifests. Accepted readback must verify manifest identity, digest, payload
checksum, row count, column names, dtypes, serialization profile, key uniqueness,
and cross-manifest bindings before returning data. Tampering with either the
manifest or payload must produce a typed `ContractError`.

## 7. Quality Governance

Every durable publication requires an externally reviewed
`model_quality_report.v2` decision. The report's `binding_type` must be one of
the four new paper families, `bound_generation_id` must equal the subject's
`generation_id`, and `subject_content_sha256` must equal the same stable
semantic content digest. The final `manifest_digest_sha256` is verified during
readback but is not embedded in the pre-publication review. Publisher-generated
passed reports are forbidden. The review registry must add the four families and
paper-specific allowed checks; the report schema's existing policy/check shape
is reused without changing released model families. Because the registry is
anchored, a registry change requires an atomic, reviewed registry plus
trust-anchor digest update; the implementation plan must record the old digest,
new digest, review evidence, and the associated commit. The Research Chain
quality provider allowlist in `src/uq/research_chain/contracts.py` must also
additively include the four paper families, and provider configurations must
declare only the paper families they actually support. Review trust-anchor and
production runtime-mode rules are inherited unchanged.

Minimum review checks are:

- schema valid;
- upstream bindings present and digest-consistent;
- payload readable and checksum-valid;
- row count and key uniqueness correct;
- column and dtype contract correct;
- execution policy values inside declared bounds;
- fees and reconciliation tolerance valid;
- cash non-negative;
- T+1 and lifecycle quantities valid;
- result/state reconciliation complete.

## 8. Runtime and Broker Boundary

The first implementation runs only with `UQ_RUNTIME_MODE=research` or
`production` and `mode=paper`. No network broker call is permitted. The
production mode may execute the same paper engine, but it must reject test trust
anchors under the inherited production guard.

A future broker slice must introduce:

- a versioned `BrokerAdapter` protocol;
- explicit venue/session credentials outside the repository;
- broker order ID mapping;
- reconnection and idempotency rules;
- typed submission, acknowledgment, rejection, partial-fill, cancellation, and
  timeout events;
- independent broker reconciliation before state publication;
- a distinct `broker` runtime mode and production approval record.

None of these capabilities may be silently inferred from the paper engine.

## 9. Acceptance Criteria

The paper layer may exit its implementation phase only when:

1. All four required schemas, representative fixtures, negative fixtures, golden
   vectors, and typed loaders exist and are tested; quality registry has all four
   paper binding types.
2. Valid target weights plus executable state produce a deterministic order plan,
   result, and next state for both `initial` and `continuation` state modes.
3. Missing mandatory market data fails closed with `no_market_data`.
4. A non-trading instrument produces a typed suspended or excluded rejection.
5. Limit-up blocks buys and limit-down blocks sells under the configured rule.
6. A same-session buy cannot be sold in the same paper session.
7. Board-lot rounding, sell-before-buy, and cash feasibility are deterministic.
8. Every rejected or unfilled target delta is represented in reconciliation.
9. State cash, holdings, fees, gross/net amounts, and portfolio value reconcile
   using the declared decision/execution valuation bases.
10. Tampered manifests, payloads, upstream bindings, and quality reports fail
    accepted readback.
11. Risk rejection prevents every downstream publication.
12. No live broker path, network adapter, credential read, or broker runtime mode
    is enabled by this slice.

## 10. Deferred Contract Evolution

The following changes require new schema versions and migration evidence:

- partial fills and child orders;
- intraday decision cutoffs or multiple execution windows;
- market or auction order types;
- real broker adapter integration;
- non-A-share markets;
- margin, short selling, derivatives, and financing;
- live state reconciliation with external custodian or broker records.
