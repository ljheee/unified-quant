# Factor Layer Specification

Status: **design v0.3; boundary partially implemented**

Implemented today:

- manifest-first canonical read path in `FactorContext`;
- immutable canonical publication and checksum verification;
- versioned adjustment derivation `adj_factor.derived_v4`.

Not implemented yet:

- factor registry/engine/store;
- factor schema/config governance;
- adjusted-close series publication;
- factor manifests and quality gate.

The acceptance matrix in section 11 distinguishes tests that are executable now
from tests blocked on those components.

## 1. Purpose

The factor layer converts published canonical datasets into versioned,
point-in-time-safe feature tables. It is a downstream consumer of canonical
data and an upstream producer of model datasets.

It must never:

- read raw provider responses;
- read canonical Parquet without a valid manifest;
- use information after a cross-section decision time;
- overwrite an existing factor partition;
- silently change factor semantics.

## 2. Boundaries

```text
CanonicalStore -> FactorContext -> FactorRegistry -> FactorEngine
                                     -> FactorStore
                                     -> DatasetBuilder
```

- `FactorContext` is the only read API for canonical data.
- `FactorRegistry` declares deterministic factors and their dependencies.
- `FactorEngine` computes one or many factor versions.
- `FactorStore` publishes immutable feature partitions.
- `DatasetBuilder` consumes factor and label stores; it is not part of this
  factor layer.

## 3. Factor Contract

### 3.1 Required Input Prerequisite

The current frozen input is `bars_daily.research-v1`. It deliberately contains
only raw OHLCV/amount and does **not** contain `adj_factor`.

Therefore the first factor implementation has two valid paths:

1. **Bars-plus-adjustment prerequisite**: introduce a new compatible research
   input, for example `bars_adjusted.research-v1`, containing the existing exact
   columns plus nullable/non-null policy-defined `adj_factor` and its derivation
   version/provenance.
2. **Two-input factor engine**: let the factor run declare both
   `bars_daily.research-v1` and an adjustment-factor dataset as required inputs,
   with each input bound by checksum.

A factor spec must not pretend that adjusted returns can be derived from the
existing eight-field bar contract alone. Until one of these prerequisites is
implemented, only raw-price factors are implementable from current canonical
bars.

Every factor has a stable identity:

```yaml
factor:
  name: momentum_20d
  version: 1.0.0
  dataset: bars_daily
  inputs:
    bars:
      dataset: bars_daily
      schema_version: research-v1
    adjustment:
      dataset: adjustment_factors_daily
      schema_version: research-v1
      lineage: adj_factor.derived_v4
  required_columns:
    - instrument
    - datetime
    - close
    - adj_factor
  output_dtype: float64
  nullable: true
  unit: ratio
  price_basis: adjusted_close
  window:
    min_history: 21
    as_of: trading_day_close
  universe_eligibility:
    min_history: 21
    require_traded_on_decision_date: true
  description: 20-day close-to-close return using adjusted close.
```

`input_schema` must identify a concrete implemented schema such as
`bars_daily.research-v1`; generic architecture examples like `bars_daily.v1`
are not valid runtime identifiers until such a schema file exists.

The example above is intentionally not runnable against the current frozen
eight-field bar contract. It is valid only after an adjustment prerequisite is
implemented. Raw-price factors may declare only the existing bars input.

Required metadata:

- `name`;
- semantic `version` (`MAJOR.MINOR.PATCH`);
- input dataset and schema version;
- output dtype and unit;
- nullability;
- adjustment semantics;
- minimum history;
- decision timestamp;
- dependencies on other factors;
- implementation fingerprint.

A major version change is required when output meaning changes. A minor version
may add a backward-compatible metadata field or a new independent factor to a
set. A patch version may fix an implementation defect without changing intended
semantics; if published values materially change, the repair must be promoted to
a set-level major version and the old generation marked deprecated.

Set version derivation rules:

1. Adding/removing/redefining any factor bumps the set MINOR/MAJOR accordingly.
2. A defect repair that changes one member's values bumps the set MAJOR when
   historical compatibility is broken, otherwise MINOR with explicit migration.
3. A member's implementation fingerprint must be recorded in every manifest.
4. `factor_version` is never inferred automatically from individual member
   versions; it comes from a reviewed factor-set definition file.

## 4. Decision-Time Semantics

### 4.1 Two Clocks

Every factor must distinguish event time from knowledge time:

- **event time**: the trading session to which a value belongs;
- **knowledge time**: when the value was first observable outside the venue.

