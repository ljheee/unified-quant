# Unified Quant Data Architecture

Status: **v0.2 design contract**  
Review decision: **architecture feasible; conditionally approved for continued implementation; not production-approved**.  
Implementation status: prototype v0.1 does not satisfy this document and remains prototype-only.  
Release gate: `specs/stable-release-checklist.md`.

## 1. Goals

1. Make source systems replaceable: TDX, Tushare, AkShare, broker/vendor APIs,
   or local files.
2. Give factors one stable internal data contract instead of exposing provider
   APIs.
3. Preserve point-in-time correctness for announcement-driven data.
4. Make every persisted dataset reproducible through schema, source lineage,
   quality results, checksums, and producer metadata.
5. Treat Qlib output as a derived snapshot, not primary storage.
6. Fail explicitly when required capabilities are missing; never return a silent
   partial dataset.

## 2. Pipeline Contract

```text
SourceCapability declaration
        |
        v
SourceAdapter.fetch(request) -> source-normalized rows + fetch report
        |
        v
FieldMapper: source fields -> canonical fields
        |
        v
SchemaGate: dtype/unit/key/invariant validation
        |
        v
QualityGate: row checks, cross-source checks, quarantine report
        |
        v
MergePolicy: ownership, complement merge, conflict policy
        |
        v
CanonicalStore / PitStore: immutable partition + manifest
        |
        +--> FactorContext (canonical pandas API)
        +--> QlibExporter -> external provider_uri snapshot
```

The repository does not store Qlib `.bin` artifacts. A Qlib directory is an
immutable, rebuildable dataset published under an external `provider_uri`.

## 3. Canonical Schema Rules

A canonical schema is versioned and immutable after publication. It must define:

- field names;
- physical dtypes;
- units;
- nullable rules;
- adjustment semantics;
- time semantics and timezone;
- key and sort rules;
- row invariants;
- allowed null sentinel policy.

Unit or semantic changes require a new schema version. Consumers declare a
compatible schema range.

### 3.1 `bars_daily.v1` (historical design name)

> Runtime correction: the implemented research schema is
> `bars_daily.research-v1`. The generic historical design name `bars_daily.v1`
> is not a runtime schema identifier. Production work must either migrate the
> examples to an implemented research schema or publish an explicit production
> schema version.

Key: `(instrument, datetime)`  
Instrument format: `600000.XSHG`, `000001.XSHE`  
Timezone: Asia/Shanghai  
`datetime`: trading day

Required core fields:

| Field | Dtype | Unit | Semantics |
|---|---|---|---|
| `instrument` | string | - | canonical instrument id |
| `datetime` | datetime64[ns] | trading day | session date |
| `open` | float64 | CNY | raw price |
| `high` | float64 | CNY | raw price |
| `low` | float64 | CNY | raw price |
| `close` | float64 | CNY | raw price |
| `volume` | float64 | share | executed volume |
| `amount` | float64 | CNY | executed turnover |

Required lifecycle/risk fields for production use:

| Field | Dtype | Unit | Semantics |
|---|---|---|---|
| `status` | string | enum | `trading`, `suspended`, `delisted`, `unknown` |
| `limit_up` | float64 nullable | CNY | price limit; null when not applicable |
| `limit_down` | float64 nullable | CNY | price limit; null when not applicable |
| `adj_factor` | float64 nullable | ratio | cumulative adjustment factor |

The initial prototype omitted `status`, `limit_up`, `limit_down`, and did not
merge the owner-provided `adj_factor`; these are mandatory before production
paper trading.

Invariants:

```text
high >= low
high >= max(open, close)
low <= min(open, close)
volume >= 0
amount >= 0
adj_factor is null or adj_factor > 0
limit_up is null or limit_up > 0
limit_down is null or limit_down > 0
status in {trading, suspended, delisted, unknown}
suspended/delisted rows may have zero volume, otherwise volume > 0 unless explicitly whitelisted
```

Raw prices plus cumulative `adj_factor` are stored separately. A dataset must
not mix raw and adjusted prices in the same price column.

## 4. Source Capability Contract

A source identity alone is insufficient. Capabilities are declared at
`(source, dataset, schema_version)` granularity.

Normative configuration shape:

```yaml
dataset: bars_daily
schema_version: v1
required_fields:
  [instrument, datetime, open, high, low, close, volume, amount, status]

sources:
  tdx:
    adapter: osq_tdx.TdxAdapter
    priority: 10
    reliability: high
    latency_class: intraday
    authentication: optional
    coverage:
      market: [XSHG, XSHE]
      period: daily
      start: "20000101"
    datasets:
      bars_daily:
        schema_version: v1
        fields:
          [instrument, datetime, open, high, low, close, volume, amount, status]
        missing_fields: [adj_factor]

  tushare:
    adapter: osq_tushare.TushareAdapter
    priority: 20
    reliability: high
    latency_class: end_of_day
    authentication: required
    quota:
      requests_per_minute: 300
    datasets:
      bars_daily:
        schema_version: v1
        fields:
          [instrument, datetime, open, high, low, close, volume, amount, adj_factor]
        missing_fields: [status, limit_up, limit_down]
```

