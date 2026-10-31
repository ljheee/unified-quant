# Research Chain Orchestrator

> Current behavior reference. Last checked: `2026-10-20` against HEAD `54676a0a3b377d1b88da09309d96d6a7c6535419`. Based on source code, configuration, tests, and release evidence; unverified items are marked `UNKNOWN`.

## Responsibilities and Boundaries

The Research Chain is the top-level governed orchestrator: it validates a reviewed request, resolves every declared input through the owning layer's store, runs the stages in the reviewed order, and reconciles the result. It is **not** a computation engine — it invokes `FactorStore`, dataset writers, the Qlib exporter, the trainers, the portfolio builder, the backtest engine and the paper execution stage through their own APIs, and it never bypasses a manifest, a quality decision, or an accepted reader.

It does **not** own any of those layers, does not create or sign quality decisions (it only looks up and verifies reviewed decisions), and does not generate prediction scores (they are supplied externally).

## Authoritative Sources

| Fact | Authoritative path |
| --- | --- |
| CLI contract and exit codes | `src/uq/research_chain/cli.py:74` `uq_research_run`, `_EXIT_CODES` at `src/uq/research_chain/cli.py:52` |
| Stage orders per contract version | `src/uq/contracts/model_layer.py:108` `RESEARCH_STAGE_ORDER_V1`, `:113` `RESEARCH_STAGE_ORDER_V3` |
| Request resolution and store protocol | `src/uq/research_chain/resolver.py:127` `ResearchChainRequestResolver.resolve` |
| Stage-plan review verification | `src/uq/research_chain/contracts.py:295` `research_request_schema_name`, `verify_stage_plan_review` |
| Run/state/result ledger | `src/uq/research_chain/resolver.py:425` `FileResearchRunStore` |
| Stage adapters and cross-stage hand-offs | `src/uq/research_chain/adapters.py`, `src/uq/execution/stage_adapter.py:61` |
| Read-side stores for chain-bound families | `src/uq/research_chain/owning_contracts.py` |
| PIT membership filtering of factor frames | `src/uq/research_chain/membership_input.py:47` `verify_request_input_sets`, `:142` `filtered_factor_frame` |
| Stage-plan trust anchors | `config/research-stage-plan-review-anchor.v1.json`, `config/research-stage-plan-review-anchor.v3.json`, `config/research-stage-plan-review-anchor.v4.json`, `config/research-stage-plan-review-anchor.v5.json`, `config/research-stage-plan-review-anchor.v6.json` |
| Signed stage-plan builder for reviews | `scripts/sign_stage_plan_review.py` |

Stage order for v1/v2 is `resolve_request → factor_computation → dataset_preparation → qlib_export → model_training → prediction_publication → portfolio_construction → backtest_execution → result_reconciliation`. v3–v8 insert `paper_execution` before `result_reconciliation`; v3/v4 are refused at runtime while v5–v8 are executable. v6 adds `sample_construction_binding` without changing the stage order. v7 keeps the reviewed v6 stage plan because the stage sequence is unchanged, and requires `model_definition_template.v2` with `algorithm=qlib_lightgbm`, `serializer_version=joblib-v1` plus `portfolio_definition_template.v2`, which permits equal or score-weighted allocations. v8 retains the v7 model template and stage plan, and routes portfolio template v3, including optional bound risk-return and point-in-time industry facts for top-N risk parity, industry-balanced weights, and industry caps. The existing `feature_preprocessing_binding` may point to `feature_preprocessing.v2`; the manifest then binds an independently reviewed PIT exposure panel and the dataset stage replays its OLS transform. This does not add a Research Chain stage or change request schema identity. The v2/exposure quality families are not admitted by the active anchored quality-review registry yet.

## Primary Runtime Paths

Entry point is the console script `uq-research-run` (`uq.research_chain.cli:uq_research_run`), with `--mode dry-run` and `--mode execute`.

**dry-run** validates the request against its schema, verifies the signed stage-plan review, matches `stage_plan_sha256` to the contract version's expected digest, recomputes the request identity, and publishes a request manifest plus a `stage=00` state manifest. It does **not** read any bound store, so a green dry-run is not evidence that execute can run.

