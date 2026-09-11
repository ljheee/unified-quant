# Unified Quant 分层架构与现状

更新时间：2026-09-11  
适用读者：想理解本仓库已经实现到哪一步、各层输入输出是什么、哪些层可以单独运行的人。  
总体定位：这是一个以“契约优先、不可变产物、可审计 lineage”为核心的研究型量化平台。当前适合个人研究验证，不是生产交易系统，也不是自动投资建议系统。

## 1. 一图看懂

```text
行情/基本面数据源
  -> Data Ingest / Canonical Store
  -> Universe Snapshot
  -> Factor Layer / Qlib Factor Adapter
  -> Labels / Feature Preprocessing / Model Dataset
  -> Qlib Export & Runtime Trainer
  -> Model Artifact / Prediction
  -> Portfolio Construction
  -> Backtest
  -> Paper Execution（可选）

Risk Control Plane 是横切层：
  治理审批、发布门禁、订单/组合风控事件贯穿上述链路。

Research Chain 是编排层：
  把上述阶段串成可复现研究 run，并持久化请求、状态和最终结果。
```

## 2. 当前已实现的层

| 层 | 当前状态 | 主要输入 | 主要输出 | 能否独立运行 |
|---|---|---|---|---|
| Data Ingest / Canonical Store | 已发布的研究原型链路 | 交易日、股票白名单、TDX/Tushare 等数据源配置 | canonical daily bars、ingest run report | 能。可通过 `uq-ingest daily` 单独采集和落库。 |
| Universe Snapshot | 已有 v1 契约和存储 | instrument list、快照日期 | 不可变 universe manifest 与成员列表 | 能通过 store/API 使用；还没有按历史日期追溯成分股的 PIT membership。 |
| Factor Layer | v1.0.0 scoped release | canonical bars、factor registry | factor partition、manifest、checksum、accepted factor index | 能。可通过 `FactorStore` 单独计算和读取。 |
| Qlib Factor Adapter | v1.0.0，仅 Alpha158 已实现 | canonical bars / governed factor store | Alpha158 因子数据、Qlib 兼容输入 | 能。Alpha360 只有契约，不是完整实现。 |
| Model Layer | v1.1.4，Phase 0–5 released | factors、labels、universe、adjusted prices、dataset/model policy | feature schema、feature preprocessing、model dataset、Qlib export/receipt、model run/artifact、prediction set | 能通过 owning stores/API 使用；完整训练通常走 Research Chain。当前研究链的预测分数仍由外部 `scores-json` 提供，尚未自动完成“加载 artifact 推理今日分数”。 |
| Portfolio / Backtest | v1.0.0，Phase 0–3 released | prediction/score、universe、portfolio config、行情与复权价 | portfolio definition、target weights、backtest config/result | 能通过对应 store 和回测引擎单独调用。当前只支持 `top_n_equal_weight`。 |
| Risk Control Plane | v0.2.2，Phase 0–5 released | policy、policy review、组合/订单/发布上下文 | risk event、risk decision、risk exception、risk run/state | 不能作为孤立业务层运行。它是横切治理与风控层，需要被数据发布、研究链或执行路径调用。 |
| Paper Execution | v0.1.9，Phase 0–5 released | execution config、target weights/order plan、market data、portfolio state、risk decisions | order plan、paper fills/execution result、paper portfolio state | 能以确定性模拟撮合方式单独调用，但不能接入真实券商。 |
| Research Chain | v0.8，Phase 0–6 released | research request、governed upstream bindings、quality decisions、scores | request manifest、stage state、research run result、stage output bindings | 作为顶层编排层可以运行。CLI 的 v1 dry-run 可直接验证；execute 需要完整治理输入。v3 paper execution 契约已扩展，但 runtime 仍 fail-closed，不应当作已接入模拟交易的全链路。 |

> 说明：表格中的“released”表示该 scoped 版本已经按对应规格、测试和证据完成发布，不等于可实盘、可全自动交易或数据覆盖完整生产级。

## 3. 关键现状与限制

1. **数据仍偏研究原型**
   - 已有 canonical schema、manifest、checksum 和基础 PIT 概念。
   - 但当前默认只使用 `config/universe/research-whitelist.txt` 的小范围股票。
   - 还没有生产级、全市场、含完整公司行为与历史成分股的数据链。

2. **因子层和 Qlib Alpha158 已打通**
   - UQ 自己的 FactorStore 负责治理因子。
   - Qlib adapter 当前只实现 Alpha158，Alpha360 是契约预留。
   - Qlib 不是直接绕过治理读取原始数据的旁路。

3. **模型层已有真实 Qlib runtime trainer**
   - 可以训练并发布受治理的 model artifact。
   - Qlib export、init receipt、dataset、model run、artifact 都有契约和校验。
   - 但“artifact -> 最新因子 -> 今日预测分数”的自动推理层还没有成为独立、可日常运行的 Signal Layer。

4. **组合与回测只有第一版基线**
   - 当前支持 `top_n_equal_weight`。
   - 输出的 target weights 表示某日目标持仓权重，不是自动生成的买卖指令。
   - 需要对比连续两日权重、当前持仓和执行规则，才能推导具体买卖动作。

