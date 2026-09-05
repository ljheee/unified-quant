# Research Chain Integration Layer Specification

Status: **released v0.8; contract-first; Phases 0–6 exited after local and remote gate evidence**

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
- `model_definition_template.v1`: a closed, request-embedded object with
  `model_set`, `model_version`, reviewed `status`, algorithm, hyperparameters,
  seed policy, compatible dataset versions, metrics, selection rule, quality
  policy, serializer version, and code fingerprint. It is a separate Phase 0
  validation shape and excludes runtime identity fields. The request schema
  must `$ref` these template schemas so request/template validation cannot
  drift. The runner synthesizes `model_definition.v1` by calling the owning
  `ModelRunBuilder.build()` only. The runner never pre-writes a bound
  definition. The builder receives the template materialized as a provisional
  definition with zero identity fields plus the verified dataset/export/
  receipt/label/universe/factor manifests and external quality decision. The
  builder assigns `model_run_content_generation_id`, run metadata, immutable
  definition identities, and the accepted model-run quality report;
- `portfolio_definition_template.v1`: a closed, request-embedded object with
  reviewed status, portfolio name, weight scheme, scheme parameters, score
  policy, constraints, rebalance schedule, universe snapshot binding, and
  industry-source policy. It is a separate Phase 0 validation shape and excludes
  runtime identity fields. The runner synthesizes `portfolio_definition.v1` by
  adding the newly produced `prediction_set_generation_id`, run metadata,
  identity/digest fields, and the owning store's accepted quality decision;
- backtest config binding;
- deterministic environment binding: code fingerprint, environment lock digest,
  serialization profile, thread count, and seed;
- `stage_plan_sha256`;
- run-local metadata: `run_id` and `created_at`;
- stable `request_content_generation_id`;
- complete `manifest_digest_sha256`.

Normative rules:

- `request_content_generation_id` is the stable semantic identity. Its excluded
  canonical field set is exactly `run_id`, `created_at`, and
  `manifest_digest_sha256`; it is invariant under key reorder.
- `manifest_digest_sha256` covers the complete request document after all
  identity fields are populated, excluding `manifest_digest_sha256` itself. It
  is attempt-sensitive and is recorded in state history, but it must not
determine result stability.
- `stage_plan_sha256` is the canonical JSON SHA-256 of this exact object:
  `{"schema_version":"v1","stage_plan":["resolve_request","factor_computation","dataset_preparation","qlib_export","model_training","prediction_publication","portfolio_construction","backtest_execution","result_reconciliation"]}`.
  The resolver must reject any other value.
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

- `request_content_generation_id` and complete request manifest digest;
- runner identity: code fingerprint, environment profile, lock digest;
- ordered stage records, each with exact keys `stage`, `status`,
  `output_bindings`, and `failure_reason` (`null` unless failed);
- each stage has zero or more closed `stage_output_binding` objects with exact
  keys `output_family`, `generation_id`, `manifest_digest_sha256`,
  `data_checksum_sha256`, `physical_path`,
  `quality_decision_checksum_sha256`, and `failure_reason`;
- final status: `running`, `passed`, `warning`, `blocked`, or `failed`;
- run-local metadata and identities.

The durable state layout is keyed by the stable request generation, with the
run-local attempt nested beneath it:

`research_runs/states/request=<request_generation>/run=<run_id>/stage=<NN>/manifest.json`

Normative rules:

- A `run_id` identifies one immutable attempt. Resume is not supported in v1;
  a new attempt, including retry after any failure, must use a new `run_id`.
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

- `request_content_generation_id`, request attempt manifest digest, and run
  metadata;
- runner code/environment fingerprints;
- ordered stage records and `stage_output_binding` objects using the same
  closed schema as state;
- final readback status for every output;
- overall logical fingerprint;
- optional `result_artifact_digest_sha256`, defined as the Parquet/file-byte
  SHA-256 of the optional physical result export; it is distinct from canonical
  JSON identity;
- run-local metadata, stable `result_content_generation_id`, and
  `manifest_digest_sha256`.

Normative rules:
- `result_content_generation_id` is invariant under key reorder. Its excluded
  canonical field set is exactly `result_content_generation_id`,
  `manifest_digest_sha256`, `run_id`, `created_at`,
  `request_manifest_digest_sha256`, state document timestamps, state attempt
  digest, and each binding's `physical_path`. It changes with bound output
  identities, checksums, semantic request fields, runner identity, semantic
  stage status, and quality decision checksums.

- A successful result may only exist after every listed manifest has been read
  back through its owning store.
- A successful result must not reference failed or quarantined outputs.
- The result generation changes when any bound stage output, request semantic
  field, runner identity, or stage status changes.
- The result does not copy DataFrames, model bytes, scores, weights, fills, or
  price panels.

## 6. Identity and Binding Rules

