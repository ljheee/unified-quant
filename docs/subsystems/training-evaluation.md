# Training Evaluation Diagnostics

This subsystem adds recomputable research diagnostics and a fixed walk-forward
protocol for the trading-grade training plan. Its metric readers consume
published backtest results, predictions, labels and dataset manifests. The
walk-forward adapter delegates fitting to the existing research runner; it never
implements a second estimator or signs quality decisions. Its
`research_evaluation.v1` report is accepted by no model-publication, promotion or
order gate. This is additional evidence, not a production approval.

## Entry Points and Contracts

- `scripts/evaluate_training.py`: CLI over the owning read-side stores.
- `src/uq/evaluation/report.py`: `paired_backtest_inputs`,
  `published_ic_inputs`, `EvaluationReportStore`.
- `scripts/compare_training_models.py`, `evaluation/model_comparison.py`:
  compare one passed `qlib_linear` run with one passed `qlib_lightgbm` run only
  after verifying the same owning dataset bytes, features, labels, split,
  portfolio template, test window and reviewed v3 backtest economics.
- `src/uq/evaluation/metrics.py`: pure return and cross-sectional IC calculations.
- `src/uq/evaluation/benchmark.py`: point-in-time equal-weight,
  market-cap-weighted, and industry-neutral benchmark target weights;
  `BenchmarkWeightStore` publishes immutable panels bound to PIT membership,
  exposure, config and strategy rebalance-schedule inputs.
- `scripts/run_benchmark_backtest.py`, `evaluation/benchmark_backtest.py`:
  publish a benchmark backtest on a reviewed strategy result's v3 config and
  rebalance dates (including a continuous walk-forward result), re-reading raw
  prices, calendar, execution facts and market guards through their owning stores.
- `config/schemas/contracts/research_evaluation.v1.json`: new diagnostic
  contract. Existing backtest, dataset and prediction contracts are unchanged.
- `scripts/run_walk_forward.py`, `walk_forward.py`, `walk_forward_runner.py`:
  session-based rolling/expanding plans and execution of pre-reviewed requests.
- `scripts/run_walk_forward_continuous_backtest.py`,
  `evaluation/walk_forward_continuous.py`: re-read verified OOS targets and replay
  them through one shared-capital v3 backtest with an immutable evidence receipt.
- `scripts/run_backtest_stress.py`, `evaluation/stress.py`: externally reviewed
  v3 cost/capital replays and recomputable `backtest_stress.v1` diagnostics.
- `scripts/reconcile_paper_backtest.py`, `evaluation/paper_reconciliation.py`:
  one-session or complete-window paper/v3 fill differences under an explicit
  five-part budget.
- `scripts/evaluate_portfolio_exposures.py`, `evaluation/exposures.py`:
  read-only actual-holdings concentration, industry and prior-turnover
  diagnostics for a reviewed v3 backtest.
- `scripts/screen_factors.py`, `evaluation/factor_screening.py`, and
  `factor_screen_report.v1.json`: read-only per-factor daily IC/RankIC stability,
  daily cross-sectional Spearman collinearity, and top-N membership turnover
  proxy over explicitly bound factor, label and calendar generations; an
  optional reviewed v3 config adds a bounded fee/slippage/impact cost proxy.
- `scripts/build_tushare_factor_candidates.py`,
  `models/tushare_factor_candidates.py`, and
  `tushare_factor_candidate.v1.json`: immutable diagnostic candidates derived
  from exact-date `daily_basic` and `moneyflow` snapshots, with canonical
  calendar and raw snapshot lineage.
- `scripts/screen_tushare_factor_candidates.py` and
  `evaluation/tushare_factor_screening.py`: recompute the existing IC/RankIC,
  HAC, collinearity and turnover diagnostics over one candidate bundle and a
  published label generation. A reviewed v3 cost config is optional for the
  report and required by the starter selection policy.
- `scripts/select_factor_candidates.py`,
  `evaluation/factor_screening.py::select_factor_candidates`, and
  `factor_selection_shortlist.v1.json`: apply a JSON-configured validation-only
  filter, direction adjustment, collinearity de-duplication and candidate cap.
  It does not modify reviewed factor definitions or authorize model use.
- `scripts/audit_label_design.py`, `evaluation/label_audit.py`: read-only
  comparison of a published close label with the next-open entry and later
  open exit on the same raw bars and an independently bound governed calendar;
  the baseline audit is documented in
  [`trading-grade-label-audit.md`](../trading-grade-label-audit.md).
- `scripts/archive_trading_experiment.py`, `evaluation/experiment_archive.py`,
  `trading_experiment_archive.v1.json`: immutable experiment names and evidence
  index over a passed research run, source-bound telemetry, an environment
  receipt and a recomputable paired evaluation.

## Label Design Audit

`audit_label_design.py` requires the independent `BacktestMarketDatasetStore`
calendar generation and byte checksum in addition to raw bars and the published
label. Readback verifies the calendar manifest digest, data checksum and content
generation; raw partition dates in the requested window must exactly equal the
governed sessions. For every non-null label, all instrument bars from decision
session through the fifth governed future session must exist. Label and execution
legs are then aligned by calendar offsets (`T+1`, `T+5`, `T+6`), not by shifting
whatever rows happened to survive for each instrument. This rejects both a
missing whole-market partition and a security-specific hole that would stretch
the nominal five-session horizon.

