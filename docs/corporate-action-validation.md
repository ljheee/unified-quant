# 复权数据的校验方法：业界做法与本仓库取舍

更新时间：2026-10-17
适用读者：要判断"复权价对不对"、或要新增/修改复权校验规则的人。

本文回答一个问题：**怎么可靠地发现"复权没做"或"复权做错了"？**

它源自一个具体缺陷（`cr.md` FC-M3）：因子层有一个测试专用的 `assert_not_raw_close`，
但生产侧没有任何守卫。最省事的修法是"拒绝 `adj_factor` 全为 1.0 的输入"，
**这个修法是错的**——实测全 A 一个真实会话里 **62/5059** 只标的的因子本来就恒为 1.0，
因为它们从未发生公司行为。对它们来说复权价就等于原始价，这是正确结果。

所以真正的问题是：**怎么区分"本来就该平坦"与"应该复权却没复权"？**

## 1. 结论先行

业界是**分层防线**，三层各自独立、可靠性递减：

| 层 | 方法 | 需要独立事件表 | 能证明"正确"吗 | 用途 |
|---|---|---|---|---|
| 主判据 | 事件表重放 + 因子**跳变**核对 | **是** | 能（在事件表可信的前提下） | 发布门禁 |
| 监控层 | 跨源复权收益比对 | 否（需第二个价格源） | 否，只能发现分歧 | 持续监控 |
| Triage | 除权日 gap / 连续性启发式 | 否 | 否，误报漏报都高 | 生成候选告警 |

**只有主判据能区分"合法平坦"和"漏复权"**，因为只有它知道"这只标的本来应该有哪些事件"。

## 2. 核心不变量

复权价 = 原始价 × 因子，且**复权价在除权日必须连续**。设除权日为 `D`、
`pre_close_D` 为交易所给出的除权参考价、`close_{D-1}` 为除权前实际收盘，则：

```text
close_{D-1} x F_{D-1}  ==  pre_close_D x F_D
=>  F_D / F_{D-1}  ==  close_{D-1} / pre_close_D
```

即**因子跳变是价格缺口的倒数**。

> **易错点（已实测踩过）**：不要把方向写成 `F_D/F_{D-1} == pre_close_D/close_{D-1}`。
> 那个式子是**倒数**，只在除基（divide-basis）因子下成立；本仓库是乘基。
> 这与 `cr.md` §8.36 修过的 hfq 方向错误是同一个坑。

用 4 个真实除权日验证（Tushare `adj_factor` + `daily.pre_close`）：

| 标的 | 除权日 | `F_D/F_{D-1}` | `close_{D-1}/pre_close_D` | 相对误差 |
|---|---|---|---|---|
| 920000.BJ | 2025-09-18 | 1.002859 | 1.002846 | 1.4e-05 |
| 600000.SH | 2025-07-16 | 1.030326 | 1.030325 | 3.4e-07 |
| 000001.SZ | 2025-10-15 | 1.021183 | 1.021183 | 1.6e-07 |
| 601318.SH | 2025-06-30 | 1.028910 | 1.028902 | 8.1e-06 |

残差量级来自交易所对除权参考价的取整（A 股价格最小变动 0.01 元），
因此容差应随价格档位放宽，而不是用一个固定相对百分比——这与
`src/uq/pipeline/adjusted_price.py` 里 `_PRICE_TICK / pre_close` 那个
"物理分辨率下界"是同一套推理。

## 3. 业界具体做法

### 3.1 事件表重放（主判据）

把独立的公司行为事件表当 oracle，重算每个事件的应有比率，再核对 vendor 因子在
事件日前后是否发生匹配的跳变。**比跳变、不比水平**：不同 vendor 把累计因子锚在
上市日、窗口起点或最新日，水平天然不同。

已验证的实现：

- **Zipline / zipline-reloaded**：raw bars 与 adjustment 库分离；`splits/dividends/mergers`
  事件表存 `effective_date, ratio`；读取时在生效日对之前的价格应用 `Float64Multiply`。
  现金分红的 ratio 由 `1 - dividend/previous_close` 从 raw close 现算，不预先存死。
