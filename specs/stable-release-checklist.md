# Stable Release Checklist

Purpose: this is the gate for promoting Unified Quant from **prototype v0.1 /
design v0.2** to a published **stable base layer**.

Rule: every item must be implemented, tested in a clean environment, and
reviewed against the normative documents:

- `specs/architecture.md`

A partially green checklist does not authorize a stable release.

## 1. Contract and Schema Gate

### 1.1 Versioned schemas

- [ ] Freeze current eight-field bar contract as `bars_daily.research-v1`.
- [ ] Publish a separate production draft, preferably `bars_daily.v2`.
- [ ] Forbid semantic changes inside a released schema version.
- [ ] Document schema compatibility rules: exact, additive-compatible, or
      breaking/new-version.
- [ ] Add golden-schema tests for released versions.
- [ ] Bind every consumer request to one explicit dataset + schema version.

### 1.2 Complete field semantics

For every canonical field define:

- [ ] name;
- [ ] physical dtype and acceptable dtype range;
- [ ] unit;
- [ ] nullable policy;
- [ ] null sentinel policy;
- [ ] timezone where applicable;
- [ ] adjustment semantics where applicable;
- [ ] enum values where applicable;
- [ ] key participation;
- [ ] sort behavior;
- [ ] lineage requirements.

### 1.3 Production bars scope

The production bars schema includes at minimum:

- [ ] instrument;
- [ ] session datetime;
- [ ] open/high/low/close;
- [ ] volume in shares;
- [ ] amount in CNY;
- [ ] status: trading/suspended/delisted/unknown;
- [ ] limit-up price;
- [ ] limit-down price;
- [ ] cumulative adjustment factor;
- [ ] calendar/session reference or equivalent manifest binding;
- [ ] explicit corporate-action linkage strategy.

### 1.4 Formal companion datasets

Define or explicitly schedule versioned datasets for:

- [ ] trading calendar/session;
- [ ] instrument metadata and lifecycle;
- [ ] corporate actions;
- [ ] PIT index membership;
- [ ] PIT fundamentals/events;
- [ ] optional liquidity extensions such as free float/turnover/VWAP;
- [ ] vendor quality flags.

### 1.5 Strict configuration governance

- [ ] YAML config rejects unknown keys.
- [ ] Schema/config/capability references are cross-validated.
- [ ] Required fields exist in the selected schema.
- [ ] Owners own existing fields and are available sources.
- [ ] Cross-validation comparators provide those fields.
- [ ] Units, timezones, enums, key fields, and invariant references validate.
- [ ] Config fingerprint is persisted with each publication.
- [ ] Schema fingerprint is persisted with each publication.

## 2. Capability, Routing, and Merge Gate

### 2.1 Capability model

Model capability as `(source, dataset, schema_version)` with:

- [ ] provided required fields;
- [ ] missing fields;
- [ ] market/instrument coverage;
- [ ] date coverage;
- [ ] latency class;
- [ ] authentication requirement;
- [ ] quota/rate-limit metadata;
- [ ] reliability class;
- [ ] correction-window semantics;
- [ ] revision support where relevant;
- [ ] supported fetch modes.

### 2.2 Fetch result envelope

Adapter output cannot be only a DataFrame. Implement:

- [ ] normalized rows/batches;
- [ ] source identity;
- [ ] requested vs delivered fields;
- [ ] observed range;
- [ ] fetch status;
- [ ] retryability;
- [ ] warnings/errors;
- [ ] source timestamp;
- [ ] source version/event identifiers when available;
- [ ] partial reason when applicable.

### 2.3 Deterministic routing

- [ ] Default routing accepts complete sources only.
- [ ] Missing complete capability raises a typed `CapabilityGapError` or
      equivalent structured error.
- [ ] Error lists requested fields, candidate sources, and field gaps.
- [ ] Fallback means another complete eligible source.
- [ ] Partial mode requires explicit `allow_partial=true`.
- [ ] Partial result carries `coverage_status=partial`, provenance, and missing
      field/key information.
- [ ] Default FactorContext rejects partial data.

### 2.4 Merge planning

Implement deterministic overlap/owned/complement handling:

- [ ] Overlapping fields take the configured primary value within tolerance.
- [ ] Secondary sources validate primary values.
- [ ] Owned fields are copied from their declared owner exactly once.
- [ ] Required owned-field absence fails publication.
- [ ] Complement merge occurs only when explicitly enabled.
- [ ] Every non-key field has field-level lineage.
- [ ] Lineage records provider, owner, validators, compared rows, missing rows,
      mismatch count, tolerance, and validated decision.

## 3. Quality and Diagnostics Gate

### 3.1 Structured quality report

QualityGate returns a persistent report containing:

- [ ] status/decision;
- [ ] policy used;
- [ ] accepted frame or rejected reason;
- [ ] quarantined keys;
- [ ] conflicts with primary/secondary values and tolerances;
- [ ] per-field coverage counts;
- [ ] missing-primary/secondary counts;
- [ ] mismatched-row counts;
- [ ] schema errors;
- [ ] report checksum;
- [ ] run/request ID.