Get `calendar_generation_id` and `data_checksum_sha256` from the
`publish_backtest_market_data.py` report's `bindings.calendar_binding`, then run:

```sh
.venv/bin/python scripts/audit_label_design.py \
  --bars-root <market-data-root> --label-root <label-store> \
  --label-generation <label-generation> --start <YYYY-MM-DD> --end <YYYY-MM-DD> \
  --calendar-generation-id <calendar-generation> \
  --calendar-checksum <calendar-data-checksum>
```

The report is a version 2 read-only diagnostic. The historical v1 baseline
evidence remains unchanged; this strengthened auditor does not retroactively
certify its old window.

## Experiment Archive

`archive_trading_experiment.py` accepts an existing research result, costed
paired evaluation, passed attempt telemetry, saved `research_environment.py`
receipt, and explicit limitation statements. It creates the name
`<universe>-<oos-start>-<oos-end>-<protocol>-<model>-<variant>` from six validated
components. The output is `<archive-root>/experiments/<name>/manifest.json`.
An identical retry is idempotent after readback; the same name with different
evidence is refused. A source change prevents readback instead of silently
retaining a stale conclusion.

Publication and every read check the request/result owning ledgers, require
matching nonzero source/lock digests and a passed attempt with the same saved
environment, then read the reviewed strategy backtest/config. The evaluation
report recomputes metrics from its archived tables; the index separately
re-runs the paired strategy/benchmark source comparison and requires exact daily
rows and economic bindings. OOS endpoints must equal the evaluation endpoints
and fit the request window; when a test split is declared they must fit it too.
External review checksums from the research result and both paired backtest
results/configs are retained as references; the paired reviews are verified by
their owning readers. This is
diagnostic evidence and never a new quality decision.

The source environment receipt commits to its historical source-file inventory,
but the archive does not reconstruct those old file bytes. It also does not
re-read prediction/label sources used by an IC evaluation, establish a year of
OOS coverage, prove PIT universe completeness, or copy every external review
payload. Those limits are encoded in `verification_scope`. The archive itself
has a self-checksum and immutable publisher path; it is not externally signed,
so a party with direct write access can alter a limitation and recompute the
self-checksum. It is therefore not a production approval or tamper-proof vault.
The current checksum also covers the complete telemetry record and absolute
source locations, including wall-clock times and data-root paths. The maintainer
confirmed this design: the archive identifies one run's evidence, so equivalent
experiments from another run or machine have different checksums and cannot
replace an existing same-name archive. Stable cross-run experiment identity
would require a separate versioned design.

## Linear and LightGBM Comparison

`compare_training_models.py` reads both passed research results and their saved
requests. The baseline must use `qlib_linear`, and the candidate must use
`qlib_lightgbm`. Both must resolve to dataset manifests whose feature order,
factor and label lineage, PIT universe, preprocessing, split, row count, logical
fingerprint and data checksum match. Each dataset is also read through
`DatasetWriter` so the comparison verifies its Parquet payload. The requested
date window must lie inside the shared test split. Each result's backtest must
match its request's reviewed config; `paired_backtest_inputs` then checks cost,
execution, calendar, prices, universe, corporate actions, suspensions, facts,
capital and risk constraints before producing daily candidate-minus-baseline
returns.

```sh
.venv/bin/python scripts/compare_training_models.py \
  --data-root <research-store> \
  --baseline-result-generation <linear-result> \
  --baseline-result-digest <linear-result-digest> \
  --candidate-result-generation <lightgbm-result> \
  --candidate-result-digest <lightgbm-result-digest> \
  --start <test-start> --end <test-end> --output-root <evaluation-store>
```

An optional `--regimes-json` maps every evaluated session to `up`, `down`, or
`sideways`; it is checked for exact date coverage by the return evaluator. The
published report's `strategy` side is the LightGBM candidate and its
`benchmark` side is the linear baseline, so excess and information-ratio fields
are candidate-minus-baseline. `one_year_oos_met` is only a duration check. The
report neither proves three-state coverage when regimes were omitted nor
authorizes model promotion. The LightGBM selector still caps candidates within
one call; a program-wide multiple-comparison budget remains unspecified.

## Benchmark Weight Construction

`build_benchmark_weight_partitions` accepts one row per `(date, instrument)` in
the already resolved point-in-time membership set. Equal weight divides each
session's budget across its members; market-cap weighting normalizes the
provided positive finite capitalization values; industry-neutral weighting
gives each represented industry an equal budget, then divides that budget
equally among its members. Membership may change by session. Missing point-in-time
capitalization or industry for any member, duplicate keys, malformed dates, or
invalid weights fail closed.

`read_pit_benchmark_membership` builds that membership panel from the governed
calendar window and `IndexMembershipStore.as_of`, verifies the complete snapshot
set before returning rows, and retains snapshot generations, manifest digests,
and member checksums. The store declares `monthly_approximation` precision; the
reader preserves and does not strengthen that limitation. `BenchmarkWeightStore`
adds immutable content/readback identity for the derived panel. The benchmark
publisher reuses the strategy's approved v3 or v4 config and rebalance dates,
requires the benchmark constituents to fit the same configured universe, applies
the same period-wide corporate-action exclusions for v3; v4 applies the reviewed
dividend event ledger and excludes only rights-issue instruments. It executes
through `BacktestEngine` before publishing a reviewed result of the matching
version. For a continuous
walk-forward result, `--walk-forward-root` verifies the matching receipt and
replays its terminal-liquidation attempt under the same engine guard.