- **CRSP**：原始价与累计调整因子分开发布，`StkDlyCumulativeAdjFactor` 提供
  `DlyCumFacPr` / `DlyCumFacShr`；CRSP-Compustat 的 `ADJFACT` 按 ex-date/pay-date 存累计因子。
- **kdb+**：官方示例从 CA 表计算累计因子，并把 vendor 因子当作可选输入。

共同点：**事件表是必需品**。`factor == 1` 只说明"在当前锚点下无累计调整"，
不能证明"历史上没有事件"。

### 3.2 跨源比对（监控层）

用第二个独立来源的复权序列，比较事件窗口内的 returns/jumps。不需要事件表，
但两个来源可能共享同一上游、漏同一个事件，所以**只能发现分歧，不能证明正确**。

本仓库已有这一层：`src/uq/pipeline/adjusted_price.py::verify_jump_cross_check`
用 TDX `xdxr` 重算的复权序列与 vendor 因子比跳变，正是这套做法。

### 3.3 gap / 连续性启发式（triage）

检查原始价在除权日的缺口，或检查"复权价应连续、原始价应有机械缺口"。
便宜但不可靠：真实暴跌会误报，小额股息会漏报（股息率被日常波动淹没）。
只适合生成候选告警，交给前两层确认。

### 3.4 一个反面例子：Qlib

**Qlib 没有事件表，也没有任何复权校验。** 它只从 Yahoo 的 `adjclose/close`
推导因子，缺失时直接置 1，另有若干针对异常倍数的启发式修正，但不做 CA reconciliation。

> 本机独立验证：在已安装的 `qlib 0.9.7` 包内搜索 `corporate`，**0 处命中**。

所以"业界标准做法"里并不包含 Qlib——它恰好是缺这一层的例子。把 Qlib 当作
"复权校验的最佳实践"引用是不成立的。

## 4. 国内市场的特殊性

### 4.1 除权日的"前收盘价"就是除权参考价

这是国内制度与部分海外市场的一个关键差异，也是一个**可用的检查支点**：

北交所规则明确，除权除息参考价为

```text
[(前收盘价 - 现金红利) + 配股价 x 股份变动比例] / (1 + 股份变动比例)
```

且**除权日行情里显示的"前收盘价"就是这个参考价**，不是前一日的实际收盘。

实测佐证：`920000.BJ` 在 2025-09-18 的 `pre_close = 24.60`，
而 09-17 实际收盘是 `24.67`，差额 0.07 正好等于当日现金分红。

由此得到一个**不需要事件表**的检查：

```text
复权收益率_D  ==  vendor pct_chg_D
```

实测：`920000` 我们的复权收益 `+8.9446%` vs vendor `+8.9431%`；
`600000` 两侧都是 `+3.1323%`。把因子强制成 1.0（模拟"漏复权"），
收益变成 `+8.6340%`，差 **0.31pp**——可检出。

**边界**：这条检查检测的是"复权 join 断裂"，**不是**"vendor 的前收盘本身错了"。
若 `pre_close` 错，这条检查看不出来。两者要分开表述。

**当前阻碍**：canonical `bars_daily/v2` 不携带 `pre_close` / `pct_chg`，
所以落地需要新增该输入（比事件表轻，但仍是接口变更）。

### 4.2 国内厂商：事件表 vs 纯因子

| 厂商 | 事件表 | 因子 | 备注 |
|---|---|---|---|
| Tushare | 有（`dividend`，含 `ex_date`/`cash_div`/`stk_div`） | `adj_factor` | 两者分离；`dividend` 不支持按 `ex_date` 区间查询，会静默返回空 |
| RiceQuant | 有（`get_dividend`/`get_split`） | `get_ex_factor`/`ex_cum_factor` | 文档给出 `ex_factor = 前收盘价/除权后价格` 的累计关系 |
| 同花顺 iFinD | 未见公开事件表 | 有 | FAQ 说明除权事件日维护向前/向后因子 |
| JoinQuant | 未见公开事件表 | 有（默认前复权 `factor`） | 原始价 = 价格 / factor |
| AkShare | 聚合层 | 支持 `qfq/hfq` 及因子 | 封装东财分红送配接口 |
| Wind / Choice | **未取得可公开核验的官方文档** | — | 不据非官方转述下结论 |

