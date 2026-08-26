# Model Layer Specification

Status: **design v1.0.4; phases 0–5 implemented draft; release blocked pending reviewed external quality reports**

Upstream source: `specs/layers/factor-layer-spec.md`
Related early design: `specs/layers/model-and-upstream-todo-spec.md`

## 1. Purpose

The model layer converts governed factor partitions into versioned training
datasets, models, and prediction artifacts. It is a downstream consumer of the
factor layer and an upstream producer for research evaluation or strategy
layers.

The first release target is a local-research supervised slice. Qlib is the
selected model runtime/engine. It may orchestrate datasets, training, and model
persistence, but it is never the source of truth for factors, labels, universe,
split policy, lineage, or publication.

## 2. Non-Goals

- production serving, online inference, or order execution;
- automated hyperparameter search as a first-release requirement;
- replacing canonical ingest, FactorStore, or DatasetBuilder;
- reading raw providers, canonical bars, staging factors, or quarantine;
- historical universe reconstruction from current membership lists;
- treating backtest performance as part of model validity.

Qlib `.bin` caches remain derived, reproducible, ignored data. They must not be
committed and cannot be used as an identity substitute for governed inputs.
Existing `QlibExporter` and canonical manifests are upstream prototypes, not
model-layer implementations.

## 3. Layer Boundary

```text
accepted FactorStore partitions
  -> LabelStore (explicit future outcome contract)
  -> ModelDatasetBuilder / Qlib feature adapter
  -> Qlib runtime
  -> ModelRegistry + immutable ModelArtifactStore
  -> PredictionStore
```

Hard boundaries:

1. Models read only published dataset manifests.
2. Datasets read only accepted factor partitions and published label manifests.
3. The Qlib adapter reads only the immutable materialized dataset snapshot.
4. Predictions record the exact model artifact and dataset generation consumed.
5. No component may bypass manifest, checksum, visibility, path-identity, or
   quality-policy checks.

## 4. Identity Model

Every durable artifact has two identities:

1. `generation_id`: stable content/lineage identity excluding run metadata such
   as `run_id`, wall-clock creation time, host path, or transient counters.
2. `manifest_digest_sha256`: digest over canonical JSON including
   `generation_id` but excluding secret material; it changes when any manifest
   field changes.

Identity payloads use canonical JSON with sorted keys, explicit UTF-8, no NaN,
and stable decimal serialization. A schema change that alters semantics requires
a new schema version; existing published versions cannot be changed in place.

For every artifact family, a normative generation payload table must declare:
included semantic fields, excluded run-local fields, decimal/float
normalization, logical-fingerprint fallback, and artifact-checksum relationship.
`manifest_digest_sha256` is SHA-256 over UTF-8 canonical JSON with sorted keys,
compact separators, and no non-finite numbers. A generation ID is invalid unless
it is recomputable solely from the declared payload. The first release uses the
factor layer's external local trust anchor; remote signatures are out of scope.

Required artifact families:

| Artifact | Stable identity binds |
|---|---|
| `label_set.v1` | formula, decision time, horizon, eligibility, null policy, upstream adjusted-price bindings |
| `model_dataset.v1` | factor set/version/generation, label generation, feature list/order, universe binding, split policy, missing policy |
| `model_definition.v1` | reviewed algorithm family, hyperparameters, seed policy, feature schema, dataset compatibility |
| `model_run.v1` | definition generation, dataset generation, code fingerprint, environment lock digest, deterministic controls |
| `model_artifact.v1` | trained bytes checksum, runtime/version provenance, run generation |
| `qlib_dataset_export.v1` | dataset generation, exported files/checksums, Qlib version, calendar/universe provenance, feature mapping, exporter fingerprint |
| `qlib_init_receipt.v1` | export generation/digest, resolved provider URI digest, file-list/calendar/instrument/feature-mapping digests, Qlib import path/version, cache root, no-external-source assertion |
| `prediction_set.v1` | model artifact generation, dataset/input generation, decision time, score/rank semantics, eligibility |
| `model_quality_report.v1` | report schema/version, binding type, bound artifact family/generation, policy/status/checks, producer fingerprint, report checksum |

Reports, calendar/instrument exports, split manifests, and feature schemas are
durable artifacts when published. Each must use one of the identity contracts
above or declare its own reviewed family before publication.

