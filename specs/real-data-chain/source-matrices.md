# Real Source Field Matrices

Status: research-integrated contract input for real-data-chain implementation.  
Canonical target for MVP: `bars_daily.research-v1`.

## Decision Summary

- **Tushare is the R1 primary source.** It has the strongest documented schema,
  raw daily bars, adjustment factors, trading calendar, limits, suspension, and
  lifecycle metadata.
- **AData is a supplemental/prototype source only.** It can provide tokenless
  daily bars and a calendar, but lacks adjustment factors, reliable security
  master, historical membership, and authoritative status metadata.
- **TDX is deferred.** Useful protocol capabilities exist, but library
  maintenance/license/service risks require an explicit operational decision.
  Do not use Research-Only code in production.

## Tushare Matrix

### Daily bars

API: `daily`

| Canonical field | Provider field | Transform |
|---|---|---|
| `instrument` | `ts_code` | `600000.SH` -> `600000.XSHG`; `000001.SZ` -> `000001.XSHE` |
| `datetime` | `trade_date` | parse `YYYYMMDD`; normalize to Shanghai day |
| `open` | `open` | CNY, raw; direct float64 |
| `high` | `high` | CNY, raw |
| `low` | `low` | CNY, raw |
| `close` | `close` | CNY, raw |
| `volume` | `vol` | **lot**; multiply by `100` -> share |
| `amount` | `amount` | **thousand CNY**; multiply by `1000` -> CNY |

Notes:

- `pre_close` is the ex-dividend previous close.
- Suspended days are generally absent from `daily`.
- Do not fill suspended OHLC with zeros or forward values.

### Adjustment factor

API: `adj_factor`

| Target field | Field | Semantics |
|---|---|---|
| production `adj_factor` | `adj_factor` | cumulative factor associated with `(ts_code, trade_date)` |

Permission starts at 2000 points. Keep canonical prices raw and persist the
factor separately; do not store provider-derived qfq/hfq prices as canonical
raw bars.

### Calendar

API: `trade_cal`

| Internal field | Provider field | Rule |
|---|---|---|
| session date | `cal_date` | ISO date |
| open flag | `is_open` | retain exact provider enum semantics |
| exchange | request parameter | SSE/SZSE first |

Required for expected coverage and non-session handling.

### Production v2 support

| Canonical field | API / field | Notes |
|---|---|---|
| `limit_up` | `stk_limit.up_limit` | 2000-point permission |
| `limit_down` | `stk_limit.down_limit` | 2000-point permission |
| `status=suspended` | missing `daily` row + `suspend_d` | do not infer from zero volume alone |
| lifecycle/listing | `stock_basic.list_status`, listing/delisting dates | current state plus dates |
| ST history | `stock_st`, cross-check `namechange` | higher permission may apply |

### Fundamentals/PIT notes

- Income/balance/cashflow expose `ann_date`, `f_ann_date`, `end_date`,
  `report_type`, and `update_flag`.
- Build revisions by event key rather than replacing history.
- `index_weight` is monthly; not sufficient for precise daily membership PIT.
- `index_member_all` has industry member in/out dates.

### Operational constraints

- Enforce account-specific rate limits dynamically.
- Persist request windows and pagination metadata.
- Treat empty responses as ambiguous until calendar/universe policy proves no
  data is expected.
- Never log tokens or write credentials to manifests/reports.

## AData Matrix

Primary API:
`adata.stock.market.get_market(stock_code, start_date, end_date, k_type=1,
adjust_type)`

| Canonical field | Provider field | Transform / risk |
|---|---|---|
| instrument | `stock_code` | six-digit code; exchange suffix must come from external master data |
| datetime | `trade_date` | ISO date/day resolution |
| OHLC | `open/high/low/close` | price follows requested `adjust_type`; use `adjust_type=0` for raw prototype rows |
| volume | `volume` | already reported as shares by implementation; verify empirically before freezing adapter |
| amount | `amount` | CNY according to implementation; validate before production |
| turnover extension | `turnover_ratio` | separate linked dataset; not core bar |
| change/pre-close extensions | `change`, `pre_close` | adjusted-snapshot dependent; do not treat as official raw prior close |

