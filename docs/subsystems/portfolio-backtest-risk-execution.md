# Portfolio, Backtest, Risk Control Plane, and Paper Execution

> Current behavior reference. Last checked: `2026-10-30` against the uncommitted worktree rooted at HEAD `2d713260eef73d595bf75bb1bd06899a8929dad1`. Based on source code, configuration, and tests; unverified items are marked `UNKNOWN`.

## Responsibilities and Boundaries

Four adjacent layers are documented together because they are verified as one chain and are only reachable as a chain. They are still distinct responsibilities:

| Layer | Responsibility | Explicitly not |
| --- | --- | --- |
| Portfolio (`src/uq/portfolio/builder.py:33`) | turn published predictions into governed target weights | order generation, execution |
| Backtest (`src/uq/backtest/engine.py:50`) | deterministic daily T+1 simulation with costs and guards | order submission, live data |
| Risk Control Plane (`src/uq/risk/*`) | cross-cutting policy/event/decision/exception governance at publication and order gates | an independent business layer |
| Paper Execution (`src/uq/execution/*`) | deterministic all-or-none paper fills and next portfolio state | broker connectivity, live market data, partial fills |

The Risk Control Plane is deliberately **not** a pipeline stage: it has no independent revenue objective and must be bound to a policy, a review, an action under review, and context (`docs/layer-architecture.md` §4).

## Authoritative Sources

| Fact | Authoritative path |
| --- | --- |
| Weight construction and constraints | `src/uq/portfolio/builder.py:33` `PortfolioBuilder.build` |
| Backtest execution, guards, cost model | `src/uq/backtest/engine.py:50` `BacktestEngine.run` |
| Risk decision engine and rule taxonomy | `src/uq/risk/engine.py`, `src/uq/risk/engine.py:41` `_SUPPORTED_METRICS`, `src/uq/risk/engine.py:57` `_ORDER_RULE_SCOPES` |
| Risk manifest/canonical identity | `src/uq/risk/contracts.py:106` `risk_contract_identities`, `:83` `validate_risk_contract` |
| Immutable risk stores | `src/uq/risk/stores.py:73`–`:204` |
| Publication gate | `src/uq/risk/adapter.py:61` `PortfolioPublicationRiskGate.gate` |
| Single-use publication authorization | `src/uq/risk/publication.py:9` `ConfigPublicationBinding` |
| Paper order planning | `src/uq/execution/planner.py:13` `PaperOrderPlanner.plan` |
| Paper matching | `src/uq/execution/engine.py:18` `PaperExecutionEngine.execute` |
| Paper portfolio state evolution | `src/uq/execution/state.py:20` `PaperPortfolioStateBuilder.build` |
| Governed market/calendar source for paper | `src/uq/execution/market_dataset_publisher.py:29` `MarketDatasetPublisher`, `src/uq/execution/market_source.py:23` `PublishedPaperMarketData` |
| Research-chain stage adapter for paper | `src/uq/execution/stage_adapter.py:61` `PaperExecutionStageAdapter` |

## Primary Runtime Paths

**Portfolio.** `portfolio_definition.v1` keeps `top_n_equal_weight`. Version 2 adds `score_weighted`: after the same NaN exclusion, universe filter, descending rank and instrument-ID tie-break, the selected top-N scores are converted to temperature-scaled softmax weights. The calculation subtracts the maximum score before exponentiation, rejects non-finite selected scores and non-positive/non-finite temperatures, applies the single-name cap, then scales down to respect the cash reserve. A cap can leave extra cash because this version does not redistribute clipped weight. Both versions retain the turnover guard: `max_turnover` requires the previous target weights unless `initial_state` is set, and the published manifest records `previous_target_weights_generation_id: null` for the first-state exemption.

Version 3 adds `risk_parity`. It first selects top-N by the existing score policy, then solves long-only equal risk contributions from sample covariance over the configured `lookback_sessions` (recommended starting value: 60 trading sessions, explicitly pinned in the reviewed request). `PortfolioRiskReturnsStore` accepts offline decimal simple returns from adjusted closes and binds them to an immutable, published backtest calendar and a declared source/approval reference. Observations are available only after session close; each decision uses exactly the configured sessions strictly before that date. The selected basket must have a complete finite return panel, and covariance must be symmetric positive definite; missing history or a failed solver rejects weight construction. Single-name, cash and turnover constraints still apply after the ERC solve and can therefore change the final risk contributions. The default Research Chain route is request v8 with the reviewed portfolio template v3; v1/v2 semantics remain unchanged. `scripts/publish_portfolio_risk_returns.py` is the offline publisher. It does not invent returns or fetch external data.