**execute** requires `--quality-decisions-json`; `--scores-json` is optional and, when omitted, scores come from the model artifact this run trained. v5–v8 additionally require `--membership-root` and `--market-dataset-root` together, plus `--paper-risk-documents-json` (missing inputs are refused before any walk-forward fold executes). Without external scores, the default decision dates are every session in the dataset's governed **test split** that the export calendar contains; training and validation dates are not backtested. The runner then:

1. validates the plan (`_validate_plan`), refusing contract versions 3/4 and verifying the stage-plan review for v2–v8;
2. publishes the request and resolution state;
3. **resumes** from stages already recorded `passed` (`src/uq/research_chain/runner.py:992` `_resume_from_passed_stages`), folding each stage's output bindings into the plan so a failure anywhere is retryable without hand-moving directories;
4. re-hydrates the one hand-off that has no read-back path (`model_training` → `model_run`/`model_artifact`) from the published manifests, so a resumed run consumes the same bytes it published;
5. runs the remaining stages through the owning adapters, then reconciles the result and publishes a result manifest.

Cancellation/failure writes a failed state on the way out, which is what makes resume possible.

## Multiple Implementations / Entry Points

- **Walk-forward batch entry.** `scripts/run_walk_forward.py` prepares a protocol
  from a governed calendar and executes a batch of already-reviewed requests
  through this same CLI implementation, with external scores disabled. The
  adapter checks exact fold splits and read-back lineage and reads completed
  results before attempting a retry. It neither changes the single-request
  contracts nor stitches independent backtest capital. See
  [training-evaluation.md](training-evaluation.md) for preflight, evidence and
  remaining continuous-portfolio limitations.

- **Contract versions 1–8** select different schemas and governed inputs. `contract_version` 3 and 4 are intentionally not executable (`research request v3/v4 paper-execution runtime is not enabled`) — a fail-closed behaviour lock registered by a test, not an unfinished wire-up. v5–v8 are executable; v6 adds sample construction, v7 selects the Qlib LightGBM model-definition template, and v8 adds portfolio-definition template v3 with risk-parity, industry-neutral and cap routes. v7/v8 reuse the v6 stage-plan digest and signed activation because no stage was added or reordered. Risk-parity requests resolve the immutable return generation, recheck its calendar and identity, and require sufficient pre-window history. Industry-aware requests resolve the execution-facts generation through the reviewed backtest config and enforce session-open visibility. A v2 preprocessing manifest is resolved through `FactorExposureStore` before stage execution, then re-read and transformed again during dataset preparation. It requires visible-by-close controls; publication is currently blocked until the two new review families are approved in the trust-anchor-pinned registry.
- **Decision providers.** The runner resolves quality decisions through a pluggable `QualityDecisionProvider` (`src/uq/research_chain/cli.py:183` mapping provider, `:201` command provider, or a JSON document). Implementations never sign; they return an immutable owning-layer decision for verification.
- **Membership filtering.** v5–v8 dataset preparation filters factors by *that day's* visible index members; a missing membership store is a hard error rather than a silent fall back to the unfiltered partition (`src/uq/research_chain/membership_input.py`).
- **Window-level universe anchor.** A v6 request binds one `universe_snapshot`, while PIT membership is published in monthly tiles. For a window spanning several tiles, `scripts/publish_universe_union.py` publishes the union of every visible snapshot and declares `semantics=window_level_union_not_daily_pit`. In v6 the snapshot is an **identity anchor**; the authoritative per-day member rule remains `index_membership_binding`, applied by `filtered_factor_frame`.
- **Paper market inputs.** `MarketDatasetPublisher` supplies the governed trading calendar for the research window; a window not covered by a calendar snapshot fails closed rather than being widened.

## State and Data Contracts

Layout under `$DATA_ROOT`:

```text
research_runs/requests/request=<generation>/run=<run_id>/manifest.json
research_runs/states/request=<generation>/run=<run_id>/stage=NN/manifest.json
research_runs/results/request=<generation>/run=<run_id>/result=<generation>/manifest.json
```

State and result manifests are content-addressed with `generation_id` and `manifest_digest_sha256`. A same-run/same-stage state write is allowed to rewrite with an identical digest (idempotent retry) and replaces the ledger otherwise; content-addressed artifacts are never rewritten, because artifact publication is the only moment content consistency can be checked.

