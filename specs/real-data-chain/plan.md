# Real Data Chain Implementation Plan

## Milestone 0: Source Research Integration

1. Integrate Tushare/AData/TDX field matrices.
2. Decide R1 provider capabilities and missing-field strategy.
3. Freeze adapter normalization mapping for MVP.

## Milestone 1: Fetch Envelope and Errors

1. Add `FetchStatus`, `FetchResult`, and typed source errors.
2. Update router/gate to consume FetchResult.
3. Add tests for empty, partial, auth, quota, upstream, and success states.

## Milestone 2: Tushare Adapter

1. Add optional dependency group `tushare`.
2. Implement calendar, stock basic, and daily endpoints.
3. Normalize symbols, dates, units, and raw prices.
4. Map provider errors to FetchResult statuses.
5. Record source version/request metadata.

## Milestone 3: Calendar and Universe

1. Persist Tushare trade calendar as versioned canonical dataset.
2. Add static universe loader with canonical symbol validation.
3. Compute expected keys and coverage report.
4. Enforce complete/partial publication policy.

## Milestone 4: Daily Ingest CLI

1. Add `uq-ingest` entrypoint.
2. Implement date/source/schema/data-root arguments.
3. Load credentials from environment only.
4. Run route -> fetch -> normalize -> validate -> quality -> publish.
5. Write run report and stable exit codes.

## Milestone 5: End-to-End Real Smoke

1. Run one historical trading date with real Tushare token.
2. Validate canonical partition and manifest.
3. Read through FactorContext.
4. Export Qlib snapshot.
5. Re-run same date and verify immutable rejection.

## Milestone 6: AData Cross-Source

1. Add optional `adata` dependency.
2. Implement ADataAdapter from researched API matrix.
3. Compare overlapping fields with tolerance.
4. Add cross-source quarantine reports.

## Milestone 7: TDX Evaluation/Adapter

1. Select supported library based on research and risk.
2. Implement local/server daily-bar adapter if viable.
3. Treat TDX as secondary validation source.
4. Document operational/legal constraints.

## Milestone 8: Production Upgrade Preparation

1. Fill `bars_daily.v2` status and limits from researched sources.
2. Merge Tushare adjustment factor as owned field.
3. Define corporate-action linkage strategy.
4. Promote only after stable checklist gates pass.

## Definition of Done for Real Chain MVP

- Real trading day publishes canonical partition without manual cleanup.
- Every source failure has a typed status and safe exit code.
- Coverage is explicit and complete by default.
- Published data is manifest-valid, immutable, and FactorContext-readable.
- Qlib snapshot carries canonical lineage.
- Tests cover normalization, source failures, coverage gaps, and immutability.

## Research-Driven Adjustments

1. Milestone 2 must implement Tushare symbol/date/unit transforms exactly:
   - `vol * 100`;
   - `amount * 1000`;
   - `YYYYMMDD` to Shanghai day;
   - `.SH/.SZ` suffixes to `.XSHG/.XSHE`.
2. Milestone 2 requires account-permission probing for `adj_factor`,
   `trade_cal`, and optional production endpoints before enabling features.
3. Milestone 3 uses Tushare `trade_cal`; AData/TDX calendars are not authoritative.
4. Milestone 6 enables AData only after sample verification of volume/amount
   units and suspended-day behavior.
5. Milestone 7 requires an explicit license/legal gate before implementing any
   TDX dependency.


## Milestone R1-H: Hybrid Free-Tushare + Mootdx Path

Because only a 200-point Tushare token is currently available, adjust R1:

1. Implement TushareFreeAdapter for permitted `daily` only.
2. Probe `suspend_d` but do not assume access.
3. Add Mootdx health probe before enabling any TDX dependency.
4. Add MootdxAdapter only after bars connectivity passes on multiple servers.
5. Verify TDX volume unit using amount/close/volume relation.
6. Persist raw xdxr events separately.
7. Implement algorithm-versioned adjustment-factor derivation after event tests.
8. Derive historical calendar from index bars and mark its provenance.
9. Start with a small static instrument whitelist.
10. Keep suspension/lifecycle/index-membership/fundamental PIT explicitly out of
    this hybrid milestone.

Acceptance requires real-sample unit checks, cross-source close comparison,
calendar sampling, immutable publication, FactorContext reads, and safe rerun.

## R1-H Implementation Status (2026-08-22)

Overall status: `exploratory_research_prototype`.

Implemented, but not correctness-complete:

1. `FetchResult` is the router/adapter contract; bare source DataFrames no
   longer cross routing boundaries.
2. Mootdx datetime-index normalization is fixed for named index responses.
3. TDX-first routing is active; route completeness remains unverified without a
   trading calendar.
4. `DailyIngestPipeline` rejects unavailable or field-incomplete non-empty primary
   sources before quality publication.
5. `uq-ingest daily` CLI is available with typed exit codes.

Verified real smoke:

```text
server=115.238.90.165:7709
trade_date=2026-08-21
instruments=600000.XSHG,000001.XSHE
status=published
rows=2
amount/(close*volume) ~= 0.9984, 1.0025
```

Review found additional blockers in
`specs/real-data-chain/cr-2026-08-22.md`: source exceptions, 800-bar history,
expected coverage, strict schema validation, installed CLI resolution, persisted
run reports, typed exit codes, and independent cross-validation.

First remediation pass:

1. Mootdx client/per-symbol/normalization failures now return typed
   `UPSTREAM_ERROR` results instead of escaping the adapter.
2. Tushare dependency/token failures are typed as unsupported/auth failures.
3. Route completeness checks observed success status, delivered fields, and
   instruments, but the current date check is incorrect because it uses natural
   days rather than trading days.
4. Empty primary partitions for a configured universe are rejected.
5. Exact schemas reject unexpected columns and weak date representations.
6. Pipeline catches routing/runtime/storage failures and emits structured
   reports with stable statuses.
7. CLI supports explicit project root, persists run reports under
   `<data-root>/runs`, and maps known failures to stable exit codes.
8. Optional real-source dependencies are declared via the `real` extra.

Second careful review found the first remediation pass was incomplete:
natural-day route checks, missing pipeline consumption of coverage, a real-shaped
Mootdx datetime-index failure, open 800-bar history, unstructured publication
I/O, and non-atomic run reports. Details are in `cr-2026-08-22.md`.

Second remediation pass (2026-08-22):

1. Fixed named-column Mootdx normalization for reordered indexed responses.
2. Router now emits `unverified` unless a trading-calendar provider is injected;
   natural days are no longer treated as trading sessions.
3. Pipeline now consumes route coverage, warns on `unverified`, and can require
   complete coverage before publication.
4. Publication I/O failures are converted into structured `storage_failure`
   reports.
5. CLI run reports are atomic and include date/config fingerprint/run UUID.

Third remediation pass (2026-08-22):

1. Added bounded TDX pagination using an ISO-range upper-bound estimate and
   explicit exhausted-history warnings.
2. Hardened the health probe and integrated it into ingest diagnostics; failed
   probes block publication as `primary_source_unhealthy`.
3. Added `TradingCalendar` with provenance; router now supports either a
   provider or calendar and remains `unverified` without one.
4. Router validates FetchResult dataset/schema/source envelope identity.
5. Accepted AkShare/AData as auxiliary calendar sources in runtime decisions.
6. Documented credential, deployment, and local retention policy.

Fourth remediation pass (2026-08-22):

1. Synchronized `uv.lock` with the `real` extras; Mootdx and Tushare are now
   locked.
2. Added canonical Parquet publish/read roundtrip and schema regression.
3. TDX client now probes a configured server list and fails over to the first
   responsive server.
4. Free-Tushare permissions were verified live: `daily`, `trade_cal`, and
   `adj_factor` are available; `suspend_d` and `stk_limit` remain unavailable.
5. Hybrid TDX + free-Tushare cross-validation published two real rows with
   AkShare/Sina calendar coverage and persisted health diagnostics.

Fifth remediation pass (2026-08-23):

1. Added TDX per-page retry with exponential backoff.
2. Run reports now include request, schema/contract fingerprints, calendar
   provenance, selected source server, and code version.
3. Added local retention cleanup for expired raw/quarantine artifacts and stale
   staging directories.
4. Added `scripts/nightly_regression.sh` for tests plus real hybrid smoke and
   retention dry-run.
5. Nightly regression passed with four instruments across SH/SZ main board,
   ChiNext, and STAR Market.

Sixth remediation pass (2026-08-23):

1. Added contract-controlled verified-only publication. Unverified or
   unexplained missing primary keys cannot enter canonical storage.
2. Added switchable raw-response capture (`raw_capture`) to TDX and free-Tushare
   adapters, including request metadata.
3. Persisted raw artifact references and SHA-256 checksums in immutable
   partition manifests.
4. Added conservative AkShare current-listing classification. Only valid
   configured symbols absent from the current exchange list can be classified as
   `not_listed_expected_missing`; suspension, delisting history, historical PIT
   lifecycle, and invalid symbols remain unknown and rejected.
5. Offline suite passed (`44 tests`). Real hybrid smoke published four verified
   rows with two captured raw artifacts, cross-validation, calendar provenance,
   and no warnings.

Seventh remediation pass (2026-08-23):

1. Upgraded adjustment derivation to `adj_factor.derived_v2`; cash dividends now
   use the event pre-close with the exchange-style formula. Missing pre-close
   for a cash event is rejected rather than silently neutralized.
2. Added `allow_unknown_missing` (default false). Conservative unknown lifecycle
   rows remain rejected unless explicitly released by configuration.
3. Fixed historical TDX requests by reserving holiday bars before ISO-date
   normalization; single-day historical sessions no longer return an empty
   latest-bar window.
4. Multi-session real regression published four verified rows on each of
   2026-08-19, 2026-08-20, and 2026-08-21 with Tushare cross-validation and no
   warnings.