Portfolio v3 also implements `industry_neutral`: after score-based top-N selection, it splits budget equally among the represented industries and equally among selected names within each industry. This is industry-balanced within the selected basket, not benchmark-relative neutrality. The per-date labels come from `ExecutionFactsStore`, which the v3 backtest config binds and whose `visible_at` must be no later than that session's open. The request's `industry_source_binding` must name that exact facts generation and the `industry` column. Missing classifications for any selected name reject publication. `max_industry_weight` scales an over-limit industry's weights proportionally and leaves the difference in cash; it does not redistribute that budget to other industries. v1/v2 industry-cap behavior remains fail-closed.

**Backtest v1/v2.** Per session: mark to market, then, on decision dates, compute target shares from T-close NAV at the next session's open with slippage and board-lot flooring, and execute. Side is classified from the **day-start** position and grouped sell → buy → hold: this is a settlement rule, not a comment — a single alphabetical pass once let an early-lettered buy consume cash a later sell had not yet freed and record `skipped_insufficient_cash` in a fundable run. A weight partition containing a corporate-action instrument is refused outright; such instruments must be removed at universe-filtering time.

**Backtest v3 risk controls.** The reviewed config may set `risk_constraints.max_drawdown_stop` and/or `risk_constraints.volatility_target`. Drawdown is measured from the highest close-marked NAV since the run began; crossing the configured threshold latches a stop, replaces desired weights with cash, and retries liquidation on each later session. Volatility targeting estimates sample standard deviation from completed daily portfolio returns over the configured lookback, annualizes by `sqrt(252)`, and scales target weights by `min(1, target / estimate)` only after the minimum history is available. It never increases exposure. Both controls are visible in daily metrics and summary fields. The stop cannot promise immediate liquidation when price bands, suspensions, participation or turnover prevent a fill. These are reviewed backtest/replay controls and do not replace the separate Risk Control Plane's policy/state/action governance or prove live risk.

