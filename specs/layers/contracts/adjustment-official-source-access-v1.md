 Adjustment Official Source Access Log v1

Status: working evidence log
Scope: adjustment golden provenance review

SZSE archived historical quote API

- Endpoint template:
  `https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1815_stock&TABKEY=tab1&PAGENO=1&PAGESIZE=30&txtDMorJC=<code>&txtBeginDate=<YYYYMMDD>&txtEndDate=<YYYYMMDD>`
- Required header: `Referer: https://www.szse.cn/market/trend/archive/index.html`.
- The response is a JSON array. Its first element has `metadata` and `data`.
- Relevant fields are `jyrq` (trade date), `zqdm` (instrument code), `qss` (prior close), and `zd` (close).
- Observed archive cutoff in the 2026-08-24 response metadata was `2025-07-31`; later record dates returned no data.
- Captured responses live under `.gate/evidence/szse-<code>.json`; provenance fixtures record each response's SHA-256.

SSE public historical access

- The browser quote-report endpoint is
  `https://yunhq.sse.com.cn:32042/v1/sh1/list/exchange/<scope>/<date>` and requires the SSE referer.
- Requests for a historical date return the current-day response with an empty list, so this endpoint cannot supply historical prior closes.
- The dayk endpoint returns historical rows but sets `prev_close=null` on ex-rights days; its open is market data, not official reference-price disclosure.
- SSE's historical-data page points to its paid technical-service platform (`https://idc.ztcloud.ssetech.com.cn`). It confirms that prior-close data exists as a purchasable product, but no direct free download was available.

Reviewed replacement cases

| Case | Record date | Direct pre-close evidence | Formula result |
| --- | ---: | --- | ---: |
| BOE A / `000725.XSHE`, cash only | 2024-06-18 | SZSE archive: prior/close CNY `4.08/4.07` | `4.08 - 0.03 = 4.05` |
| Zhongjing Technology / `003026.XSHE`, transfer only | 2024-07-17 | SZSE archive: prior/close CNY `32.70/31.14` | `32.70 / 1.2993115 = 25.1696` |

The issuer announcements disclose dates, ratios, cash amounts, and adjusted formulas; their PDF SHA-256 values are recorded in `golden-provenance.v2.json`. Reference prices derived from those formulas remain formula-derived unless an exchange or settlement page directly publishes the resulting price.