## Dataset-stage missing-data policy (`missing_policy`)

Three mutually exclusive values, guarding three different defects. They are separate
because a null *feature* and a null *label* are not the same fact: a feature can be
undefined by definition (`close_location_1d` when `high == low`, i.e. a limit-locked
session; a 20-session warm-up), while a label is undefined because the forward price does
not exist. scikit-learn draws the same line inside one call — `ensure_all_finite` governs X
only and its docstring states it "does not influence whether y can have np.inf, np.nan,
pd.NA values", while `_assert_all_finite(y)` is unconditional. Qlib separates them too:
`DropnaLabel` is a distinct processor and `is_for_infer()` is `False` for it, `True` for
`DropnaProcessor(fields_group="feature")` — a row can be untrainable and still scoreable.

| Value | Removes | Post-merge null check |
| --- | --- | --- |
| `fail_closed` | nothing | any null in features or label → refuse |
| `drop_incomplete_rows` | rows with a null **feature** | any surviving **label** null → refuse |
| `drop_unlabeled_rows` | rows with a null **label** (plus feature nulls) | none — it drops them |

Dropping happens **before** the preprocessing transform, not after the label merge:
`DatasetWriter` requires the published frame and the preprocessing output to carry the
same rows and re-derives `output_frame_sha256` from the dataset frame on both publish and
read. A null label is only knowable after the factor/label join, so v6 requests bind a
separate `sample_construction.v1` artifact. Its selected frame is the preprocessing input;
preprocessing itself remains label-agnostic and stateless. v5 requests retain their legacy
stage-local path, but cannot acquire sample-construction semantics without a new request
version.

**Two shapes of a missing label, and why both must be dropped.** A label that is *present
but null* leaves a row in `label_frame` that `unlabeled_keys` can name. A label whose key is
*absent* leaves no row at all, so no value test can see it — yet a left join turns it into a
NaN that would reach the published dataset. The sibling policies caught both shapes because
they assert on the merged frame; this one caught only the first, because the check was
frame-local. `drop_unlabelled_factor_rows` now compares key sets with `pandas`
`indicator=True` and treats `left_only` the same as null, reporting `null_labels` and
`absent_keys` separately. No `validate=` value detects this — those guard key *uniqueness*.

## Constraints, Failures, and Compatibility

- **Observed execution provenance.** CLI `execute` now checks that
  `--project-root` is the executing checkout and verifies both request environment
  digests against actual files before calling the runner. Generate the digests
  with `scripts/research_environment.py --project-root .` before signing a new
  request. `source_tree_sha256_v1` hashes sorted paths, sizes and SHA256 values
  for Python sources under `src/uq`, Python/shell scripts under `scripts`,
  JSON/YAML configuration under `config`, and `pyproject.toml`; `uv.lock` has a
  separate digest. Uncommitted runtime changes are covered; docs, evidence, data
  and caches are excluded. HEAD, Python/platform and installed package versions
  are archived as observations, without asserting installed packages conform to
  the lock. Source/lock symlinks inside the root are refused. All-zero legacy
  requests remain readable and usable for dry-run, but CLI execution requires a
  newly bound request. Direct runner APIs retain explicitly injected identities
  for isolated test/embedding callers; only the CLI performs this local checkout
  measurement (`provenance.py`, `tests/test_training_provenance.py`).
- **Independent timing receipt.** On return or a Python exception, a CLI runner
  invocation publishes a unique
  `research_runs/telemetry/request=<id>/run=<id>/attempt=<uuid>.json`, including
  observed environment, data-root locator, monotonic total/stage durations,
  success/failure and a checksum. Failures retain completed measurements and the
  exception type; resumed stages are `reused` with no invented execution time.
  Telemetry does not enter any request/state/result identity. It measures coarse
  runner stages and, within a fresh Qlib export, feature-bin writing, file
  inventory/checksums, review binding, prepublish readback and durable
  publication. The export adapter separately measures owning readback, cache
  boundary plus receipt binding, and stage-state publication; an immutable
  export recovery gets its own retry readback measurement. These attempt-only
  measurements leave manifest and result identities unchanged. Direct API callers can pass
  `RunTelemetry` explicitly. Qlib-enabled
  regression tests in `tests/test_research_chain_phase5.py` compare timed and
  untimed result manifests and exercise a failed export.
  Telemetry publication is on the successful run's critical path: a telemetry
  write failure makes an otherwise successful invocation raise. If the research
  run already failed, telemetry failure is attached as an exception note and
  the original run failure remains primary.