5. Proved clean Python 3.12 installation with real/dev extras: all 52 tests and
   real adapter import checks passed.
6. Retention dry-run found zero expired raw/quarantine objects and zero stale
   staging directories; the three-day sample used 144 KiB total.

Eighth remediation pass (2026-08-23):

1. Changed TDX recent-history pagination to request a full deterministic page;
   live probes confirmed that short offsets do not address an arbitrary target
   date. This closes a second class of historical-date empty responses.
2. Located real cash-only corporate actions for 600000, 000001, and 300750 via
   TDX XDXR and confirmed matching raw TDX bars on each ex-date.
3. Added AkShare exchange delisting classification using recorded SH
   suspension / SZ termination dates; future termination dates remain unknown,
   while effective terminations are conservatively classified as expected
   missing. Suspension history remains unknown and is never inferred.
4. Expanded offline suite to 51 passing tests.

Ninth remediation pass (2026-08-23):

1. Added optional `TUSHARE_TOKENS` comma-separated rotation with backward-
   compatible `TUSHARE_TOKEN` fallback. Rotation is documented as low-frequency
   validation support requiring explicit authorization from each token owner.
2. Verified that the local free token exhausted its daily `adj_factor` quota
   (`5/day`); provider-factor numerical validation remains pending until quota
   resets or an authorized additional token is supplied.
3. Probed AkShare/Baidu suspension notices back to at least 2018. The endpoint
   returns event rows rather than a complete daily suspended-state table, so it
   is suitable only for exact-date evidence.
4. Implemented exact-date SH/SZ A-share suspension classification as
   `suspended_expected_missing`; historical windows are not reconstructed.
5. Live probes confirmed expected behavior: `601121.XSHG` was classified on
   2025-01-07 and unknown on 2025-01-13 after resumption.
6. Offline suite passed (`54 tests`); real hybrid regression published four
   verified rows on each of 2026-08-19, 2026-08-20, and 2026-08-21 without
   errors or warnings.

Tenth remediation pass (2026-08-23):

1. Completed provider-factor comparison using the first explicitly authorized
   token configured in `TUSHARE_TOKENS`; no token values were printed or
   persisted.
2. Compared three live cash-only corporate actions against Tushare:
   600000 on 2026-07-16, 000001 on 2026-06-12, and 300750 on 2026-08-10.
3. Found that the prior cash formula treated TDX `fenhong` with the wrong price
   scale. Upgraded derivation to `adj_factor.derived_v3`: the backward cash
   multiplier is now `pre_close / (pre_close - per-share cash)`, combined
   multiplicatively with share-count changes.
4. Provider comparison results are within tolerance: relative differences were
   approximately 0.00026%, 0.00002%, and 0.00072% for the three events. The
   residual is consistent with provider factor rounding.
5. Added provider-ratio golden regression and expanded the suite to 55 passing
   tests.

Eleventh governance pass (2026-08-23):

1. Upgraded factor-layer design after a blocking review. Formal manifest schema,
   checksum encoding, deterministic generation identity, nested Hive path rules,
   adjustment snapshot binding, quality thresholds, and an executable/blocked
   acceptance matrix are now specified.
2. Renamed ambiguous turnover semantics and defined zero-range null behavior.
3. Corrected architecture examples from historical `bars_daily.v1` to the
   implemented `bars_daily.research-v1`.
4. Bumped adjustment lineage to `adj_factor.derived_v4` as a governance marker
   for the factor-facing breaking-change rule; persisted effective-date snapshots
   remain required before adjusted factor publication.

Still open before broader research use: complete historical suspension coverage,
longer multi-session/server-failover evidence, and retention execution after
artifacts age beyond policy.

Remaining before research-grade claim:

1. Probe at least three servers across repeated sessions.
2. Implement factor registry/engine/store, formal manifests, and quality gates;
   only canonical unpublished-read acceptance is executable today.
3. Extend provider-factor validation beyond cash-only events when additional
   permissions allow.
4. Integrate authoritative historical suspension coverage if a reliable source is
   found; otherwise continue rejecting unevidenced missing rows.
5. Run retention cleanup after raw artifacts exceed 30 days.

## 2026-08-23 — Twelfth governance pass

- Added a governed canonical manifest JSON Schema and centralized manifest validation.
- Added `generation_id` canonicalization plus an explicit partition trust-anchor field; readers reject regenerated or tampered manifests.
- Canonical manifests now carry schema, code, run, timestamp, lineage, quality, and raw-artifact lineage.
- Adjustment derivations now emit a deterministic snapshot ID and effective-date-table checksum.
- `FactorContext.read_bars` now supports verified date ranges, instrument filtering, field projection, and rejects unpublished dates.
- Aligned factor documentation to `adj_factor.derived_v4`, nested Hive identity paths, and the runtime schema identifier.
- Added regression coverage for manifest tampering, adjustment snapshot binding, and multi-partition factor reads.