Example for an already published strategy result and reviewed execution inputs:

```sh
.venv/bin/python scripts/run_benchmark_backtest.py \
  --data-root <strategy-store> --benchmark-root <benchmark-store> \
  --membership-root <pit-membership-store> \
  --strategy-result-generation <reviewed-v3-result-generation> \
  --scheme equal_weight --quality-decisions-json <review-decisions.json>
```

For a continuous walk-forward result, also pass
`--walk-forward-root <continuous-receipt-store>`. Market-cap and industry-neutral schemes additionally
require `--exposure-root` and `--exposure-generation` for a reviewed PIT exposure
panel. The command publishes a weight panel and a separate reviewed
`backtest_result.v3`; neither publication establishes historical source completeness.

The returned `{decision_date: [instrument, weight]}` frames can be passed to
`BacktestEngine.run`; the CLI performs that replay under the same reviewed config,
costs, price/calendar bindings, and execution rules as the strategy result it
names. The publisher does not fetch or approve sources: it requires owning PIT
membership and exposure readers. It records the membership source's monthly
precision and does not certify historical completeness. Market-cap/industry
publication additionally requires a reviewed PIT exposure panel; the newly added
factor-exposure review family remains unavailable until its trust-anchor policy
is approved.

Archive example (all inputs must already be published):

```sh
.venv/bin/python scripts/archive_trading_experiment.py \
  --data-root <strategy-store> --benchmark-root <benchmark-store> \
  --evaluation-root <diagnostic-store> --archive-root <archive-store> \
  --result-generation <research-result-id> --result-digest <research-result-digest> \
  --evaluation-generation <evaluation-id> --telemetry-path <attempt.json> \
  --environment-json <environment.json> --limitations-json <limitations.json> \
  --universe all-a --oos-start 2025-01-02 --oos-end 2025-12-31 \
  --protocol single --model qlib_linear --variant baseline
```

`--model` must equal the reviewed request's algorithm. `--limitations-json`
contains a nonempty JSON array of statements. Exit status is 0 for `archived`
and 2 for `archive_rejected`.

## Evaluation Report

Example (all inputs must already be published):

```sh
.venv/bin/python scripts/evaluate_training.py \
  --strategy-root <strategy-store> --strategy-result <generation> \
  --benchmark-root <benchmark-store> --benchmark-result <generation> \
  --start 2025-01-02 --end 2025-12-31 --output-root <diagnostic-store>
```

The start and end are mandatory session dates, not inferred from a result's
valuation window. Optional `--regimes-json` is an exact session-to-state mapping
(`up`, `down`, `sideways`); missing/extra dates or unknown states fail closed. No
market regime is inferred from future benchmark returns. A partial set of the
three states is reported as partial, not as coverage of all market conditions.

For IC also supply all four options: `--prediction-root`,
`--prediction-bindings-json`, `--label-root`, `--label-generation`. Prediction
bindings are a JSON array of `{generation_id, decision_date}`. The CLI loads every
prediction through `PredictionBuilder.read`, verifies its unique reviewed input
dataset manifest, checks that the label generation matches the dataset, and
restricts prediction dates to the dataset's declared test split. Each dataset's
manifest digest and split policy are retained. The evaluator checks this
**declared** inference-dataset lineage; it does not re-train the artifact or prove
that the earlier fitting process respected those splits. Dataset feature
payloads are not reloaded and are not claimed reverified. Lower-better scores
are negated with the original score semantics recorded. Labels come through
`LabelStore.read_frame`, which verifies their row checksum.

Exit status is 0 for `diagnostic_published`, 2 for `evaluation_rejected`. A report
with insufficient IC observations may still publish an explicit undefined
statistic; publication is not proof of a passing trading experiment.

## Actual portfolio exposures

`evaluate_portfolio_exposures.py` reads a reviewed v3 backtest/config and its
checksum-verified fills/equity, raw `bars_daily/v2` partition set and bound
pre-open execution facts. It reconstructs shares from actual fills, carries the
last close over a missing suspended bar, and requires daily marked positions to
equal the reviewed equity curve's holdings value. It rejects an oversell,
unusable held close, extra session or accounting mismatch.

For each session the diagnostic reports maximum single-name weight, squared
weight concentration (HHI), cash weight, industry weights and maximum industry
weight. Classifications come from that session's bound pre-open execution
facts; any null industry contributes to `unclassified_weight`. The liquidity
proxy is the largest position value divided by the prior session's raw close
times volume. A missing or unusable previous bar contributes to
`unknown_liquidity_weight`; it is never filled with a later volume. These are
actual end-of-day holdings, not target-only allocations. Market-cap exposure
remains explicitly unavailable until a governed historical market-cap source
exists. Industry facts themselves still need historical-source approval and
the report is diagnostic, not an exposure-limit approval.
For valuation, an unusable or absent current close may carry forward the prior
close only when the bound execution facts mark that instrument suspended; an
unusable close for a tradable holding is rejected. This matches the v3 engine's
marking rule and still requires reconstructed holdings value to reconcile.
The scan retains price and industry lookups only for instruments referenced by
the reviewed fill table, while still checking full session coverage.