A source is eligible only when it provides every requested field and supports the
exact schema version. Capability metadata is descriptive; eligibility is based on
declared fields and schema compatibility.

## 5. Routing Policy

Routing must be deterministic and explicit.

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

## 7. Quality Gate

Validation happens before and after merge.

Required checks:

- schema dtypes and nullability;
- key uniqueness;
- sortability of key fields;
- OHLC and non-negative invariants;
- calendar/session alignment;
- suspended/lifecycle consistency;
- limit-price sanity;
- adjustment-factor continuity;
- duplicate revision detection for PIT datasets;
- cross-source numeric tolerance;
- minimum secondary-source coverage.

The gate returns a structured report instead of reducing failures to an exception
string:

```json
{
  "status": "rejected",
  "accepted": false,
  "row_count": 1000,
  "quarantined_keys": ["600000.XSHG|2026-08-21"],
  "conflicts": [{"field": "close", "primary": 12.0, "secondary": 13.0}],
  "coverage": {"close": {"compared_rows": 980}},
  "errors": []
}
```

Policy options are `reject_all`, `quarantine_rows`, or `accept_with_warnings`.
Production publication defaults to `reject_all`.

## 8. PIT Rules

Announcement-driven datasets separate reporting period from visibility time:

- `period`: reporting period;
- `announcement_datetime`: public availability timestamp;
- `revision`: monotonically increasing revision;
- `value`: reported value;
- `source_event_id`: stable external event identifier.

Rules:

1. Readers at decision time `t` observe only
   `announcement_datetime <= t`.
2. Revisions are append-only; old revisions cannot be overwritten.
3. The effective row at `t` has the greatest
   `(announcement_datetime, revision)` not later than `t`.
4. Reads are exposed through an explicit `read_asof(...)` API.
5. Publication manifests record event count, revision count, duplicate keys, and
   latest revision boundaries.

The current Parquet store does not yet implement `read_asof`; it is not a PIT
store until this API exists.

## 9. Storage Protocol

External data root:

```text
$UQ_DATA_ROOT/
  canonical/
    bars_daily/
      v1/
        date=2026-08-22/
          data.parquet
          manifest.json
  qlib/
    a_share_daily/
      v20260822/
```

Publication algorithm:

1. Validate the input against schema before writing.
2. Create a unique staging directory:
   `date=2026-08-22.staging.<run-id>`.
3. Write deterministic Parquet bytes.
4. Compute SHA-256 over the final file.
5. Write a manifest containing:
   - dataset and schema version;
   - partition date;
   - row count and column fingerprint;
   - data checksum;
   - schema checksum;
   - producer run id;
   - UQ version;
   - source versions;
   - lineage and quality report digest;
   - created timestamp.
6. Read back the Parquet file and revalidate row count/schema/checksum.
7. Atomically publish the directory.
8. Treat `data.parquet` without a sibling `manifest.json` as unpublished and
   reject reads.

Existing immutable partitions are not overwritten. Republishing requires a new
run/partition version or an explicit maintenance operation that writes a new
replacement manifest generation.

## 10. Reader Contract

Factor code consumes a typed `FactorContext`, not a source SDK:

```python
context.read_bars(
    instruments=["600000.XSHG"],
    start="2026-01-01",
    end="2026-08-22",
    fields=["open", "high", "low", "close", "volume"],
    schema="bars_daily.research-v1",
)
```

Reader guarantees:

- canonical column order and dtypes;
- stable instrument format;
- timezone-normalized datetime;
- no silent schema migration;
- no silent partial coverage;
- manifest-bound dataset version.

## 11. Qlib Boundary

Qlib export occurs after CanonicalStore/PitStore:

```text
Canonical/PIT Store
  -> dataset selector
  -> QlibExporter
  -> external immutable provider_uri
  -> qlib.init(provider_uri=...)
  -> Alpha158 / expression factors / model training
```

The exporter must write a source manifest mapping Qlib snapshot version to UQL
canonical partitions, schemas, checksums, and source versions.

## 11.4 Model Feature Preprocessing Boundary

```text
FactorStore (accepted factors)
  -> Model Dataset Feature Schema
  -> feature_preprocessing.v1 (stateless, cross-sectional transform)
  -> Model Dataset / Qlib runtime
```

Preprocessing is model governance, not factor computation. The first slice may
only use same-date cross-sectional stateless transforms. Qlib runtime processors
are disabled unless they consume a governed preprocessing manifest.

## 11.5 Portfolio and Backtest Layer Boundary

```text
Model Layer (prediction_set.v1)
  -> Portfolio Layer (portfolio_definition.v1, target_weights.v1)
  -> Backtest Layer (backtest_config.v1, backtest_result.v1)
```

