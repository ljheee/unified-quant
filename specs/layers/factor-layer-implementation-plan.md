# Factor Layer Implementation Plan

Status: **gated implementation order v0.9; plan-exit contract preparation complete; Phase 0/1 gates exited**  
Source spec: `specs/layers/factor-layer-spec.md`  
Current boundary: `FactorContext`, canonical-v2 publication, adjustment snapshot governance, `FactorRegistry`, factor manifest validation, and universe/quality artifact governance are implemented. `FactorEngine`, `FactorStore`, executable factors, and immutable factor partitions are not.

## 0. Scope and Non-Goals

This plan covers only the first local-research factor layer described by the source spec. It does not cover:

- machine-learning feature selection, neutralization, ranking normalization, or online computation;
- fundamental/analyst factors or announcement-driven feature datasets;
- production multi-tenant storage, remote object stores, or distributed compute;
- replacing canonical ingest, routing, quality merging, or DatasetBuilder;
- historical universe reconstruction. Universe inputs must be explicit governed bindings; this plan never treats a current list as PIT membership evidence.

The first release target is a small deterministic raw-price factor set, followed by adjusted returns only after adjustment lineage is governed.

## 0A. Source-Spec Coverage Map

The implementation plan intentionally narrows execution order but must preserve all normative source-spec sections:

| Source-spec section | Plan handling |
|---|---|
| §1 Purpose / §2 Boundaries | Governs component boundaries and non-goals; enforced across phases. |
| §3 Factor contract and versioning | Phase 2 registry/config governance; adjusted prerequisites remain deferred to Phase 4. |
| §4 Decision-time semantics | Applied in Phase 2A request/window contracts and every publication phase. |
| §5 Price adjustment rule | Phase 1 adjustment lineage and Phase 4 adjusted inputs. |
| §5A Merge Policy Boundary | Explicitly out of scope for factor-layer v0; canonical routing owns merge policy. No factor implementation may bypass it. |
| §6 Identity and Store Layout | Phase 5 immutable Hive-like store. |
| §6A Manifest and generation contract | Sole machine schema in `config/schemas/manifests/factor_manifest.v1.json`; §6A is the human summary. Phase 2 enforces identities. |
| §7 Initial Factor Set | Split by input readiness: raw-price subset in Phase 3, adjusted subset in Phase 4. |
| §8 Missing/non-tradable rows | Phase 3 row reconciliation and null policy. |
| §8A Determinism | Phases 3, 4A, and 5 fingerprints/checksums. |
| §9 Quality Gate | Phase 2 quality report contract plus Phase 5 runtime gate/publication. |
| §10 Public API Shape | Normative simplified facade expanded through Phase 2A internal `FactorComputeRequest`. |
| §11 Acceptance Tests | Acceptance matrix remains authoritative; statuses update as phases exit. |
| §12–§14 Non-goals, run visibility, adjustment versioning | Carry as global constraints into later gates. |

No omitted source-spec section is authorization to skip its constraint; if a plan gap is found, pause the affected gate and amend this map.



## 1. Execution Rules

This is a gated plan, not a parallel roadmap.

1. A phase may start only when every entry criterion for that phase passes.
2. Phase ordering is strict:
   - Phase 0 must exit before any later phase;
   - Phase 1 may start only after Phase 0 exits;
   - Phase 2-contract may start only after Phase 0 exits and may design schemas that reference the future Phase 1 snapshot identity, but representative end-to-end fixture validation remains blocked until Phase 1 exits;
   - Phase 2-runtime, including a typed registry API, may start only after Phase 0 and Phase 1 exit;
   - Phase 2A may start only after Phase 0 and Phase 1 exit;
   - no executable factor calculation, store code, or promotion path may open before its declared contract phase exits.
3. Scope cannot leak across phases. In particular:
   - adjusted factors must not be implemented before the Phase 4 input contract freezes;
   - raw-price engine work in Phase 3 must not add adjustment derivation or an implicit adjusted close;
   - store work must not begin before the factor manifest and engine-interface contracts exist;
   - Phase 2A may begin only after Phase 0/1 exit; it must finish before any executable factor calculation is opened.
