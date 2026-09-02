# Research Chain Integration Layer Specification

Status: **draft v0.2; contract-first; all runtime stages paused until Phase 0 exits**

Source specs:

- `specs/layers/factor-layer-spec.md`
- `specs/layers/model-layer-spec.md`
- `specs/layers/portfolio-backtest-layer-spec.md`
- `specs/architecture.md`

This layer is an orchestrator over already-governed stores and engines. It must
not reimplement factor computation, model training, portfolio construction, or
backtesting.

## 1. Purpose

The Research Chain Integration Layer answers one reproducible question:

> Given a reviewed research request, what accepted artifacts are produced from
> canonical/governed inputs through factors, dataset construction, Qlib
> training, prediction, portfolio construction, and backtest?

The normative output is a durable research run identity and evidence index, not
an in-memory DataFrame handed between layers.

## 2. Non-Goals

- No new factor, label, model, portfolio, or backtest algorithm.
- No replacement of `FactorStore`, `DatasetWriter`, `ArtifactStore`,
  `PredictionBuilder`, `TargetWeightStore`, or `BacktestResultStore`.
- No automatic selection of the “latest” model, dataset, factor, universe, or
  config.
- No live order routing, execution, monitoring, or production serving.
- No bypass of quality reports, lineage checks, checksums, cutoffs, or
  immutable publication rules.
- No attempt to make cross-platform Parquet bytes identical.
- No silent reuse of a failed stage output.

## 3. Layer Boundary

```text
ResearchRunRequest
  -> resolve reviewed inputs and stage plan
  -> FactorEngine / FactorStore
  -> LabelBuilder / DatasetBuilder / DatasetWriter
  -> QlibDatasetExporter / QlibInitReceiptBuilder
  -> ModelRunBuilder / QlibRuntimeTrainer / ArtifactStore
  -> PredictionBuilder
  -> PortfolioBuilder / TargetWeightStore
  -> BacktestEngine / BacktestResultStore
  -> ResearchRunState
  -> ResearchRunResult
```

The runner may receive typed store objects and adapters, but it must not mutate
a manifest after the owning layer has published it.

## 4. Normative Run Stages

The first governed slice uses a fixed stage order:

| Order | Stage | Owner |
|---|---|---|
| 0 | `resolve_request` | Research Chain |
| 1 | `factor_computation` | Factor layer |
| 2 | `dataset_preparation` | Model layer |
| 3 | `qlib_export` | Model layer |
| 4 | `model_training` | Model layer |
| 5 | `prediction_publication` | Model layer |
| 6 | `portfolio_construction` | Portfolio layer |
| 7 | `backtest_execution` | Backtest layer |
| 8 | `result_reconciliation` | Research Chain |

A stage may start only after the preceding stage is `passed` and its output
manifest has been read back successfully. A failed stage must stop the run and
persist a failed stage state. Outputs already published by the failed attempt
remain immutable, but they must not appear in a successful
`research_run_result`.

The first implementation may not enable, reorder, skip, or add a stage. A
semantic stage-order change requires a new request schema version.

## 5. Contract Families

### 5.1 `research_run_request.v1`

The request is the immutable semantic input to a chain.

Required logical fields:

- `research_name`;
- `execution_mode`, first release `full_research_run`;
- `run_visible_cutoff` and decision timezone;
- inclusive `window_start_date` and `window_end_date`;
- factor set, factor version, universe snapshot binding;
- adjusted-price dataset binding for labels;
- label name, semantic version, horizon, split policy binding;
- seed model definition template: reviewed algorithm, hyperparameters, seed
  policy, compatible dataset versions, metrics, selection rule, quality policy,
  serializer version, and code fingerprint. It must not require the later
  `feature_schema_generation_id` or `model_run_content_generation_id`;
- portfolio definition template: reviewed portfolio name, weight scheme,
  scheme parameters, score policy, constraints, rebalance schedule, universe
  snapshot binding, and industry-source policy. It must not require the later
  `prediction_set_generation_id`;