```sh
.venv/bin/python scripts/evaluate_portfolio_exposures.py \
  --backtest-root <reviewed-v3-store> --backtest-generation <result-id>
```

## Paper/backtest fill reconciliation

`reconcile_paper_backtest.py` reads the reviewed v3 backtest result/config, its
published target weights and calendar, and the reviewed paper config, order plan
and execution result. The paper decision date must have exactly one matching
backtest target-weight generation/digest and its execution date must be the next
session in the backtest calendar. A target-weight store at another root can be
supplied with `--target-root`. Any mismatched paper result/plan/config binding,
missing or tampered input, duplicate order key or malformed quantity/price/fee
is rejected.

The mandatory `--budget-json` object has `maximum_unmatched_orders`,
`maximum_requested_quantity_gap`, `maximum_filled_quantity_gap`,
`maximum_execution_price_gap_bps` and `maximum_fee_gap_cny`, all nonnegative.
The comparison keys each order by instrument and side. Requested and filled
quantity gaps and fees are summed; price gap is the maximum absolute bps
difference between filled paper price and filled backtest net execution price.
An order missing on one side is counted even when both fill quantities are zero.
The CLI prints `diagnostic_ready` with `within_declared_budget`,
`exceeds_declared_budget` or `no_orders_to_compare` for an empty day; it returns
status 2 only when inputs are invalid.
This is a transient diagnostic with source bindings, not a published quality
decision or a live-fill attestation. The two engines use different fill and
cost assumptions; a budget pass alone does not establish matching economics.

For a complete backtest window, pass `--paper-generations-json` instead of
`--paper-generation`. This file is a JSON array of paper result IDs; order does
not matter. The comparator requires one unique paper result for every published
backtest target-weight decision date, verifies each T+1 session and refuses
backtest fills outside those decision/execution pairs. It reads a v3 result
and calendar once, groups fills by decision/execution pair, then reports each
day plus aggregate unmatched orders, share gaps, maximum price gap and total
fee gap. The same five thresholds apply to the **aggregate**. It lists days
without orders and returns `no_orders_to_compare` if the whole window has no
orders. Coverage is over the published backtest decisions; it does not prove
the historical PIT universe or that a year of OOS data exists.

Example:

```sh
.venv/bin/python scripts/reconcile_paper_backtest.py \
  --backtest-root <backtest-store> --backtest-generation <v3-result-id> \
  --paper-root <paper-store> --paper-generation <paper-result-id> \
  --budget-json <five-thresholds.json>
```

Window example: replace `--paper-generation <paper-result-id>` with
`--paper-generations-json <paper-result-ids.json>`.

## Comparable Backtest Inputs

Both results are read through `BacktestResultStore` with artifact checksum checks.
The evaluator also verifies manifest generation/digest, signed result and config
reviews, and result/config period and price-source lineage. Comparison refuses a
difference in contract/schema version, window, initial capital, cost model,
execution model, limit rules, calendar, price source, universe snapshot,
corporate actions, suspension, v3 execution-facts or risk bindings. V3 facts
are read and verified through their owning store. Calendars must match exactly;
evaluation never silently uses an inner join. A benchmark must therefore be an
executed, costed portfolio, not a frictionless index-return column.

This verifies shared input bindings, not historical completeness of those inputs.
The report explicitly leaves PIT completeness and complete OOS-process
attestation unverified. Equal-weight, market-cap and industry-neutral benchmark
construction and historical onboarding remain separate work in the plan.

## Metric Semantics

Return inputs are daily **net** strategy and benchmark returns from the published
artifacts. Annualization uses 252 sessions. The report includes:

- strategy/benchmark compounded and annualized returns;
- annualized excess as strategy CAGR minus benchmark CAGR, and a separately named
  relative-wealth annualized return;
- information ratio from arithmetic daily active returns with sample standard
  deviation, plus annualized tracking error;
- full-period win rate and holding-interval win rate separately; a holding
  interval has current/prior valuation inventory or a filled trade, so a final
  liquidation/cost day counts even when ending in cash;
- mean/total executed turnover;
- maximum drawdown from initial capital before the first return, the associated
  peak/trough/recovery dates, recovery in sessions and longest underwater period;
- yearly and supplied-regime breakdowns. Each group compounds its selected
  observations and resets the drawdown origin to one; noncontiguous regime
  groups are diagnostics, not separately executable strategies.

Zero holding days, single observations and zero active-return variance produce
`null` for undefined rates/ratios. Non-finite data, returns at/below -100%,
negative turnover, duplicate dates and arithmetic overflow are rejected rather
than written as NaN or Infinity. These are new evaluation semantics and do not
rewrite the historical backtest-v1/v2 summaries.

IC is the daily cross-sectional Pearson correlation; RankIC uses average ranks
for ties. There must be at least three complete pairs and nonconstant scores and
labels per session. Missing label keys, null labels, empty forecast dates and
constant cross-sections are counted explicitly. The summary treats sessions,
not instrument rows, as its sampling units.