The portfolio layer converts published prediction partitions into constrained
target weights. The backtest layer simulates execution against governed price
data with costs, T+1 alignment, and trading guards. Both layers inherit the
model layer's manifest, checksum, and quality-report governance.

## 11.6 Research Chain Orchestrator Boundary

```text
Reviewed Research Run Request
  -> Request Resolver / Stage Plan
  -> External Quality Decision Provider / Trust Root
  -> Factor Layer
  -> Model Layer (dataset -> Qlib export -> run -> prediction)
  -> Portfolio Layer
  -> Backtest Layer
  -> Research Run State / Result Evidence Index
```

Research Chain is a governed orchestrator, not a new computation engine. It
resolves reviewed templates and immutable inputs, invokes owning-layer stores,
records stage lineage, and reconciles readback evidence. It never bypasses
manifests, quality decisions, immutable publication, or accepted reader APIs.
The external quality decision provider and its trust root are separate inputs;
the runner can only look up and verify reviewed decisions, never create or sign
them.

## 12. Acceptance Criteria for the Stable Contract

The architecture is considered implemented only when all pass:

1. A TDX-like source can replace another complete daily-bar source without
   changing factor code.
2. A required capability gap causes an explicit routing failure.
3. An owned `adj_factor` appears in the merged canonical frame with lineage.
4. Cross-source conflicts produce structured quarantine reports.
5. Every published partition has a valid manifest and checksum.
6. Existing published partitions are immutable.
7. Financial/fundamental reads require an as-of PIT API.
8. Qlib snapshots can be rebuilt from canonical partition manifests.
9. Tests cover conflicting sources, incomplete sources, duplicate keys, invalid
   OHLC, missing owner fields, failed publication, and PIT revision selection.

## 13. Subagent Review Decision

Two independent reviews inspected the contract model, field scope, architecture,
and feasibility.

### Agreed Decision

- The target pipeline is architecturally sound and technically feasible.
- Python 3.11, pandas, YAML, Parquet, local immutable partitions, and a derived
  external Qlib snapshot are sufficient for the current research/local platform
  scope.
- Continue evolving the repository; do not restart it.
- The existing code is a v0.1 prototype and must not be called a stable
  contract implementation.
- Production approval is blocked until P0 gaps and acceptance tests pass.

### Binding Corrections

1. Resolve the `bars_daily.v1` dual meaning:
   - keep an explicitly research-only prototype schema, or
   - publish a new production schema version containing lifecycle and risk
     fields.
2. Capability must be modeled as `(source, dataset, schema_version)` with
   complete fields plus coverage, authentication, latency, quota, and reliability
   metadata.
3. Remove silent partial fallback. No complete source means routing failure;
   partial data requires explicit request opt-in and a partial coverage marker.
4. Implement merge planning for overlapping, owned, and complement fields.
5. Return structured quality reports with conflict keys, tolerances, coverage
   counts, policy decisions, and report checksums.
6. Harden publication: pre-validation, unique staging run id, publication lock,
   data/schema/report checksums, readback validation, manifest-last promotion,
   and rejection of unpublished or already-published partitions.
7. Add append-only PIT revisions and `read_asof(decision_time)`.
8. Extend manifest governance to canonical-v2 and factor manifests; preserve
   the already implemented manifest-validating `FactorContext` boundary.
9. Formalize instrument grammar, trading-calendar identity, timestamp semantics,
   correction windows, fetch-result taxonomy, and manifest JSON Schema.

### Additional Field Scope Guidance

Keep execution-critical fields explicit in bars or a directly linked dataset:

- lifecycle/status;
- price limits;
- adjustment factor;
- corporate-action reference;
- board/segment and ST metadata;
- calendar/session identifier.

Research extensions may remain separate datasets to avoid overloading daily
bars:

- free-float shares and turnover;
- VWAP;
- source-specific raw identifiers;
- vendor quality flags.

### Minimum Implementation Order

1. Strict config/schema model and cross-validation between YAML documents.
2. Complete-source routing and explicit partial mode.
3. Owner/complement merge with per-field lineage.
4. Production bars schema or clearly separated research/production versions.
5. Structured quality report and quarantine policy.
6. Hardened atomic publication protocol.
7. Fundamental PIT schema, revision writer, and as-of reader.
8. Canonical-v2 identity migration, factor manifest schema, universe/quality
   artifact contracts, and QlibExporter source-manifest alignment.
9. Acceptance tests for every documented failure path and pluggable replacement
   scenario.

## 14. Current Implementation Gap Summary

Prototype v0.2 is useful for YAML validation, basic routing, cross-field
comparison, simple Parquet publishing, manifest-first `FactorContext` reads,
canonical publication/checksums, and prototype manifest anchoring. It still
does not satisfy the full stable contract because it lacks stable canonical
content generation, external trust anchoring, production field completeness,
complement merge coverage, structured factor quality reports, robust atomic
factor publication, persisted adjustment snapshot as-of reads, and end-to-end
pluggable source adapters.
