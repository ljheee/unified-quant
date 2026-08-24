# Real Data Chain Specification

Status: active hybrid draft; TDX-first is enabled for the current free-tier credential state.  
Goal: produce trustworthy daily canonical bars from real A-share providers and
make them consumable by FactorContext/QlibExporter.

## MVP Scope

The first real chain now uses TDX/Mootdx as the credential-free primary quote
source, with Tushare `daily` retained as a cross-check when a token is present.
It does not attempt full production `bars_daily.v2`.

```text
Mootdx / TDX primary
  -> MootdxSourceAdapter -> FetchResult
Tushare free cross-check (optional)
  -> TushareFreeAdapter -> FetchResult
  -> complete-source routing
  -> canonical bars_daily.research-v1
  -> QualityGate
  -> CanonicalStore
  -> FactorContext
  -> QlibExporter
```

Success means a daily command can publish:

```text
$UQ_DATA_ROOT/canonical/bars_daily/research-v1/date=YYYY-MM-DD/
```

with valid manifest, checksum, row coverage, and run report.

## Non-Goals for MVP

- production `bars_daily.v2`;
- corporate-action reconstruction;
- fundamentals PIT;
- index membership PIT;
- minute/tick data;
- live trading.

## Runtime Contract

### Request

```python
run_daily_ingest(
    trade_date="YYYY-MM-DD",
    instruments=None,          # None = configured universe
    schema="bars_daily.research-v1",
    source="tushare",
)
```

### Required inputs

- Tushare token from environment or credential loader;
- trading calendar for requested exchange;
- instrument universe snapshot;
- canonical schema/config;
- external data root.

### Output artifacts

- canonical `data.parquet`;
- canonical `manifest.json`;
- run report JSON;
- optional Qlib snapshot manifest.

## Adapter Contract

Every real adapter must return `FetchResult`, never a bare DataFrame.

```python
@dataclass
class FetchResult:
    rows: pd.DataFrame
    status: FetchStatus
    source: str
    dataset: str
    schema_version: str
    requested_fields: frozenset[str]
    delivered_fields: frozenset[str]
    observed_start: date | None
    observed_end: date | None
    source_fetched_at: datetime
    retryable: bool
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
```

Statuses:

- `success`;
- `empty`;
- `partial_fields`;
- `partial_rows`;
- `auth_failed`;
- `rate_limited`;
- `upstream_error`;
- `unsupported_request`.

Only `success` may enter default publication. `empty` is valid only when the
calendar/universe policy proves no rows are expected.

## Normalization Rules

Adapter/mapper responsibilities:

1. convert provider symbol to canonical `600000.XSHG` / `000001.XSHE`;
2. normalize date to day resolution Shanghai trading day;
3. convert volume to shares;
4. convert amount to CNY;
5. preserve raw prices;
6. reject NaN for non-nullable fields;
7. emit field mapping/version in run metadata.

No factor or store code may perform provider-specific cleanup.

## Coverage Policy

The chain must distinguish:

- trading session with row;
- suspended instrument with expected no-row policy;
- non-session date;
- instrument not listed;
- delisted instrument;
- provider omission.

MVP requires:

- Tushare trade calendar;
- configured instrument universe snapshot;
- expected row policy for selected universe;
- coverage counts by instrument/exchange/date;
- explicit `coverage_status=complete|partial`;
- publication rejection when required coverage is incomplete unless an explicit
  research override is supplied.

## Quality Policy

Before publication:

- schema validation;
- key uniqueness;
- OHLC invariants;
- non-negative volume/amount;
- date/calendar membership;
- universe membership;
- duplicate detection;
- provider field completeness;
- optional secondary comparison after TDX/AData adapters exist.

Quality report must include run ID, row counts, missing universe keys, rejected
keys, provider status, field mapping, and checksum.

## Publication

Reuse hardened `CanonicalStore`:

- unique staging;
- publication lock;
- schema validation;
- readback;
- SHA-256;
- manifest-last;
- atomic promotion;
- immutable partition rejection.

Run report is written after canonical publication and must not be required to
make data readable.

## CLI

MVP entrypoint:

```bash
uq-ingest daily \
  --date 2026-08-21 \
  --data-root /data/uq
```

Exit codes:

- `0`: published complete partition;
- `2`: expected empty;
- `3`: quality/routing failure;
- `4`: source/auth/quota failure;
- `5`: storage conflict.

## Configuration

```yaml
source: tdx
schema: bars_daily.research-v1
universe:
  type: static_file
  path: config/universe/a_share_core.txt
calendar:
  source: tushare
  exchange: XSHG
credentials:
  mode: none
quality:
  require_full_universe: true
publication:
  allow_research_partial: false
```

Credentials are never written into manifests or logs.

## Research Phases

### Phase R1: Tushare-only MVP

Superseded by the hybrid credential strategy. Tushare remains the documented
reference source, but the executable R1 path is now TDX-first.

- FetchResult;
- TushareAdapter for daily bars/calendar/basic list;
- symbol/unit normalization;
- calendar/universe coverage;
- daily ingest CLI;
- canonical publication and run report.

### Phase R2: Cross-source research

- AData adapter;
- TDX adapter where license/maintenance risk is acceptable;
- field/capability matrices;
- cross-source tolerance and quarantine.

### Phase R3: Production bars

- promote `bars_daily.v2`;
- status/limit/adj_factor merge;
- corporate-action reference strategy.

### Phase R4: PIT datasets

- index membership;
- fundamentals/events;
- as-of reads integrated into FactorContext.

## Acceptance Criteria for R1

1. A configured Tushare token can fetch a real trading day.
2. Adapter output validates against `bars_daily.research-v1`.
3. Non-session date returns expected-empty and publishes no canonical data.
4. Missing instruments produce a structured partial/coverage failure by default.
5. CanonicalStore publishes a valid manifest/checksum partition.
6. FactorContext can read the published partition.
7. QlibExporter can create a lineage-bound snapshot.
8. Re-running the same date fails safely due to immutability.
9. Token is absent from logs, manifests, and reports.

## Current Credentials Decision

The available Tushare account has 200 points, so paid-only endpoints are
unavailable. Use the hybrid policy in
`specs/real-data-chain/hybrid-tushare200-mootdx.md`.

R1 becomes a hybrid research chain:

- Tushare `daily`: primary raw OHLCV;
- Mootdx: cross-check, xdxr events, derived adjustment factor;
- index-derived calendar: historical sessions only;
- static whitelist universe;
- ST/delisting/membership/fundamentals PIT are out of scope.

## Source Decisions from Research

1. Tushare is the R1 primary source.
2. `daily.vol` is lots and must multiply by 100.
3. `daily.amount` is thousand CNY and must multiply by 1000.
4. Raw prices stay raw; adjustment factors are separate owned data.
5. AData may become an R2 supplemental source after empirical unit checks.
6. TDX is deferred pending library/license/legal decision.
7. See `source-matrices.md` for the integrated field matrix and references.