5. **风控不是单一 service**
   - Risk Control Plane 统一了 policy、event、decision、exception、run/state 等契约。
   - 它设计为横切层，在研究发布、组合构建、订单执行等边界设卡。
   - 不应把它理解成只能放在最后一步的“止损模块”。

6. **模拟撮合已经实现，但不是实盘**
   - Paper Execution 是确定性 paper replay。
   - 它用于验证规则、状态机、风控和执行结果，不保证真实市场成交价、滑点、流动性或券商接口行为。

7. **Research Chain 是最高层入口，但不是零输入一键器**
   - dry-run 可以校验请求并落 request/state。
   - execute 需要已存在的 governed artifacts、quality decisions 和外部 scores。
   - 这是有意设计：避免未审批模型、未治理数据或未审查策略悄悄进入结果。

## 4. 哪些层可以独立运行

“可独立运行”这里指：可以用该层 API 或 CLI，配合受治理输入单独完成工作；不要求一定从 Research Chain 进入。

### 可以独立运行

- **Data Ingest / Canonical Store**
  - 典型入口：`uq-ingest daily`。
  - 前提：有可用数据源和网络/凭证，具体取决于 source adapter。

- **Factor Layer**
  - 输入必须是 canonical bars。
  - 可直接通过 `FactorStore` 计算和读取。

- **Qlib Factor Adapter**
  - 可基于 governed canonical/factor 数据生成 Alpha158。
  - 不需要启动 Research Chain。

- **Model Layer**
  - 可以在已有 governed factors、labels、universe、adjusted prices、policy 的前提下单独训练。
  - 实际使用建议仍走 Research Chain，保证 lineage 完整。

- **Portfolio / Backtest**
  - 可以在已有 prediction/score、universe、行情、复权价等输入下单独调用。
  - 适合做策略实验，但输入必须仍满足契约。

- **Paper Execution**
  - 可以单独模拟执行 target weights/order plan。
  - 但输出只代表确定性 paper replay，不代表实盘。

- **Research Chain**
  - 它本身就是顶层编排层，可以独立发起完整研究 run。
  - 不过 execute 模式的准备成本较高。

### 不适合独立运行

- **Risk Control Plane**
  - 它没有“独立产生收益”的业务目标。
  - 必须绑定 policy、review、被审查动作和上下文。

- **Universe Snapshot**
  - 它更像治理数据产物，而不是独立计算引擎。
  - 通常由数据层、模型层、组合层消费。

- **Qlib Export / Receipt / Dataset / Artifact Store**
  - 这些是 Model Layer 的治理子产物。
  - 可以单独读取或校验，但单独运行没有完整业务意义。

## 5. 目标完善版还需要什么

不是说这些层现在都必须马上建，而是按目标触发。

| 未来能力/层 | 何时应该加 | 解决什么问题 |
|---|---|---|
| Signal / Inference Layer | 下一个最自然的切片 | 加载已发布 model artifact，读取最新 governed factors，生成每日 prediction/score，并发布受治理 prediction set。补齐“训练完成但无法一键给自选股打分”的缺口。 |
| Universe PIT Membership v2 | 需要用指数历史成分、跨多年训练/回测时 | 避免 survivorship bias，记录每个历史交易日的真实可投资成员。 |
| Production Data Chain v2 | 要做长期、全市场或接近实盘研究时 | 补齐公司行为、全市场覆盖、跨源对账、历史修复和运营监控。 |
| Richer Portfolio Schemes | top-N 等权基线验证后 | 支持 score-proportional、约束优化、风险平价等，而不是只选前 N 名。 |
| Execution Analytics / Slippage Model | paper replay 被频繁使用后 | 更真实地估计冲击成本、滑点、部分成交和容量限制。 |
| Broker / Live Execution Adapter | 合规、账户、交易时段、监控和异常处理全部满足后 | 连接真实券商。当前明确不做，也不应绕过 paper 治理直接实盘。 |
| Operations / Monitoring Layer | 多人使用或定时任务上线时 | 监控数据延迟、因子失败、模型漂移、任务重跑和告警。 |

目标完善版的数据流会是：

```text
Production Data Chain
  -> PIT Universe
  -> Factor/Feature Layer
  -> Signal/Inference Layer
  -> Portfolio Construction
  -> Risk Control Plane
  -> Paper Execution
  -> Broker/Live Execution（最后才考虑）
```

## 6. 当前最推荐的推进顺序

1. **先做 Signal / Inference Layer**
   - 原因：模型层已有 artifact，但还没有“每日自动推理”的标准化入口。
   - 价值：把训练结果变成可消费的每日 score/prediction。

2. **再做 Universe PIT Membership v2**
   - 原因：一旦从自选股扩展到指数成分或全市场，历史成分正确性会直接影响研究可信度。

3. **然后增强 Production Data Chain**
   - 原因：长期训练、复权、分红送转、退市股和跨源一致性都需要生产级数据。

4. **之后扩展组合方案和执行分析**
   - 原因：有了稳定信号后，才有必要研究更复杂的权重构造和真实成本。

5. **最后才考虑实盘/broker adapter**
   - 原因：这涉及资金安全、合规、异常恢复、权限控制和实时运维，不能只靠当前研究平台能力。