Capabilities:

- tokenless daily bars;
- annual trading calendar via `trade_calendar`;
- basic stock list via `all_code`;
- current index constituent snapshot;
- core financial indicators with `notice_date`.

Hard gaps:

- no adjustment-factor series;
- no delisting/ST authoritative status;
- no historical membership effective intervals;
- no complete three-statement financials;
- silent-empty failure modes;
- multiple web-source fallbacks;
- adjusted history can be rewritten.

Adapter classification:

```text
capability_class = supplemental_daily_bars / prototype_source
not allowed as security-master or corporate-action authority
```

## TDX Matrix

Recommended evaluation order:

1. `mootdx` + `tdxpy`: conservative compatibility layer.
2. `eltdx`: richest maintained implementation, but Research-Only license.
3. Do not build new production work on archived `pytdx`.

Daily K-line mapping:

| Canonical field | TDX field | Transform |
|---|---|---|
| instrument | market + code | map market to `.XSHG` / `.XSHE` |
| datetime | K-line date | Shanghai trading day |
| OHLC | parsed price fields | raw CNY |
| volume | volume field | usually lot; multiply by 100 |
| amount | amount field | CNY |

Capabilities/risks:

- raw K-lines and xdxr events are available through several libraries;
- adjustment factors should be derived locally from xdxr unless using a helper;
- limits/status/lifecycle/calendar are not authoritative single-field outputs;
- public TDX servers are not a contracted API;
- server/schema behavior can vary;
- `eltdx` cannot be used where its research-only terms prohibit production use;
- transaction/trade APIs must remain out of scope for the market-data adapter.

Decision for this project:

```text
R1: not used.
R2/R3: evaluate mootdx/tdxpy behind FetchResult with explicit legal review.
Do not import eltdx into production code without resolving its license.
```

## Unified Capability Matrix

| Capability | Tushare | AData | TDX |
|---|---|---|---|
| raw daily OHLCV | yes | yes | yes, protocol-dependent |
| volume normalization confidence | high | medium; verify | medium-high |
| amount normalization confidence | high | medium; verify | high |
| adjustment factor | yes, dedicated API | no | derivable from xdxr |
| trading calendar | yes | annual | derive/index-based |
| security lifecycle | strong | weak | weak/current-only |
| ST/delisting | available with permissions | weak heuristic | weak/current-only |
| price limits | dedicated API | no | local rules/helper |
| suspension | dedicated API | inferred | weak |
| fundamentals PIT | strong with permissions | limited indicators | snapshot/files |
| historical membership | partial/monthly or SW members | current snapshot only | weak |
| operational governance | best | weak | medium technical, legal risk |

## MVP Normalization Contract

For `bars_daily.research-v1`:

```text
Tushare:
  instrument = canonicalize_ts_code(ts_code)
  datetime   = date(trade_date)
  open/high/low/close = float(raw field)
  volume     = float(vol) * 100
  amount     = float(amount) * 1000

AData:
  instrument = external_master_lookup(stock_code)
  datetime   = date(trade_date)
  open/high/low/close = float(field); adjust_type=0 only
  volume     = float(volume); verify share unit before enabling publication
  amount     = float(amount); verify CNY before enabling publication

TDX:
  instrument = market_code_to_canonical(market, code)
  datetime   = date(bar_datetime)
  OHLC       = parsed raw CNY
  volume     = float(vol) * 100
  amount     = float(amount)
```

No adapter may emit adjusted prices into a raw-price canonical partition.

## Required Empirical Checks Before Adapter Freeze

1. Tushare account permissions for required endpoints.
2. Tushare response row/page limits under the configured token tier.
3. AData actual units and behavior on suspended stocks across sample dates.
4. AData exchange suffix reconstruction against security master.
5. TDX server field stability if later enabled.
6. Sample-date cross-source close/volume tolerance after two sources exist.