4. Every phase ends with:
   - all listed exit criteria passing;
   - the selected locked environment is green through the implemented mechanical runner `scripts/run_gate.sh`. The preferred runner uses `uv sync --locked` and `uv export --format requirements-txt --hashes`; when `uv` is unavailable it may use only the committed lockfile-fallback exporter, which produces a hash-pinned requirements artifact from `uv.lock` in an isolated virtual environment. The uv path generates `.gate/requirements.lock.txt` via `uv export --format requirements-txt --hashes`. Both paths record its SHA-256 and write `.gate/gate-report.json` with runner identity (`uv` or `lockfile-fallback`), Python version, extras, lockfile digest, requirements digest, platform, UTC timestamp, test result, and git commit. CI invokes this same runner and uploads/preserves all three gate artifacts. `.gate/` is ignored as local evidence, not repository data.
   - the acceptance matrix in `factor-layer-spec.md` updated to distinguish executable tests from blocked tests;
   - a focused review of new contracts and failure paths.

5. Gate-plan exit requires all Phase 0/1 contract artifacts to exist and pass automated validation before implementation work opens:
   - `canonical_manifest.v2.json`, migration/compatibility policy, identity golden vectors, and negative tests;
   - `scripts/run_gate.sh` and a successful machine-readable gate report;
   - `adjustment_snapshot.v1.json`, storage layout, visibility/as-of policy, typed reader interface, and formula golden vectors;
   - `factor_manifest.v1.json`, `universe_snapshot.v1.json`, and `quality_report.v1.json` with representative valid/invalid fixtures;
   - a normative engine API decision and acceptance matrix expanded to phase, blocked-by, test ID, and status.
   Until then this remains a gate plan; phases define implementation order but do not certify exit.

Current gate status:

- Phase 0/1 gates have exited with green runtime evidence.
- Phase 2 has exited after independent review remediation: reviewed basic-v1 registry, typed manifest identities, universe governance, quality binding, field decision table, and negative acceptance evidence are green.
- Phase 2A has exited: typed request/result contracts, deterministic plan compilation, facade equivalence, and typed invalid-request tests are green. Calculation remains intentionally unimplemented.
- Phase 3 raw-price factors are the next gate; `FactorStore` and immutable publication remain later gates.
- Later phases remain gated by their declared contracts and acceptance criteria.
Implementation status correction: the original Phase 0/1 blockers have been closed by canonical-v2 runtime work, persisted adjustment snapshot runtime, external-anchor/path validation, governed visibility, and exchange-style formula coverage. Gate reports are generated under ignored local `.gate/` evidence after each working-tree change.

## Phase 0 — Canonical Manifest Governance Gate

Goal: make canonical lineage trustworthy enough to support factor publication.

Entry criteria:

1. Canonical manifest schema v1 exists.
2. Canonical publish/read tests pass.

Tasks:

1. Add an externally supplied expected trust anchor to `ManifestFirstReader`. Keep the embedded anchor as an integrity check only; document that it cannot detect a malicious writer without out-of-band trust.
2. Validate the complete physical-path triple: requested dataset, schema version, and partition date must equal manifest values.
3. Formalize identity separation through an additive `canonical-v2` contract; do not reinterpret already published `canonical-v1` partitions. Required rules:
   - `manifest_digest_sha256`: canonical-JSON SHA-256 over the complete run-local manifest, including `run_id`, `created_at`, and trust metadata;
   - `generation_id`: canonical-JSON SHA-256 over stable content fields and explicitly excludes `run_id`, `created_at`, trust metadata, and `manifest_digest_sha256`;
   - writer/reader/schema/spec share one golden-vector fixture;
   - v1 partitions remain readable only under a compatibility reader that records their legacy identity and cannot enter factor publication until migrated or republished as v2;
   - same logical content with different run metadata keeps `generation_id` unchanged while `manifest_digest_sha256` changes.
4. Freeze a separate `canonical_migration.v1.json` audit artifact for every v1-to-v2 transition. Required fields are source dataset/schema/path, legacy manifest digest and legacy generation ID, source data/schema checksums, target dataset/schema/path, target content generation ID, target manifest digest, migration algorithm version, action (`read_only_legacy | republish_v2`), decision time, run-visible cutoff, operator/runner identity, and approval reference. Republishing MUST create a new immutable v2 partition; it MUST NOT modify, move, delete, or reuse the v1 path. The mapping is append-only and itself carries a checksum.
5. Enable JSON Schema format checking for UUID/date-time fields; validate partition dates as real calendar dates.
6. Require dtype map keys to equal frame columns exactly.
7. Record structured package/version provenance in addition to the current component fingerprint. Include the project/package version, Python, pandas, PyArrow, NumPy, and relevant dependency versions; keep the existing `code_fingerprint` as the implementation-content digest.

