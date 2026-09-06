 Adjustment Official Source Access Log v1

Status: evidence log; all four reviewed cases certified against SZSE `qss` responses
Scope: adjustment golden provenance review

SZSE archived historical quote API

- Endpoint template:
  `https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1815_stock&TABKEY=tab1&PAGENO=1&PAGESIZE=30&txtDMorJC=<code>&txtBeginDate=<YYYYMMDD>&txtEndDate=<YYYYMMDD>`
- Required header: `Referer: https://www.szse.cn/market/trend/archive/index.html`.
- The response is a JSON array. Its first element has `metadata` and `data`.
- Relevant fields are `jyrq` (trade date), `zqdm` (instrument code), `qss` (prior close), `ss` (close), and `zd` (low).
- Observed archive cutoff in the 2026-08-24 response metadata was `2025-07-31`; later record dates returned no data.
- Captured formula-input responses live under `.gate/evidence/szse-<code>-window.json` (record date), official ex-date responses live under `.gate/evidence/szse-<code>-ex-date.json`; provenance fixtures record each response's SHA-256.

SSE public historical access

- The browser quote-report endpoint is
  `https://yunhq.sse.com.cn:32042/v1/sh1/list/exchange/<scope>/<date>` and requires the SSE referer.
- Requests for a historical date return the current-day response with an empty list, so this endpoint cannot supply historical prior closes.
- The dayk endpoint returns historical rows but sets `prev_close=null` on ex-rights days; its open is market data, not official reference-price disclosure.
- SSE's historical-data page points to its paid technical-service platform (`https://idc.ztcloud.ssetech.com.cn`). It confirms that prior-close data exists as a purchasable product, but no direct free download was available.

Reviewed replacement cases

| Case | Ex date | Direct SZSE reference price | Formula reconciliation |
| --- | ---: | ---: | --- |
| Shannon Semiconductor / `300475.XSHE`, rights | 2023-02-16 | `qss=18.36` | `(19.10 + 0.0894423 × 10.07) / 1.0894423 = 18.3586`; issuer's actual-ratio disclosure is `0.894423` per ten shares |
| BOE A / `000725.XSHE`, cash only | 2024-06-19 | `qss=4.11` | `(4.14 - 0.03) = 4.11` |
| Zhongjing Technology / `003026.XSHE`, transfer only | 2024-07-18 | `qss=23.97` | `31.14 / 1.2993115 = 23.9665` |
| Anker Innovations / `300866.XSHE`, cash bonus transfer | 2024-05-24 | `qss=69.69` | `(92.60 - 2.00) / 1.30 = 69.69` |

The ex-date SZSE historical quote field `qss` is the exchange's prior-close/reference-price field. All four responses are captured with SHA-256 values in `golden-provenance.v2.json`. Generic open/high/low/close prices are never substituted for this field.