Daily exchange bars are special because their close is both an event-time price
and observable at the declared decision time. Announcement-driven data cannot
use this shortcut and must provide `knowledge_time`.

### 4.2 Daily Research Decision Time

Daily research factors use a single cross-section decision time:

```text
decision_time = trading_day 15:00 Asia/Shanghai
```

A factor computed for date `D` may use:

- canonical bars with `datetime <= D`;
- corporate actions effective on or before `D`;
- lifecycle evidence known for `D`.

It must not use:

- bars after `D`;
- later corrections to historical bars unless the correction was already part
  of the canonical partition used for the run;
- future suspension, delisting, or index membership information.

For future announcement-driven datasets, factors must consume an explicit
`read_asof(decision_time)` API. The current bars-only layer may use
`datetime <= D` because daily bar close is the declared decision time.

### 4.3 Corporate-Action Restatement Risk
1. Resolve the schema first.
2. Filter sources that support `(dataset, schema_version)` and every required
   field.
3. Require the configured primary source to remain available.
4. Select additional sources needed for cross-validation.
5. Select owner sources for non-overlapping fields.
6. If no complete source exists, fail with a routing error listing missing
   fields and candidate gaps.
7. Partial mode is forbidden unless the request sets
   `allow_partial=true`; partial output must be marked
   `coverage_status=partial` and cannot feed default factor/training pipelines.

Fallback means “another complete eligible source”, not silently accepting an
incomplete source.

## 6. Merge Policy

Cross-source merging has three field classes:

1. **Overlapping fields**: multiple sources can provide them.
   - Primary value wins within tolerance.
   - Secondary values validate the primary.
   - Conflicts outside tolerance enter quarantine.
2. **Owned fields**: exactly one declared owner.
   - Value is copied from its owner.
   - Owner absence is an error if the field is required.
3. **Complement fields**: provided by only one selected source but without an
   explicit owner declaration.
   - The router/gate may merge them only when explicitly enabled.
   - Lineage records the provider and validation state.

The merged frame must contain all required fields. For each non-key field it
must persist lineage containing:

```json
{
  "source": "tdx",
  "owner": "tushare",
  "validated_by": ["tushare"],
  "compared_rows": 1000,
  "missing_primary_rows": 0,
  "missing_secondary_rows": 20,
  "mismatched_rows": 0
}
```

A column is `validated` only when compared rows meet the configured minimum
coverage threshold.

## 6. Identity and Store Layout

```text
$UQ_DATA_ROOT/
  factors/
    bars_daily/
      research-v1/
        dataset=bars_daily/
          schema_version=research-v1/
          factor_set=basic/
          factor_version=1.0.0/
          date=2026-08-21/
          data.parquet
          manifest.json
```

Identity rules:

1. `factor_set` names a stable family of factors.
2. `factor_version` is the semantic version of that set's definitions and
   implementations.
3. They must be separate path dimensions because API callers select them
   independently.
4. A partition key is therefore
   `(input dataset, input schema version, factor_set, factor_version, date)`.
5. The physical Hive path must nest each key dimension exactly once:
   `dataset=.../schema_version=.../factor_set=.../factor_version=.../date=...`.
6. The five-tuple is canonical identity; the directory layout is its encoded
   form and must not flatten unrelated dimensions into one level.

A partition is immutable. `manifest.json` must include:

- factor set name and semantic version;
- input dataset/schema version;
- input partition manifests and checksums;
- factor definitions and implementation fingerprints;
- universe snapshot checksum;
- row count, columns, dtypes;
- data SHA-256;
- engine/code version;
- run UUID and created timestamp;
- quality summary.

It may reference an upstream field only when that field exists in the upstream
manifest. Current `canonical-v1` manifests provide a schema-file checksum, but
do not yet provide stable content generation IDs, universe snapshot artifacts,
or external quality-report artifacts. The factor implementation must either:

1. use the Phase 0 additive `canonical-v2` contract for first-class content
   identity and governed migration, or
2. compute and record their fingerprints inside the factor manifest while
   explicitly marking them as v0 substitutes.

Silently referencing nonexistent canonical fields is invalid.

Reads must be manifest-first and checksum-verified.

## 6A. Formal Manifest and Generation Contract

Factor manifests are governed documents, not free-form dictionaries. The first
implementation must ship a JSON Schema under `config/schemas/manifests/` and
reject non-conforming manifests before publication or read.

Required top-level fields:

```yaml
manifest_version: 1
manifest_digest_sha256: sha256hex
generation_id: sha256hex
input_dataset: string
input_schema_version: string
factor_set: string
factor_version: semver
partition_date: ISO date
decision_time: RFC3339 timestamp
run_visible_cutoff: RFC3339 timestamp
inputs:
  - binding: bars | adjustment | universe
    dataset: string
    schema_version: string
    partition_date: ISO date
    manifest_generation_id: sha256hex
    data_checksum_sha256: sha256hex
    schema_checksum_sha256: sha256hex | null
    adjustment_snapshot_id: sha256hex | null
    effective_date_table_checksum: sha256hex | null
factor_definitions:
  - name: string
    version: semver
    implementation_fingerprint: sha256hex
universe_snapshot:
  artifact_generation_id: sha256hex | null
  checksum_sha256: sha256hex | null
row_count: integer >= 0
columns: non-empty unique string list
dtypes: object mapping column -> dtype string
data_checksum_sha256: sha256hex
logical_fingerprint: sha256hex
engine_version: string
code_fingerprint: sha256hex
run_id: UUID
created_at: UTC RFC3339 timestamp
quality:
  status: passed | warning | rejected
  policy: reject_all | accept_with_warnings
  report_checksum_sha256: sha256hex
serialization_profile_id: string
engine_package_provenance:
  project_version: string
  python_version: string
  dependency_lock_digest: sha256hex
```

`manifest_digest_sha256` is the run-local manifest digest; `generation_id` is
the stable content identity described below and excludes run-local metadata.
The two identities are both required in factor manifests.

Checksum encoding rules:

1. All checksums are lowercase hexadecimal SHA-256.
2. `data_checksum_sha256` hashes exact `data.parquet` bytes.
3. `logical_fingerprint` hashes canonical JSON of sorted factor values after a
   declared rounding/tolerance normalization; it is not the Parquet checksum.
4. `report_checksum_sha256` hashes the exact quality report bytes.
5. A canonical manifest digest is SHA-256 over canonical JSON of every field
   except its external trust anchor. `trust_anchor_sha256` is SHA-256 over the
   ASCII bytes of `generation_id`. It detects regeneration of a self-consistent
   manifest by an untrusted writer only when the expected anchor value is
   supplied out-of-band; for the current local research store, the anchor is
   derived from the pinned CanonicalStore contract and readers reject a
   mismatching value. A production deployment must replace this with an
   operator-controlled KMS/signature or append-only trust log before stable
   release.

Generation identity:

```text
generation_id =
  sha256(canonical_json(
    input bindings,
    resolved factor definitions,
    universe snapshot,
    engine/code fingerprint,
    quality decision,
    data artifact checksum,
    logical fingerprint,
    partition key,
  ))
```

Rules:

1. `generation_id` is deterministic for identical accepted content.
2. Two accepted generations cannot share a generation ID unless their logical
   fingerprints, artifact checksums, and lineage are identical.
3. Re-publication with different run metadata but identical content is allowed
   only as a new physical snapshot referencing the same generation ID.
4. Any upstream correction changes at least one input binding and therefore
   creates a new generation.
5. Readers verify JSON Schema, required checksums, path identity, and
   recomputed generation ID.

## 7. Initial Factor Set v0

The first factor set should remain small and deterministic.

| Name | Definition | Minimum History |
|---|---|---:|
| `return_1d` | adjusted close / previous adjusted close - 1 | 2 |
| `return_5d` | adjusted close / adjusted close 5 sessions ago - 1 | 6 |
| `return_20d` | adjusted close / adjusted close 20 sessions ago - 1 | 21 |
| `volatility_20d` | standard deviation of 20 daily returns | 21 |
| `volume_ratio_20d` | mean volume / rolling 20-session mean volume; renamed from `turnover_20d` because no free-float share count is governed | 20 |
| `amount_20d` | rolling mean amount | 20 |
| `range_ratio_1d` | `(high - low) / raw_close` | 1 |
| `close_location_1d` | `(raw_close - low) / (high - low)` | 1 |

All rolling operations are backward-looking and include the current session.

Naming correction history:

- `turnover_20d` was renamed to `volume_ratio_20d`; the old name overstated
  semantics because free-float share count is not a governed prerequisite input.
- For `close_location_1d`, when `high == low`, emit null. Zero division is not
  zero, infinity, or a sentinel value.

## 8. Missing and Non-Tradable Rows

A factor row may exist only when its input history is sufficient.

Rules:

1. Insufficient history produces null, not zero.
2. A suspended day may be retained if canonical bars explicitly contain it;
   otherwise it is absent.
3. A missing canonical bar must not be forward-filled for return factors.
4. A factor row must not be published for an instrument absent from the input
   canonical partition.
5. Universe filtering is recorded but not silently applied unless requested in
   the factor run configuration.
6. Cross-sectional operations are out of scope for v0 because each row would
   depend on the full universe snapshot and complicate deterministic incremental
   computation.

## 8A. Determinism and Floating Point

Within one locked environment, factor outputs must be reproducible from
identical canonical inputs, code, configuration, and universe. Across platforms,
require logical equivalence, not byte equality.

Rules:

1. Sort by `(instrument, datetime)` before rolling calculations.
2. Avoid unordered group operations and nondeterministic parallel reduction.
3. Record numeric precision assumptions in the factor definition.
4. Do not claim byte-level cross-platform reproducibility. BLAS, CPU vectorization,
   pandas, Arrow, and Parquet encoders may produce different physical bytes.
5. Define numeric equivalence with explicit tolerances per factor, for example
   `abs_diff <= 1e-12` or `rel_diff <= 1e-12`.
6. Canonical serialization must specify compression level, dictionary encoding,
   row-group size, null representation, column order, sort order, and float
   rounding before checksumming if cross-run byte identity is required.
7. Within one locked local environment, identical logical output must produce
   identical artifact checksums. If it does not, investigate as a
   reproducibility defect.
8. Published manifests must distinguish:
   - logical fingerprint: tolerance-equivalent values and semantics;
   - artifact checksum: exact serialized bytes.

## 9. Quality Gate
Quality thresholds come from a reviewed factor-set configuration file, not from
ad-hoc engine defaults. Each rule has an error level (`error` or `warning`) and
an explicit threshold.

Required checks:

| Check | v0 default | Level |
|---|---:|---|
| Manifest validates against JSON Schema | required | error |
| Primary-key uniqueness | zero duplicates | error |
| Exact row reconciliation | output rows == input rows for same keys | error |
| Factor coverage per instrument/date | configured minimum, e.g. 0.95 | error |
| Null rate per factor | configured maximum, e.g. 0.20 | error |
| Non-finite values in non-nullable fields | zero occurrences | error |
| Missing dependency | zero missing | error |
| Input/output checksum mismatch | zero mismatches | error |
| Freshness beyond visible cutoff | zero violations | error |
| Coverage below target but above hard floor | configured band | warning |

Row-count reconciliation is key-based, never file-row-count-only:

1. Expected keys equal canonical bar keys for that date.
2. Suspended rows present in canonical input remain expected.
3. Instruments absent from canonical input remain absent from factor output.
4. Extra output keys are errors.
5. Missing output keys are errors even if a universe filter was requested; in
   that case the expected-key set must be reduced explicitly by the filtered
   universe binding.

## 10. Public API Shape

```python
class FactorEngine:
    def compute(
        self,
        trade_date: date,
        factor_set: str,
        factor_version: str,
        universe: list[str] | None = None,
    ) -> FactorResult: ...
```

`FactorResult` must contain:

- frame;
- factor definitions;
- input lineage;
- quality report;
- status;
- warnings/errors.

Publication is separate from computation.

## 11. Acceptance Tests

Minimum acceptance:

| ID | Requirement | Phase | Blocked By | Test ID | Status |
|---|---|---|---|---|---|
| F1 | Factor code cannot read unpublished canonical partitions. | 0/3 | None for single-date full-frame reads; range/filter semantics need Phase 2A | `test_factor_context_reads_published_data_only` | Partial: single-date case executable now |
| F2 | Insufficient history emits null without failing unrelated rows. | 3 | Factor engine | TBD-F2 | Blocked |
| F3 | Implementation change changes fingerprint and requires reviewed set-version action. | 2 | Registry/config governance | TBD-F3 | Blocked |
| F4 | Identical locked-environment runs produce identical artifact checksums. | 4A/5 | Engine, serialization profile, factor store | TBD-F4 | Blocked |
| F5 | Future-dated input cannot affect a historical partition. | 3 | Engine/input selection | TBD-F5 | Blocked |
| F6 | Adjusted return factors do not use raw close. | 4 | Adjusted-input prerequisite and adjustment lineage gate | TBD-F6 | Blocked |
| F7 | Immutable factor partitions reject overwrite. | 5 | Factor store | TBD-F7 | Blocked |
| F8a | Tampered canonical data prevents canonical reads. | 0 | External trust/path hardening | `tests/test_canonical_v2_runtime.py::test_reader_requires_external_anchor_and_path_identity` | Passed for canonical-v2 |
| F8b | Tampered factor data prevents factor reads. | 5 | Factor store/reader | TBD-F8b | Blocked |
| F9a | Tampered canonical manifest fails schema/digest/anchor/path verification. | 0 | canonical-v2 identity, external anchor, path identity | `tests/test_canonical_v2_runtime.py::test_generation_excludes_run_metadata`, `test_rejects_invalid_uuid_datetime_date_dtype_map` | Passed for canonical-v2 runtime |
| F9b | Tampered factor manifest fails schema/digest/anchor/path verification. | 2/5 | Factor manifest schema/reader and store | `tests/test_factor_governance.py::test_manifest_identity_mutations_are_rejected` (schema/identity only) | Partial; store path blocked |
| F10 | Changed semantics cannot reuse old semantic version. | 2 | Registry validation | TBD-F10 | Blocked |
| F11 | Missing dependency rejects computation. | 2A/3 | Registry and engine interface | TBD-F11 | Blocked |
| F12a | Canonical quality report missing/mismatch rejects publication/read binding. | 2 | `quality_report.v1` binding and canonical publication | TBD-F12a | Blocked |
| F12b | Factor null rate above threshold rejects publication. | 5 | Factor quality gate/store | TBD-F12b | Blocked |
| F13a | Duplicate canonical keys reject canonical publication. | 0 | Canonical primary-key validation in publish/read path | `tests/test_canonical_v2_runtime.py::test_rejects_invalid_uuid_datetime_date_dtype_map` plus schema validation | Passed for canonical-v2 runtime |
| F13b | Duplicate factor keys reject factor publication. | 5 | Factor quality gate/store | TBD-F13b | Blocked |
| F14a | Stable canonical generation excludes run metadata and changes on content/lineage change. | 0 | canonical-v2 identities/golden vectors | `tests/test_canonical_v2_runtime.py::test_generation_excludes_run_metadata` | Passed |
| F14b | Factor generation changes on any bound input/definition/artifact/quality/partition change. | 2/5 | Factor manifest/generation contract and store | `tests/test_factor_governance.py::test_reviewed_basic_v1_registry_loads_and_manifest_matches` (run metadata stability only) | Partial; full mutation matrix blocked |

F1 single-date canonical reads and Phase 0 sub-items F8a/F9a/F13a/F14a have executable runtime evidence. Phase 2 provides partial F9b/F14b governance evidence but does not complete factor-store acceptance. All remaining items stay release gates.

## 12. Explicit Non-Goals for v0

- machine-learning feature selection;
- online/streaming computation;
- cross-sectional ranking normalization;
- industry/style neutralization;
- high-frequency factors;
- fundamental and analyst factors;
- correction-aware intraday knowledge reconstruction;
- automatic replacement of historical factor generations after later corporate
  actions.

## 13. Decision Time Versus Run Visibility

`decision_time = trading_day 15:00 Asia/Shanghai` is a declared modeling time,
not proof that ingestion finished at that moment.

Factor runs must separately record:

```yaml
decision_time: "2026-08-21T15:00:00+08:00"
run_visible_cutoff: "2026-08-23T09:30:00+08:00"
```

Rules:

1. Historical computation may use data whose event/session time is at or before
   decision time.
2. Live or near-live publication requires an explicit freshness gate proving the
   selected partitions were available by the declared cutoff.
3. Later provider corrections create a restatement problem. They may produce a
   new factor generation, but cannot mutate an accepted historical partition.
4. Corporate-action effectiveness must remain part of the adjustment lineage;
   this layer must not infer effective dates from availability alone.

## 14. Adjustment Versioning Clarification

`adj_factor.derived_v4` is the current implementation lineage. The v2-to-v3
change corrected a cash-dividend scale defect that materially changed outputs;
under strict semantic-version discipline it should have introduced a new major
factor-facing lineage rather than appearing as another minor derivation label.

For the factor layer:

1. Treat adjustment lineage as a required dependency identity, not merely a
   provenance string.
2. Any future change to adjustment semantics must define whether it is a defect
   repair, compatible metadata addition, or breaking output change.
3. Breaking changes require a new major adjustment lineage and a new factor-set
   major version when published values change.
4. Existing exploratory labels may remain in history but cannot be reused as a
   stable basis without migration documentation.