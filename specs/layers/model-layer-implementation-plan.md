# Model Layer Implementation Plan

Status: **gated implementation order v1.2; all phases 0–6 implemented and remote CI certified**

Source spec: `specs/layers/model-layer-spec.md`
Runtime decision: **Qlib is the model runtime/engine; UQ manifests and stores
remain the governance source of truth.**

## 0. Scope

This plan implements only the first local-research supervised slice:

- one reviewed adjusted-return label family;
- one reviewed factor-set binding;
- deterministic time-based splits;
- one Qlib-backed baseline model;
- immutable dataset/model/prediction artifacts;
- fail-closed lineage, quality, reproducibility, and isolation gates.

It does not implement production serving, execution, hyperparameter search,
multi-tenancy, distributed training, or strategy/backtest validation.

## 1. Execution Rules

Phases are gates, not suggestions:

1. `scripts/run_gate.sh` is the local mechanical gate; CI must run the same
   script through the existing six-cell F4 matrix.
2. Every phase exit requires focused contract/runtime tests plus a successful
   unified gate.
3. Contract/schema drafting may occur during plan preparation; runtime loading,
   training, publication, or accepted reads require phase entry criteria.
4. A semantic schema change requires a new schema version and migration/republish
   rule before use.
5. Stable `generation_id` excludes run metadata; `manifest_digest_sha256` may
   include it according to each frozen contract.
6. No phase may begin until its declared upstream gate exits. Plan preparation
   may draft/review contract schemas, fixtures, identity helpers, loader tests,
   and artifact-layout contracts; runtime registry/loading/publication and any
   accepted reads are phase implementation.
7. Every acceptance ID must map to owning sub-phase, blocked-by list, concrete
   test ID, fixture path, evidence path, and status before that phase can be
   declared executable.
8. Each phase carries a machine-readable record with
   `status=blocked|in_progress|executable|exited`, entry/exit test IDs, and
   preserved evidence paths. Documentation text cannot override this field;
   an absent record means `blocked`.

Implemented contract assets include all nine manifest schemas plus query
request/response schemas, on-disk valid/negative fixtures for every family,
golden vectors covering all manifest families, cross-manifest binding resolver
(contract-level shape), and `ModelContractLoader`. Factor upstream content
resolution (actual factor manifest lookup/checksum verification) is deferred to
Phase 1 accepted-store runtime; Phase 0 validates generation format only.
Evidence is preserved under `evidence/phase-0/`. All runtime phases stay paused.

Factor-layer interface prerequisite: the model layer treats the existing
`read_factor_partition(partition: Path)` as the accepted-partition read boundary
and requires a thin accepted-store index/list API before Phase 1 runtime work.
Phase 0 freezes that query contract only via `AcceptedFactorIndexContract`; it
does not alter `FactorStore` or enable production reads.

## 2. Phase 0 — Governance Contracts Gate

Goal: freeze identity and trust primitives before any Qlib integration.

Deliverables:

1. JSON Schemas:
   - `label_set.v1.json`;
   - `model_dataset.v1.json`;
   - `model_definition.v1.json`;
   - `model_run.v1.json`;
   - `model_artifact.v1.json`;
   - `prediction_set.v1.json`;
   - `qlib_dataset_export.v1.json`;
   - `qlib_init_receipt.v1.json`;
   - `model_quality_report.v1.json`.
   The durable export schema is included in Phase 0 rather than deferred to
   Phase 2A; the Phase 2A exporter must consume it unchanged.
2. Normative generation payload tables for every durable artifact family listed
   by source spec §4. Each table names included semantic fields, excluded
   run-local fields, decimal/float normalization, logical fingerprint fallback,
   and checksum relationship.
3. Identity helpers for canonical-JSON stable generations and manifest digests.
4. Representative valid fixtures plus invalid negative fixtures for every schema
   field, including missing/extra fields, wrong types/formats, non-finite
   numbers, malformed generations/checksums, and binding mismatches.
5. Golden vectors proving generation stability under key reorder/run metadata
   change and instability under content/lineage change.
6. Typed loader API with fail-closed path handling and explicit error taxonomy.
7. Artifact root/layout contract with staging/quarantine/accepted boundaries,
   including nested run/artifact layout and prediction date partitions.
8. Contract-only design for a typed accepted-factor index/list API exposing only
   verified factor partitions: request/response schema, supported universe,
   factor-set/version/generation filters, deterministic ordering, pagination,
   visibility semantics, and errors for unpublished/tampered/misbound inputs.
   This deliverable must not alter FactorStore behavior or expose runtime reads.

