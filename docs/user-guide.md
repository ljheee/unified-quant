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

## 5. 运行一次安全的 Research Chain dry-run

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

输出里的 `manifest_path` 就是初始状态文件位置。一般类似：

```text
/tmp/unified-quant-data/research_runs/requests/request=.../run=.../manifest.json
/tmp/unified-quant-data/research_runs/states/request=.../run=.../stage=00/manifest.json
```

> 注意：当前 CLI 的 dry-run 使用的是 v1 示例请求。仓库里也有更严格的 v2/v3 契约，但不要把 v3 fixture 直接复制进这个 CLI dry-run；它目前不会被 CLI 的 dry-run 按 v3 路由。

## 6. execute 模式是什么？小白应该用吗？

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

## 7. 结果在哪里看

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

## 8. 常见错误与处理

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

## 9. 常用术语

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

## 10. 新手推荐路线

1. 安装依赖并跑通 `pytest`。
2. 用 `uq-ingest daily` 尝试采集一个交易日。
3. 用仓库 fixture 跑一次 `uq-research-run --mode dry-run`。
4. 打开生成的 manifest，看懂 `run_id`、`generation_id`、`manifest_path`。
5. 阅读 `docs/layer-architecture.md`，确认你想用的层。
6. 等 Signal / Inference Layer 完成后，再进入“模型 artifact -> 每日分数 -> 目标持仓”的日常使用路径。