### 3.2 Policy support

- [ ] `reject_all`;
- [ ] `quarantine_rows`;
- [ ] `accept_with_warnings`.

Publication defaults to `reject_all`.

### 3.3 Typed diagnostics

Provide distinguishable errors/codes for:

- [ ] capability gap;
- [ ] source auth failure;
- [ ] source rate limit;
- [ ] upstream outage;
- [ ] unsupported range;
- [ ] empty-but-valid response;
- [ ] schema violation;
- [ ] duplicate key;
- [ ] invariant failure;
- [ ] quality rejection;
- [ ] publication conflict;
- [ ] repository I/O failure;
- [ ] unpublished/tampered partition read.

Add structured lifecycle logging without credentials or raw payloads.

## 4. Storage and Publication Gate

### 4.1 Immutable partition protocol

Publication performs all steps:

- [ ] pre-write schema validation;
- [ ] unique staging directory/run ID;
- [ ] publication lock scoped to dataset/partition;
- [ ] deterministic Parquet write;
- [ ] data SHA-256;
- [ ] schema checksum;
- [ ] quality/report digest;
- [ ] readback validation;
- [ ] row/column/dtype fingerprint verification;
- [ ] manifest written last;
- [ ] atomic promotion of the complete artifact;
- [ ] refusal to overwrite an existing published partition;
- [ ] cleanup of orphan staging only under ownership/lock.

### 4.2 Manifest-first reader

Reader refuses data unless a valid sibling/partition manifest exists.

Manifest includes:

- [ ] dataset and schema version;
- [ ] partition identity/date/range;
- [ ] row count;
- [ ] column order and dtype fingerprint;
- [ ] data checksum;
- [ ] schema checksum;
- [ ] quality-report digest/checksum;
- [ ] source versions;
- [ ] field-level lineage summary;
- [ ] producer run ID/tool version;
- [ ] config fingerprint;
- [ ] calendar reference where applicable;
- [ ] created timestamp.

- [ ] Manifest JSON Schema exists.
- [ ] Manifest tampering is detectable.
- [ ] Bare Parquet without valid manifest is unreadable through public API.

### 4.3 Storage-neutral ports

Preserve future backend flexibility:

- [ ] Define DatasetRepository/read/write port boundaries.
- [ ] Core contracts do not require local filesystem paths.
- [ ] Core adapter/result boundaries do not require pandas directly; a local
      pandas runtime may wrap the neutral boundary.
- [ ] Local filesystem remains the first driver, not the core API.
- [ ] Object-store/database/service drivers can be added without changing factor
      APIs.

## 5. PIT Gate

Announcement-driven data becomes usable only when all pass:

- [ ] Separate event/effective period from knowledge/announcement time.
- [ ] Store append-only revisions.
- [ ] Enforce stable `source_event_id`.
- [ ] Enforce monotonic producer revision allocation.
- [ ] Detect duplicate revision payloads.
- [ ] Detect illegal announcement regressions.
- [ ] Resolve effective revision deterministically by
      `(announcement_datetime, revision)` not after decision time.
- [ ] Expose explicit `read_asof(decision_time)`.
- [ ] Test delayed announcements.
- [ ] Test corrected/revised announcements.
- [ ] Test same announcement-time ordering.
- [ ] Test that future revisions never leak backward.

## 6. Downstream Boundary Gate

### 6.1 FactorContext

FactorContext is the only supported factor-facing read API.

It must:

- [ ] require valid manifests;
- [ ] verify schema/version;
- [ ] verify checksum/fingerprint before returning data;
- [ ] normalize columns/dtypes/order/timezone according to schema;
- [ ] reject unpublished partitions;
- [ ] reject tampered partitions;
- [ ] reject partial coverage by default;
- [ ] expose explicit partial-only access separately if needed;
- [ ] bind returned data to manifest metadata.

### 6.2 QlibExporter

Qlib output remains derived and reproducible.

- [ ] Export reads only manifest-valid canonical partitions.
- [ ] Output lives under external immutable `provider_uri`.
- [ ] Snapshot manifest maps Qlib version to input partitions.
- [ ] Snapshot manifest records exporter code version and parameters.
- [ ] Snapshot manifest records input/output checksums.
- [ ] A snapshot can be rebuilt from canonical partitions plus manifest inputs.
- [ ] Repository still excludes generated `.bin` artifacts.

## 7. Engineering and Dependency Gate

- [ ] Python 3.11+ enforced.
- [ ] Runtime dependencies include Parquet engine (`pyarrow` or explicit
      equivalent).
- [ ] Pandas compatible range is declared and tested.
- [ ] Datetime resolution is handled explicitly across supported pandas versions.
- [ ] Lockfile is committed.
- [ ] Clean-environment install works.
- [ ] CI runs on Python 3.11+.
- [ ] CI validates package, configs, schemas, and tests.
- [ ] No credentials/data caches/generated market data are committed.
- [ ] Public package/dataset deprecation process is documented.