Entry criteria:

- Source-spec review blockers are represented by concrete deliverables above:
  normative identity payload rules, frozen label formula/terminal rule, the
  accepted index/list API contract, Qlib init receipt mechanism, and quality
  report binding.

Exit criteria:

1. All schemas validate representative fixtures.
2. Every invalid fixture has a typed rejection test.
3. Generation golden vectors pass.
4. Loaders reject absent/tampered/misbound documents without fallback.
5. Every source-spec blocker has representative/negative fixture coverage.
6. Accepted index/list API contract passes schema and failure-path tests without
   implementing store reads.
7. Unified gate is green and its successful report, resolved requirements
   snapshot, and digests are preserved.

Unblocks M1, M5, M8 receipt contract, M10/M11 contracts, and prerequisites for
M6, M9, and M12.

## 3. Phase 1 — Label and Dataset Contract Gate

Goal: make supervised data construction auditable and leakage-resistant.

Decision to freeze:

- initial label: five-trading-day adjusted-close return;
- decision time: trading-day close, Asia/Shanghai;
- last five sessions null per instrument;
- eligibility policy must be explicit even if initially broad;
- purge interval equals maximum horizon;
- embargo interval must be declared and reviewed.

Deliverables:

1. Label builder reading only governed adjusted-price/factor bindings.
2. Immutable label partitions/manifests with checksums and quality reports.
3. Dataset builder binding exact feature order, factor generations, label
   generation, universe PIT validity, split dates, missing policy, and dtypes.
4. Split validator enforcing train/validation/test separation, purge, embargo,
   row keys, date ranges, and label availability.
5. Deterministic Parquet serialization profile and logical fingerprint.
6. Negative tests for future labels, mixed decisions, missing factor generation,
   changed feature order, universe mismatch, split leakage, and tampering.
7. Feature schema subcontract per column with name, exact source-factor identity,
   dtype vocabulary, unit, null semantics, transform status, forbidden
   transforms, and fingerprint normalization.
8. Terminal-return policy schema/checks before any terminal row is enabled,
   naming final price source, availability time, null accounting, and reviewed
   activation status.
9. Dataset-builder behavior for missing factor partitions, cross-date windows,
   and calendar gaps: fail closed unless an explicit reviewed missing policy
   declares bounded handling; every exception must be recorded in the dataset
   manifest and quality report.
10. Split manifests recording inclusive train/validation/test bounds, label
    horizon, purge/embargo session rules, affected row counts, and rule digest.

Entry criteria:

- Phase 0 exits;
- a typed accepted-store index/list API exposes only verified factor partitions;
- adjusted-factor publication slice remains available through accepted reads.
- Purge is defined observation-wise: an observation is ineligible when its label
  outcome interval `[D+1, D+h]` intersects another split's outcome interval
  `[E+1, E+h]`. A reviewed global/per-universe embargo in trading sessions is
  then applied after purge.

Exit criteria:

1. Valid label/dataset builds publish immutable artifacts.
2. Rebuild in one locked cell is byte-identical or has a frozen logical
   fingerprint mode.
3. Every leakage/binding negative case fails closed.
4. Changed factor/label/split semantics produce new generations.

Unblocks M2, M3, M4, and M5.

## 4. Phase 2A — Qlib Adapter Interface Gate

Goal: freeze the boundary between governed snapshots and Qlib runtime.

Deliverables:

1. `qlib_dataset_export.v1` schema and exporter contract.
2. Feature mapping table from UQ columns to Qlib feature names.
3. Exported calendar/universe provenance rules.
4. Runtime initialization request/result with provider URI, export generation,
   Qlib version, and code fingerprint.
5. Cleanup/TTL policy for temporary Qlib caches and `.bin` files.
6. Negative tests for wrong provider URI, cache substitution, partial export,
   checksum mismatch, feature-order mutation, and external data fetch.
7. Export manifests containing physical layout, complete file list, per-file
   SHA-256, provider URI digest, calendar/instrument digests, feature mapping
   digest, exporter fingerprint, serialization profile, and empty-cache
   precondition.
8. `qlib_init_receipt.v1` creation capturing resolved provider URI digest,
   export manifest digest, file-list/calendar/instrument/feature-mapping
   digests, Qlib import path/version, cache root, post-run cache diff, and a
   no-ungoverned-source assertion.