- **Static reviewer documents.** With no command provider, `quality_decisions`
  contains stage decisions, while optional `request_quality_decisions` contains
  resolver envelopes (defaults to the same mapping for legacy documents). This
  removes an uninitialized-variable failure in the static execution path;
  signature/subject validation remains with the owning resolver and stages.

- Failure reasons are a closed set (`src/uq/research_chain/resolver.py:43`): `request_invalid`, `input_unresolved`, `input_tampered`, `lineage_mismatch`, `quality_decision_missing`, `quality_decision_rejected`, `stage_failed`, `store_read_failed`, `overwrite_conflict`, `reproducibility_failed`, `result_reconciliation_failed`. Each maps to a CLI exit code.
- Execute is deliberately not zero-input: it requires already-published governed upstream artifacts, an externally signed stage-plan review, external quality decisions and external scores. This is a design choice to stop unreviewed models or ungoverned data entering results (`docs/layer-architecture.md` §3).
- The v1 dry-run CLI path uses the v1 example request; copying a later-version fixture into it will not be routed as that version.
- Trust material: `require_production_review_key` refuses repository test-mode Ed25519 anchors when `UQ_RUNTIME_MODE=production` (`src/uq/runtime.py:34`), so a test-signed review cannot authorize a production run.

## Verification Evidence

- `.venv/bin/python -m pytest` → `1265 passed, 28 skipped`; `.venv-qlib/bin/python -m pytest` → `1293 passed, 0 skipped` (the 28 `.venv` skips are Qlib-gated tests). Counts updated by the v6 sample-construction change.
- Research-chain suites: `tests/test_research_chain_phase0.py`–`phase5.py`, `tests/test_research_chain_release_evidence.py`, `tests/test_research_v4_contract.py`, `tests/test_research_membership_input.py`.
- Paper-stage integration: `tests/test_paper_execution_research_stage_runner.py` and `tests/test_paper_execution_research_integration_phase*.py`.
- CLI dry-run smoke with the shipped fixture: `evidence/research-chain/phase-0/fixtures/research_run_request-valid.json` (documented in `docs/user-guide.md` §6).
- Release record: `evidence/research-chain/release/release-record.json` (v0.8, `release_head` `9f208267e585b0f95a219af2dd3f9367a5fb4d3e`, CI run `34004669663`, 10 covered cells) is verified mechanically by `tests/test_research_chain_release_evidence.py` and `scripts/verify_research_evidence.py`.

## Unknowns and Pending Confirmation

- `UNKNOWN`: a complete `execute` run from a clean data root in this worktree. Six upstream binding families must be published first; only four have production entry points (`docs/user-guide.md` §7). No `--data-root` was prepared or run during this bootstrap, and no credentials were used.
- `UNKNOWN`: a full v7 execute on an owner-provided data root. The v7 schema, signed v6 stage-plan reuse, LightGBM model-definition routing and model-level Qlib training/inference are regression-tested; a full request with real published inputs and one-year OOS evidence is not available in this worktree.

## `adjusted_price_binding`: one partition per session, not a window artifact

`adjusted_price_binding` (schema `config/schemas/contracts/research_run_request_v5.json`) is
`{generation_id, data_checksum_sha256}` with no window field, and
`scripts/publish_adjusted_window.py` publishes **one `canonical/bars_adjusted/adjusted-v2`
partition per session**, each with its own generation id. So a request binds one
session's partition — the schema has never expressed a window-level price artifact, and
no publisher creates one.

This is a real binding constraint, not an oversight to paper over: the **backtest stage**
needs a frame covering every date it trades, because it derives the governed calendar from
the price panel and asserts `set(price_dates) == set(calendar_dates)` plus
`set(decision_dates) ⊆ set(calendar_dates)`
(`src/uq/research_chain/adapters.py:1128-1134`). A request that binds a single-session
partition while its window spans many sessions therefore fails at the backtest stage,
which is exactly what stopped the first full-A v5 execute (cr.md §8.62 续三).