Mean IC uncertainty uses Bartlett Newey-West HAC without a finite-sample
multiplier. For centered session scores `u`, the sandwich numerator is
`sum(u²) + 2 sum_l (1-l/(L+1)) sum_t u_t u_(t-l)` and the mean variance divides
by the square of valid-session count. `L` must be at least label horizon minus
one; `--hac-lags` can increase it. Missing cross-sections keep their trading
calendar positions with zero estimating equations, rather than turning distant
observations into adjacent ones. Insufficient observations and zero variance
are explicitly reported; no normal p-value is asserted. The effective-session
diagnostic is capped at valid-session count. The equally spaced time-series and
Bartlett-kernel basis is documented by the
[statsmodels HAC reference](https://www.statsmodels.org/v0.14.3/generated/statsmodels.stats.sandwich_covariance.cov_hac.html).
No statsmodels dependency was added; hand-calculated covariance and missing-day
fixtures exercise the implemented formula.
`evaluate_returns` reports return, drawdown, active-return and turnover metrics;
it does not provide Sharpe or annualized volatility, which remain in the engine
summary. The public HAC helper accepts numeric values or `None` and returns a
typed contract error for malformed input.

## Persistence and Failure Behavior

Layout: `<output-root>/research_evaluations/generation=<sha256>/manifest.json`,
with `daily.parquet` and, when IC is requested, `predictions.parquet` and
`labels.parquet`. The manifest binds the protocol, original input bindings,
artifact checksums/shapes, all metrics and the diagnostic-only publication scope.
All inputs needed to recompute the reported metrics are archived.

Publication takes an advisory lock, stages and re-reads artifacts, recomputes
metrics, fsyncs and renames. Identical publication is idempotent only after the
existing report verifies; changed or tampered reports are never overwritten.
Interrupted writes clean up staging directories. On every read the store checks
schema, content identity, exact artifact set and filenames, checksums and shapes,
then recomputes all metrics. Even a forged metric with a recomputed manifest
identity is rejected if it disagrees with archived tables. The common in-store
symlink guard protects report paths, artifacts and lock; a symlinked ancestor or
store-root alias is supported.

## Verification and Remaining Scope

The factor screening CLI reads one published factor partition per trading
session, a published label generation and a whole-window published calendar.
It checks the calendar checksum/content generation and consistent factor
schemas, then binds partition, label, calendar, source and lock provenance into
`factor_screen_report.v1`. Each feature gets daily Pearson IC and RankIC,
positive RankIC frequency, and a HAC t-statistic that enforces at least the
label horizon as its overlap lag. Pairwise collinearity is the mean absolute
session-level Spearman correlation over joint observations. The turnover value
is a half-L1 top-N equal-weight membership change proxy; absent dates break the
transition. When supplied, a reviewed v3 config is read back with its signed
quality receipt and identity verified. The bps estimate applies proportional
commission to both sides, stamp duty to sells, slippage to both sides, and the
configured impact at the maximum participation allowed by the config. It
deliberately omits minimum commission, actual fills, turnover/position limits,
prices, board lots and capacity; it is a rough comparison proxy, not modeled
trading cost. Without that config, cost fields remain null. The contract says
`diagnostic_only_no_factor_selection` and
the publisher never overwrites an existing report. These screens do not infer
factor direction or auto-select/drop factors, establish incremental value, or
replace point-in-time data and out-of-sample testing.

### Tushare Candidate Factors and Validation Shortlist

`build_tushare_factor_candidates.py` consumes only immutable exact-date
`daily_basic` and `moneyflow` responses plus the canonical `bars_daily/v2`
session partitions. For decision session T, it uses the previous governed
session's provider rows and records availability at T's 15:00 Shanghai close.
The manifest pins every raw generation/checksum/fetch timestamp and every
calendar generation/checksum, and the reader can re-verify those bindings
against the owning stores. The derived factors are earnings yield, book-to-price,
sales yield, TTM dividend yield, free-float turnover, volume ratio, net money
flow/market-cap, and large-order net flow/market-cap. The companion exposure
panel carries prior-session total market value and free-float turnover rate.
Provider units and each formula are included in the manifest.

These bundles always declare `strict_pit_eligible=false` and
`historical_vintage_verified=false`. The local historical snapshots were fetched
after the represented sessions; exact-date keys and a one-session lag do not
prove that the provider preserved the original values or that each historical
revision was visible then. The visibility mapping is a conservative research
assumption. Tushare documents `daily_basic` as updating between 15:00 and 17:00
and labels `dv_ttm` in percent and `total_mv` in 10k CNY; `moneyflow` reports
amounts in 10k CNY. See the [daily indicators endpoint](https://tushare.pro/document/2?doc_id=32)
and [money-flow endpoint](https://tushare.pro/document/2?doc_id=170). Historical
vintage verification and source approval are still required before any bundle
can be promoted to a production PIT panel.

The market-cap/liquidity Parquet is shaped for the existing
`publish_factor_exposure_panel.py` entry point. Promotion still requires a
maintainer-approved source reference and a passed external quality decision;
the source approval reference should cite the candidate generation. The existing
Research Chain then reads the reviewed `factor_exposure_panel.v1` through its
`feature_preprocessing.v2` binding and recomputes neutralization. Candidate
factor values remain in the diagnostic path until a reviewed factor definition
and a factor owning-store publication are available.

`screen_tushare_factor_candidates.py` binds the candidate bundle, published
labels, canonical sessions, source lineage and environment. It can also bind a
reviewed v3 backtest config for the same bounded turnover cost proxy as the
standard factor screener. Run it on the validation window only, then pass its
content-addressed report and a policy JSON to `select_factor_candidates.py`.
The starter policy at `config/factor-selection/tushare-candidate-policy.v1.json`
requires cost evidence, at least 60 valid RankIC sessions, 60 pairwise sessions,
minimum coverage and positive directional RankIC, caps the shortlist at three,
and de-duplicates by mean absolute daily Spearman correlation. If a configured
cost ceiling is present but no reviewed cost config was bound, the factor is
rejected for missing cost evidence. Direction values are research hypotheses
that must be fixed before inspecting the validation labels. The shortlist is
not a multiple-testing correction, does not prove incremental value, and does
not replace the held-out OOS comparison with existing factors.

`tests/test_tushare_factor_candidates.py` covers field/unit formulas, strict
previous-session joins, exposure availability normalization, missing-source
refusal, raw/calendar lineage re-verification, candidate screen bindings,
direction-adjusted selection, collinearity de-duplication and insufficient
correlation-history refusal. On the current uncommitted worktree based on HEAD
`cd1befb4b5fcbe954e16aea148cc2332dbc1dc21`, the ordinary full suite passed
(`1593 passed, 33 skipped`) and the Qlib full suite passed
(`1625 passed, 1 skipped`). These test fixtures verify the mechanisms, not
provider historical vintages or strategy value.

`tests/test_factor_screening.py` covers direct/inverse IC, collinearity,
membership turnover, reviewed-config cost calculation, missing-calendar
transitions, invalid inputs, and a published-generation integration with report
schema/provenance validation.
On the worktree at HEAD `2d713260eef73d595bf75bb1bd06899a8929dad1`, the full
suite passed with `.venv/bin/python -m pytest` (`1474 passed, 33 skipped`) and
`.venv-qlib/bin/python -m pytest` (`1506 passed, 1 skipped`).

`tests/test_evaluation_metrics.py` covers hand-calculated excess/IR/win rates,
initial-capital drawdowns and recovery, yearly/regime grouping, HAC overlap and
missing-day spacing, IC ties/coverage and invalid inputs.
`tests/test_evaluation_report.py` exercises real reviewed backtest stores and CLI,
published prediction/label/dataset-manifest inputs, mismatch refusal, altered
reviews/configs/artifacts, forged-but-rehashed metrics, interrupted publication,
idempotence and root-alias/internal-symlink boundaries. An isolated in-memory
mutation disables comparability and overlap-lag guards; all five corresponding
regressions then fail.

`tests/test_evaluation_benchmark.py` covers changing daily membership, all three
weighting rules, normalization, and refusal of missing/invalid point-in-time
exposures. `tests/test_factor_neutralization.py` covers row-aligned same-session
OLS residualization, null preservation, zero liquidity, unsupported controls,
rank deficiency, malformed exposures, PIT exposure store publication/readback,
preprocessing-v2 dataset replay and tamper rejection. The active quality registry
registers both passed-only review families, and the active trust anchor pins the
registry digest. Reviewed publication is enabled when approved source data and
quality decisions are supplied; no real PIT panel is available. The 2026-10-31 worktree ran the four
related suites (`test_evaluation_benchmark`, `test_factor_neutralization`,
`test_factor_screening`, `test_experiment_archive`) with `43 passed`; the
continuous replay related suites had `69 passed, 1 skipped`. After adding the
PIT membership reader, its benchmark/index-membership suites passed `37` tests,
and the benchmark-stage full suites passed with `.venv/bin/python -m pytest`
(`1541 passed, 33 skipped`) and `.venv-qlib/bin/python -m pytest`
(`1573 passed, 1 skipped`) at HEAD `cd1befb4b5fcbe954e16aea148cc2332dbc1dc21`. Following
trust-anchor activation and review fixes, final sequential suites on the same HEAD
passed with `1563 passed, 33 skipped` and `1595 passed, 1 skipped`, respectively.

The benchmark path now has immutable panel publication and a reviewed same-config
v3/v4 backtest CLI. Synthetic tests verify the panel and runner handoff; no real
annual benchmark has been published because the required historical PIT sources
are absent. Factor residualization now has an active preprocessing-v2 review
policy and dataset replay. These paths do not yet supply
one-year historical OOS evidence, a historical delisting/universe audit,
governed market-cap exposure, calibrated cost assumptions or real execution
evidence. The trading-grade objective remains open until those and the other
plan stages are implemented and verified.

## Walk-forward Protocol and Execution

`WalkForwardSpec` declares train/validation/test session counts, rolling or
expanding training, label horizon, purge, embargo, warmup and whether a partial
last test fold is explicitly allowed. The planner reads a published whole-window
calendar through `BacktestMarketDatasetStore`, verifies checksum, manifest digest
and content generation, and preserves its store locator and full session list.
It does not infer trading dates from weekdays or surviving feature rows.

Warmup is at least 60 sessions before each training start. Purge must be at least
the label horizon, with embargo added between train/validation and
validation/test. Test folds are disjoint and contiguous in the supplied calendar.
The planner reserves horizon sessions after each test end for observing labels;
the request window must run from train start through this observation tail.
Unassigned short tail sessions are named, or become an explicitly partial fold.
Each plan is regenerated from its spec/calendar on read, so rehashing an edited
fold, qualification flag or overlap does not make it valid.

The execution coverage screen requires both 240 OOS sessions and 365 inclusive
calendar days. It is a conservative protocol screen, not proof of complete
historical data or all market states. Short diagnostic execution requires
`--allow-short-diagnostic`; the plan and attempt retain failed one-year coverage
and never certify the trading strategy. Calendar budgeting alone also does not
prove that raw bars/factors actually contain a sufficient warmup history.
Plans include the absolute published-calendar store root in their identity;
moving the store or changing its path spelling requires a newly published plan.

Planning example (the spec JSON can contain
`{"train_sessions":252,"validation_sessions":63,"test_sessions":21}`):

```sh
.venv/bin/python scripts/run_walk_forward.py --mode plan \
  --calendar-root <market-store> --calendar-generation <id> \
  --calendar-checksum <sha256> --spec-json <spec.json> --output-root <evidence-store>
```

Execution uses `--mode execute --project-root . --plan-generation <id>
--jobs-json <jobs.json> --output-root <evidence-store>`. Jobs, in fold order, name
`request_json`, `data_root`, `quality_decisions_json`, and optionally
`decision_provider_json`, `membership_root`, `market_dataset_root`,
`paper_risk_documents_json`. Paths use the command's current working directory.
Unknown job fields, including external scores, are rejected. All requests and
their upstream data must be prepared/published/reviewed through existing owning
interfaces; the batch runner neither mutates a published request nor invents
data bindings or reviews. This command does not automatically publish the
fold-specific factor/label/preprocessing inputs or synthesize reviewed requests.

Before the first fold, the batch validates every request identity/stage plan,
exact split/window, fixed model/portfolio/missing-data/environment settings,
actual source/lock bindings, label horizon/digest, reviewed backtest config,
per-fold calendar and shared cost/execution/capital settings. Reviewer-document
shape and required paired roots/paper-risk document structure are checked too.
Full factor/preprocessing resolution remains with each owning research run.
`ResearchFoldExecutor` invokes the accepted `_execute` entry without external
scores, so inference comes from that fold's trained artifact.
For v3 configs the preflight also reads every bound raw canonical partition and
the execution-facts table and checks their complete fold calendar before any
fitting starts. Risk constraints must remain fixed across folds.
The shared request-plan check is a static validator, so batch preflight does
not initialize or exercise the rest of the single-runner object.
The current adapter calls the runner's static `_validate_plan` with a
request-only `SimpleNamespace`; this is valid while that helper reads only
`plan.request`. If the validator begins depending on runner state, it must be
extracted as a pure request validator rather than silently making preflight
construct a runner.

After a fold, verification re-reads request/result identities, the complete
dataset payload, declared splits and label binding, all test-date predictions,
trained run/artifact/dataset/prediction lineage, model review and the reviewed
backtest/config. Every planned test session must appear exactly once in both
prediction and target-weight bindings. Actual ordered features, factor version
and label semantics must stay fixed across folds. A failure stops later folds
and records the verified prefix plus failing fold/request/run/data-root context.

Each fold retains independent initial capital and its existing backtest result.
After all folds pass, `run_walk_forward_continuous_backtest.py` can re-read their
research results, reviewed backtests and target-weight partitions, then send
the ordered OOS targets through one `BacktestEngine` v3 run. Cash and holdings
carry across fold boundaries; independent fold PnL is not concatenated.

Continuous replay requires a separately published and reviewed v3 config in
the backtest root. Its calendar, raw prices, execution facts, suspension data
and corporate-action data must cover the contiguous OOS decision sessions plus
two following sessions: the first executes the last OOS target, and the second
is the terminal liquidation attempt. Cost, execution, initial-capital and risk
settings must match every fold config. The replay result gets its own
external quality decision; the orchestration layer never creates one. The
immutable receipt binds the plan, passed attempt, result, composite target
sequence and each source target-weight store. Receipt reads re-verify fold
research/backtest lineage and target sources through their owning stores.

The final session forces a zero target at its open while retaining T+1
sellability, price-band, suspension, participation, risk and cost guards. The
receipt records the attempt, terminal holdings value and any unfilled statuses;
`terminal_liquidation` is true only when close-marked holdings reach zero.
Incomplete liquidation is reported as such, and trading acceptance remains
uncertified. This path does not prove one-year data completeness, coverage of
three market states, PIT universe completeness, calibrated costs or strategy
profitability.

```sh
.venv/bin/python scripts/run_walk_forward_continuous_backtest.py \
  --walk-forward-root <evidence-store> --attempt-json <passed-attempt.json> \
  --backtest-root <v3-market-store> --backtest-config-generation <reviewed-config-id> \
  --quality-decision-json <external-result-review.json>
```

The command refuses a failed or incomplete attempt, mismatched source lineage,
missing target date, economics drift or incomplete replay window. An identical
retry reads and verifies the existing receipt, result and target-weight sources
without re-entering the backtest engine. A new attempt or config identity creates
a different target-sequence identity.

Completed-result retry is explicit: the adapter discovers one immutable result
for the original request/run and reads it before entering any publisher, then
performs all owning-layer verification again. A malformed or ambiguous completed
result fails closed; it never falls back to re-fitting. Incomplete runs retain
the existing stage-ledger resume behavior. No artifact or ledger is removed,
rewritten or marked failed to force a repeat.

Storage is versioned JSON evidence under
`walk_forward/plans/plan=<id>.json` and
`walk_forward/attempts/plan=<id>/attempt=<uuid>.json`. Publication uses staging,
fsync and atomic hard-link creation; plans are idempotent only byte-identically,
and each attempt has a new path. Reads recheck checksums, recompute plans and
verify attempt scope, coverage and ordered fold records. Attempt metadata alone
is not an independent replay of trained models; owning outputs remain the
verification authority. Root aliases are supported; links inside evidence paths
are rejected. These records are not model-quality contracts.

`tests/test_walk_forward.py` and `tests/test_walk_forward_runner.py` cover the
calendar/split rules, short coverage, partial tails, tampering and root aliases,
preflight with real published calendar/label/config stores, cost drift, first
failure stopping later jobs, feature drift, immutable completed-result retry,
and forged attempt metadata. Control-flow doubles are labelled as such. A
Qlib-gated integration test actually fits a purged training split, infers its
OOS date, publishes a backtest and verifies the full owning read path and retry.
It is a short synthetic single-fold integration, not a one-year or multi-fold
real-data experiment. Isolated in-memory mutations of purge, coverage and retry
guards make their respective regression tests fail.

## Unknowns and Pending Confirmation

- A one-year historical multi-fold run from newly published fold inputs has not
  been executed; historical onboarding and PIT-universe evidence remain pending.
- Terminal liquidation is attempted under the v3 guards and verified in the
  receipt, but suspended/limited/participation-blocked shares can remain. Costs
  are the same configured model assumptions and are not calibrated. The benchmark
  CLI can now consume the continuous walk-forward backtest result and receipt to
  reuse its fold-wide rebalance schedule and terminal liquidation. A real
  multi-fold strategy/benchmark comparison still needs historical PIT data.
- The legacy `backtest_result.v1` schema rejects `sharpe_ratio: null`, even when
  the engine legitimately produces no holdings/return variance. This is captured
  by `test_legacy_v1_cash_only_fold_exposes_undefined_sharpe_contract_gap`.
  New v3 results accept undefined Sharpe as null; original v1/v2 folds retain
  their published schemas and still fail closed in that case.
- The label-leg audit checks endpoint partitions and records the raw partition
  count/set, but does not compare partition dates with a separately governed
  calendar. A missing middle raw partition can therefore stretch the observed
  session horizon while publisher and audit agree.

## Cost and Capital Stress

`scripts/run_backtest_stress.py` requires one published v3 baseline, an explicit
capital grid and capacity thresholds, and external quality decisions for both
configs and results. Its first action after readback is to replay the baseline
from the actual raw-price/facts/market stores and signed target weights. Every
artifact value and summary must match; a baseline produced with an unavailable
extra risk gate or different implementation is refused before variant publishing.

```sh
.venv/bin/python scripts/run_backtest_stress.py \
  --data-root <store> --baseline-generation <v3-result-id> \
  --capital-grid 10000 100000 1000000 \
  --minimum-fill-ratio 0.95 --maximum-risk-breach-sessions 0 \
  --require-positive-return --quality-decisions-json <external-provider.json> \
  --report-root <diagnostic-store>
```

The required thresholds in this example are experiment choices, not calibrated
recommendations. Three cost cases run at baseline capital: commission rate and
minimum fee ×2 together; slippage ×2; fill-rate multiplier ×0.5. Each grid point
uses base economics with changed capital. Inputs and target-weight generations
stay fixed. Each changed config gets its own reviewed identity, and each replay
is published through `BacktestResultStore`; no old signed config is edited or
reused as approval of new economics. Existing reviewed outputs are recovered
and verified on retry; ambiguous config reviews fail closed. The command does
not create quality signatures. Code/lock/HEAD observations are bound to the
report, and a change detected during the batch refuses report publication.

`BacktestStressStore` publishes immutable, atomically written diagnostics under
`backtest_stress/generation=<id>/manifest.json`. Reads verify all referenced
reviewed results/configs, recheck raw/fact/target input bytes, enforce the exact
declared variants and target lineage, recompute metrics from equity/daily/fill
artifacts, and recompute every conclusion. Rehashing edited capacity conclusions
cannot make them pass. Metrics reconcile returns with initial capital and NAV
and include actual fees, slippage/impact, fill ratio, unpriced orders and risk
breach sessions. An unpriced order or undefined fill ratio cannot qualify a
capital point, even when the minimum-fill threshold is zero.

`maximum_tested_capital_meeting_policy` is the maximum qualified **tested grid
point**, with no monotonic interpolation or claim about larger capital. Policy
can require positive net return. `positive_base_and_stress_returns` only checks
the signs of the four costed replay returns; it does not certify stable excess
return, statistical significance or market-state coverage. Lower assumed fills
can improve a losing strategy's return; the report does not impose a preferred
outcome. `diagnostic_only: true`, `assumptions_not_market_calibrated` and
`market_capacity_certified: false` remain explicit. Real liquidity/impact
calibration, historical source approval and trading acceptance remain pending.
