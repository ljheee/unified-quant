# Unified Quant 小白用户指南

本指南面向第一次接触这个仓库的人。读完你可以完成：安装项目、跑测试、尝试数据入库、运行一次安全的 dry-run，并知道结果在哪里看。

> 重要提醒：这是一个量化研究平台，不是投资建议系统，也不是实盘交易系统。请不要把输出直接当作“必买/必卖”信号。

## 1. 你将得到什么

完成后你会得到：

- 一个可运行的 Python 研究环境。
- 一个数据入库入口：`uq-ingest`。
- 一个研究链入口：`uq-research-run`。
- 一次安全的 dry-run 输出，用来确认请求和状态可以被平台接受。

你现在还不会得到：

- 自动加载模型并对你的自选股生成今日买卖信号的 Signal Layer。
- 接入真实券商的自动交易。
- 生产级全市场历史数据库。
- 稳赚不赔的策略。

## 2. 准备环境

需要：

- macOS 或 Linux。
- Python 3.11 或更高版本。
- Git。
- [uv](https://docs.astral.sh/uv/)。

如果没有安装 uv，可先按 uv 官方文档安装。macOS 上常见方式是：

```bash
brew install uv
```

获取代码：

```bash
git clone <你的仓库地址> unified-quant
cd unified-quant
```

安装依赖：

```bash
uv sync --locked --extra dev --extra real
```

如果你要使用 Qlib 相关能力，额外执行：

```bash
uv sync --locked --extra dev --extra real --extra qlib
```

安装后仓库里会有 `.venv`。后续命令建议使用 `.venv/bin/...`，避免误用系统 Python。

## 3. 先跑测试确认环境正常

在仓库根目录执行：

```bash
.venv/bin/python -m pytest
```

如果最后看到大量 `passed`，说明环境正常。当前主分支基线曾经是 `569 passed, 22 skipped`，你的数字可能因本机环境和已安装 extras 略有不同。重点是不要出现大量 `failed` 或 `error`。

## 4. 尝试数据入库

先准备一个数据目录。不要把它放进仓库目录，避免误提交生成数据：

```bash
DATA_ROOT=/tmp/unified-quant-data
mkdir -p "$DATA_ROOT"
```

采集一个交易日的默认白名单数据：

```bash
.venv/bin/uq-ingest daily \
  --date 2026-09-10 \
  --data-root "$DATA_ROOT"
```

说明：

- `--date` 必须是交易日，例如 `2026-09-10`。
- 默认股票范围来自 `config/universe/research-whitelist.txt`，当前只是很小的研究白名单。
- 这个命令会尝试访问外部数据源。网络不通、数据源限流、非交易日或数据不可用时可能失败。
- 成功后会在 `$DATA_ROOT/runs/` 下生成一个 JSON run report。

成功输出里重点关注：

- `status`
- `run_id`
- 数据源与覆盖情况

如果失败，先看 JSON 里的 `errors`。新手阶段不必强行修复数据源问题，可以先跳到下面的 dry-run。

## 5. 可用数据源

同一类数据往往有多个来源。下表列出当前**已实测**的取数路径、用途与限制，方便你在搭建真实数据链时选择，也方便判断某个来源能否作为另一个来源的交叉校验。

### 5.1 行情与参考数据

| 来源 | 取什么 | 访问方式 | 当前状态与限制 |
|---|---|---|---|
| Tushare（2000 积分账号） | `daily` 原始 OHLCV、`adj_factor`、`stk_limit`、`suspend_d`、`stock_basic`、`trade_cal`、`index_weight`（部分） | HTTP API，需 `TUSHARE_2K_TOKEN` | **本项目主力源**。CLI 不自动加载 `.env`，需 `env $(grep -E '^TUSHARE' .env \| xargs)` 传入。免费/低积分档限速约 300 次/分钟，一个全 A 交易日约 104 次请求。`income_vip` 类接口不可用。 |
| TDX / 通达信（via `mootdx`） | **除权除息事件**（`xdxr`）、行情 `bars`、指数日线 | 原生 TCP 协议，端口 7709 | `xdxr` **实测可用**（默认主机 `115.238.90.165:7709`，实测返回真实事件）。`bars`/`quotes` 在部分网络下被拦截返回空，但 `xdxr` 仍可用——**探测时必须用真正要发的命令**，否则会把"命令族被拦"误判成"服务器不可用"。 |
| EastMoney | 独立交易日历（指数日线推会话表） | HTTP | 实测可用，但**抖动明显**（约 1/6 概率掉连接），入库与就绪门都带重试。 |

### 5.2 复权数据的两条链

复权价 = 原始价格 × 复权因子。复权因子可以从**公司行为事件**推导，本项目有两条已实现的路径，lineage 不同、语义相同（都是乘基、锚定最新交易日 1.0）：

| lineage | 事件来源 | 实现 | 说明 |
|---|---|---|---|
| `adj_factor.exchange_v2` | Tushare `dividend` 公告 | `src/uq/market/adjustment_chain.py::build_adjustment_chain` | 需把公告映射为交易所事件形状；只采用 `div_proc == 实施` 的记录，税前金额 |
| `adj_factor.derived_v1` | **TDX `xdxr`（交易所原始事件）** | `src/uq/market/adjustment_chain.py::build_tdx_adjustment_chain` | 无需映射公告，事件即交易所形状；适合作为上面那条链的交叉校验 |

快照 schema 版本与 lineage 绑定（`snapshot_version=3` ↔ `exchange_v2`，`5` ↔ `derived_v1`），两个版本都**可读**，读取侧只拒绝血缘不在 `CONSUMABLE_FORMULA_VERSIONS` 内的快照。

> 版本号注意：TDX 链是**全新的第 1 版**（`derived_v1`），不是 `derived_vN` 序列的延续——那个序列数的是**公告派生**的修订（v2→v3 修过现金分红量纲缺陷），TDX 派生没有这段历史可继承。

**实测吻合度**：83 个除权事件（6 只标的）上，TDX 事件重算的因子跳变与 Tushare `adj_factor` 的跳变最大差 0.109%、平均 0.023%，83/83 在 0.5% 以内。

### 5.3 新浪财经（仅调研，未接入）

| 来源 | 取什么 | 访问方式 | 状态 |
|---|---|---|---|
| 新浪财经 | 分红送股方案页 | 纯 HTTP：`https://vip.stock.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/{代码}.phtml` | **可用但已知有分歧，尚未接入** |

实测（6 只标的、190 个匹配除权日）：**185 个完全一致**，2 个仅是送股/转增分类不同（总股本相同），**3 个存在真实数值分歧**（例如中国平安 2018-06-07 派息：新浪 1.0 vs TDX 1.2；五粮液 1999-06-08：1.25 vs 1.0）。

如果要把它接入，注意：

- 用**纯 `requests` + `pandas.read_html`**，**不要装 `akshare`**——它会连带升级 pandas 到 3.x，与仓库锁定的 pandas 2.3.3 冲突。
- 必须先处理上面 3 处分歧的成因（是历史补丁差异、还是新浪页面的精度问题），再决定它能否作为第三源。

### 5.4 取不到数据时先查什么

1. **先确认是"命令族被拦"还是"主机不可达"**：TDX 上换 `xdxr` 再试一次。
2. Tushare 报限流：请求之间加间隔（`scripts/ingest_window.py --session-interval-seconds`，默认 25 秒）。
3. EastMoney 掉连接：加重试（`--calendar-attempts`）。
4. 某只股票日线中途消失且不停牌：见 `--excluded-instruments`（供应商停发，属已知缺陷，需要在证据文件里显式剔除）。

## 6. 运行一次安全的 Research Chain dry-run

dry-run 不会训练模型，不会生成因子，也不会做回测。它只校验 research request 并落一个 request manifest 和初始 state manifest。

复制仓库自带的合法示例请求：

```bash
REQUEST_PATH=/tmp/unified-quant-request.json
cp evidence/research-chain/phase-0/fixtures/research_run_request-valid.json "$REQUEST_PATH"
```

运行 dry-run：

```bash
DATA_ROOT=/tmp/unified-quant-data
mkdir -p "$DATA_ROOT"

.venv/bin/uq-research-run \
  --project-root "$PWD" \
  --data-root "$DATA_ROOT" \
  --mode dry-run \
  --request-json "$REQUEST_PATH"
```

如果成功，你会看到类似 JSON：

```json
{
  "status": "dry_run_published",
  "request_content_generation_id": "...",
  "run_id": "...",
  "manifest_path": "...",
  "manifest_digest_sha256": "..."
}
```

输出里的 `manifest_path` 指向本次 dry-run 的 `stage=00` 状态 manifest，类似：

```text
/tmp/unified-quant-data/research_runs/states/request=.../run=.../stage=00/manifest.json
```

同一个 run 的 request manifest 会另外保存在：

```text
/tmp/unified-quant-data/research_runs/requests/request=.../run=.../manifest.json
```

> 注意：当前 CLI 的 dry-run 使用的是 v1 示例请求。仓库里也有更严格的 v2/v3 契约，但不要把 v3 fixture 直接复制进这个 CLI dry-run；它目前不会被 CLI 的 dry-run 按 v3 路由。

## 7. execute 模式是什么？小白应该用吗？

`execute` 模式才是完整研究链：

```text
factor computation
  -> dataset preparation
  -> Qlib export
  -> model training
  -> prediction publication
  -> portfolio construction
  -> backtest execution
  -> result reconciliation
```

但它不是“随便给一个 JSON 就能跑”的模式。你必须准备：

1. 已存在的 governed upstream artifacts。
2. 合法且与 artifact 绑定的 research request。
3. 外部 quality decisions。
4. `research_scores.v1` 格式的分数输入。

所以小白建议：

- 先跑 dry-run，理解请求和产物结构。
- 不要手改 fixture 后直接 execute。
- 等你理解 universe、factor、label、dataset、quality decision 这些概念后，再让研究流程生成完整 artifacts。

execute 的命令形状如下，仅作结构说明：

```bash
.venv/bin/uq-research-run \
  --project-root "$PWD" \
  --data-root "$DATA_ROOT" \
  --mode execute \
  --request-json /path/to/request.json \
  --quality-decisions-json /path/to/quality-decisions.json \
  --scores-json /path/to/scores.json
```

`scores.json` 的最小结构类似：

```json
{
  "contract_version": 1,
  "schema_version": "1.0.0",
  "scores_by_decision_date": {
    "2025-12-01": [
      {"instrument": "600000.XSHG", "score": 0.92},
      {"instrument": "000001.XSHE", "score": 0.61}
    ]
  }
}
```

但这只是格式示例。真实 execute 还要求分数对应的 universe、factor、dataset、model artifact 和 quality decision 都能被解析。

### execute 前必须已发布的上游制品

请求里的这些绑定,必须在 execute 之前就已经作为受治理分区存在于 `--data-root`。研究链对它们**只读取并校验**,不会代为生成:

| 请求绑定 | 读取它的 store | 仓库内是否带生产者 |
| --- | --- | --- |
| `factor_binding` / `factor_partition_set_binding` | `uq.factors.store.FactorStore` | 是:`scripts/publish_factor_partitions.py`(读 canonical bars → 投影 → 算 → 发布,并输出 `factor_partition_set` 绑定) |
| `universe_snapshot_binding` | `uq.contracts.artifacts.UniverseSnapshotStore` | 是:`scripts/publish_universe_snapshots.py`(把 `scripts/fetch_index_membership.py` 的 PIT 成分快照铺成 `[visible_from, 下一 visible_from)` 区间) |
| `adjusted_price_binding` | `src/uq/research_chain/owning_contracts.py:31` `AdjustedPriceDatasetStore` | 否:只读 |
| `feature_preprocessing_binding` | 同文件 `FeatureSchemaStore` + `FeaturePreprocessingStore` | 否:只读 |
| `label_binding` | 同文件 `LabelStore` | 否:只读 |
| `backtest_config_binding` | 同文件 `BacktestConfigStore` | 否:只读 |

v5 请求额外要求的两类绑定**有**随仓库提供的生产者:

- `index_membership_binding`:`scripts/fetch_index_membership.py`。
- `paper_execution_template` 的市场数据四族:`scripts/publish_market_datasets.py`。

两点提醒:

- §6 的 dry-run 只校验请求本身并写 request/state manifest,**不会**去解析上面这些 store;绑定是到 execute 阶段才逐个读取校验的,所以 dry-run 通过不代表可以 execute。
- 因此"一条命令从零跑到回测"按现有代码还做不到:上面六类要先产出并落到同一个 `--data-root`。已有生产入口的是两类:因子(`scripts/publish_factor_partitions.py`)与 universe snapshot(`scripts/publish_universe_snapshots.py`);其余四类(`adjusted_price`、`feature_schema`/`feature_preprocessing`、`label_set`、`backtest_config`)目前只有测试里的 helper 会产出。

### 论文执行(v5 请求)的两个额外参数

`contract_version: 5` 的请求(带 `paper_execution_template`)额外要求两个治理输入,必须成对提供:

```bash
.venv/bin/uq-research-run \
  --project-root "$PWD" \
  --data-root "$DATA_ROOT" \
  --mode execute \
  --request-json /path/to/request-v5.json \
  --quality-decisions-json /path/to/quality-decisions.json \
  --scores-json /path/to/scores.json \
  --membership-root "$DATA_ROOT/a500-store" \
  --market-dataset-root "$DATA_ROOT/market-datasets"
```

- `--membership-root`:已发布的指数成分快照 store(v5 数据集阶段按"当日可见成员"过滤因子,缺它直接报错,不会静默退回原始因子分区)。
- `--market-dataset-root`:Phase 0.5 的 `MarketDatasetPublisher` store。研究窗口的交易日历由此处的 `trading_calendar_snapshot_v1` 家族导出;窗口没有覆盖到的日历快照会 fail-closed。

只提供其中一个会报 `--membership-root and --market-dataset-root must be provided together`。

日历快照的跨源核对口径:`--skip-independent-calendar` 会把 `independent_source` 记为 `not_checked`、`cross_check_result` 记为 `skipped`(而不是伪报 `agreement`);两家供应商会话列表不一致时直接 fail-closed。

## 8. 结果在哪里看

### 数据入库结果

主要看：

```text
$DATA_ROOT/runs/<run_id>.json
```

canonical 数据通常会出现在类似：

```text
$DATA_ROOT/canonical/bars_daily/research-v1/date=<YYYY-MM-DD>/
```

具体文件以 manifest 为准，不要手工修改数据文件。

### Research Chain dry-run 结果

主要看 CLI 输出的：

- `manifest_path`
- `manifest_digest_sha256`
- `request_content_generation_id`
- `run_id`

### Research Chain execute 结果

成功后 CLI 会返回：

```json
{
  "status": "published",
  "result_content_generation_id": "...",
  "manifest_path": "...",
  "final_status": "passed"
}
```

最终 result manifest 通常在：

```text
$DATA_ROOT/research_runs/results/request=<request_generation_id>/run=<run_id>/result=<result_generation_id>/manifest.json
```

研究链会记录每个阶段的 output bindings。你可以顺着 `stage_records` 找到：

- factor partition
- model dataset
- Qlib export/receipt
- model run/artifact
- prediction set
- portfolio definition
- target weights
- backtest config/result

### Target weights 是什么？

Target weights 是“某一天的组合目标权重”，不是买入/卖出命令。

例如：

| decision_date | instrument | weight |
|---|---:|---:|
| 2025-12-01 | 600000.XSHG | 0.25 |
| 2025-12-01 | 000001.XSHE | 0.25 |
| 2025-12-01 | 300750.XSHE | 0.25 |
| 2025-12-01 | 688981.XSHG | 0.25 |

它表示这四只股票各占目标组合 25%。至于今天应该买多少股、卖多少股、是否有现金、是否可卖、是否停牌，需要 Portfolio Execution/Paper Execution 和当前持仓状态共同决定。

## 9. 常见错误与处理

### `uv: command not found`

说明 uv 没装，或不在 `PATH`。先安装 uv，再重新打开终端。

### Python 版本不对

确认：

```bash
.venv/bin/python --version
```

必须至少是 3.11。

### 数据入库失败

常见原因：

- 非交易日。
- 外部数据源不可达。
- 数据源限流。
- Tushare 需要 token 但没有配置。
- 当前白名单股票当天没有数据。

可以先换一个最近的真实交易日，或先跳过数据入库，继续 dry-run。

### dry-run 返回 exit code 3

通常是请求契约校验失败。确认你使用的是：

```bash
evidence/research-chain/phase-0/fixtures/research_run_request-valid.json
```

不要使用 v3 fixture，也不要手工修改其中的 generation/checksum 字段。

### execute 提示缺少 quality decisions 或 scores

这是正常 fail-closed 行为。execute 模式必须有外部质量审批和分数输入，不能跳过。

### 想看某个 schema 的字段定义

契约文件在：

```text
config/schemas/contracts/
```

例如：

```text
config/schemas/contracts/target_weights.v1.json
config/schemas/contracts/backtest_result.v1.json
config/schemas/contracts/research_run_result.v1.json
```

## 10. 常用术语

| 术语 | 通俗解释 |
|---|---|
| Canonical | 统一后的内部标准数据格式。数据源可以换，但下游只依赖 canonical。 |
| PIT / Point-in-time | 只使用当时真实可知的数据，避免把未来信息偷偷用进训练或回测。 |
| Universe Snapshot | 某个研究范围内可用股票的快照。当前是固定白名单，还不是历史指数成分。 |
| Factor | 从原始行情中计算出的特征，例如量比、动量、波动率等。 |
| Manifest | 描述数据/模型产物的 JSON 文件，记录 schema、checksum、generation、lineage 等信息。 |
| Generation ID | 一份内容的稳定身份哈希，用来判断产物是否可复现、是否被篡改。 |
| Quality Decision | 对某类产物或动作的外部审批/质量判断，发布路径会校验它。 |
| Target Weights | 某日希望达到的组合权重，不等于下单指令。 |
| Backtest | 用历史数据模拟策略表现。结果受数据、成交假设和成本假设影响。 |
| Paper Execution | 确定性模拟撮合，用于研究执行规则，不连接真实券商。 |
| Risk Control Plane | 横切风控与治理层，负责 policy、event、decision、exception 和状态记录。 |
| Research Chain | 把数据、因子、数据集、训练、预测、组合、回测串起来的顶层编排器。 |

## 11. 新手推荐路线

1. 安装依赖并跑通 `pytest`。
2. 用 `uq-ingest daily` 尝试采集一个交易日。
3. 用仓库 fixture 跑一次 `uq-research-run --mode dry-run`。
4. 打开生成的 manifest，看懂 `run_id`、`generation_id`、`manifest_path`。
5. 阅读 `docs/layer-architecture.md`，确认你想用的层。
6. 等 Signal / Inference Layer 完成后，再进入“模型 artifact -> 每日分数 -> 目标持仓”的日常使用路径。