## 8. Acceptance Tests

All named regression scenarios exist and pass:

### Source replacement

- [ ] Two complete daily-bar adapters are interchangeable behind one
      FactorContext call.
- [ ] Factor code does not change when replacing sources.

### Routing failures

- [ ] No complete source produces typed capability-gap error.
- [ ] Incomplete fallback is not silently selected.
- [ ] Partial mode works only with explicit opt-in.
- [ ] Partial result is rejected by default reader.

### Schema/data validity

- [ ] Duplicate primary key rejected.
- [ ] Invalid OHLC rejected.
- [ ] Bad dtype rejected.
- [ ] Non-nullable null rejected.
- [ ] Unknown config/schema key rejected.
- [ ] Invalid owner/comparator reference rejected.

### Merge and quality

- [ ] Owned `adj_factor` reaches canonical output.
- [ ] Owner absence fails.
- [ ] Field lineage contains provider/validation/coverage details.
- [ ] Conflict below tolerance uses primary value.
- [ ] Conflict above tolerance creates structured quarantine/report.
- [ ] Minimum coverage threshold controls `validated_by`.
- [ ] All three quality policies behave correctly.

### Storage/publication

- [ ] Concurrent publishers target the same partition; exactly one succeeds.
- [ ] Republishing an immutable partition fails safely.
- [ ] Failed publication leaves no readable artifact.
- [ ] Readback catches corrupted write.
- [ ] Manifest tampering is detected.
- [ ] Bare data file without manifest is rejected.
- [ ] Orphan staging does not become readable.

### PIT

- [ ] Future announcement excluded from historical as-of read.
- [ ] Delayed announcement appears only after visibility time.
- [ ] Revised announcement selects latest visible revision.
- [ ] Same-time revision ordering is deterministic.
- [ ] Append-only history preserves prior revisions.

### Reproducibility

- [ ] Published partition can be verified from manifest.
- [ ] Canonical partition can be rebuilt from recorded source/request metadata.
- [ ] Qlib snapshot can be rebuilt from canonical manifests/exporter parameters.
- [ ] Two clean runs with same inputs/config/code produce compatible
      fingerprints.

## 8A. Additional Governance Gate

These items are release-blocking additions required for stable-release governance.

### Schema/config/API governance

- [ ] No released schema version carries dual research/production meaning.
- [ ] Golden snapshot tests exist for released schemas.
- [ ] Golden snapshot tests exist for dataset/capability configuration format.
- [ ] Golden snapshot tests exist for manifest JSON format.
- [ ] Compatibility matrix defines whether each change is breaking:
      - [ ] additive nullable field;
      - [ ] enum expansion/shrink;
      - [ ] invariant tightening;
      - [ ] null sentinel change;
      - [ ] datetime resolution change;
      - [ ] unit precision/conversion policy change.
- [ ] Public stable API surface is listed and frozen.
- [ ] Internal/unstable types are explicitly marked non-public.
- [ ] Deprecation/removal policy and migration guide exist.

### Diagnostics, retention, and security

- [ ] Failure taxonomy table maps error codes to retryability, quarantine
      eligibility, and routing/publication/read impact.
- [ ] Structured logging contract defines request/run/source/schema/partition
      fields.
- [ ] Credential redaction is tested.
- [ ] Producer/tool fingerprints appear in diagnostics where relevant.
- [ ] Data-retention periods exist for raw fetch, quality, quarantine, and
      lineage reports.
- [ ] Replay/correction-window boundary is documented.
- [ ] Manifest trust model distinguishes corruption, silent-write, and malicious
      tampering.
- [ ] External trusted journal/signature requirement is defined if malicious
      tampering protection is claimed.
- [ ] External data-root permission and credential-loading boundaries are
      documented and tested.

### Performance, artifacts, and approvals

- [ ] Local partition row/file-size targets are defined.
- [ ] Query scan/field-pushdown boundary is defined.
- [ ] Concurrent reader/publisher limits and supported filesystem scope are
      defined.
- [ ] Power-loss durability promise, if made, has fsync tests.
- [ ] Replacement generation behavior is either forbidden or fully specified.
- [ ] Stable release artifact list includes package, schemas, JSON Schemas,
      contract docs, migration guide, compatibility matrix, test vectors, and
      review sign-offs.
- [ ] Independent sign-offs exist for architecture, schema evolution,
      storage/PIT, A-share domain, and engineering/release governance.

## 9. Final Release Rule

Stable release is allowed only when:

1. every checkbox above is complete or explicitly deferred to a documented
   post-stable milestone;
2. no P0 item is deferred;
3. all acceptance tests pass in a clean environment;
4. two interchangeable complete sources demonstrate pluggability end-to-end;
5. PIT data is unavailable to consumers until `read_asof` passes;
6. architecture, review decisions, and release checklist agree on the same
   schema versions and policies;
7. additional governance additions in section 8A are complete.

Until then, label releases as:

```text
0.x prototype / design implementation
```

not:

```text
stable base layer
```