Exit criteria:

1. Reader rejects a fully regenerated self-consistent manifest when the external expected anchor differs.
2. Reader rejects a valid partition copied under another date path.
3. A `canonical-v2` partition has identical `generation_id` when only `run_id` or `created_at` changes, while `manifest_digest_sha256` changes.
4. Invalid UUID/date-time/calendar dates/dtype maps fail explicit validation tests.
5. Legacy `canonical-v1` reads cannot satisfy factor-publication entry criteria unless explicitly migrated/republished and marked as such.
6. Migration negative tests prove rejection of missing/unmatched source checksum, mutated legacy manifest, reused/overwritten v2 destination, duplicate migration mapping, unapproved action, and tampered migration audit artifact.
7. A read-only legacy migration emits no new data partition and cannot be referenced by factor publication; a republish migration produces a fresh immutable v2 partition and an auditable mapping.
8. The selected locked test environment remains green through `scripts/run_gate.sh`.

Acceptance impact:

- Strengthens F1/F8/F9/F14 canonical prerequisites.
- Does not by itself complete any factor-layer acceptance item.

## Phase 1 — Adjustment Lineage Gate

Goal: define reproducible corporate-action inputs before adjusted factors exist.

Entry criteria:

1. Current adjustment derivation emits in-memory snapshot metadata; this is diagnostic evidence only and does not satisfy snapshot persistence.
2. Cash-action provider regression remains green.
3. The Phase 1 contract-first slice defines `adjustment_snapshot.v1.json`, storage layout, visibility/as-of policy, typed reader interface, and golden formula vectors before changing the runtime formula.
4. Existing tests that assert the rights-price-ignoring combined-action behavior are marked obsolete and excluded from the new gate; they must be replaced, not treated as passing coverage.
5. Formula repair is atomic with test replacement: the first Phase 1 implementation commit MUST add snapshot/golden fixtures and exchange-calibrated tests, remove or rewrite every legacy rights-ignoring assertion in that commit, record removed/replacement test IDs in `.gate/test-migrations/v1.json`, and leave `scripts/run_gate.sh` green. A recorded skip may cover only those obsolete assertions for at most that one transition commit; no unrelated test may be weakened.

Tasks:

1. Replace the current combined-action approximation with the exchange-style reference-price formula:
   `theoretical_ex_right_price = (pre_close - cash_per_share + rights_price * rights_ratio) / (1 + bonus_ratio + rights_ratio)`, where per-share/ratio values are converted consistently from the provider's ten-share convention; backward multiplier is `pre_close / theoretical_ex_right_price`.
2. Mark existing combined-action assertions that ignore rights price as obsolete, then replace them with golden tests covering rights-only, cash-only, bonus/transfer-only, cash plus shares, all combined, multiple events on one date, and events on non-sessions.
3. Freeze `adjustment_snapshot.v1`: JSON Schema, storage layout, event/effective-table artifacts, manifest, formula version, visibility time, checksum coverage, content generation ID, and typed reader API.
4. Define per-decision-date as-of binding: which snapshot/effective table may be used for each historical window.
5. Define cross-day window consistency: one factor computation window binds one immutable adjustment snapshot; mixing snapshots inside a return/volatility window is invalid.
6. Define restatement behavior: corrected events produce a new snapshot/generation, never mutate published factors, and dataset builders select one generation explicitly.

Exit criteria:

1. Rights-issue and combined-action factors match documented exchange-style formulas within declared tolerance against authoritative exchange/reference-price golden cases.
2. Snapshot artifacts are persisted, manifest-bound, checksum-verified, and readable as-of their governed visibility time.
3. A later correction cannot silently alter an already selected historical window.
4. Adjustment dependency identity is ready to be embedded in factor manifests.

Golden-source priority:

1. Exchange-published ex-right/reference price or official corporate-action notice.
2. A governed provider field explicitly identified as exchange-derived reference price.
3. A reviewed worked example using documented ten-share conversion, decimal policy, and hand calculation.

Every case records source/document/date, raw provider inputs, converted per-share/ratio values, expected reference price/multiplier, tolerance, reviewer, and retrieval time. Provider-only round-trip comparisons are supporting evidence, never authoritative exit evidence.

F6 gate: Phase 1 exits only when exchange-calibrated cash, share-change, rights, and combined-action tests pass. Provider-only comparisons are supporting evidence, not sufficient exit evidence.

Acceptance impact:

- Unblocks F6 for adjusted factors in Phase 4.
- Does not itself complete F6 because no adjusted-return implementation exists yet.

## Phase 2 — Registry and Factor Manifest Governance

This phase has two gates. **2-contract** freezes schemas/artifacts after Phase 0 and may prepare fixtures before Phase 1 exits; **2-runtime** implements typed registry loading/validation only after Phase 0 and Phase 1 exit.

The first deliverable is a formal factor manifest contract, not a runtime registry API. Implement it in two reviewable slices:

1. Freeze and validate `factor_manifest.v1.json`; representative valid/invalid manifests must pass through the same loader as publication and reads.
2. Freeze and validate the factor-set definition/config schema that drives registry construction. The registry is then a typed in-memory view over reviewed files, not an ad-hoc Python declaration.

Without slice 1, Phase 5 cannot bind lineage; without slice 2, semantic-version governance cannot be enforced.

Goal: freeze machine-checkable factor identities, dependencies, and lineage.

Entry criteria:

1. For 2-contract: Phase 0 exit criteria pass. For 2-runtime: Phase 0 and Phase 1 exit criteria pass.
2. The factor spec's manifest and quality sections have no known internal contradictions.

Tasks:

1. Add `factor_manifest.v1.json` under `config/schemas/manifests/`. Required fields must include the five-tuple partition identity, decision/run-visible timestamps, ordered input bindings with upstream generation/checksum/schema identity, adjustment snapshot/effective-table binding when applicable, per-factor definitions/fingerprints, universe snapshot, output artifact/logical fingerprints, serialization profile ID, engine/package provenance, quality status/policy/report checksum, run ID, created timestamp, manifest digest, and content generation ID.
   The approved field decision table is `specs/layers/contracts/factor-manifest-field-decision-v1.md`. `config/schemas/manifests/factor_manifest.v1.json` is the sole machine-enforceable required-field list; source-spec §6A is its human-readable summary and must not introduce competing fields.
2. Add a reviewed factor-set definition/config schema, including member names, versions, dependencies, required columns, fingerprints, numeric policy, and quality policy binding.
3. Verify no stale `turnover_20d` remains in normative tables, registry examples, or tests; the source-spec primary table already uses `volume_ratio_20d`. Keep only the explicit naming-history reference.
4. Implement registry validation:
   - unknown dependencies fail;
   - changed semantics cannot reuse an old semantic version;
   - implementation fingerprint mismatch fails unless reviewed through the declared set-version action;
   - set versions come only from reviewed definition files.
5. Define logical fingerprint normalization per factor: sort order, column order, NaN, signed zero, rounding, and tolerance composition.
6. Freeze supporting artifact contracts before store work:
   - `universe_snapshot.v1`: source, snapshot/visibility time, membership evidence, PIT validity interval, checksum, generation ID, and storage path; raw-price v0 may allow an explicitly null universe binding only when every canonical input key is expected.
   - `quality_report.v1`: canonical serialization bytes, JSON Schema, storage path, checksum, missing/report-mismatch rejection policy, warning/error taxonomy, and binding to the exact factor/canonical run.
   Artifact storage/reuse/error semantics:
   - universe artifacts live under `$UQ_DATA_ROOT/universes/<universe_id>/<generation_id>/`;
   - quality reports live under `$UQ_DATA_ROOT/reports/<binding_type>/<bound_generation_id>/`; this reviewed v1 refinement binds immutable reports by content generation rather than mutable run ID.
   - reuse is allowed only when generation ID, checksum, and PIT validity interval exactly match the requested binding;
   - readers fail closed on absent artifact, checksum mismatch, generation mismatch, outside PIT validity, absent report, report checksum mismatch, wrong run binding, unknown taxonomy code, or malformed serialization;
   - minimum acceptance: valid bind; absent artifact; tampered bytes; reuse outside PIT validity; report bound to another run.