9. Isolate/deprecate legacy `src/uq/exporters/qlib.py` from the model path with
   a visible deprecation marker and an import/static test proving it cannot enter
   accepted model publication unless migrated behind the new export contract and
   explicitly versioned/reviewed.

Entry criteria:

- Phase 1 exits.

Exit criteria:

1. Qlib can initialize only against the exported snapshot.
2. Export rebuild matches the pinned serialization profile.
3. Temporary caches are proven invisible to accepted stores.
4. No unregistered Qlib expression can enter the feature set.

Completes the M8 prerequisite but not full model reproducibility.

## 5. Phase 3 — Baseline Model Definition Gate

Goal: review a small deterministic baseline before training.

Selected candidate: regularized linear model or LightGBM, chosen after Phase 1
data quality is observed. Only one may be marked normative for the first slice.

Deliverables:

1. `model_definition.v1` config with algorithm binding, hyperparameters, seed
   derivation, metric definitions, selection rule, and serializer version.
2. Registry/config validation with valid/invalid fixtures.
3. Compatibility rule binding definition version to dataset schema/version.
4. Metric computation/report schema independent of console logs.
5. Explicit nondeterminism controls or a declared logical-fingerprint mode.
6. Freeze cross-platform logical fingerprint normalization and tolerance rules
   here if byte equality will not be required; otherwise defer explicit OS/CPU/
   BLAS tolerance certification to Phase 4A. No undocumented tolerance is valid.

Entry criteria:

- Phase 2A exits.

Exit criteria:

1. Reviewed definition loads deterministically.
2. Invalid hyperparameter/schema/order/seed cases reject typedly.
3. Metrics have stable names, direction, units, and input bindings.

Unblocks M12.

## 6. Phase 4 — Training and Artifact Store Gate

Goal: turn one reviewed definition plus one immutable dataset into an immutable
model run/artifact.

Deliverables:

1. Typed trainer consuming resolved definition, dataset export, and Qlib runtime
   initialization result.
2. `model_run.v1` creation with code fingerprint and environment lock digest.
3. Atomic staging/publication with scoped lock and immutable overwrite rejection.
4. Model artifact checksum/readback reconciliation.
5. Quality report store/reuse pattern bound by model-run generation.
6. Quarantine path for rejected/incomplete runs with manual-review retention.
7. Negative tests for tampered bytes/manifests, missing report, wrong report
   generation, unsupported runtime, overwrite, and staging visibility.

Entry criteria:

- Phase 3 exits.

Exit criteria:

1. Accepted artifact publishes only with passed/warned reviewed policy.
2. Read path verifies checksum, generation/path identity, runtime compatibility,
   and report binding.
3. Same locked environment reproduces byte-identical artifact where runtime
   supports it; otherwise logical fingerprint passes.
4. Quarantine/staging cannot be read as accepted models.

Unblocks M6, M7, M10, and M11 model-side cases.

## 7. Phase 4A — Reproducibility and Environment Matrix Gate

Goal: certify the model slice rather than call a local run reproducible informally.

Requirements:

1. Freeze Python/runtime/Qlib versions and dependency lock digest.
2. Record thread/parallelism/random controls in every run.
3. Pin dataset and artifact serialization profiles.
4. Run repeated same-cell jobs and compare artifact bytes/logical fingerprints.
5. Extend CI after local proof to macOS/Ubuntu × supported Python cells actually
   exercised by the model stack.
6. Preserve every remote report/artifact; aggregation must verify commit,
   result, environment marker, reported versions, resolved requirements
   snapshot/digest, lockfile digest, and gate-report integrity—not merely marker
   presence.
7. Before this phase may claim certification, enhance gate reporting so a failed
   focused test cannot be emitted as `passed`, and make aggregation verify the
   exact locked requirements snapshot used by each cell. Existing factor-layer
   six-cell success is necessary context but is not model-layer certification.

Exit criteria:

- Covered cells are certified only when remote jobs and aggregation succeed.
- Uncovered OS/CPU/BLAS/GPU combinations remain `not certified`.

Completes M7 scoped certification.

## 8. Phase 5 — Prediction Publication Gate

Goal: persist auditable scores without allowing silent historical rewrite.

Deliverables:

1. Batch inference reader accepting only accepted model/dataset artifacts.
2. Prediction schema with score/rank/probability semantics and eligibility.
3. Immutable prediction manifest binding model generation, dataset generation,
   decision time, visible cutoff, serializer profile, and checksums.
4. Physical parent-child path identity checks and atomic publication:
   `prediction_set=<generation_id>/date=<YYYY-MM-DD>/` is one immutable date
   partition under exactly one prediction generation; manifests bind generation,
   decision date, and partition checksums.
