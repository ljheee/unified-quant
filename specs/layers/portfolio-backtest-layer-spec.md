# Portfolio and Backtest Layer Specification

Status: **design v1.0.0; contract drafting in progress**

Upstream source: `specs/layers/model-layer-spec.md`

## 1. Purpose

This specification defines two cooperating layers that convert governed model
predictions into measurable historical performance:

1. **Portfolio Layer**: converts prediction scores into target portfolio
   weights under explicit constraints. It answers "what to hold".
2. **Backtest Layer**: simulates execution of target weights against
   governed market data with costs, trading constraints, and T+1 alignment.
   It answers "what would have happened".

Both layers are downstream consumers of the model layer's published
prediction partitions. Neither may bypass manifest, checksum, or quality
gates established upstream.

## 2. Non-Goals

- live order routing or production execution;
- intraday signal generation;
- options or derivatives pricing;
- multi-asset class support in the first release;
- STAR Market (20% limit) and ChiNext (20% limit) instruments;
- ST/*ST stocks (5% limit);
- replacing Qlib's internal backtest for research exploration.

## 3. Layer Boundary

```text
PredictionStore.read(generation_id, decision_date)
  -> PortfolioBuilder.build(scores, universe, constraints)
  -> TargetWeights artifact (immutable, governed)
  -> BacktestEngine.run(weights, market_data, cost_model)
  -> BacktestResult artifact (equity curve, turnover, drawdown)
```

Hard boundaries:

1. Portfolio reads only published prediction partitions via the typed reader.
2. Backtest reads only published target-weight partitions and governed price data.
3. Target weights record the exact prediction generation consumed.
4. Backtest results record exact weight generations and price source bindings.
5. No component may bypass manifest, checksum, visibility, or lineage checks.

## 4. Identity Model

Every durable artifact has two identities following the same convention as
the model layer:

1. `generation_id`: stable content identity excluding run metadata.
2. `manifest_digest_sha256`: digest over canonical JSON including
   `generation_id`.

Required artifact families:

| Artifact | Stable identity binds |
|---|---|
| `portfolio_definition.v1` | weight scheme, constraints, rebalance schedule, prediction binding |
| `target_weights.v1` | decision date, instrument set, weight values, portfolio definition generation |
| `backtest_config.v1` | execution model, cost parameters, calendar binding |
| `backtest_result.v1` | equity curve checksum, turnover summary, config, weight generation bindings, and price source binding |

## 5. Portfolio Layer Contract

### 5.1 Input Contract

- Prediction frame from `PredictionStore.read()`.
- Universe membership: explicit list of eligible instruments per date.
- Constraint configuration: max single position, max industry exposure,
  max turnover, cash reserve.

### 5.2 Weight Schemes (first release)

| Scheme | Formula | Parameters |
|---|---|---|
| `top_n_equal_weight` | rank by score desc, top N equal weight | n |
| `score_proportional` | softmax(score / temperature) normalized | deferred to v0.2 |

### 5.3 Constraints

Constraints are applied after weight computation:

1. Single-position cap: clip each weight; residual goes to cash.
2. Industry cap: proportionally scale over-cap industries down.
3. Turnover cap: one-sided turnover = 0.5 × Σ|w_target − w_executed_prev|. Interpolate linearly toward previous executed weights when the cap is exceeded. New positions start from zero.
4. Cash reserve: sum of stock weights must not exceed 1 minus reserve.

All constraint parameters must be declared in `portfolio_definition.v1`.

### 5.4 Rebalance Schedule

First release supports `daily` only.

## 6. Backtest Layer Contract

### 6.1 Execution Model (first release)

- Decision at date T close.
- Execution at T+1 open price.
- Sell before buy within each rebalance step.
- **T+1 sellable quantity**: only shares acquired on or before the prior
  trading day are sellable on day T+1. Shares bought today cannot be sold
  until the next trading day.
- Volume guard: a fill is skipped if the required share count exceeds
  `volume_participation_cap × open_day_volume` (default cap 0.10).
  No partial fills.
- Unfilled target delta is recorded in the fills ledger; cash remains idle.

### 6.2 Cost Model

| Cost Type | First Release |
|---|---|
| Commission | fixed bps per side |
| Stamp duty | sell side only |
| Slippage | fixed bps on execution price |

### 6.3 Trading Constraints

1. Limit-up detection: buy is rejected when execution open price ≥ `prev_close × (1 + limit_ratio)`. `prev_close` uses raw (unadjusted) previous close from the governed price dataset. Default `limit_ratio` = 0.10 (main board A-shares only; STAR/ChiNext excluded per §2).
3. Suspension: cannot trade suspended instruments.
4. Minimum lot: A-share board lot of 100 shares.

### 6.4 Output Metrics

Per-date time series:
- portfolio value, daily return, turnover, cash ratio.

Summary statistics:
- cumulative return = V_end / V_start − 1;
- annualized return = (V_end / V_start)^(252 / trading_days) − 1;
- annualized volatility = std(daily simple returns) × √252;
- Sharpe ratio = mean(daily returns − rf_daily) / std(daily returns) × √252, where rf_daily = 0 (first release);
- maximum drawdown = max peak-to-trough decline of portfolio value at daily close valuation;
- average daily turnover = mean(one-sided turnover);
- win rate = fraction of days with positive daily return.

## 7. Quality Gates

Every publication binds an externally reviewed `model_quality_report.v2`.
Publishers cannot self-generate passed reports. The report checksum is a
canonical JSON digest (SHA-256 over sorted-key compact JSON excluding the
`report_checksum_sha256` field itself) and **participates in the manifest's
stable generation computation**. Replacing the quality report with another
valid report changes the generation ID, preventing silent substitution.
Reports are stored under `external_quality_reviews/` in the layer root,
following the same layout as the model layer.

Portfolio-specific checks:
- weight sum is at most one minus cash reserve with absolute tolerance of 1e-8;
- no negative, NaN, or infinite weights;
- instruments must belong to the declared universe.

Backtest-specific checks:
- every equity value is finite and strictly positive;
- turnover is non-negative;
- result period matches config period;
- all referenced weight generations pass read validation.

## 8. Acceptance Criteria Summary

Each phase has a machine-readable record under `evidence/portfolio-backtest/phase-N/`
with status, entry/exit test IDs, and preserved evidence paths. An absent record
means the phase is blocked.

### Phase 0

- All four schemas validate representative valid fixtures.
- All four schemas reject at least two distinct negative fixtures each.
- Golden vectors for stable generation are deterministic across runs.

### Phase 1

- E2E prediction-to-weight publication and read-back passes all identity checks.
- Single-position cap produces expected clipped weights.
- Industry cap scales over-cap industries proportionally.
- Turnover cap interpolates toward previous weights when exceeded.
- Tampered manifest or missing quality report rejects read.

### Phase 2

- Synthetic deterministic backtest produces expected equity curve values.
- Commission, stamp duty, and slippage reduce PnL by exact expected amounts.
- Limit-up blocks buy fill; limit-down blocks sell fill.
- Board-lot rounding floors share count to multiples of 100.

## 9. Governance

The first release reuses:
- factor-layer trust anchor for local signing;
- model-layer external reviewer registry for quality reports;
- unified gate runner for mechanical testing.

Phase records follow the same machine-readable format as the model layer.