**Valuing a held name across a suspension.** A per-session vendor panel carries only the rows the vendor returned, so a genuinely suspended name has *no row at all* — a `status`-only read cannot see it. The engine therefore keeps a `last_close_prices` map (distinct from `prev_close_prices`, which answers a different question: the next session's limit-test reference, cr.md FC-M7). A held name absent from the session is valued at its last traded close, which is what keeps the equity curve continuous while the execution branch records `skipped_suspended` for it (cr.md 8.64). Two fail-closed cases remain: the panel carries the name but its close is unusable, and the panel never carried it at all — the latter is defensive only, since a holding can only exist after a fill, which needs a valid open, and a session with a valid open for that name would have supplied a close.

**Risk.** `PortfolioPublicationRiskGate.gate` writes policy, optional state, decision and event evidence **before** deciding, then refuses publication unless the action is executable. Two action sets exist and are deliberately allowlist-shaped: `EXECUTABLE_ACTIONS = {allow, warn}` for portfolio publication and `ORDER_GATE_EXECUTABLE_ACTIONS = {allow, warn, resize}` for orders. A `resize` order is executed only as an allowed-shares constraint. Listing the executable set rather than the blocked set means `de_risk`, `flatten`, `halt_strategy`, `escalate_review`, or any action added to the enum later blocks the order instead of executing unexamined.

**Paper execution.** `PaperOrderPlanner` requires `sell_before_buy`, `enforce_t1` and no partial fills to be configured, refuses a config that contradicts them, produces an all-or-none plan, and `PaperExecutionEngine.execute` fills it against governed market data with an explicit reject taxonomy (`risk_blocked`, `suspended`, `limit_up`, `limit_down`, `insufficient_cash`, `t1_not_sellable`, `no_market_data`, `lot_size`, …). `PaperPortfolioStateBuilder` then produces the next state, reconciling buys, sellable quantities, average cost and cash.

## Multiple Implementations / Entry Points

### Version 3 raw execution replay

`BacktestEngine.run` dispatches v3 to `backtest/engine_v3.py`; v1/v2 retain
their existing prices, defaults, accounting and published schemas. Both config
and result have new `.v3.json` contracts. The quality-policy binding remains
`backtest_config_v1` / `backtest_result_v1`, with a fresh signed subject for
each actual v3 artifact. A v1 cash-only result still refuses undefined Sharpe;
v3 represents it as `null`, preserving the original contract.

The research adapter and `scripts/publish_backtest_config.py --contract-version 3`
read the canonical raw `bars_daily/v2` partition set through its owning reader,
including manifest, schema, quality-report and byte checks. Adjusted prices
remain model/label inputs; they are not v3 execution prices. The governed
calendar must match the complete raw window. A missing tradable open is a data
error; explicitly suspended holdings carry their last usable close.

`scripts/publish_backtest_execution_facts.py` archives an offline Parquet table
via `ExecutionFactsStore`. It requires source identity and a source-approval
reference; that reference alone is not independent approval. The external
config review binds the table's generation, manifest, byte and logical hashes.
No vendor or historical-status adapter is selected by this publisher.
Rows have unique date/instrument keys, timezone-aware visibility no later than
09:30 Shanghai, board, historical ST/suspension flags, explicit unlimited/limited
status, published upper/lower bands, limit reference, raw prior close, minimum
buy/increment/sell lots, and nullable industry. No historical rule is inferred
from today's status, an instrument prefix or a universal percentage. Per-order
evidence records the reference-price adjustment relative to raw prior close.
Raw opens outside the published band fail, and assumed execution prices after
slippage/impact must also remain within that band.

Execution is chronological: a T-close target uses the prior NAV and T+1 raw
open; sells precede buys, only day-start inventory is sellable, then the day is
marked at close. Explicit prior-session-volume participation and fill-rate
multipliers permit partial fills on either side. Cadence counts calendar
sessions from the first decision, and actual gross one-way turnover is bounded
as gross traded notional / (2 × prior NAV). Source lots and minimum buys apply
after all clipping; full odd-lot liquidation cannot bypass blocking guards.
Unfilled quantities and each clipping reason are retained.
An odd residual can be liquidated in full when raw participation and turnover
budgets cover every share; a smaller partial sale still follows the sell lot.
The v3 research adapter re-reads each signed target-weight manifest and payload
and compares them with the in-process handoff before using them. Its corporate
action exclusion covers the entire bound backtest window.

Costs include slippage on both sides, linear/square-root participation impact
with an explicit cap, minimum commission per filled order, and sell-only stamp
duty. Commission/stamp use executed notional and half-up cent rounding; zero
fills pay no fee. Buys are clipped using quantity-dependent actual payment.
Single-name, industry and cash checks use projected opening NAV after costs;
industry limits require visible classifications for every relevant holding.
Passive close-price moves can breach a budget; daily evidence and summary
count those breaches. There is no claim of forced liquidation or guaranteed
close-time risk compliance. Corporate-action names remain excluded until
position/cash events are modeled. These are assumed daily fills, not market
calibration or broker evidence.

On a buy, single-name and industry checks test every held position against the
projected NAV. A passive breach already present in the opening portfolio can
therefore block all buys for that session, including an otherwise unrelated
name. The daily `risk_budget_breached` field reports the resulting end-of-day
state; it is distinct from the buy-side rejection reason.

`scripts/evaluate_portfolio_exposures.py` reads the reviewed v3 result and raw
price/fact bindings, rebuilds actual shares from fills and reconciles their
close-marked value with the equity curve. It reports end-of-day single-name,
industry and cash concentration plus a prior-session turnover liquidity proxy;
missing classifications and prior liquidity remain explicit. This is a
read-only diagnostic, documented in
[training-evaluation.md](training-evaluation.md#actual-portfolio-exposures).

Cost/capital diagnostics and their CLI are documented in
[`training-evaluation.md`](training-evaluation.md#cost-and-capital-stress).
Tests exercise the actual research backtest adapter, signed stores and result
readbacks without an adjusted-price store. Source and result tampering fail
closed. See `tests/test_backtest_v3.py`, `test_backtest_execution_facts.py` and
`test_backtest_stress.py`.

The read-only `scripts/reconcile_paper_backtest.py` compares a reviewed paper
execution result with the next-session fills from a reviewed v3 backtest using
the exact same published target-weight identity. Its window mode requires one
paper result per backtest decision date, checks all published execution pairs,
and rejects omitted dates or extra backtest fills. It reads the paper config
and order plan as well as the result, and reports unmatched orders and quantity,
price and fee differences against explicit budgets. It does not change paper
state or backtest artifacts, and it does not infer real fills; see
[training-evaluation.md](training-evaluation.md#paperbacktest-fill-reconciliation).

- **Two risk decision scopes.** Portfolio metrics (`gross_exposure`, `single_name`, `top_n`, `industry`, `turnover`, `cash_reserve`) and order metrics (`notional`, `participation`, `insufficient_cash`, …). A policy must declare the scope the caller evaluates with, and a scope mismatch is rejected.
- **Two independent risk gates:** publication (`PortfolioPublicationRiskGate`, allow/warn only) and order submission (`RiskPolicyOrderSubmissionSource`, allow/warn/resize). A decision signed for one is not reusable for the other.
- **Research-chain paper stage.** `contract_version` 3 and 4 requests are refused by the runner until the paper runtime lands; v5–v8 execute with `paper_execution` before `result_reconciliation` (`src/uq/research_chain/runner.py` `_validate_plan`). v7 pins LightGBM model-definition template v2 and portfolio-definition template v2. v8 retains that model route, uses portfolio-definition template v3, and resolves the bound risk-return generation through its owning store. This is a fail-closed behaviour lock for v3/v4, not an oversight.
- **Independent-calendar opt-out.** `--skip-independent-calendar` records `independent_source: not_checked` with `cross_check_result: skipped`, never a fabricated `agreement` (`src/uq/execution/market_dataset_publisher.py:50`).

## State and Data Contracts

Risk families (`risk_policy`, `risk_event`/`risk_run`, `risk_state`, `risk_decision`, `risk_exception`) are immutable manifests under `$ROOT/<family>/generation=<id>/manifest.json`, plus one canonical serialization shared by every file artifact (`src/uq/risk/contracts.py:52`). Paper families (`execution_config`, `order_plan`, `execution_result`, `paper_portfolio_state`) follow the same pattern at `src/uq/execution/stores.py`.

A `ConfigPublicationBinding` is single-use: once it has authorized a config publication, a second call raises, so one risk decision cannot license two publications.

## Constraints, Failures, and Compatibility

- New post-hoc evaluation lives in
  [`training-evaluation.md`](training-evaluation.md). It compares two published
  backtests only when cost, execution, market and universe bindings match, then
  archives recomputable excess/IR/win-rate/drawdown diagnostics. It does not
  change the backtest-v1/v2 result summaries or grant publication/order approval.

- Everything here is deterministic and replay-oriented: no broker, no live stream, no network, no credentials. Fill prices are assumed prices, and results say nothing about real liquidity or slippage (`docs/layer-architecture.md` §3).
- A suspended instrument is skipped with its own status, never priced at 0. An earlier defect passed `execution_price=0.0` into the risk engine, which then refused the entire gated backtest.
- Fail-closed ordering matters: the risk gate is evaluated *after* the suspension short-circuit, so a suspended name cannot crash the risk path.
- Publishing a risk decision or config requires an approved, in-effect policy whose digest chain resolves; a test-mode Ed25519 key is rejected in `UQ_RUNTIME_MODE=production` (`src/uq/runtime.py:34`).

## Verification Evidence
- Portfolio/backtest: `tests/test_portfolio_layer.py` (including score-weighted v2), `tests/test_portfolio_backtest_contracts.py`, `tests/test_backtest_layer.py`; v7 model routing and v8 risk-parity routing: `tests/test_research_v7_lightgbm.py`, `tests/test_research_v8_risk_parity.py`, `tests/test_portfolio_risk_model.py`.
- Risk: `tests/test_risk_control_plane_phase0.py`–`phase4.py`.
- Paper execution: `tests/test_paper_execution_contracts.py`, `tests/test_paper_execution_phase1.py`–`phase5.py`, `tests/test_paper_execution_market_source.py`, `tests/test_publish_market_datasets.py`.
- Portfolio v2 score weighting: `tests/test_portfolio_layer.py`; portfolio v3 risk-parity solver and data-store routing: `tests/test_portfolio_risk_model.py` and `tests/test_research_v8_risk_parity.py`.
- Verification after this CR on the worktree based at HEAD `2d713260eef73d595bf75bb1bd06899a8929dad1`: `.venv/bin/python -m pytest` → 1500 passed / 33 skipped; `.venv-qlib/bin/python -m pytest` → 1532 passed / 1 skipped. Qlib and pandas emit existing dependency/deprecation warnings; no real risk-return or industry-history panel was available, so weights were verified on governed test fixtures only.
- Research-chain paper integration: `tests/test_paper_execution_research_integration_phase0.py`–`phase3.py`, `tests/test_paper_execution_research_stage_runner.py`, `tests/test_paper_execution_research_integration_evidence.py`, `tests/test_paper_execution_research_integration_release.py`.
- Golden identity vectors: `evidence/paper-execution/phase-0/identity-golden-vectors.json`, consumed by `tests/test_paper_execution_contracts.py`.

## Unknowns and Pending Confirmation

- `UNKNOWN`: whether any paper result has been reconciled against a real broker or market outcome. By design it has not; the layer is a deterministic replay and must not be read as execution evidence.
- `UNKNOWN`: production real-time execution risk. It is explicitly deferred until an execution layer exists (`specs/architecture.md` §11.7), and no date or owner is recorded in the repository.
