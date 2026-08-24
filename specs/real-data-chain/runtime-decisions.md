# Runtime Decisions

Date: 2026-08-22

## Credential Policy

- A 200-point free `TUSHARE_TOKEN` is allowed for local research only.
- `TUSHARE_TOKENS` may contain comma-separated tokens for low-frequency
  validation only. Every token owner must explicitly authorize that use.
- `TUSHARE_TOKEN` remains supported as a single-token fallback. Requests rotate
  across configured tokens, but rotation must not be used to circumvent provider
  terms or build bulk production datasets.
- Put credentials in `.env`; `.env` and `.env.*` are ignored.
- `.env.example` contains no secret.
- Tokens must never enter logs, manifests, canonical data, or run reports.

## Calendar Policy

- AkShare and AData are accepted as auxiliary trading-calendar sources.
- Calendar provenance is mandatory.
- Without a calendar, route coverage is `unverified`; outputs remain exploratory.
- Index-derived calendars are fallback evidence, not an authoritative source.

## Verified-Row and Lifecycle Policy

- `verified_only: true` is the research default: a row is published only when
  the primary source returned that instrument/date key.
- Missing keys are rejected unless an enabled auxiliary lifecycle source can
  conservatively classify them as expected missing.
- AkShare exchange listing snapshots may classify a valid, configured symbol as
  `not_listed_expected_missing` when it is absent from the current SH/SZ list.
- AkShare exchange delisting snapshots may classify a valid configured symbol as
  `delisted_expected_missing` only on and after its recorded SH suspension /
  SZ termination date.
- AkShare/Baidu daily suspension notices can classify same-day SH/SZ A-share
  suspensions as `suspended_expected_missing`. Evidence is event-based and must
  cover the exact trade date.
- Historical suspension windows are not reconstructed by default because the
  feed records announcements rather than a complete daily state table.
- Rows without exact-date suspension evidence remain
  `unknown_requires_review`.
- Historical listing snapshots are current-state evidence, not point-in-time
  listing status; unlisted historical dates remain conservative unless the
  explicit `allow_unknown_missing` switch is enabled.
- AData remains accepted by contract for auxiliary lifecycle evidence but is not
  implemented in this pass.
- Suspension is never inferred from zero volume or a missing bar.

## Raw Capture Policy

- `row_policy.raw_capture` controls provider-response capture; it defaults to
  true and is explicitly set to true for the research dataset.
- Captured bytes are written under `<data-root>/raw/<trade-date>/`.
- Canonical manifests record each raw artifact reference and SHA-256 checksum.
- Raw files contain no credentials and are subject to the 30-day local
  retention policy.

## Deployment Policy

- Current target is local research only.
- Future open-source publication is allowed because credentials stay outside
  the repository and public TDX servers remain configurable research inputs.
- Clean-install and lockfile reproducibility must pass before any external
  release.

## Data Retention

Keep:

1. immutable canonical Parquet partitions;
2. dataset manifests;
3. atomic run reports;
4. quality reports/checksums embedded in manifests;
5. source capability/config snapshots needed to interpret a run.

Keep temporarily, with explicit retention limits:

1. raw provider responses for debugging and golden-sample construction;
2. quarantine partitions for failed validation;
3. staging directories after abnormal exits.

Do not keep by default:

1. credentials;
2. full tick/minute data unless a new research task requires it;
3. duplicate raw copies after canonical validation;
4. generated Qlib binaries in Git.

Suggested local retention:

```text
canonical + manifests + runs: retain
raw: 30 days
quarantine: 30 days after resolution
staging: delete after successful publication
```