7. Record the canonical compatibility rule: `canonical-v1.json` is the local prototype contract and cannot gain these identities in place; factor publication requires the Phase 0 `canonical-v2` output or a governed migration/republish action.

Exit criteria:

1. A valid reviewed basic-v1/raw-price factor set loads successfully.
2. Every malformed registry case has a typed rejection test.
3. `factor_manifest.v1.json` validates representative manifests and rejects missing/extra/mistyped fields.
4. Set-version rules are enforced, not inferred at runtime.
5. `universe_snapshot.v1` and `quality_report.v1` pass all five artifact acceptance cases with typed failures and governed storage/read paths.
6. The §6A field decision table is approved, represented by valid/invalid fixtures, and no second normative required-field list remains.

## Phase 2A — Engine Interface Contract

Goal: freeze computation inputs/results before implementing factors.

Entry criteria:

1. Phase 0 and Phase 1 exit review passes.
2. Factor-set definitions are loadable through the Phase 2 registry slice or a frozen schema-only fixture approved for interface design.
3. Canonical input bindings and serialization profile placeholders have stable typed names.
4. Normative API decision: source-spec §10 is the public simplified facade, `compute(trade_date, factor_set, factor_version, universe=None)`. The facade constructs and validates one internal expanded `FactorComputeRequest` containing resolved registry definition, session dates/window selector, explicit universe binding, decision time, run-visible cutoff, serialization profile placeholder, and dry-run/publication-intent mode. A separate typed method may accept the expanded request; there are two signatures but one normative request model. Facade defaults are recorded in request metadata and cannot silently alter lineage semantics.

Tasks:

1. Define `FactorEngine.compute()` arguments: resolved registry definition, one-or-more session dates, explicit historical-window selector, universe binding, decision time, run-visible cutoff, serialization profile placeholder, and dry-run/publication-intent mode.
2. Freeze `FactorResult`: typed frame schema, definitions, ordered input bindings, quality report object, status (`passed | warning | rejected | empty`), warnings/errors, and null-policy metadata.
3. Define session/window selection: either bind a governed trading calendar or explicitly use sorted published canonical dates as the v0 session calendar; missing non-trading days must not be errors.
4. Define empty-result and all-null semantics, including whether each is accepted, warned, or rejected by factor type and configured universe size.
5. Freeze dry-run versus publication-intent behavior. Dry-run results remain in-memory or staging only and cannot enter accepted factor storage.
6. Define the Phase 4A staging boundary now: a temporary deterministic-output writer/reader may validate bytes and fingerprints, but it must not create Hive partitions, manifests, promotion metadata, publication locks, or accepted-store read APIs owned by Phase 5.
7. Define Phase 4A physical isolation: `$UQ_DATA_ROOT/repro_staging/<gate_run_id>/` with creation timestamp, TTL/cleanup policy, unique gate run ID, no accepted-store registration, and an accepted-reader probe proving staged paths are invisible to accepted reads.

Exit criteria:

1. Representative compute requests compile into a deterministic execution plan without performing calculations.
2. Missing dependencies, unknown registry versions, invalid windows, invalid universes, and future inputs produce typed failures.
3. Empty and all-null outcomes have documented, tested status transitions.
4. Simplified facade and expanded request compile to identical deterministic execution plans for representative inputs; facade defaults appear in request metadata.
5. Repro-staging isolation tests prove accepted readers cannot discover or read a staging path before cleanup.

Acceptance impact:

- Strengthens F3/F10/F11/F12/F13 prerequisites but does not complete them until engine/store/gate implementations exist.

## Phase 3 — Raw-Price Factor Engine

Goal: implement only factors supported by frozen `bars_daily.research-v1`.

Allowed initial factors:

| Name | Basis |
|---|---|
| `range_ratio_1d` | raw same-day prices |
| `close_location_1d` | raw same-day prices; `high == low` emits null |
| `amount_20d` | rolling mean amount |
| `volume_ratio_20d` | mean volume / rolling 20-session mean volume; raw-volume only and not share turnover |