`run_id` may appear in manifests only outside the stable generation payload.

### Normative generation payload tables

The loader derives every stable generation from the complete semantic
manifest, excluding exactly `generation_id`, `manifest_digest_sha256`, and the
family-specific fields below. Numbers use canonical JSON serialization with no
non-finite values. Logical-fingerprint fallback means the recorded logical
fingerprint is part of content identity when byte equality is not required.

| Family | Excluded run-local fields | Logical fallback | Artifact checksum relationship |
|---|---|---|---|
| `label_set.v1` | `run_id`, `created_at` | no | `data_checksum_sha256` |
| `model_dataset.v1` | `run_id`, `created_at` | yes | `data_checksum_sha256` |
| `model_definition.v1` | `run_id`, `created_at` | no | none |
| `model_run.v1` | `run_id`, `created_at` | no | none |
| `model_artifact.v1` | `run_id`, `created_at` | no | `artifact_checksum_sha256` |
| `qlib_dataset_export.v1` | `run_id`, `created_at` | no | `files[].checksum_sha256` |
| `qlib_init_receipt.v1` | `run_id`, `created_at` | no | `cache_diff_checksum_sha256` |
| `prediction_set.v1` | `run_id`, `created_at` | yes | `data_checksum_sha256` |

`qlib_dataset_export.v1.files[].checksum_sha256` is individually required and
the file-list checksum binds their order and contents. A schema version that
changes any payload rule above requires a new contract version.

## 5. Label Contract

Supervised training requires an explicit reviewed label set. Labels are not
factors and cannot be embedded silently in a factor partition.

A label manifest must declare:

- name and semantic version;
- instrument/date primary key;
- decision date/time convention;
- horizon in trading days;
- formula, units, and adjustment basis;
- benchmark or excess-return basis if applicable;
- eligibility rules for suspension, listing age, limit events, and delisting;
- null policy for insufficient future observations;
- upstream adjusted price/factor bindings with generation IDs and checksums.

Rules:

1. Labels use observations strictly after the decision time unless a same-close
   convention is explicitly named and reviewed.
2. Rows without complete post-decision observations remain null; forward filling
   is forbidden.
3. Delisted instruments require a terminal-return rule before production use.
4. A-share T+1 and tradability assumptions are not implicit in labels; they
   belong to an explicit downstream execution/tradability policy.

Initial candidate: five-trading-day adjusted-close return, with the last five
sessions null per instrument. The frozen formula for this candidate is
`adjusted_close[decision_date + 5 trading sessions] /
adjusted_close[decision_date] - 1`, using the governed Asia/Shanghai trading
calendar. `decision_date` is excluded from the return window. A row remains null
unless all six endpoint observations exist and pass eligibility. Delisting or
terminal suspension requires a reviewed terminal-return policy that names its
final price source and availability time; absent that policy, terminal rows are
null. Benchmark/excess-return labels are explicitly excluded from the first
release.

## 6. Dataset Contract

A model dataset is immutable and independently reproducible. It must bind:

- dataset name and semantic version;
- exact ordered feature list and source factor definitions;
- factor set/version plus accepted factor generation(s);
- label set/version plus generation;
- universe snapshot generation and PIT validity interval;
- train/validation/test date ranges;
- purge and embargo intervals derived from the maximum label horizon;
- row eligibility predicate and expected columns/dtypes;
- missing-value policy and forbidden transforms;
- random seed derivation policy where sampling exists;
- row count, key uniqueness, checksums, and deterministic logical fingerprint.

Dataset builders may read only accepted factor partitions through a typed
accepted-factor query API, published label manifests, and explicitly bound
universe artifacts. They must not enumerate arbitrary files or read
staging/quarantine paths. A factor manifest without a required universe binding
is model-ineligible unless the dataset manifest independently binds and verifies
an approved universe snapshot.

A `feature_schema.v1` subcontract must define each column's name, source factor,
dtype from a closed vocabulary, unit, null semantics, transform status, forbidden
cross-sectional transforms, and fingerprint normalization. Dataset validation
must reconcile declared schema/order against actual columns and values.

