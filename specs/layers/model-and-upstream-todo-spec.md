# Model and Upstream Layers: TODO Specification

Status: **historical TODO design input; superseded by model-layer-spec v0.2.1**

This document deliberately stays brief. Each section must become an independent
versioned specification before implementation.

## 1. Label Layer

Purpose: define prediction targets without lookahead. It is mandatory for
supervised model training, but not part of the factor layer.

### Why a separate layer?

Labels are not factors:

- factors are inputs observable at decision time;
- labels are outcomes realized after decision time;
- they have different leakage risks and lifecycle rules;
- unsupervised or rule strategies may consume features without labels.

The dataset builder may support feature-only mode, but supervised training must
bind an explicit label dataset/version. A label embedded silently inside the
factor layer would violate input/output separation and make reuse unsafe.

TODO contract:

- label dataset name and semantic version;
- instrument/date key;
- decision date and decision time;
- horizon in trading days;
- label formula and units;
- eligibility rules;
- null policy for insufficient future data;
- label manifest and checksum.

Initial candidate:

```text
label_return_5d = adjusted_close[D + 5] / adjusted_close[D] - 1
```

For long/short research, excess return versus a benchmark or risk-free rate is
preferred over raw return. A-share T+1 and buy/sell price conventions must be
explicit before any tradability claim.

Rules:

1. Labels use only observations strictly after the decision time unless the
   formula explicitly defines a same-close execution convention.
2. The last horizon sessions remain null.
3. Suspended/non-tradable outcomes require explicit policy; they must not be
   silently forward-filled.
4. Delisted instruments need a terminal-return rule before production use.

## 2. Dataset Layer

Purpose: bind features, labels, universe, and time range into a reproducible
training artifact.

TODO contract:

- dataset name and semantic version;
- train/validation/test date ranges;
- universe snapshot and checksum;
- factor set/version and factor manifest checksum;
- label set/version and label manifest checksum;
- feature list and order;
- row eligibility rules;
- missing-value policy;
- dataset manifest and deterministic fingerprint.

### Design cautions

- Time-based splitting is mandatory for financial panels; random row splitting
  leaks regime information.
- Validation/test ranges should include purge and embargo around the label
  horizon.
- Feature rows at `D` must bind only to labels whose decision date is also `D`.
- Missing-value policy is part of dataset semantics, not a hidden model detail.
- Universe changes must create a new dataset version.

## 3. Model Layer

Purpose: train one model from one immutable dataset.

TODO contract:

- model name and semantic version;
- model family/algorithm;
- dataset fingerprint;
- feature schema;
- hyperparameters;
- random seed;
- training code version;
- train/valid metrics;
- artifact path and checksum;
- environment/dependency lock fingerprint.

A model must not read canonical data directly. It may only consume a published
dataset manifest.

Initial baseline: regularized linear model or LightGBM, selected after the
dataset layer exists.

### Design cautions

- Financial samples are highly cross-sectionally correlated; row counts do not
  imply independent observations.
- Low signal-to-noise ratio makes small metric differences unstable.
- Non-stationarity can make in-sample ranking meaningless out-of-sample.
- Hyperparameter search needs time-aware validation and must itself leave an
   auditable trial manifest.
- Determinism requires pinned dependencies, seeds, thread/parallel controls,
   and stable feature order.

## 4. Prediction Layer

Purpose: store versioned scores for later research or backtesting.

TODO contract:

- prediction dataset name/version;
- model artifact checksum;
- feature dataset checksum;
- decision date/time;
- instrument, score, rank, and probability columns as applicable;
- null/eligibility status;
- immutable prediction manifest.

Predictions are append-only and cannot overwrite historical scores.

### Design cautions

- Scores from different models or feature versions are not comparable without
   normalization semantics.
- Rank/score units must be recorded.
- Eligibility and tradability belong with predictions or a linked universe
   snapshot, not as implicit assumptions downstream.

## 5. Backtest Layer

Purpose: evaluate a strategy, not prove profitability.

TODO contract:

- strategy name/version;
- prediction input checksum;
- universe and eligibility rules;
- rebalance frequency;
- execution price and timing;
- commission, stamp duty, slippage;
- position and cash constraints;
- turnover and capacity assumptions;
- trade/event ledger;
- performance and risk report schema;
- backtest manifest and checksum.

The first version should be vectorized daily research backtest, not an
event-driven execution simulator.

### Industry hard problems

- **Lookahead execution**: deciding and filling at the same close is often
  unrealistic; A-share T+1 further constrains same-day exits.
- **Capacity**: daily turnover and volume participation limits can invalidate
  paper returns.
- **Costs**: commission, stamp duty, transfer fee, slippage, and market impact
  dominate many daily signals.
- **Survivorship**: delisted instruments must remain eligible historically when
  they were investable.
- **Suspensions**: positions cannot always be rebalanced; missing fills must be
  modeled rather than ignored.
- **Corporate actions**: raw-price PnL breaks across splits/dividends.
- **Benchmark choice**: excess return, hedged return, and absolute return answer
  different questions.
- **Multiple testing**: repeated strategy/model trials inflate reported alpha;
  holdout and deflated evaluation are required.

## 6. Experiment Layer

Purpose: make every result reproducible and comparable.

TODO contract:

- experiment/run ID;
- git/code version;
- config fingerprint;
- dataset/model/prediction/backtest lineage;
- metrics;
- artifact references;
- environment fingerprint;
- immutable run summary.

A result without lineage is exploratory output, not an experiment.

### Minimum governance

1. Every artifact has content checksum and producer lineage.
2. Every metric references an immutable dataset/model/backtest generation.
3. Failed runs are retained separately from accepted results.
4. Secrets never enter configs, manifests, logs, or metrics.

## 7. Implementation Order

1. Factor layer v0.
2. Label layer v0.
3. Dataset builder v0.
4. Model runner v0 with one baseline.
5. Prediction store v0.
6. Vectorized backtest v0.
7. Experiment tracking v0.

Do not implement later layers before the earlier layer has an immutable
manifest and checksum protocol.

## 8. Cross-Layer Principle

Keep the original separation:

```text
observation -> decision -> outcome
```

Factors encode only what was knowable at the decision point. Datasets bind
observations to decisions. Labels describe later outcomes. Models map datasets
to scores. Backtests evaluate executable decisions under explicit constraints.
