# Portfolio, Backtest, Risk Control Plane, and Paper Execution

> Current behavior reference. Last checked: `2026-10-20` against HEAD `54676a0a3b377d1b88da09309d96d6a7c6535419`. Based on source code, configuration, tests, and release evidence; unverified items are marked `UNKNOWN`.

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

**Portfolio.** `build` supports exactly one scheme, `top_n_equal_weight`; anything else is refused. Scores are NaN-excluded, restricted to the universe snapshot, ranked descending with instrument-ID tie-break, then equal-weighted under `max_single_weight`, `max_industry_weight`, `max_turnover` and `cash_reserve`. `max_turnover` requires the previous target weights unless `initial_state` is set, and the published manifest records `previous_target_weights_generation_id: null`, which is what makes the exemption auditable rather than silent.

**Backtest.** Per session: mark to market, then, on decision dates, compute target shares from T-close NAV at the next session's open with slippage and board-lot flooring, and execute. Side is classified from the **day-start** position and grouped sell → buy → hold: this is a settlement rule, not a comment — a single alphabetical pass once let an early-lettered buy consume cash a later sell had not yet freed and record `skipped_insufficient_cash` in a fundable run. A weight partition containing a corporate-action instrument is refused outright; such instruments must be removed at universe-filtering time.

**Valuing a held name across a suspension.** A per-session vendor panel carries only the rows the vendor returned, so a genuinely suspended name has *no row at all* — a `status`-only read cannot see it. The engine therefore keeps a `last_close_prices` map (distinct from `prev_close_prices`, which answers a different question: the next session's limit-test reference, cr.md FC-M7). A held name absent from the session is valued at its last traded close, which is what keeps the equity curve continuous while the execution branch records `skipped_suspended` for it (cr.md 8.64). Two fail-closed cases remain: the panel carries the name but its close is unusable, and the panel never carried it at all — the latter is defensive only, since a holding can only exist after a fill, which needs a valid open, and a session with a valid open for that name would have supplied a close.

**Risk.** `PortfolioPublicationRiskGate.gate` writes policy, optional state, decision and event evidence **before** deciding, then refuses publication unless the action is executable. Two action sets exist and are deliberately allowlist-shaped: `EXECUTABLE_ACTIONS = {allow, warn}` for portfolio publication and `ORDER_GATE_EXECUTABLE_ACTIONS = {allow, warn, resize}` for orders. A `resize` order is executed only as an allowed-shares constraint. Listing the executable set rather than the blocked set means `de_risk`, `flatten`, `halt_strategy`, `escalate_review`, or any action added to the enum later blocks the order instead of executing unexamined.

**Paper execution.** `PaperOrderPlanner` requires `sell_before_buy`, `enforce_t1` and no partial fills to be configured, refuses a config that contradicts them, produces an all-or-none plan, and `PaperExecutionEngine.execute` fills it against governed market data with an explicit reject taxonomy (`risk_blocked`, `suspended`, `limit_up`, `limit_down`, `insufficient_cash`, `t1_not_sellable`, `no_market_data`, `lot_size`, …). `PaperPortfolioStateBuilder` then produces the next state, reconciling buys, sellable quantities, average cost and cash.

## Multiple Implementations / Entry Points

- **Two risk decision scopes.** Portfolio metrics (`gross_exposure`, `single_name`, `top_n`, `industry`, `turnover`, `cash_reserve`) and order metrics (`notional`, `participation`, `insufficient_cash`, …). A policy must declare the scope the caller evaluates with, and a scope mismatch is rejected.
- **Two independent risk gates:** publication (`PortfolioPublicationRiskGate`, allow/warn only) and order submission (`RiskPolicyOrderSubmissionSource`, allow/warn/resize). A decision signed for one is not reusable for the other.
- **Research-chain paper stage.** `contract_version` 3 and 4 requests are refused by the runner until the paper runtime lands; v5 is the executable one that inserts `paper_execution` before `result_reconciliation` (`src/uq/research_chain/runner.py:334` `_validate_plan`). This is a fail-closed behaviour lock, not an oversight.
- **Independent-calendar opt-out.** `--skip-independent-calendar` records `independent_source: not_checked` with `cross_check_result: skipped`, never a fabricated `agreement` (`src/uq/execution/market_dataset_publisher.py:50`).

## State and Data Contracts

Risk families (`risk_policy`, `risk_event`/`risk_run`, `risk_state`, `risk_decision`, `risk_exception`) are immutable manifests under `$ROOT/<family>/generation=<id>/manifest.json`, plus one canonical serialization shared by every file artifact (`src/uq/risk/contracts.py:52`). Paper families (`execution_config`, `order_plan`, `execution_result`, `paper_portfolio_state`) follow the same pattern at `src/uq/execution/stores.py`.

A `ConfigPublicationBinding` is single-use: once it has authorized a config publication, a second call raises, so one risk decision cannot license two publications.

## Constraints, Failures, and Compatibility

- Everything here is deterministic and replay-oriented: no broker, no live stream, no network, no credentials. Fill prices are assumed prices, and results say nothing about real liquidity or slippage (`docs/layer-architecture.md` §3).
- A suspended instrument is skipped with its own status, never priced at 0. An earlier defect passed `execution_price=0.0` into the risk engine, which then refused the entire gated backtest.
- Fail-closed ordering matters: the risk gate is evaluated *after* the suspension short-circuit, so a suspended name cannot crash the risk path.
- Publishing a risk decision or config requires an approved, in-effect policy whose digest chain resolves; a test-mode Ed25519 key is rejected in `UQ_RUNTIME_MODE=production` (`src/uq/runtime.py:34`).

## Verification Evidence

- `.venv/bin/python -m pytest` → `1185 passed, 25 skipped` on HEAD `54676a0`; `.venv-qlib/bin/python -m pytest` → `1210 passed, 0 skipped`.
- Portfolio/backtest: `tests/test_portfolio_layer.py`, `tests/test_portfolio_backtest_contracts.py`, `tests/test_backtest_layer.py`.
- Risk: `tests/test_risk_control_plane_phase0.py`–`phase4.py`.
- Paper execution: `tests/test_paper_execution_contracts.py`, `tests/test_paper_execution_phase1.py`–`phase5.py`, `tests/test_paper_execution_market_source.py`, `tests/test_publish_market_datasets.py`.
- Research-chain paper integration: `tests/test_paper_execution_research_integration_phase0.py`–`phase3.py`, `tests/test_paper_execution_research_stage_runner.py`, `tests/test_paper_execution_research_integration_evidence.py`, `tests/test_paper_execution_research_integration_release.py`.
- Golden identity vectors: `evidence/paper-execution/phase-0/identity-golden-vectors.json`, consumed by `tests/test_paper_execution_contracts.py`.

## Unknowns and Pending Confirmation

- `UNKNOWN`: whether any paper result has been reconciled against a real broker or market outcome. By design it has not; the layer is a deterministic replay and must not be read as execution evidence.
- `UNKNOWN`: production real-time execution risk. It is explicitly deferred until an execution layer exists (`specs/architecture.md` §11.7), and no date or owner is recorded in the repository.