Time-based splitting is mandatory. Random row splitting is forbidden. Every
feature row dated `D` may join only to a label whose decision date is also `D`.
A training observation is ineligible whenever its label outcome interval
`[D+1, D+h]` intersects any evaluation outcome interval `[E+1, E+h]`. After that
purge, apply the declared global or per-universe embargo in trading sessions.
Split manifests must record inclusive decision-date bounds, horizon `h`, purge
rule, embargo rule, and affected row counts.

## 7. Qlib Runtime Boundary

Qlib is the selected training engine, not a governance layer.

Allowed:

- consuming one immutable model-dataset export snapshot;
- implementing dataset loaders, model classes, training loops, metrics, and
  serialization through its APIs;
- writing temporary Qlib caches under a generated runtime directory.

Forbidden:

- using Qlib expression factors as unregistered model features;
- allowing Qlib to fetch provider data during training/inference;
- using Qlib calendars/universes as authoritative PIT evidence unless they are
  byte-for-byte exports of governed bindings;
- storing `.bin` files in Git;
- treating a Qlib cache hit as input immutability proof.

The adapter must produce:

1. `qlib_dataset_export.v1`: mapping from governed dataset generation to the
   exported snapshot, including file list, checksums, Qlib version, calendar/
   universe provenance, feature mapping, and exporter code fingerprint.
2. A typed initialization result proving that Qlib initialized against the
   exported snapshot and not another provider URI.

The export manifest must contain the physical layout, complete file list,
per-file SHA-256, provider URI digest, calendar/instrument digests, feature
mapping digest, exporter fingerprint, serialization profile, and empty-cache
precondition. A typed `QlibInitReceipt.v1` must record resolved provider URI
digest, export manifest digest, file-list/calendar/instrument/feature-mapping
digests, Qlib import path/version, initialized cache root, post-run cache diff,
and an assertion that no ungoverned provider source was configured. The runtime
must fail if a required governed file changes or an ungoverned source/cache is
detected.

For supervised model slices, this section supersedes architecture §11's
canonical direct-export prototype. Existing `QlibExporter` output is not a valid
`qlib_dataset_export.v1`; reuse requires a new migration contract and negative
tests.

Qlib is a Phase 2A integration dependency. It must not be added to project
optional development/model extra until the Phase 2A interface is frozen and all
its negative tests pass. Production dependencies may be added only when Phase 3+
actually requires the runtime.

## 8. Model Definition and Run

A reviewed model definition freezes semantic intent:

- model family and implementation binding;
- loss/objective;
- hyperparameters;
- feature schema and ordering;
- allowed dataset compatibility rule;
- seed policy;
- metric definitions and selection rule;
- serialization format.

A model run records execution facts:

- resolved definition and dataset generations;
- code fingerprint;
- Python/runtime package lock digest;
- thread/parallelism and nondeterminism controls;
- start/end timestamps and run ID outside stable identity;
- train/validation metrics;
- metric report checksum;
- dataset slice hashes;
- artifact location/checksum and serialization version.

The reproducibility envelope must define code-fingerprint inputs, lockfile
digest source, package/runtime provenance, backend versions, hardware-sensitive
controls, failure retention, and comparison mode.

Same content plus environment must reproduce the same artifact bytes when the
runtime guarantees determinism. If a runtime cannot guarantee byte equality, the
definition must declare a logical-fingerprint mode and tolerance.

## 9. Artifact Store

Model artifacts are immutable, checksummed, and physically bound to their
identity. Minimum layout:

```text
models/
  factor_set=<name>/factor_version=<semver>/
    model_set=<name>/model_version=<semver>/
      run_generation=<run_generation_id>/
        artifact_generation=<artifact_generation_id>/
          artifact.bin
          artifact_manifest.json
reports/model_v1/<bound_generation_id>/report.json
predictions/
  prediction_set=<prediction_generation_id>/
    date=<YYYY-MM-DD>/data.parquet
    manifest.json
```

Readers must reject:

- absent or malformed manifest;
- checksum mismatch;
- generation/path mismatch;
- missing or tampered quality/report binding;
- unsupported serializer/runtime version;
- quarantine/staging paths presented as accepted artifacts;
- overwrite attempts.

Publication uses staging plus atomic rename and a scoped lock. Rejected runs go
to manual-review quarantine and are invisible to accepted readers.
Quarantine manifests must record reason taxonomy, input generations, checksums,
review status, and retention policy. In-place promotion is forbidden; recovery
requires a new run/artifact generation.

### External Quality Review Contract