### 4.3 容差

**没有找到**中国监管机构或交易所规定的"复权因子比较容差"。

可核实的边界：A 股价格最小变动 0.01 元；北交所权益分派参数可保留 7 位小数。

工程经验值（**非标准**，仅作起点）：价格/参考价比较用 `0.005-0.01` 元；
因子用相对误差 `1e-6-1e-4`（累计因子大时不要用绝对误差）；收益率用 `0.1-1 bp`。

## 5. 北交所（BSE）的现实

### 5.1 TDX 不提供北交所事件

**结构性缺失，不是查询姿势问题。** 实测 5 只北交所标的
（920000/920242/920834/920278/920405）的 TDX `xdxr` 全部返回 0 行；
同样命令对 `600000.SH` 返回 88 行。手工按多种 market 编号尝试亦为 0 行。

**后果**：`verify_jump_cross_check` 拿 TDX 作为北交所的交叉校验源是**不可行的**。
北交所必须另找事件源。

### 5.2 可用的事件源

按可靠性排序：

1. **北交所官方权益分派实施公告**（`www.bse.cn` 的上市公司公告，分类含"权益分派"）。
   这是制度上的原始来源，但需要写抓取与解析。
2. **Tushare `dividend`**（按 `ts_code` 或 `ann_date` 查询，已验证可取到北交所事件）。
   工程上最省事，且本仓库已有 store。
3. **东方财富 `RPT_SHAREBONUS_DET`**（无需 token，覆盖北交所，含预案/实施进度）。
   适合作为第二来源交叉校验。

### 5.3 一个已知的坑：查询方式

`tushare.dividend(ex_date=..., end_date=...)` **静默返回 0 行**——该接口不接受
`ex_date` 区间过滤。必须按 `ts_code` 或 `ann_date` 查询后本地过滤。
本仓库的 `scripts/fetch_dividend_events.py` 按 `ann_date` 查询，是正确的。

**但**：若 store 从未抓取过某个日期区间（实测 `/tmp/uq-dividend-store` 缺整个
2025 年 9 月），该事件在 PIT 语义下就是"不存在"，下游会误判为"无事件"。
**这是抓取覆盖问题，不是数据源问题**，排查时要先看 store 的分区覆盖。

## 6. 本仓库的落地建议

按投入产出排序：

1. **补齐事件 store 的分区覆盖**（尤其是交易窗口内缺失的月份）。
   这是最便宜的修复，且能直接消除"误判为无事件"。
2. ~~给因子层增加 `pre_close` 输入~~ —— **已否决**。见 §6.1。
3. **北交所换事件源**：TDX → Tushare `dividend`（主）+ 东财（校验）。
4. ~~长期：以事件表重放作为发布门禁的主判据~~ —— **已落地**，见 §6.2。

### 6.1 为什么否决了"给因子层加 `pre_close` 参数"

原始想法是加一个 `pre_close=` 参数，用它校验 `复权收益 == pct_chg`。否决理由：

1. **与现有契约不一致。** `calculate_adjusted_factors` 的入参是"一个自包含帧 +
   lineage 身份"。`pre_close` 是同一行的另一个价格列，做成并列参数等于把一列拆成
   两处传，调用方要自己保证行对齐——最容易出 bug 的形状。
2. **绕过 `required_columns` 契约。** 因子定义用 `required_columns` 声明输入列
   （`src/uq/factors/inputs.py` 据此强制），加参数等于有一条输入不走这套校验。
3. **需要 schema 版本变更。** canonical `bars_daily/v2` 没有 `pre_close`
   （12 个字段里没有），要落地得开新版本，代价远超收益。

更根本的是：**"完全忘记 join"已经被拦住了**。`_validate_binding` 第一行检查列是否
存在，实测缺 `adj_factor` 直接抛 `adjusted dependency absent`。真正漏掉的只是更窄的
一种——列在、但值全是 1.0。