1. All identities use canonical JSON SHA-256.
2. `request_content_generation_id` is stable under key reorder and run
   metadata changes. The complete request manifest digest is attempt-sensitive.
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
- Phase 0 must define typed request, state, and result store interfaces:
  `publish_request(manifest, path_policy) -> PublishedRequest`,
  `read_request(request_content_generation_id, manifest_digest_sha256) -> Request`,
  `publish_state(manifest, stage) -> PublishedState`,
  `read_state(request_content_generation_id, run_id, stage, manifest_digest_sha256) -> State`,
  `list_state_snapshots(request_content_generation_id, run_id) -> list[StateSummary]`,
  `publish_result(manifest, path_policy) -> PublishedResult`, and
  `read_result(result_generation_id, manifest_digest_sha256) -> Result`. All
  publishers are atomic and overwrite-safe; the result publisher is implemented
  in Phase 5. State and result paths must reject traversal, symlink escape,
  missing parent, and overwrite.

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
  the CLI/store wiring, not inferred from the request or current process;
- frozen interface:
  `resolve(binding_type, subject_generation_id, subject_manifest_digest_or_none, output_family, provider_config_ref) -> QualityDecision`;
- Research Chain does not introduce a second governance format. The Phase 0
  `quality_decision.v1` envelope is a strict lookup wrapper around an existing
  owning report: `model_quality_report.v2` for model/portfolio/backtest-facing
  families and factor-layer `quality_report.v1` for `factor_v1`. The wrapper
  fields are exactly `schema_version`, `binding_type`,
  `subject_generation_id`, `subject_manifest_digest_sha256`, `owning_report`,
  `decision_checksum_sha256`, `provider_id`, and `trust_anchor_id`. It returns
  the accepted owning report unchanged and never replaces or re-signs it;
  `decision_checksum_sha256` is the owning report's canonical checksum.
- The Phase 0 binding enum is the closed union of the existing
  `model_quality_report.v2` enum plus `factor_v1`. Chain stages must use the
  exact owning binding names: label set, feature preprocessing, model dataset,
  Qlib export/receipt, model definition/run/artifact, prediction, portfolio
  definition, target weights, backtest config, backtest result, and factor
  report. If an owning report family is missing, its Phase 0 slice remains
  blocked until that owning contract is added;
- canonical decision checksum is `sha256_json(decision_document)`; a byte digest
  is stored separately when a physical decision file exists. Signature
  verification must reject unregistered trust anchors, malformed signatures,
  binding mismatches, and non-matching checksums before the decision is visible
  to a stage;
- `provider_config_ref` is a registered configuration identifier and path token
  under the CLI trust root. It must reject traversal, symlink escape, unregistered
  configuration, and unregistered trust anchors;
- provider unreachable and provider configuration invalid map to
  `quality_decision_missing`; untrusted keys map to
  `quality_decision_rejected`.

The runner must persist:

- the decision checksum used for each output;
- the binding type and subject generation selected by the owning store;
- the provider identity and key/registry anchor where available.

If a decision is missing, rejected, bound to another generation, or signed by
an untrusted key, the run fails before publication.

## 8.1 Owning Layer Contract APIs

The following readback and binding APIs are Phase 0 contract deliverables.
They are additive owning-layer APIs, not runner-side fallbacks:

- `FactorStore.read_manifest(generation_id)` and
  `FactorStore.read_partition(generation_id)`, both manifest-first and
  checksum/identity verified;
- `PortfolioDefinitionBinding.bind(...) -> (portfolio_definition, quality_report)`
  as the only owning template-to-runtime definition boundary;
- typed `FeatureSchemaStore.read_manifest/read_schema(generation_id)`;
- typed `LabelStore.read_manifest/read_frame(generation_id)`;
- typed `UniverseSnapshotStore.read_manifest/read_members(generation_id)`;
- existing model artifact, prediction, target-weight, and backtest result read
  APIs must be declared as the only accepted reuse/readback boundary for their
  families.

If an API does not exist, the dependent Phase 2–4 slice is blocked; the runner
must not scan manifests, reconstruct identity, or read physical files directly.

## 9. Determinism and Reproducibility

The request records the execution environment:

- Python/pandas/NumPy/PyArrow/Qlib versions where applicable;
- lockfile digest;
- OS family and CPU architecture for evidence only;
- runner code fingerprint;
- deterministic seed and thread policy;
- serialization profile ID.

Reproducibility rules:

- `reproducibility_mode` has exactly two v1 values:
  `logical_fingerprint` and `locked_byte_identity`.
- `logical_fingerprint` compares stable manifest generations, logical
  fingerprints, and checksums of logical content; it tolerates Parquet byte
  drift.
- `locked_byte_identity` additionally compares all Parquet/file bytes inside the
  same locked OS/Python/dependency cell and fails on any byte difference.
- Cross-platform acceptance may use only `logical_fingerprint`; tolerance is the
  exact numeric tolerance recorded in the manifest. Binary model states are
  byte-compared only under `locked_byte_identity`.
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
  --project-root /path/to/project/root \
  --request /path/to/research_run_request.json \
  --data-root /path/to/governed/root \
  --mode dry-run
```

`--project-root` resolves schemas, review registries, provider configuration,
and supported store wiring. `execute` publication requires explicit
`--mode execute`.

`--request` may point to any readable request document outside the governed
data root. The runner validates it before treating it as a chain input.

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
