# Data Sources, Calendars, and Training Readiness

> Current behavior reference. Last checked: `2026-10-31` against the working tree based on HEAD `cd1befb4b5fcbe954e16aea148cc2332dbc1dc21`. Based on source code, documented measurements, and tests; unverified items are marked `UNKNOWN`.

## Responsibilities and Boundaries

This page covers the parts that decide what the platform *may* believe: vendor source adapters, the trading-session calendar and its provenance, lifecycle classification of missing rows, and the pre-training readiness gate over governed inputs. It is the evidence layer for `docs/subsystems/ingest-and-canonical-store.md` and for `docs/subsystems/model-layer.md`.

It does **not** own routing decisions (the router consumes the calendar, the calendar does not decide routing) and does not own factor or model computation.

## Authoritative Sources

| Fact | Authoritative path |
| --- | --- |
| Source capability contract and adapter protocol | `src/uq/contracts/capabilities.py:10` `SourceAdapter`, `:25` `SourceCapability` |
| Typed fetch envelope and status taxonomy | `src/uq/sources/fetch.py:12` `FetchStatus`, `:14` `FetchResult` |
| TDX adapter (events, bars) | `src/uq/adapters/mootdx_source.py:13` `MootdxSourceAdapter` |
| Tushare adapters | `src/uq/adapters/tushare.py`, `src/uq/adapters/tushare_free.py`, `src/uq/adapters/tushare_2k.py` |
| Bounded Tushare dependency acquisition | `src/uq/adapters/tushare.py` `research_data`, `src/uq/contracts/tushare_dependency.py` `TushareDependencyStore`, `scripts/fetch_tushare_dependency.py`, `scripts/fetch_tushare_industry_history.py` |
| Deterministic test adapter | `src/uq/adapters/demo.py:13` `DeterministicDemoAdapter` |
| Independent session calendar provider | `src/uq/adapters/eastmoney.py:43` `EastMoneySessionProvider` |
| Calendar wrapper and index-derived sessions | `src/uq/market/calendar.py:8` `TradingCalendar`, `src/uq/market/calendar.py:43` `IndexCalendarDeriver` |
| Lifecycle classification of missing rows | `src/uq/market/lifecycle.py:8` `AkShareLifecycleProvider` |
| Static universe loader | `src/uq/market/universe.py:6` `StaticUniverseLoader` |
| Readiness gate (pure, fail-closed) | `src/uq/pipeline/readiness.py:91` `assess_readiness`, `:519` `assess_factor_label_linkage` |
| Readiness CLI | `scripts/validate_training_readiness.py` |
| Measured source behaviour | `docs/user-guide.md` §5, `docs/corporate-action-validation.md`, `docs/universe-coverage.md` |

## Primary Runtime Paths

**Adapters** implement `fetch(dataset, instruments, start, end, fields) -> FetchResult`, returning rows plus a typed status, delivered-field set, optional raw payload bytes, and warnings/errors. The router rejects an adapter whose `source_name` disagrees with its registration key, so envelope identity is enforced at the boundary.

**Calendar.** `TradingCalendar` is a thin predicate over an explicit session provider. `EastMoneySessionProvider` derives sessions from a third vendor's index klines so a wrong session list cannot cancel itself out between two bar vendors, and it only certifies the contiguous range its payload actually covers: a window reaching past the newest published bar raises `IndependentCalendarError` rather than returning an empty, coverage-passing set.

**Lifecycle.** When a verified-only row gap appears, `AkShareLifecycleProvider.classify_missing` and `.suspension_window` classify it (suspended, not listed, delisted, unknown). A gap that cannot be explained fails closed unless `allow_unknown_missing` is configured; an explained gap is reported as a warning, not silently dropped.

**Readiness.** `assess_readiness` is pure and fail-closed: PIT membership per session, required bar fields present and non-null, bar gaps reconciled against the governed suspension source, label horizon reaching far enough that the last sessions are trainable, and factor/label linkage. A canonical row explicitly marked `suspended` agrees with a suspension record; a row with another status (including `unknown`) on that session is a conflict. Excluded instruments are an explicit, auditable parameter; an excluded instrument that actually has no gap is itself a blocking finding, so the exclusion list cannot quietly shrink the universe.

## Multiple Implementations / Entry Points

- **TDX `xdxr` is the reliable path; `bars`/`quotes` are frequently blocked on measured hosts.** Probing with a quote-shaped command and concluding "the server is down" is the documented, measured mistake (`src/uq/adapters/mootdx_source.py:13`); the adapter's default server list is live and answers `xdxr` with real data.
- **Tushare tiers differ.** `TushareFreeAdapter` shares the implementation in `TushareAdapter`; `Tushare2kAdapter` is the 2000-point account used for index membership, `adj_factor`, `stk_limit`, `suspend_d`, and bounded research inputs. The adapter reads `TUSHARE_2K_TOKEN` from process environment only. The CLI does not load `.env`; an operator must inject the value at process startup. Live-fetch launchers may parse only that key from `.env` and pass it through the child process environment without printing or persisting the value.
- **Supplemental research data is a raw-source layer.** `research_data()` allowlists `daily_basic`, SW industry classification/membership, standard statements and indicators, company forecast/express, seller forecasts (`report_rc`), `moneyflow`, `hk_hold`, and `top_list`; a bounded query filter is mandatory. `TushareDependencyStore` preserves the exact query and response bytes as immutable, checksum-verified Parquet snapshots. `fetch_tushare_industry_history.py` requests both `is_new=Y` current members and `is_new=N` non-current members for every SW2021 L3 code. It does not normalize fields into canonical factors or certify PIT availability. The two fetch scripts read `TUSHARE_2K_TOKEN` from the process environment and do not load `.env` themselves.
- **Endpoint timing remains a consumer responsibility.** Daily indicators, money flow, and top-list data are available after close; `hk_hold` is published the next morning. Do not use them for same-session open decisions. `index_member_all` contains `in_date`/`out_date` but no historical provider-publication vintage, so its valid-time history alone does not establish transaction-time PIT. Tushare documents `report_rc` at 2000 points as a 10-request/day trial, which is insufficient for a full historical analyst-expectation panel.
- **AkShare is an optional dependency and is not installed** in `.venv` or `.venv-qlib` (verified by import). It is only reached through the lifecycle provider and the optional `akshare_calendar` helper; the default v2 path uses EastMoney instead. Any claim that AkShare ran must name the environment it ran in.
- **Two calendar shapes:** an injected `set[date]`, or a provider callable. A sessions-only calendar has no evidence payload, so `provenance` is recorded as `unspecified` — which is itself audit-relevant.
- **Independent-calendar opt-out is recorded honestly.** With `--skip-independent-calendar`, the manifest carries `independent_source: not_checked` and `cross_check_result: skipped`, never a fabricated `agreement`.