Publication quality decisions are external inputs, not publisher outputs.
`model_quality_report.v2` binds a reviewer-approved decision to the stable
subject generation using `subject_content_sha256` and
`review_signature_sha256`. Publishers may mechanically bind an unchanged
decision but cannot create, mutate, or re-sign review conclusions. The bound
report checksum is recorded by the manifest and stored in an immutable
governance root; cache directories are never quality-report storage.

Because report binding participates in durable manifest identity, changing a
review decision requires republishing a new artifact generation under the v1
republish rule; existing partitions are immutable and cannot be edited.

## 10. Quality Gate

Every publication binds an immutable quality report. First-release checks:

- dataset key uniqueness and row-count reconciliation;
- feature schema/order equality;
- null-rate threshold per feature and label;
- coverage minimum per cross-section/date;
- split leakage: purge/embargo violation fails closed;
- label availability and terminal-null accounting;
- deterministic controls present;
- artifact readback reconciliation;
- prediction rank/score finite-value and eligibility checks.

Quality policies follow the factor-layer pattern: `reject_all` or
`accept_with_warnings`, selected in a reviewed definition and enforced on both
publication and read.

`model_quality_report.v1` must declare its schema/version, binding type,
bound artifact family/generation, policy/status, checks with thresholds and
observed values, producer/code fingerprint, and report checksum. Threshold
defaults and aggregation rules come from reviewed definitions; warning policy
may accept only checks explicitly marked warning-level.

## 11. Prediction Contract

Prediction output must contain:

- instrument and decision datetime;
- model generation/artifact checksum;
- input dataset generation;
- score/rank/probability columns actually produced;
- declared output-column set exactly matching actual columns;
- units, direction, ranking scope, tie policy, and normalization;
- eligibility status;
- visible cutoff;
- immutable manifest and checksum.

Predictions are append-only. Historical predictions cannot be overwritten or
recomputed into the same generation.

## 12. Acceptance Tests

Minimum gates:

Slash phase notation means every listed phase must eventually provide its own
sub-item evidence. Before any phase exit, each row must be expanded into sub-IDs
such as `M1a-factor-manifest-missing`, `M1b-factor-checksum-tamper`, and
`M1c-wrong-generation-binding`, each with one owning phase, blocked-by list,
exact test ID, fixture path, evidence path, and status.

| ID | Requirement | Owning phase(s) | Test ID placeholder | Status |
|---|---|---|---|---|
| M1 | Unpublished/tampered/misbound factor input rejects dataset build. | 0 contract, 1 runtime | TBD-M1a-c | Blocked |
| M2 | Future observation cannot enter feature or label row. | 0 contract, 1 runtime | TBD-M2a-b | Blocked |
| M3 | Split purge/embargo violations fail closed. | 1 contract, 1 runtime | TBD-M3a-b | Blocked |
| M4 | Dataset rebuild is byte/logically identical under locked environment. | 1 contract, 4A certification | TBD-M4a-b | Blocked |
| M5 | Changed factor/label/feature/split semantics create a new dataset generation. | 0 identity, 1 runtime | TBD-M5a-d | Blocked |
| M6 | Missing/tampered/wrong/failed report rejects publication/read. | 0 report contract, 4/5 runtime | TBD-M6a-d | Blocked |
| M7 | Same locked run reproduces deterministic model artifact or declared fingerprint. | 3 envelope, 4A certification | TBD-M7a-b | Blocked |
| M8 | Qlib uses only the governed exported snapshot: URI, calendar, universe, features, and cache. | 0 receipt contract, 2A runtime | TBD-M8a-e | Blocked |
| M9 | Prediction records exact model/dataset generations and rejects overwrite. | 0 identity, 5 runtime | TBD-M9a-d | Blocked |
| M10 | Quarantine/staging is invisible to accepted model readers. | 0 layout, 1/4/5 readers | TBD-M10a-c | Blocked |
| M11 | Manifest tampering, path mismatch, and checksum mismatch fail closed. | 0 identity, 4/5 readers | TBD-M11a-c | Blocked |
| M12 | Feature/schema/order change requires reviewed definition/version action. | 0 feature schema, 3 definition registry | TBD-M12a-b | Blocked |

No acceptance item may be marked executable while its implementation/test is
absent. Security/lineage items have no deferral.