Two consequences, locked rather than assumed:

- **The failure belongs at the backtest stage.** Adding a window-coverage check at the
  dataset stage would turn "the window-level publisher is missing" into "the data is
  wrong", so the coverage invariant stays where it already is and is now tested
  behaviourally (`test_the_backtest_stage_refuses_a_calendar_that_does_not_cover_the_decision_dates`
  plus its positive control).
- **The price frame's own session key is used.** `bars_daily/v2` renames `datetime` to
  `session_date` (`config/schemas/bars_daily.v2.yaml` migration), and the published
  partitions key on whichever the source schema declares, so the backtest reads the
  column the frame carries instead of hardcoding `datetime`.

## Two market-data stores, two layouts, not one

`BacktestMarketDatasetStore` and `MarketDatasetPublisher` are **not** interchangeable:

| | layout | calendar column | consumer |
|---|---|---|---|
| `BacktestMarketDatasetStore` (`src/uq/research_chain/owning_contracts.py:175`) | `backtest_market_data/dataset=<generation>` | `date` | the backtest stage's calendar/suspension/corporate inputs |
| `MarketDatasetPublisher` (`src/uq/execution/market_dataset_publisher.py:29`) | `<family>/<partition_date>` under `market-datasets/` | `session_date` | paper-execution bindings and the governed-calendar path |

`scripts/publish_market_datasets.py` produces the second; the first has **no production
publisher** — only test helpers write it.

These are two contracts' entry points, not two copies of the same data, so they must not
be merged by "pointing the backtest at `market-datasets/`". The reason is the binding
schema, not the layout: `backtest_config.v1` pins `calendar_binding`,
`suspension_binding` and `corporate_action_binding` to a **single generation id and a
single checksum** (`config/schemas/contracts/backtest_config.v1.json`), with no window
field and no partition set. A research-chain backtest consumes one whole-window artifact
per family, which is what makes the run reproducible — a fact `partition_set` bindings
(the paper path) do not provide. Changing the backtest to read `market-datasets/` would
therefore change the published contract (`backtest_config.v1`), not just a path, and
belongs to its own round with a maintainer decision.

The window-level publisher for the three backtest families is
`scripts/publish_backtest_market_data.py` (store: `BacktestMarketDatasetStore.publish`,
contract: `config/schemas/contracts/backtest_market_dataset.v1.json`). Its input sources
are fixed by `specs/layers/paper-execution-research-integration-plan.md` phase 0.5, not
chosen ad hoc:

- **calendar** — the canonical `bars_daily/v2` session directories themselves, so the
  backtest's `set(price_dates) == set(calendar_dates)` invariant holds by construction
  rather than by reconciliation against a second vendor calendar.
- **suspension** — primary source is the bars `status` column; Tushare `suspend_d` is
  recorded as *reconciliation provenance only*, never a second truth. Anything that is
  not `trading` (`suspended`, `delisted`, `unknown`) is recorded as suspended, which is
  the conservative direction.
- **corporate_action** — the same `DividendEventStore` the adjustment chain derives
  factors from, taking only `div_proc=实施` records whose **`ex_date`** falls in the
  window. The announcement date is *not* the membership test: the disclosure store is
  announcement-anchored, so a partition dated before the window is ordinary, and an
  announcement inside the window whose ex-date lands later is not this window's event.

A window with no suspensions or no implemented corporate action is a state, not a
missing input, so the publisher emits an explicit negative row rather than an empty
frame (the store refuses an empty frame). Republishing the same window is a skip, not a
refusal — every writer here is immutable, and an interrupted run must resume.

`backtest_config` itself is assembled by `scripts/publish_backtest_config.py`, which
resolves the four bindings from named, already-published artifacts (never inferred from
the filesystem) and re-reads every market binding through its store before publishing,
so a report that disagrees with the store is refused rather than baked into a
content-addressed, immutable config. Its review arrives as
`{"provider_command": [...]}` — the same external-reviewer protocol
`--decision-provider-json` gives the research CLI — because this family's identity is
only determined after the review is bound (cr.md U9). `run_id` and `created_at` are the
fixed zero/epoch values: neither participates in the identity, so a per-run value would
mint a second generation for one dataset and break "this config belongs to this request".