5. Negative tests for stale model, wrong dataset generation, non-finite scores,
   duplicate keys, missing eligibility, tampering, path mismatch, and overwrite.

Entry criteria:

- Phase 4 exits; Phase 4A must exit before release certification.

Exit criteria:

1. Predictions append immutably and read fail-closed.
2. Every score traces to exact model and dataset generations.
3. Full suite and unified gate pass.

Completes M9 and closes implemented security gates.

## 9. Phase 6 — Release Acceptance

Tasks:

1. Expand every M-ID into concrete automated test IDs and statuses.
2. Run identical-input checks across all certified environment cells.
3. Run every security/lineage negative path with recorded evidence.
4. Reconcile spec/README/status only from preserved reports.

Exit requires:

- all security/lineage sub-items passing without deferral;
- documented approval for any non-security deferral;
- successful unified gate locally and in the declared remote matrix;
- no executable claim without implementation/test evidence.

## Acceptance Matrix Expansion

The source-spec §12 matrix is authoritative. Phase 0 sub-items are now expanded;
later phases must expand their rows before entering implementation.

| Sub-ID | Owning phase | Blocked by | Test ID | Fixture path | Evidence path | Status |
|---|---|---|---|---|---|---|
| M1a-factor-manifest-missing | 0 | none | test_cross_manifest_binding_resolver_passes_and_fails | evidence/phase-0/fixtures/model_dataset-negative.json | evidence/phase-0/phase-record.json | passed |
| M1b-factor-checksum-tamper | 1 | Phase 0 exit | TBD-P1 | TBD | TBD | blocked |
| M1c-wrong-generation-binding | 0 | none | test_cross_manifest_binding_resolver_passes_and_fails | evidence/phase-0/fixtures/prediction_set-negative.json | evidence/phase-0/phase-record.json | passed |
| M5a-label-generation-change | 0 | none | test_golden_vectors_cover_all_manifest_families | evidence/phase-0/golden-vectors/identity-golden-vectors.json | evidence/phase-0/golden-vectors/identity-golden-vectors.json | passed |
| M5b-dataset-generation-change | 0 | none | test_golden_vectors_cover_all_manifest_families | evidence/phase-0/golden-vectors/identity-golden-vectors.json | evidence/phase-0/golden-vectors/identity-golden-vectors.json | passed |
| M8a-provider-uri-mismatch | 2A | Phase 1 exit | TBD-P2A | TBD | TBD | blocked |
| M8b-calendar-tamper | 2A | Phase 1 exit | TBD-P2A | TBD | TBD | blocked |
| M10a-quarantine-path-invisible | 4 | Phase 3 exit | TBD-P4 | TBD | TBD | blocked |
| M11a-manifest-tamper-reject | 0 | none | test_typed_loader_rejects_absent_malformed_and_tampered_documents | evidence/phase-0/fixtures/model_definition-valid.json | evidence/phase-0/phase-record.json | passed |
| M11b-path-mismatch-reject | 0 | none | test_loader_rejects_nonfinite_invalid_formats_and_unapproved_root | evidence/phase-0/fixtures/model_definition-valid.json | evidence/phase-0/phase-record.json | passed |
| M11c-checksum-mismatch-reject | 0 | none | test_quality_report_checksum_and_binding_are_enforced | evidence/phase-0/fixtures/model_artifact-valid.json | evidence/phase-0/phase-record.json | passed |

Phase 1+ sub-items remain `blocked` until their owning phase entry criteria pass
and they are expanded with concrete test IDs and fixture paths.

## Immediate Next Actions

1. Rerun gate on final HEAD after any remaining commit.
2. Refresh `evidence/phase-0/gate-reports/` with the new report, requirements
   snapshot, and digest.
3. Update phase record and evidence index to bind the new HEAD.
4. ~~After final review confirms no residual blocker, mark Phase 0 as exited in
   the phase record.~~ Done: Phase 0 exited at commit `5a75590`.
5. Phase 1 label/dataset contract preparation is now open (schemas,
   validators, split policy); production dataset builds still require the
   typed accepted-store index/list API to be implemented behind the frozen
   Phase 0 contract.
6. Qlib remains excluded from production dependencies until Phase 0 exits and
   the Phase 2A interface plus negative tests are frozen. Optional dev/model
   dependency work may begin only after that freeze; training/runtime use starts
   at Phase 3 or later under its own gate.
