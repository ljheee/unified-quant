# Hybrid Source Decision: Tushare Free 200 + Mootdx

Status: research decision for current credentials.  
Goal: run an R1 local research daily chain without paid Tushare permissions.

## Direct Answer

**Mootdx cannot fully replace paid Tushare Pro, but a hybrid chain can support
an R1 local research MVP.**

Use:

- Tushare free `daily` as the primary raw OHLCV source;
- Mootdx/TDX for cross-checking raw bars;
- Mootdx `xdxr` events to derive adjustment factors locally;
- index-derived calendar only for historical sessions;
- static whitelist universe for the first implementation.

This is a **research prototype**, not a stable production base.

## Tushare 200-Point Availability

| Interface | Minimum points | Available at 200 | R1 use |
|---|---:|---:|---|
| `daily` | 120+ | yes | primary raw bars |
| `stock_basic` | 2000+ | no | unavailable |
| `trade_cal` | 2000+ | no | derive historical sessions |
| `adj_factor` | 2000+/5000+ | no | derive from Mootdx xdxr |
| `stk_limit` | 2000+ | no | local rule engine or defer |
| `suspend_d` | not documented here | test token empirically | do not assume |
| `index_weight` | 2000+ | no | defer PIT membership |

## Hybrid Responsibility Matrix

| Data domain | Source / method | R1 policy |
|---|---|---|
| Raw OHLCV | Tushare `daily`; Mootdx cross-check | publish Tushare rows after validation |
| Volume normalization | adapter layer | canonical share; Mootdx lot likely ×100 pending probe |
| Amount | both sources | canonical CNY; verify units |
| Historical trading calendar | index daily bars | mark `calendar_source=index_derived_v1` |
| Corporate action events | Mootdx `xdxr()` | persist raw events with lineage |
| Adjustment factor | derived from xdxr | algorithm-versioned `adj_factor.derived_v1` |
| Price limits | local rule engine | main board ±10%, ChiNext/STAR ±20%; special cases unsupported |
| Suspension | unknown unless token permits `suspend_d` | never infer solely from zero volume |
| Security master/universe | static whitelist first | avoid treating TDX list as A-share master |
| ST/delisting lifecycle | deferred | explicitly out of MVP |
| Index membership PIT | deferred | explicitly out of MVP |
| Fundamentals PIT | deferred | explicitly out of MVP |

## P0 Gates Before Any Real Publication

1. Mootdx `bars()` succeeds on at least three configured servers.
2. Sample symbols include SH main board, SZ main board, and ChiNext.
3. Verify volume unit:
   ```text
   amount / close / 100 ≈ vol
   ```
4. Verify raw close agreement between Tushare `daily.close` and Mootdx close on
   recent sessions within floating tolerance.
5. Derive calendar from index bars and compare sampled dates against another
   public calendar source.
6. Preserve raw xdxr fields and bind every derived factor to:
   - algorithm version;
   - base/direction convention;
   - event lineage;
   - source/server/fetch time.
7. Convert empty responses into typed FetchResult statuses; never silently treat
   empty frames as complete data.
8. Keep tokens, server metadata, fetch time, requested/delivered fields, and
   warnings/errors in run diagnostics without exposing credentials.

## Adapter Topology

```text
TushareFreeAdapter
  -> fetch_daily_bars()
  -> normalize symbol/date/volume/amount

MootdxAdapter
  -> health_probe(stocks/bars/index_bars/xdxr)
  -> fetch_daily_bars(cross-check)
  -> fetch_xdxr_events()
  -> derive_adj_factor()

CalendarDeriver
  -> index bars -> historical session set

LimitRuleEngine
  -> board-aware basic limits; unsupported cases remain null/unknown

UniverseFilter
  -> explicit whitelist for R1
```

## What This Cannot Do

- production-grade full-market universe;
- precise historical suspension reconstruction;
- authoritative ST/delisting lifecycle;
- authoritative limit prices for all special cases;
- precise historical index membership;
- fundamentals PIT;
- commercial redistribution or batch resale.

## Current Executable State

The TDX-first path is implemented behind typed `FetchResult` envelopes:

```bash
uq-ingest daily --date YYYY-MM-DD --data-root /path/to/data
```

The command currently uses the research whitelist and one configured standard
TDX server. It publishes only after the quality gate accepts the partition.
Network/server availability and real-sample validation are still required
before treating outputs as research-grade.

## Release Label

Until all stable-release gates pass, label outputs:

```text
research prototype / hybrid tushare-free + mootdx
```

not:

```text
stable base layer
```

## Can the Key Limitations Be Sourced Elsewhere?

Most are not absolutely unavailable; they are not reliably available under the
current zero-extra-cost, low-governance stack. The limitation is about
authoritativeness, PIT correctness, and maintenance cost.

| Missing capability | Can another current source provide it? | Practical answer |
|---|---|---|
| Security master / universe | AData basic list; AkShare; exchange lists; TDX filtered list | Yes for a research whitelist, but needs filtering and reconciliation. Not yet authoritative full-market security master. |
| Historical trading calendar | AData annual calendar; AkShare/exchange calendars; index-derived fallback | Yes. Prefer a real external calendar; keep index-derived only as fallback/provenance. |
| Adjustment factor | Paid Tushare `adj_factor`; derive from Mootdx xdxr; some vendor APIs | Partially yes. Derivation is possible but must be algorithm-versioned and event-verified. |
| Corporate action raw events | Mootdx `xdxr`; AData dividend endpoint; announcements | Yes for common events, but coverage/semantics need cross-validation. |
| Price limits | Paid Tushare `stk_limit`; local rule engine; exchange notices | Partially yes. Basic boards can be derived; special/new-stock/ST cases need careful rules or remain unknown. |
| Suspension history | Paid Tushare `suspend_d`; exchange announcements; AkShare endpoints if reliable | Hard without paid source. Do not infer solely from zero volume. |
| ST status/history | Paid Tushare `stock_st`; name-change sources; exchange disclosures | Hard historically. Current-name heuristic is insufficient. |
| Delisting/lifecycle | Paid Tushare `stock_basic`; exchange delisting lists; AkShare assembly | Possible with extra engineering, but not already covered by current hybrid. |
| Index membership PIT | Paid Tushare monthly weights/SW members; official index documents; vendor data | Not solved by current free stack. Monthly/approximate at best; precise daily PIT remains hard. |
| Fundamentals PIT | Paid Tushare statements; AData core indicators + notice date; filings | Partially yes for prototype metrics; complete statement PIT requires more work/permissions. |

Therefore the correct statement is:

```text
Not permanently impossible;
not safely solved by the current R1 hybrid alone.
```

To remove these limitations, add governed companion datasets and/or additional
sources, then validate units, effective dates, announcement times, revisions,
and lifecycle semantics.