### 6.2 已落地：事件表重放 + 弱检查

**主判据（事件表重放）**：`src/uq/factors/adjusted_price.py::verify_against_events`。
关键发现是**事件表已经在库里了**——`AdjustmentSnapshotReader.read_events()` 能直接读回
已发布快照的事件（实测 `data/adjustment-store` 里 29 份快照均可读），
`effective_dates` 表也在。所以不需要新数据源，`FactorEngine.execute` 新增
`adjustment_events=` 参数即可接线。

比较的是**跳变而非水平**（不同派生锚点不同，水平天然有常数差）：

```text
observed = adj_factor_D / adj_factor_{D-1}
expected = pre_close / (pre_close - cash)        # 有现金
         = 1 / (1 + shares)                       # 纯送转
```

窗口外的除权日**记为 skipped 而非失败**——窗口起点晚于事件是合法的。

**弱检查（§6 选项 C）**：`calculate_adjusted_factors` 现在拒绝"`adj_factor` 全 1.0
**且** `close` 全相等"的输入。刻意做得窄：它**不**拒绝单独的全 1.0 因子，因为
62/5059 只真实标的本来就平坦。

实测确认这个窄口径不会误杀：在真实 21 会话窗口上，5058 只有完整历史的标的中，
62 只因子平坦，但**没有一只**同时满足"因子平坦且收盘不变"（真实标的收盘总会动）。

## 7. 参考

- 北交所除权除息参考价规则：<https://www.bse.cn/jygl_list/200028217.html>
- 北交所业务指南（除权除息参数保留 7 位小数）：<https://www.bse.cn/cxjg_list/200014449.html>
- Tushare `adj_factor` 文档（qfq/hfq 口径）：<https://tushare.pro/document/2?doc_id=146>
- Tushare `dividend` 文档：<https://tushare.pro/document/2?doc_id=103>
- RiceQuant 复权因子与事件：<https://www.ricequant.com/doc/rqdata/python/stock-mod.html>
- 同花顺 iFinD 复权 FAQ：<https://quantapi.10jqka.com.cn/gwstatic/static/ds_web/quantapi-web/help-center/faq.html>
- JoinQuant 数据说明：<https://www.joinquant.com/help/data/stock>
- AkShare 股票数据（复权/分红）：<https://akshare.akfamily.xyz/data/stock/stock.html>
- kdb+ Corporate Actions：<https://code.kx.com/q/kb/corporate-actions/>
- zipline-reloaded adjustments：<https://github.com/stefan-jansen/zipline-reloaded/blob/main/src/zipline/data/adjustments.py>
- CRSP 累计调整因子说明：<https://indexes.morningstar.com/api/docs/6ab69a06e9df60878cfe85ba>

## 8. 证据等级声明

**已核实（读了原始文档或本机实测）**：§2 的不变量与四个真实除权日的数值；
§4.1 北交所参考价规则与 `920000` 的 `pre_close` 实测；§5.1 TDX 无北交所事件（本机实测）；
§5.3 Tushare `ex_date` 区间查询返回空（本机实测）；Qlib 无 CA 校验（本机搜包）；
Zipline/CRSP/kdb+ 的机制描述（读源码或官方文档）。

**二手/推断**：§4.2 中"未见公开事件表"仅指公开文档范围内未找到，不等于厂商内部没有；
§4.3 的工程经验值不是任何标准；§3.2 "常见做法"的普及程度属行业推断，未找到公开统计。

**未取得**：Bloomberg `PDF/ADJ`、LSEG/Refinitiv、Wind、Choice 的官方字段级文档，
因此不对其具体不变量下结论。FISD/ISITC 或 vendor SLA 中没有找到可直接引用的
"复权价必须通过某数值容差"规则，也没有找到名为 "factor jump reconciliation" 的正式标准术语。

**一处已纠正的错误**：调研过程中曾得到"`F_D/F_{D-1} == 除权参考价/前收盘价`"的说法，
经 4 个真实除权日验证**方向写反了**（是倒数）。§2 记录的是验证后的正确式。