The source-spec adjusted factors (`return_1d`, `return_5d`, `return_20d`, and
`volatility_20d`) are explicitly deferred to Phase 4; they are not omissions.

Forbidden until Phase 4 completes:

- `return_1d`;
- `return_5d`;
- `return_20d`;
- `volatility_20d`.

Tasks:

1. Implement deterministic row-wise/grouped calculations over `(instrument, datetime)` ascending order.
2. Enforce insufficient-history nulls, no forward filling, and key-based output reconciliation against canonical bars.
3. Compute implementation fingerprints from governed source/config content.
4. Produce `FactorResult` with frame, definitions, input lineage, quality report, status, warnings/errors.
5. Keep computation separate from publication.

Exit criteria:

1. Identical locked-environment runs produce identical artifacts; cross-platform comparison uses logical fingerprints.
2. Insufficient history emits null without failing unrelated rows.
3. Future partitions cannot affect historical computations.
4. Missing dependencies and duplicate keys fail deterministically.
5. Raw-price factors never consume adjustment data.

Acceptance mapping:

- F2, F3, F5, F10, and F11 become testable for this subset.
- F6 remains blocked because it applies specifically to adjusted return factors.
- A raw-price dependency-isolation test is required here, but it is not a substitute for F6.

## Phase 4 — Adjusted Input and Adjusted Factors

Goal: introduce one reviewed prerequisite and only then enable returns/volatility.

Decision gate: choose exactly one path and record it in config/spec before coding:

1. `bars_adjusted.research-v1`; or
2. two-input engine consuming `bars_daily.research-v1` plus a governed adjustment snapshot dataset.

Tasks:

1. Freeze the selected schema/input bindings, nullability, derivation lineage, snapshot ID, and effective-date-table checksum.
2. Publish adjusted close as a governed intermediate or derive it inside the engine from bound inputs; choose one owner.
3. Implement `return_1d`, `return_5d`, `return_20d`, and `volatility_20d`.
4. Prove that raw close cannot satisfy adjusted-return dependencies.
5. Bind every historical window to one Phase 1 adjustment snapshot/as-of selection.

Exit criteria:

1. Adjusted factors reject absent, mismatched, restated, or mixed adjustment lineage.
2. Provider/golden regressions cover dividend, bonus/transfer, rights, and combined actions used in sampled windows.
3. F6 passes for all adjusted-return members.

Both Phase 4 paths MUST run the same shared F6 negative-test suite:

1. adjusted dependency absent;
2. adjustment lineage version mismatch;
3. snapshot ID mismatch;
4. effective-date table checksum mismatch;
5. two snapshots inside one return/volatility window;
6. raw close substituted for adjusted close.

A path-specific implementation may add cases, but cannot replace or weaken this suite.

## Phase 4A — Serialization and Environment Profile

This phase has two gates. **4A-contract** freezes profile/schema; **4A-validation** proves reproducibility in controlled staging runs that are explicitly not accepted factor partitions.

Goal: make F4 auditable instead of treating “locked environment” as an informal phrase.

Entry criteria:

1. Phase 3 raw-price factors pass their engine tests.
2. Factor result schemas and logical fingerprint rules are frozen.

Tasks:

1. Define a versioned serialization profile covering Parquet compression level, dictionary encoding, row-group size, metadata policy, null representation, column order, sort order, float rounding, and NaN/signature handling.
2. Pin the acceptance environment by lockfile digest plus Python, pandas, PyArrow, NumPy, BLAS/LAPACK backend, OS family, and CPU architecture matrix.
3. Implement both fingerprints:
   - artifact checksum over exact bytes under the declared profile;
   - logical fingerprint over normalized values and declared tolerance equivalence.
4. Add golden vectors for each factor's logical fingerprint and at least one artifact checksum within the pinned environment.
5. In 4A-validation, write repeated controlled outputs to staging/temporary immutable run directories, compare bytes/logical fingerprints, and reject promotion to accepted factor partitions.

Exit criteria:

1. Identical runs inside one declared environment produce identical staged artifacts, and staged outputs cannot be read as accepted factor partitions.
2. Cross-platform comparisons use logical fingerprints with explicit tolerances; byte equality is not claimed across environments.
3. Any profile change creates a new output generation.

Acceptance mapping:

- Completes F4 for the implemented subset after Phase 5 publication exists.
- If CI cannot execute every declared platform/backend combination, F4 remains accepted only for the CI-covered locked environments. Any uncovered matrix cell must be marked `not certified`; cross-platform equality claims are limited to logical fingerprints.

## Phase 5 — Immutable Factor Store

Goal: publish auditable feature partitions.

Entry criteria:

1. Factor manifest schema and registry governance exist.
2. At least the Phase 3 raw-price factor set computes successfully.
3. The Phase 4A serialization profile is frozen.

Tasks:

1. Implement nested Hive paths exactly as:  
   `dataset=.../schema_version=.../factor_set=.../factor_version=.../date=...`.
2. Implement unique staging run IDs, publication locks, pre-validation, manifest-last promotion, readback validation, and immutable overwrite rejection.
3. Bind every upstream input manifest, checksum, universe fingerprint, adjustment snapshot, and quality report.
4. Implement exact canonical JSON generation ID plus golden hash vectors.
5. Implement configurable quality policy with error/warning levels, thresholds, expected-key universe filter bindings, and structured reports.
6. Define rejected-artifact quarantine behavior; rejected artifacts must not be readable as accepted factor partitions.
7. Enforce decision-time versus run-visible-cutoff freshness: live/near-live publication requires evidence that every selected input partition existed by its declared cutoff.

Exit criteria:

1. Duplicate `(instrument, datetime)` keys reject publication.
2. Null-rate threshold rejection works.
3. Overwrite and tampered manifest/data reads fail.
4. Generation ID changes whenever any bound input, definition, code fingerprint, quality decision, artifact, logical fingerprint, or partition key changes.
5. Path identity and manifest identity agree for every accepted read.
6. Freshness violations fail closed.

Acceptance mapping:

- F7, F8, F9, F12, F13, and F14 become release-testable.
- F4 is completed through the Phase 4A serialization/environment profile and Phase 5 publication tests.

## Risk Register

| Risk | Gate | Resolution |
|---|---|---|
| Adjustment formula lacks authoritative exchange-golden coverage | Blocks Phase 1 exit and all adjusted factors | Obtain/derive documented exchange reference prices for cash, bonus/transfer, rights, and combined actions; record source and tolerance per case |
| Environment matrix cannot be fully covered by CI | Blocks universal F4 certification | Certify only covered lockfile/platform/backends; expose uncovered cells as `not certified`; compare other platforms by logical fingerprint only |

## Phase 6 — Release Acceptance

Goal: prove the first governed factor layer, not merely finish code.

Tasks:

1. Map every canonical/factor/publication acceptance sub-item—not only top-level F IDs—to concrete automated test IDs and current status.
2. Run identical-input determinism checks using the pinned lockfile and declared platform matrix; compare artifact checksums locally and logical fingerprints across platforms.
3. Run negative paths for unpublished input, future input, tampering, overwrite, dependency absence, semantic-version reuse, null rate, duplicates, and changed generation bindings.
4. Update README/spec statuses only after all release gates pass.

Exit criteria:

1. Every security/lineage sub-item must pass: unpublished input, future input, data/manifest tamper, immutable overwrite, dependency absence, semantic-version reuse, null-rate rejection, duplicate keys, generation binding change, path identity, freshness fail-closed, staging isolation, and artifact missing/tampered/misbound rejection. These have no deferral.
2. Non-security determinism/coverage items may use an explicitly approved, documented deferral only if the release label states its scope limitation.
3. No acceptance item is marked executable while its implementation/test remains absent.
4. Full suite and focused contract reviews pass.

## Immediate Next Actions

Immediate action is to preserve the latest successful unified-gate evidence, then resolve the remaining release gate before declaring layer release.

1. Run and archive `scripts/run_gate.sh` outputs: `.gate/gate-report.json`, requirements artifact, and digest; CI must invoke the same runner.
2. Keep factor-v1 F12a evidence aligned with the four report negative paths plus reader enforcement.
3. Keep F4 scope explicit: certify only covered locked environments; compare other platforms through logical fingerprints only.
4. Complete canonical-publication F12a coverage separately before claiming the broader canonical release gate.
5. Before release, rerun the full suite plus focused contract reviews and update statuses only from recorded gate evidence.