- backtest config binding;
- deterministic environment binding: code fingerprint, environment lock digest,
  serialization profile, thread count, and seed;
- `stage_plan_sha256`;
- run-local metadata: `run_id` and `created_at`;
- stable `generation_id` and complete `manifest_digest_sha256`.

Normative rules:

- Stable `generation_id` excludes run-local fields and stage timestamps.
- `manifest_digest_sha256` covers the complete request document.
- `stage_plan_sha256` is the reviewed digest of the fixed ordered stage names,
  owning-layer contract versions, and request input families. The resolver must
  reject a value that does not match the normative Phase 0 stage plan.
- `resolved_execution_plan_sha256` is a separate resolver output, not a request
  input.
- Every generation/checksum reference must be a 64-character lowercase SHA-256.
- The request must not contain model, prediction, weight, or backtest result
  output IDs. Runtime binding fields required by owning manifests are
  synthesized from the reviewed templates and exact stage outputs; they are not
  request inputs.
- The request must not embed arbitrary DataFrames or file contents.
- No path field may contain `..`, `/`, `\`, or a leading `.`.
- `full_research_run` means all normative stages execute or bind an exact
  pre-existing output only when the stage contract explicitly permits reuse.
  The first release does not permit partial execution.

### 5.2 `research_run_state.v1`

The state is the durable stage ledger.

Required logical fields:

- request identity and digest;
- runner identity: code fingerprint, environment profile, lock digest;
- ordered stage results;
- for each stage: name, status, output family bindings, output generation IDs,
  manifest digests, physical paths, quality report checksums, and failure
  taxonomy;
- final status: `running`, `passed`, `warning`, `blocked`, or `failed`;
- run-local metadata and identities.

The durable state layout is keyed by the stable request generation, with the
run-local attempt nested beneath it:

`research_runs/states/request=<request_generation>/run=<run_id>/stage=<NN>/manifest.json`

Normative rules:

- State documents are append-forward by stage order; an earlier state document
  is never rewritten.
- Each completed state snapshot has its own canonical generation and manifest
  digest.
- Stage paths must be inside the configured data root.
- A failed or blocked state must identify the exact failing stage and typed
  reason.
- A state document is evidence, not an accepted downstream input. Downstream
  stages must consume the owning layer's store read API.

### 5.3 `research_run_result.v1`

The result is the final reconciled evidence index.

Required logical fields:

- request generation and digest;
- runner code/environment fingerprints;
- ordered output bindings for every stage;
- final readback status for every output;
- overall logical fingerprint;
- optional artifact checksum for the result document in the current locked
  environment;
- run-local metadata, stable generation, and manifest digest.

Normative rules:

- A successful result may only exist after every listed manifest has been read
  back through its owning store.
- A successful result must not reference failed or quarantined outputs.
- The result generation changes when any bound stage output, request semantic
  field, runner identity, or stage status changes.
- The result does not copy DataFrames, model bytes, scores, weights, fills, or
  price panels.

## 6. Identity and Binding Rules

1. All identities use canonical JSON SHA-256.
2. `research_run_request.generation_id` is stable under key reorder and run
   metadata changes.
3. `research_run_result.generation_id` is stable under state timestamp and run
   metadata changes, but changes with any bound artifact identity or semantic
   stage status.
4. Every upstream binding must include both `generation_id` and
   `manifest_digest_sha256` where the owning schema has both. For artifact
   families exposing only a data checksum, the request must record that
   checksum explicitly.
5. Binding resolution must load the referenced manifest and validate it with the
   owning typed loader before the next stage.
6. A referenced path may only be used after its manifest identity and checksum
   match.

## 7. Data and Store Rules

- The runner reads canonical data only through `FactorContext` or another typed
  accepted reader.
- Factor outputs publish through the existing `FactorStore` path.
- Labels, feature schemas, datasets, Qlib exports, receipts, model runs,
  artifacts, and predictions publish through their existing model-layer APIs.
- Target weights and backtest results publish through their existing stores.
- Physical output layout is owned by each layer; Research Chain must not invent
  a parallel accepted layout.
- The only Research Chain-owned durable artifacts are request, state, result,
  and their fixtures/golden vectors.
- Phase 0 must define typed request, state, and result store interfaces. The
  result store must provide atomic, overwrite-safe publish and verified read;
  its publisher is implemented in Phase 5.

## 8. Quality and External Review

The runner must not create, mutate, or approve a quality decision.

A typed decision provider is required. For each stage that publishes an
artifact, the provider must return an externally reviewed decision accepted by
the owning store. The provider must be independent from publication code at
runtime; an in-process test helper may exist only for contract tests and must
not be used as production evidence.

The Phase 0 provider contract is:

- input: owning-layer `binding_type`, subject `generation_id`, subject
  `manifest_digest_sha256` when applicable, requested output family, and
  provider configuration reference;
- output: the owning layer's typed quality decision document, including its
  canonical checksum, binding type, subject identity, reviewer/registry anchor,
  status, policy, and checks;
- behavior: pure lookup and verification. It must not construct, sign, approve,
  cache a substitute decision, or accept a decision whose binding or checksum
  does not match the exact subject;
- configuration: the provider implementation and trust anchor are supplied by
  the CLI/store wiring, not inferred from the request or current process.

The runner must persist:

- the decision checksum used for each output;
- the binding type and subject generation selected by the owning store;
- the provider identity and key/registry anchor where available.

If a decision is missing, rejected, bound to another generation, or signed by
an untrusted key, the run fails before publication.

## 9. Determinism and Reproducibility

The request records the execution environment:

- Python/pandas/NumPy/PyArrow/Qlib versions where applicable;
- lockfile digest;
- OS family and CPU architecture for evidence only;
- runner code fingerprint;
- deterministic seed and thread policy;
- serialization profile ID.

Reproducibility rules:

- Within one locked environment, repeated staging outputs must match the mode
  selected by the request.
- Cross-platform comparison is by logical fingerprints and declared tolerances.
- Parquet byte equality is claimed only for the same locked environment.
- Any change to a semantic input, stage order, runner logic, serialization
  profile, seed, or bound upstream output produces a new request/result
  generation.

## 10. Failure Taxonomy

Typed failure reasons at minimum:

- `request_invalid`;
- `input_unresolved`;
- `input_tampered`;
- `lineage_mismatch`;
- `quality_decision_missing`;
- `quality_decision_rejected`;
- `stage_failed`;
- `store_read_failed`;
- `overwrite_conflict`;
- `reproducibility_failed`;
- `result_reconciliation_failed`.

A failed run must not fall back to a prior successful run.

## 11. Command Surface

The first command is:

```bash
uq-research-run \
  --request /path/to/research_run_request.json \
  --data-root /path/to/governed/root \
  --mode dry-run
```

`execute` publication requires explicit `--mode execute`.

Command output is JSON and must include:

- request generation;
- state path or latest state generation;
- current stage;
- final status;
- result generation when the run completes.

The command must not read credentials from the request.

## 12. Acceptance Summary

| ID | Requirement |
|---|---|
| RC1 | A valid request resolves all reviewed upstream manifests or fails closed. |
| RC2 | The fixed stage order is mechanically enforced. |
| RC3 | Every output binds its exact upstream generation/digest. |
| RC4 | Missing/tampered/misbound upstream inputs reject before stage execution. |
| RC5 | Missing/rejected/misbound quality decisions reject publication. |
| RC6 | A completed result has readback evidence for every output. |
| RC7 | Failed stages stop the run and do not enter a successful result. |
| RC8 | The same locked-environment run reproduces under the declared mode. |
| RC9 | Request run metadata does not change stable request identity. |
| RC10 | Result identity changes when any bound output changes. |
| RC11 | State and result paths cannot escape the data root. |
| RC12 | CLI exposes dry-run and explicit execute modes. |