## State and Data Contracts

Calendars are stateless except for the EastMoney provider's cached, self-certifying contiguous range (earliest session plus payload digest), which is pinned into the ingest report as `calendar_evidence` so coverage completeness is auditable after the fact.

Supplemental Tushare snapshots live under the user-selected source-data root, separate from canonical bars. Generation identity binds endpoint, query, and Parquet checksum; identical responses are idempotent and changed provider responses create a new generation. `build_tushare_factor_candidates.py` can derive lagged valuation, turnover and money-flow candidate features plus market-cap/liquidity exposure Parquet from exact-date snapshots. Its immutable bundle binds every source snapshot and canonical calendar partition, but always sets `historical_vintage_verified=false` and `strict_pit_eligible=false`: the local historical responses were fetched after their represented sessions, and an assumed next-session visibility does not prove historical provider vintages. Downstream factor/exposure publishers still require their owning schemas, source approval, PIT timing, and external quality review.

Market dataset families published for paper execution (`trading_calendar_snapshot_v1`, suspension and corporate-action sets) are immutable manifest-first snapshots from `MarketDatasetPublisher`, read through `PublishedPaperMarketData` as ordered partition-set triples with a digest convention shared with the set-binding resolver.

## Constraints, Failures, and Compatibility

- **No degradation to a smaller expected set.** Any transport, parse or coverage failure in the independent calendar raises; shrinking the expected set would make `observed >= expected` trivially true and publish unverified data as complete.
- **`adj_factor` flat at 1.0 is not a defect signal.** Measured: in one full-A session, 62 of 5059 instruments never had a corporate action. Validation must distinguish "correctly flat" from "should have adjusted but did not" (`docs/user-guide.md` §5.2).
- **A source's coverage gap must be declared, not inferred.** Instruments whose daily bars disappear mid-window without a suspension record are a known vendor defect; the evidence file must exclude them explicitly (`docs/user-guide.md` §5.4).
- **Suspension and bars from the same vendor can mask each other.** The readiness gate uses `suspend_d` to distinguish long suspension gaps from missing bars and rejects contradictory canonical rows, including rows whose status is not `suspended`. This remains a same-vendor check; a correlated vendor fault could still hide an ingest gap.

## Verification Evidence

- `.venv/bin/python -m pytest` → `1588 passed, 33 skipped`; `.venv-qlib/bin/python -m pytest` → `1620 passed, 1 skipped` on the current worktree.
- Adapter and calendar suites: `tests/test_real_chain.py` (fake mootdx/Tushare clients, no live network), `tests/test_mootdx_source.py`, `tests/test_tushare_v2_adapter.py`, `tests/test_tushare_rotation.py` (token rotation), `tests/test_eastmoney_calendar.py` (injected opener), `tests/test_real_data_policy.py` (lifecycle classification).
- Readiness: `tests/test_training_readiness.py` plus `scripts/validate_training_readiness.py` in CI-independent use.
- No automated test in this repository contacts a live vendor endpoint; network behaviour is asserted with injected openers and fake clients only. Separately, the ignored local history root has been populated through real Tushare requests; its readiness report currently remains blocked and is summarized in `docs/trading-grade-progress.md`.
- Supplemental source coverage is tested in `tests/test_tushare_dependency_store.py` and `tests/test_fetch_tushare_dependency.py`; the full-market daily-query optimization and 6,000-row truncation refusal are covered in `tests/test_tushare_v2_adapter.py`. These automated tests use fakes. Separately, live acquisition evidence and query totals are recorded in `docs/trading-grade-progress.md`; automated tests still do not contact vendors.

## Unknowns and Pending Confirmation

- `KNOWN BLOCKED`: the ignored local history root contains 683 canonical sessions and 5,430 instruments with bars for 2024-01-01..2026-10-30, but readiness found 17 unexplained required-session gaps and 2 instruments with fewer than 60 bar sessions. Fourteen gap instruments occur in the current `stock_basic(list_status=D)` result; because that source does not provide historical delisting dates, it does not resolve their mid-window gaps. Raw `daily_basic` and other dependency snapshots do not constitute approved PIT exposure panels.
- `UNKNOWN`: `akshare` behaviour in this environment. It is not installed in either local virtualenv; `import akshare` raises `ModuleNotFoundError`. Any AkShare-dependent code path is therefore unexercised locally.
- `KNOWN BLOCKED`: the institute-quality bar set (`status`/limit/`adj_factor` in `bars_daily.v2`) was published over the full 683-session window and independently read back with checksum verification, but the real-data readiness gate is not green because of the gaps and short histories above. No full-market passing archive has been published in `evidence/`.
