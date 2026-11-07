> **历史快照**：以下完整保留 2026-11-06 重写前的文档，包括工作树中尚未提交的修订。文中的状态、数字和计划仅代表当时记录；当前状态请看 [落地计划](trading-grade-training-plan.md) 和 [进度](trading-grade-progress.md)。

# 交易级训练落地规划

> 目标不是“链路跑通”，而是让训练结果具备交易决策价值：样本外、扣成本、可复现、可解释、可风控，并且结论能指导下一轮调仓。
已知约束：2000积分的tushare token，获取指数成本只能提供月度PIT，使用月度近似替代日维度即可。
>
> 2026-11-02 更新：683-session 全窗口 snapshot-backed readiness 已通过；Alpha158 因子和新标签已重发，训练样本候选已生成并等待独立外部质量决定。测试密钥隔离链路已完成样本发布、32 标的子集 Qlib Linear/LightGBM 训练与回读；另在 `data/trading-grade-history/test-key-smoke/canonical-v2-pilot/` 将 683 个旧格式行情分区重发为真正的 canonical-v2 manifest，并生成 683 份 owning quality report。测试根记录 `canonical-v2-test-migration-receipt.json`。canonical `quality_report.v1` 不含签名字段，测试密钥不能签进该报告；隔离根与报告只证明测试级迁移/读回，不是独立来源质量批准。2026-11-04 已在隔离测试根以测试密钥跑通 LightGBM 两折 walk-forward（264 个 OOS sessions，attempt `folds_passed`）；该诊断不构成正式质量结论。年度线性基线配对比较、正式 OOS 资格和 paper 对账仍未完成，计划目标未完成。指数成分月度近似按维护者决定接受，但必须保留精度声明。

## 1. 当前基线

### 1.1 已跑通的真实结果

- request：`41064ea28ffd5db95ecc736c6c340da77505162698636ea1c4cbb26c4114df19`
- run：`00000000-0000-4000-8000-000000000011`
- result：`e1267f29287fbcdfe62efe14edf0940fc9b9b1ecb1934e9227dc3555c8e4a749`
- `final_status=passed`，10/10 stage passed，readback 无失败
- 训练样本：728,880 行 / 4,891 只
- 因子：157 个 Alpha158 因子
- 回测区间：`2025-05-13..2025-12-31`，160 个交易日
- 决策日：11 个，`2025-12-10..2025-12-24`

### 1.2 当前结果

| 指标 | 数值 |
|---|---:|
| 累计收益 | -2.85056192% |
| 年化收益 | -4.45269071% |
| 年化波动 | 6.92049316% |
| Sharpe | -0.623365 |
| 最大回撤 | 7.426166% |
| 平均日换手 | 3.187808% |
| 胜率（160 个回测日中上涨日占比） | 4.375% |

> 胜率口径：160 个回测日中有 145 个空仓日收益为 0；该值不是“持仓日胜率”。

### 1.3 必须明确的边界

- 该结果是**样本外但样本很短**：只有 11 个决策日。
- 回测行情覆盖 160 天，不代表策略持仓 160 天。
- 质量决策仍使用仓库测试密钥，不能作为生产发布依据。
- 模型是 `qlib_linear` 基线，未做模型选择。
- 回测在一个**隐含的 10% 成交量参与度上限**下产出：`volume_participation_cap` 未写入 config，引擎缺省静默套用 0.10（`src/uq/backtest/engine.py`）。
- 结果只能说明链路、成本、T+1、涨跌停、停牌等机制已闭合，**不能说明策略具备正期望**。

### 1.4 年度 OOS 执行前核验（2026-11-02）

现有 walk-forward 计划在结构上达到一年覆盖：两个测试折各 132 个 session，合计 264 个 OOS session、397 个日历日；fold 0 测试期为 `2025-08-07..2026-02-26`，fold 1 为 `2026-02-27..2026-09-07`。这只是计划覆盖，不是已执行结果。计划仍标记 `market_regimes_verified=false`、`historical_pit_completeness_verified=false`。

本次对 `data/trading-grade-history/` 的拥有层预检发现：

- 源目录 `bars-csiall/canonical/bars_daily/v2/` 的 683 个分区实际是 `manifest_version=1`，且缺少 683 份对应质量报告；这不是只补报告就能修复的 canonical-v2 store。已按维护者本轮指示，在隔离的 `test-key-smoke/canonical-v2-pilot/` 用 `CanonicalV2Store` 重发为 manifest v2，并生成/读回 683 份 `quality_report.v1`，覆盖 3,428,679 行。源每日 CSV 与重发 bars 的 OHLCV/amount 六字段逐行一致，683 个新 Parquet 的 data checksum 均与源分区一致；target generation 变化，因为 manifest 版本和 provenance 不同。实际 `RawExecutionPriceStore` 单日读取也通过。测试根中的质量报告是机械校验，不是测试密钥签名或独立来源裁决；报告明确标注 status、限价和 adj_factor 未在本次独立重取核验。此前 `ManifestFirstReader` 的旧 manifest checksum 读取不等同于 owning canonical-v2 质量读回。
- 该历史根下没有 `backtest_execution_facts/`、`backtest_configs/`、`research_runs/requests/` 或 `research_runs/results/`，也没有逐折 v3 回测配置和 paper 风控文档。`run_walk_forward.py --mode execute` 的全协议预检因此尚无可执行 jobs。
- 全量 `sample_construction.v1` 候选仍等待独立质量决定。现有测试密钥签名只用于隔离 smoke，不是正式决定，也不能授权年度研究结论。
- `oos-market-regimes-20261102.json` 覆盖全部 264 个测试 session，并按诊断代理分为 up 122、down 46、sideways 96；它使用月度近似成分和等权市场代理，审计状态为 `diagnostic_proxy_predeclared_before_model_run`，不能将计划的市场状态资格改成已核验。

`compare_training_models.py` 要求两边均为已通过的研究结果，且回测配置为 v3/v4；因此当前没有合法的输入可生成年度扣成本模型比较。canonical-v2 迁移已在测试根完成，但尚未与正式研究输入组装成完整 data root，也不替代独立来源质量审查。本次未执行 walk-forward、模型对比或 paper 对账，也没有将测试密钥结果标成正式 OOS。继续执行前仍须取得全量样本的独立质量决定，准备完整窗口的来源声明执行事实及已评审 v3 配置，并为每折生成已评审 request 与 paper 风控材料；之后才运行全协议 preflight 和两折执行。

## 2. 什么叫“对交易有指导意义”

训练结果至少同时满足以下条件，才允许进入模拟盘或小资金实盘评审：

1. **样本外足够长**：至少覆盖 1 年、多个市场状态，且训练/验证/测试严格按时间隔离。
2. **扣成本后仍有正期望**：佣金、印花税、滑点、冲击成本全部计入。
3. **相对基准有稳定超额**：不能只看绝对收益。
4. **风险可控**：最大回撤、换手、行业暴露、单票集中度在预设范围内。
5. **可复现**：相同 request、数据、代码和环境能复现同一逻辑结果。
6. **可解释**：能说明收益来自哪些因子、行业、风格和交易行为。
7. **可执行**：考虑成交容量、停牌、涨跌停、T+1、整手和资金约束。

## 3. 升级路线

### 贯穿前置：数据 onboarding 与治理门槛

以下依赖在多个阶段反复出现，单列一次，各阶段引用不再展开：

- **新数据类接入状态**：`TushareAdapter.research_data()` 与不可变 `TushareDependencyStore` 已承接 `stock_basic`、`daily_basic`、SW 行业、标准财报/指标、业绩预告/快报、`report_rc`、资金流、沪深股通持股和龙虎榜；原始响应不等同 canonical 因子或质量批准。实际 2000 积分端点调用、行数与时间范围见 [`trading-grade-progress.md`](trading-grade-progress.md)。`report_rc` 仅有每日 10 次试用权限，完整分析师预期不是启动 Alpha158 线性模型与 LightGBM 主比较的前置条件；该类特征仅作为可选扩展，未通过历史 PIT 与质量审核前保持缺失，不得用合成值替代。
- **离线历史数据根**：仓库忽略目录 `data/trading-grade-history/` 已作为本机历史根并填充 2024-01-01 至 2026-10-30 的 raw Tushare 依赖快照、月度 `csi-all` 成分和 `bars_daily.v2` canonical 行情。readiness 已在 683 个 session 上通过完整 snapshot-backed 复核：683 个不可变 `suspend_d` 快照、官方公告区间和 343 行 `stock_basic(list_status=D)` 退市日期快照共同解释所有必需行情缺口，没有补造或改写 canonical bars。当前通过报告 `readiness-csiall-20240101-20261030-suspension-intervals.json` 将 174 个退市有效日及之后缺失 session、以及 2 个只在 60-session warmup 内出现的短历史标的记为 informational。较早的 `corrected-20261102.json` 曾把这两个标的按全窗口短历史规则误判为 blocking；按首个模型可见 session 复算后，该旧结果已被较新的通过报告取代。688766 的 7 个缺失 session 由发行人公告区间解释，不补造 canonical bars；首个模型可见 session 为 2024-04-03。2026-11-02 已把日线切到 easy_tdx MAC 客户端，三个实测节点都对 `600000` 返回 800 根日线。指数成分严格日频 transaction-time PIT 不可得，按维护者已确认的月度近似口径使用并披露；原始市值/行业/财报等仍未转成受评审 exposure/factor panel。
- **PIT 纪律**：每日指标/资金流/沪深股通/龙虎榜盘后可见，决策使用至少滞后一 session；基本面与预期按公告/披露时点对齐。SW `index_member_all` 的 `in_date/out_date` 是有效期字段，但没有供应商历史发布时间，不能宣称严格 transaction-time PIT。历史 universe 仍需核验退市股、停牌与新股覆盖。
- **新依赖治理**：本次数据接入复用既有 Tushare 与 Parquet 依赖，未修改 `uv.lock`；若后续依赖锁变更，现有认证矩阵仍须完整重跑 `scripts/run_gate.sh` 与远程矩阵。
- **生产评审路径**：生产 trust anchor 与外部质量评审流程的 provisioning 是维护者决策项，是阶段 6 的硬前置。

### 阶段 0：冻结基线（必须先做）

**目标**：把当前结果固定为“基线”，后续所有升级都必须与它可比。

**动作**

- 将当前 request/run/result 的 identity、窗口、split、因子集、模型配置、成本参数写入 `evidence/` 下的基线记录，并绑定代码 HEAD、数据根、环境锁和结果 generation。
- 补上两个已查实的复现缺口，否则“任何人能复现”不成立：
  - 基线 run 的 `runner_identity.code_fingerprint` 与 `environment.lock_digest` 全为零——代码与环境绑定当前并未发生，可能需要先实现绑定机制，再谈记录。
  - 标签资格使用的 `listing_date` 来自 tushare `stock_basic` 实时拉取，不被受治理 store 的 checksum 覆盖（标签发布证据的 `reproducibility_note` 已自承认）；必须钉住这份映射，否则标签层面无法复现。
- 把分阶段耗时一并写入证据：已记录一次 `2d71326` 之后的部分全链路耗时，但该运行在 exporter 清点阶段被中断，不能当作完整全 A 基线；完成优化后仍需完整重跑并分阶段记录。
- 明确记录测试密钥、非生产评审、短样本外等限制。
- 建立统一的实验命名和结果归档规则。

**验收**

- 任何人能从证据记录复现当前 11 决策日结果。
- 基线结果不被后续实验覆盖。

### 阶段 1：评估协议升级（最高优先级）

**目标**：先让评估可信，再谈模型。

**动作**

1. **前置：扩展数据窗口。** 原 export 日历止于 `2025-12-24`。目前已接入 `2024-01-01..2026-10-30` 的 canonical bars 和月度近似 PIT 成分；snapshot-backed readiness 已通过：活跃期缺口均由逐日停牌快照/官方公告区间解释，或仅出现在有效退市日之后；2 个短历史标的只在首个 60-session warmup 内出现。原始 canonical bars 保持不变。新窗口 `return_5d/2.0.0` 已发布并通过标签腿审计；Alpha158/2.0.0 已发布 623 个因子分区（分区集 digest `3676c2af3c15900ad6824fb151f4a7244fa8a5fadf0332b1c9c249a64d4c7fee`）。`sample_construction.v1` 候选 generation `2f4c3ad5afd26f8f56b51eb488a2e3cc90ef3fd0eaeb6a1867b8271ffcae1132` 已构建：2,997,703 行、157 个因子、618 个成熟标签 session，Parquet checksum `6d170541ec5060fa2dcb51cc17208b8028e39e34a898ed6476a6561a6c029304`；独立外部质量决定仍待完成。相同 generation 另有测试密钥 smoke 发布于 Git 忽略的 `data/trading-grade-history/test-key-smoke/governed/`；另从中抽取 32 个标的发布了 19,018 行 smoke dataset，并完成 Qlib Linear/LightGBM 训练。以上产物仅用于链路验证，不得作为正式 dataset 输入或研究结论。只有独立决定通过并完成正式 dataset 发布后，才可执行年度 OOS。Alpha158 需约 60 个交易日暖机 lead-in，历史数据起点必须早于训练起点至少一个暖机窗口。
2. **前置：标签设计审计。** 新窗口标签绑定独立治理日历和退市快照，严格按日历 T+5 定位，并在 D..D+5 任一 bars 缺失时输出 null；资格标记按 suspension、限价和有效退市日期从源字段计算并进入输入 checksum。2026-11-02 全窗口审计验证 683/683 行情分区及 3,333,110 个非空标签，详情见 [`trading-grade-label-audit.md`](trading-grade-label-audit.md) 和 gitignored 审计报告。结论：
   - 五日标签的相邻样本共享四个 session；IC/RankIC 显著性必须用逐日截面序列并至少设 HAC lag=4。现有评估路径已实施该门槛，不从名义股票行数推算有效时间样本量。
   - close(T)→close(T+5) 相对 open(T+1)→close(T+5) 的平均差为 −7.2052 基点，p95 绝对差 2.3834%，151,875 行方向相反；T+6 开盘退出比较中有 216,376 行方向相反。缺口已量化；目标本身仍是 close-to-close，模型结论必须以成本化 T+1 回测为准。若要声称目标完全可交易，须另发开盘到开盘语义版本并重训。
   - `stock_basic(D)` 当前退市日期快照没有历史可见时点证据。314 个标的最后出现早于窗口末日，5,584 条空标签在全局末五 session 之前；审计将 `pit_delisting_completeness_certified=false`，历史 universe/PIT 与退出价值仍未通过验收。
   - 标签审计校验原始分区与日历 session 完全一致，并逐个非空标签核对 D..D+5 行情网格。审计 CLI 现在支持独立 calendar root，允许声明范围从非交易日开始。
3. 将 test split 从 11 个交易日扩展到至少 6–12 个月。新数据日历上的 expanding 两折计划已发布：generation `12b84e7fb0856333ac6272d4bff2e38f4baaa74c501d0c40086ea916c53f5f8d`，每折 252 session 起始训练、60 session 验证、132 session 测试，含 5-session purge、2-session embargo 与 60-session Alpha158 warmup；合计 264 个 OOS session、397 个日历日。此前隔离 smoke 使用单折形状切分并只训练训练段。2026-11-04 已在测试根跑通全量 LightGBM 两折执行：attempt `folds_passed`、退出码 0、264 个 OOS sessions/397 个日历日；但 `market_regimes_verified=false`、`historical_pit_completeness_verified=false`，只证明测试链路和执行协议可跑，不产生正式质量结论。相同数据、split、成本下的线性基线已在测试根完成两折，逐折配对报告已生成并通过回读；两折独立回测的年度汇总仍待执行。
4. 采用 walk-forward 或滚动训练/验证/测试，而不是一次性固定 split。下一步需在因子分区/模型数据齐备后为每折创建新的、已评审 request，并先通过全协议 preflight，再执行。
   - 已实现逐折结果的统一 v3 回放入口：重新读取每折经评审的 OOS 目标权重，并在共享现金/持仓的一次引擎运行中执行；成本、成交、初始资金与风险设置必须和各折一致。末折最后目标后增加两个 session，依次完成该目标的 T+1 执行和期末强制清仓尝试；清仓仍受停牌、涨跌停、参与率和风控约束，receipt 记录是否清零，交易验收仍需真实数据另做。
5. 增加基准（数据依赖见“贯穿前置”一节）：
   - 全 A 等权
   - 市值加权
   - 行业中性基准
   - `read_pit_benchmark_membership` 从 owning `IndexMembershipStore` 按治理日历逐日回读成员，并保留 snapshot-set generation/digest/checksum；当前源精度为月度近似 PIT。`BenchmarkWeightStore` 不可变发布权重面板，`scripts/run_benchmark_backtest.py` 复用策略的已评审 v3/v4 配置和调仓日，经同一 T+1 引擎发布基准回测；真实 PIT 市值/行业源及真实同成本验收仍待完成。
6. 增加评估指标：
   - 超额年化收益
   - 信息比率
   - IC / Rank IC
   - 持仓日胜率与全区间胜率（两者分开报告）
   - 换手率与成本敏感性
   - 分年度、分市场状态表现
   - 最大回撤与回撤恢复时间
   - 行业、市值、流动性暴露

**验收**

- 至少 1 年样本外，覆盖上涨、下跌、震荡三种状态。
- 每个指标都能从结果制品直接重算。
- 基准和策略使用同一成本、同一 universe、同一交易约束。
- 历史 universe 按获批的数据精度核验：CSI-All 月度成分近似由维护者明确接受，制品与报告必须标注 `monthly_approximation`，不得表述成严格日频 transaction-time PIT；新上市/退市边界和标签资格可知时点另需有书面审计结论。
- 标签审计三问（自相关、可执行口径、delisting 剔除的时点可知性）有书面结论。

### 阶段 2：交易约束与成本升级

**目标**：让回测结果接近可执行交易。

**动作**

- **已具备、已核验**：真实运行使用的 config（generation `c67a48fd…`）确认为 `daily_t1_open`、`board_lot=100`、`prior_day_holding_only`、佣金 2.5bps/印花税 5bps/滑点 1bps。
- **历史基线限制**：v1/v2 基线 config 未显式保存参与率上限，引擎旧缺省为 0.10；冻结基线不改写。v3 config 将 `volume_participation_cap`、fill-rate multiplier 和调仓间隔设为必填字段，避免新结果继承隐式执行假设。
- **v3 执行与成本功能已实现**：离线执行事实逐日绑定历史板块/ST/停牌、涨跌停状态与价格、限价参考、整手规则；引擎检查原始开盘及滑点/冲击后价格是否越带。线性/平方根冲击、最低佣金、双边滑点、卖出税、成交率/参与率部分成交、调仓间隔、实际换手上限、单票/行业/现金预算均已由 v3 config 与同版本引擎处理。对应行为和失败边界见 [`portfolio-backtest-risk-execution.md`](subsystems/portfolio-backtest-risk-execution.md) 与 `tests/test_backtest_v3.py`。
- **成本敏感性与资金网格已实现**：`scripts/run_backtest_stress.py` 在同一目标权重和 owning 数据上分别评估佣金翻倍（含最低费）、滑点翻倍、成交率减半及明确资金网格；各派生 config/result 单独通过外部质量评审并复算 readback。网格的最高合格资金只代表被测点，不等同于经市场校准的容量。
- **公司行为 v4 已实现，真实源核验仍未完成**：配置绑定 exact-window `corporate_action_events` 面板及 dividend/rights completeness approval。除息日按股权登记日收盘持股形成税后现金和送股应收并计入 NAV、不可用于下单；现金仅在支付日可用；送股在上市日入账并遵守当日不可卖的 T+1；权利发行覆盖清单内标的全窗口排除；碎股和关键日期/覆盖不完整时拒绝。v1/v2/v3 行为保持不变。v4 逻辑有 synthetic engine/store/publisher/exposure、benchmark/stress 回归；2026-11-02 对 6 只标的、2024-01-01..2026-10-30 的 live Tushare/TDX 事件 pilot 全部一致，但全市场及全窗来源审计、配股覆盖和 paper 验证仍未完成。paper 对账及跨折连续 OOS 回放仍只接受 v3。
- **其余数据依赖和实证仍未完成**：执行事实已有读取/发布契约，但历史来源和规则覆盖尚未获得真实全窗口审计；`reference_adjustment_bps` 会留证除权参考相对原始前收的调整幅度，但没有实际校准误差证据。行业约束需要真实、开盘前可见的行业分类。压力场景未在一年 OOS 数据上证明策略结论不反转，容量网格也没有实盘流动性校准。

**验收**

- 在更保守成本下，策略结论不反转。
- 容量分析能给出可承载资金规模。

### 阶段 3：模型升级（在评估可信之后）

**目标**：用同一协议比较线性基线与更强的非线性模型。

**动作**

1. 保留 `qlib_linear` 作为基线。
2. 先定义模型选择协议：同一特征、同一 split、同一成本下，只用验证集选择一个候选进入样本外测试，避免三个模型在测试集上择优。现已实现最多三个 LightGBM 候选、按验证段日均截面 RankIC 选择，测试标签不参与选择；`scripts/compare_training_models.py` 现在校验线性与 LightGBM 运行绑定同一实际数据制品、特征、标签、切分、组合模板与已评审回测成本后，发布 candidate-minus-baseline 指标。这个比较仍只是诊断，不能自动替换模型。候选上限只作用于单次选择调用；整个研究程序的多重比较预算尚未定义，不能把它说成已控制。
3. 接入 LightGBM；依赖通过 `gbdt` extra 提供，并在 dev 测试环境安装。Research Chain v7 已能通过 model-definition template v2 选择并训练 Qlib LightGBM。XGBoost/CatBoost 未纳入本次范围。
4. 统一：
   - 特征集
   - 样本
   - split
   - 成本
   - 组合约束
5. 只在**样本外、扣成本后**稳定优于线性基线时替换。

**验收**

- 新模型在至少 1 年样本外上，信息比率和超额收益同时优于基线。
- 结果不是由单一行业、单一风格或少数标的贡献。
- 模型复杂度增加有明确收益，而不是只增加过拟合风险。

### 阶段 4：因子升级

**目标**：在模型之外增加有效信息。

**动作**

- 本阶段的新基本面、预期、资金行为因子是 Alpha158 主比较之外的可选扩展，不是启动线性模型与 LightGBM 基线比较的前置条件。若纳入，基本面数据必须按公告日而非报告期对齐，报告期即用即前视；预期数据须有可用的历史发布时点和全市场覆盖，否则保持缺失。
- 每个新因子先做单因子增量实验，再进入批量组合。
- 明确与现有 157 因子的共线性处理，以及样本外稳定性门槛。
- 补充基本面因子：
  - 估值
  - 盈利质量
  - 成长
  - 杠杆
- 补充预期与资金行为因子：
  - 分析师预期修正
  - 资金流
  - 北向/龙虎榜等可用数据
- 做因子中性化：
  - 行业中性
  - 市值中性
  - 流动性中性
- 做因子筛选：
  - IC 稳定性
  - 共线性
  - 换手成本

阶段 4 的诊断入口现已覆盖已有受治理因子：逐因子日频 IC/RankIC 与 HAC 稳定性、日频横截面 Spearman 共线性、等权 top-N 成员变动换手代理，以及绑定已评审 v3 成本配置的换手成本代理。新增 `build_tushare_factor_candidates.py` 从逐日 `daily_basic`/`moneyflow` 原始快照派生 8 个候选值因子及市值/流动性暴露；清单绑定各快照和 canonical 日历。它明确标记 `historical_vintage_verified=false`、`strict_pit_eligible=false`，只能用于研发诊断，不能冒充严格 PIT 或发布到生产 owning store。`screen_tushare_factor_candidates.py` 可对候选面板重算 IC/RankIC/HAC、共线性和换手成本代理；`select_factor_candidates.py` 按 `config/factor-selection/tushare-candidate-policy.v1.json` 对验证窗口生成最多三个候选的 shortlist，按显式方向、有效期数、RankIC、覆盖、共线性和成本阈值筛选，不自动改变已评审 factor set。成本代理按参与率上限估算冲击，仅计比例佣金、印花税、滑点和冲击；不计最低佣金、真实成交、持仓约束或市场容量，因此不能替代回测成本。日历/快照绑定与验证集 shortlist 的算法路径已实现；严格 PIT 源审查、经评审 exposure/factor 发布、单因子增量和跨期 OOS 验收仍未完成。

中性化的代码路径已补齐：新增 `factor_exposure_panel.v1` owning store 和离线 Parquet 发布入口，记录 source approval reference；`available_at` 必须带时区且不晚于当日上海时间 15:00。`feature_preprocessing.v2` 把暴露 generation、manifest digest、数据 checksum 与 OLS 控制顺序写入清单，`feature_schema.v2` 标记 `neutralized`；Research Chain 使用现有预处理 binding 按清单版本选择，并在 dataset stage 重读暴露、复算变换。维护者已批准 `factor_exposure_panel_v1` 与 `feature_preprocessing_v2` 的 passed-only review policies，活动 trust anchor 已更新并核验 digest。真实行业/市值/流动性 PIT 源仍不存在；测试夹具不是历史数据，不可用于实证验收。

**验收**

- 新增因子在样本外有稳定增量贡献。
- 因子收益不是由单一时期驱动。

### 阶段 5：组合与风控升级

**目标**：把“预测分数”变成可交易组合。

**动作**

- v1 `portfolio_definition` 与模板固定 `top_n_equal_weight`；v2 已加入分数加权；v3 已加入风险平价和行业中性，Research Chain v8 使用 v3 模板。建议初始风险窗口为 60 个交易日，且必须写入经评审请求。风险平价先按分数选 top-N，再用受治理日收益的历史样本协方差求等风险贡献；每个决策日严格只用此前已收盘的完整收益窗口。行业中性在 top-N 选股后，把预算均分给组合中出现的行业，再在行业内等权；行业上限按比例削减超限行业并留存现金，不向其他行业再分配。这是组合内部行业平衡，不是相对基准的中性。行业标签取自回测配置绑定、开盘前可见的 execution facts。无收益数据、缺分类、错误绑定或非正定协方差都会拒绝生成权重。单票/现金/换手约束随后应用，可能改变风险/行业预算。真实历史收益和行业面板尚无，策略验收待真实源；v3 的最大回撤停止与目标波动率模拟控制年度效果也待真实历史窗口。
- 从单一 `top_n_equal_weight` 扩展到：
  - 分数加权（v2 已实现 softmax 权重；完整回测验证待真实样本）
  - 风险平价（v3 算法与 Research Chain v8 路由已实现；真实收益源和成本下验收待完成）
  - 行业中性组合（v3 已实现组合内等行业预算和 PIT execution-facts 绑定；真实行业源验收待完成）
- 增加：
  - 最大回撤停止与波动率目标（v3 已实现；年度实证效果待真实历史窗口）
  - 换手上限、单票和行业上限、现金管理（portfolio/backtest v3 已实现；行业数据质量与 OOS 效果待真实源）

**验收**

- 组合在压力测试下仍满足风险预算。
- 换手和成本在可接受范围。

### 阶段 6：模拟盘与小资金验证

**目标**：从回测走向真实执行。

**动作**

- 前置：建立生产评审路径（见“贯穿前置”）——`runtime.py` 的生产守卫会拒绝仓库测试锚，未 provisioning 前小资金实盘评审无从谈起。
- 先跑 paper execution。
- 明确 paper execution 是确定性回放，只能验证流程、状态演进和约束，不能证明真实成交率或真实冲击成本。
- 对比模拟成交与回测成交差异。
- **当前范围仅含模拟盘**：项目架构明确禁止 broker/live 路径，故本计划不连接券商、不下真实订单。小资金实盘指标需另行维护者决策并改变架构边界，不能作为本轮默认执行步骤。
- 若未来经单独决策解除边界，再做小资金实盘验证：
  - 成交率
  - 滑点
  - 冲击成本
  - 实际持仓与目标权重偏差

**验收**

- 模拟盘与回测的偏差在预设范围内。
- 小资金实盘的交易成本与回测假设一致。

## 4. 优先级排序

1. **评估协议升级**（含标签设计审计与数据窗口扩展）：没有可信样本外，模型升级没有意义。
2. **交易约束与成本升级**：没有真实成本，收益不可交易。
3. **模型升级**：线性基线之后再做 GBDT。
4. **因子升级**：与模型升级并行，但必须做增量验证。
5. **组合与风控升级**：把分数变成可执行组合。
6. **模拟盘与小资金验证**：最后才谈真实交易。

## 5. 当前最值得做的下一步

### 下一步 A：扩展样本外

- 前置依赖：先完成阶段 1 的数据窗口扩展与标签设计审计，仅改 split 得不到更长的样本外。
- 把 test split 扩展到至少 6–12 个月。
- 使用 walk-forward 重新评估当前线性模型。
- 输出基准、超额、IC、换手、回撤和成本敏感性。

### 下一步 B：解决 exporter 性能

已锚定的耗时事实：基线 run（`…0011`）首遍全链路约 3 小时 20 分（state ledger `created_at` 锚点 09:32:45→12:53:21 UTC），且跑在 `2d71326` 批量化修复**之前**。2026-11-02 的 attempt telemetry 记录了一个后续运行：总耗时 1,789.61 秒，`dataset_preparation` 1,185.37 秒，`feature_files` 100.28 秒，`file_inventory` 207.78 秒后以 `KeyboardInterrupt` 失败；它是中断时的部分耗时，不是完整导出基线。先前流传的“1 小时 56 分 / export 28–30 分钟”仍无出处，不得作为基线引用。

建议：

- 已在导出器中实现写入期摘要：从序列化缓冲区计算 checksum 并登记文件清单；有序目录清点核验路径/大小且不保留全树 `Path` 列表，不再为首轮 manifest 哈希重读所有 `.bin`。发布前全文件读回校验、durable fsync、最终 Qlib 布局和 manifest schema 均保留；完整 `manifest.files` 仍因身份绑定而驻留内存。
- `QlibRuntimeTrainer` 现在在构造 `DataHandlerLP` 前解析训练 split，只加载训练区间；`uq-research-run --qlib-kernels N` 控制旧 `.bin` 路径的逐标的读取 worker 数，默认 4。2026-11-03 的 256 标的微基准（157 特征和标签）在 1/2/4/8 workers 下分别耗时 37.70/26.38/24.89/33.03 秒，frame hash 一致；4 workers 进程树 RSS 求和峰值约 1.38 GiB（包含共享页）。随后对全部 5,276 标的、真实训练区间、1,230,325 行做了完整 QlibDataLoader 输入读取：4 workers 耗时 365.766 秒，进程树 RSS 求和峰值约 3.24 GiB。它尚不含 manifest 校验、`DataHandlerLP` 后处理与拟合，不能替代完整 model-stage 计时。
- 11 月 3 日使用现有已发布 export 直接运行 `QlibRuntimeTrainer(kernels=4)`，LightGBM 4.7.0 在内存中成功拟合并序列化；从逐文件摘要核验到模型字节完成共 833.82 秒，产物 26,214 bytes，未发布模型 artifact、未更改 run state，也未计算预测或质量指标。旧签名请求绑定优化前源码指纹，因此该次是隔离的 trainer 诊断调用，不是完整 CLI/Research Chain run，也不能替代正式 OOS 结果。完整记录见 `trading-grade-progress.md` 第四十五轮。
- 已在新鲜 Research Chain 路径把连续的 exporter 全内容 read-back 合并：export stage 交给 model stage 已验证的 snapshot，续跑只做结构/大小检查，trainer 在 Qlib load 前保留完整 SHA-256，最终 reconciliation 保留严格回读。回归锁定新鲜链路 read 模式和同尺寸内容篡改拒绝。按执行路径计数，完整 `.bin` 内容扫描由六轮减为三轮，另有一轮结构/大小清点；具体端到端节省仍待完整链路实测。
- 2026-11-03 对既有全量 export 单独重跑 trainer 并加入阶段计时：833,611 文件、5,276 标的、157 特征，4 workers。训练前 SHA-256 为 374.48 秒，Qlib 训练窗口读取为 364.27 秒，LightGBM fit 为 18.00 秒；总计 784.45 秒，模型字节 checksum 与此前诊断一致。它是 trainer 直调而不是完整 Research Chain，不能当作重复扫描优化后的端到端对照；详情见 `trading-grade-progress.md` 第四十六轮。
- Research Chain 已把 `DatasetWriter.read()` 返回的受治理 frame 交给 Qlib `StaticDataLoader`；直接 trainer 调用未提供该 frame 时保留 `.bin` 读取。全量 train frame（1,196,803 × 158）与 Qlib 0.9.7 `.bin` 路径的索引、列、dtype 和全部值完全一致，摘要均为 `f3936a69534849e3227d6115c5c7908b9808ef5e3931bacdbfe69b923f326788`。全窗 `.bin` loader 加 handler prepare 耗时 322.02 秒；静态路由的独立诊断耗时 8.07 秒（含另行读取 Parquet，handler 本身 1.04 秒）。使用 owning frame 的 trainer 诊断中，dataset read 169.78 秒、完整 export hash 397.66 秒、静态转换 4.10 秒、handler 0.01 秒、split prepare 0.49 秒、fit 16.56 秒、总计 446.07 秒；模型字节 checksum 与 `.bin` 路径相同。该诊断不是完整 Research Chain 受控对照，不能据此宣称端到端加速比例。完整记录见 `trading-grade-progress.md` 第四十七轮。
- Research Chain 的同一 owner-verified frame 现用于请求日期的推理，Qlib `StaticDataLoader` 只构造所需日期的数据；静态路径由 export instrument/date inventory 计算缺失组合数，保留 `unscorable_rows` 语义而不创建稠密证券×日期矩阵。新鲜运行复用训练器刚完成的全量 export 内容校验，避免推理再哈希 83 万个 `.bin`；续跑与无 owner frame 的直接调用仍严格哈希并保留 `.bin` 加载回退。两折旧请求诊断已结束但批次未通过：fold0 和 fold1 的 Research Chain result/telemetry 均为 passed（6,090.16/7,705.57 秒），但 walk-forward attempt 在 fold1 因 `research environment code_fingerprint does not match observed bytes` fail-closed。请求仍绑定 668aac712909288977f8155778fb48cf5eb3d506df14e77a826ab368b9e38e51，当前工作树指纹为 0c853698deb1d73bd5a6e8535d5a17910072e6425ee4d061a5a2223920fa4ea5；attempt 记录 fold0 passed、fold1 failed，因此不能作为年度批次比较或新推理路径的端到端速度证据。下一次完整计时需用固定工作树源码指纹重新创建并评审两折请求。
- 2026-11-04 两次重跑均停在 fold1 dataset preparation：PID 62294 明确以 exit 137/SIGKILL 结束；PID 8253 后续从进程表消失但未取得退出码。fold0 stage 00–08 及最终 read-back passed；fold1 stage 00–01 passed，stage 02 没有持久化，也没有新的 walk-forward attempt。之前 rank 路径的 Pandas fragmentation warning 已通过连续输出矩阵修复。详见 `trading-grade-progress.md` 第四十九轮。
- 随后进一步降低 dataset-preparation 峰值：v6–v8 经 lineage digest 验证后直接用 sample artifact，不再重读完整 factor panel 或重复物化 universe members；标签对齐用浅拷贝复用宽特征块；按列验证 transform，已排序 frame 跳过重排；publish 后在 owning read-back 前释放临时宽 frame。单份 2,229,842×157 float64 panel 约 2.80 GB，此为可避免分配量级估算。普通全量测试 `1667 passed, 38 skipped`，Qlib 全量 `1704 passed, 1 skipped`。用新 request 和测试审阅密钥重跑后，两折 LightGBM walk-forward 已通过；完整计时为 12,616.52 秒，maximum RSS 11,598,905,344 bytes，peak memory footprint 33,738,961,864 bytes，swap 0。此前中断运行无同口径最终计时，因此不能声称峰值降低 28%。执行记录、正式质量限制及剩余线性配对比较见 `trading-grade-progress.md` 第五十一轮。
- 已为现有 Qlib export 添加严格复用途径：从当前帧内存内重算每个文件摘要和 generation，再验证质量决定、provider URI 与既有文件的完整内容；匹配时不再重写 staging。纸面结果 reconciliation 在单次回读中为四类 store 分别建立路径索引，并在结束时复查目录变动；每份制品仍完整核验。小规模合成基准、两套全量测试及尚未实测的全年耗时见 `trading-grade-progress.md` 第五十三轮。
- exporter read-back 合并后，新鲜链路对 `.bin` 的完整内容扫描按路径计数从六轮减为三轮，另有一轮结构/大小清点；trainer 训练前完整 SHA-256 与最终 reconciliation 严格回读仍保留。定向回归锁定同尺寸内容篡改拒绝。完整 Research Chain 受控计时仍待执行，需记录 dataset owning read、导出、发布校验、训练前校验、训练、持久化和最终 reconciliation；如果校验仍为瓶颈，再评估并行校验并保留 fail-closed 语义。
- 为大窗口实验提供更快的迭代能力。
- 验收目标：保持最终 Qlib 布局、manifest、train-only 边界和完整性门禁不变；报告静态/`.bin` 训练帧全量一致性、export 与模型训练加载耗时，并完成可复现的完整链路计时及内存峰值记录。

### 下一步 C：建立模型比较实验

- 运行 `scripts/compare_training_models.py` 比较线性基线与 LightGBM；入口拒绝训练数据或成本/组合配置不一致的结果。
- 使用至少一年真实样本外窗口，并报告 regime 覆盖、IR、超额收益及收益贡献分散性。
- 只在样本外、扣成本后稳定优于基线时替换。

## 6. 明确不做的事

- 不基于当前 11 个决策日的负收益直接否定全部因子。
- 不基于旧 1 决策日的 +1.66% 宣称策略有效。
- 不在评估协议未升级前堆叠复杂模型。
- 不把测试密钥签发的质量决策用于生产。
- 不把“链路跑通”当成“策略可用”。

## 7. 完成定义

满足以下全部条件，才算完成“真实且对交易有指导意义的训练”：

- 至少 1 年样本外，覆盖多种市场状态。
- 扣成本后相对基准有稳定超额。
- 风险、换手、容量在可接受范围。
- 结果可复现、可解释、可审计。
- 标签口径审计有书面结论：自相关已调整、标签与可执行路径的偏差已量化、eligibility 剔除的时点可知性已核验。
- 通过 paper execution。小资金实盘验证因 broker/live 禁止边界不属于当前授权范围，须经独立维护者决策后再纳入后续计划。
- 结论能明确指导下一轮调仓，而不是只给出一个收益率数字。

## 8. 剩余缺口与落地依赖复核（2026-11-05）

下面的顺序将程序缺口、数据依赖和实证验收一起纳入；完成某个入口或测试不等于其真实数据验收通过。测试密钥仍可用于隔离诊断；正式质量结论需要独立来源质量决定和生产评审材料。CSI-All 月度近似已获维护者接受，保留精度声明；分析师预期不是主比较前置，缺少可用历史源时其因子保持不可用，不能记成已验收。

| 缺口 | 落地位置及验收交付 |
| --- | --- |
| 多折年度比较 | 阶段 2–3：覆盖每折输入、配置和日期的可读回报告；分别声明独立折连乘诊断与真实共享账户回放，不能将前者冒充连续账户 |
| 公司行为 v4 的跨折回放和 paper 对账 | 阶段 1–3：在已批准的现金分红、送股和权利发行排除口径下补齐跨折、paper 状态与对账所需的版本化能力，覆盖事件、账户状态、失败和兼容回归；v3 旧行为保留 |
| 行业、风格和标的的收益贡献 | 阶段 3–4：从可核验成交、估值及事件重建贡献，发布带输入血缘的报告；现有持仓暴露不能替代收益贡献归因 |
| LightGBM 选择器与受治理模型链路 | 阶段 4–5：将验证集选择收据、候选预算和所选参数绑定到经评审模板和模型产物；测试集不参与选择，来源不符时拒绝 |
| 盈利质量、成长、杠杆及资金行为因子 | 阶段 4–6：先实现契约、公式和缺失/时点回归；数据可证明可见时点和覆盖后再发布并评估逐因子增量、共线性和成本。现有 8 个候选不代表完整基本面因子已完成。分析师预期单独保留可选状态 |
| 基准、压力、容量、paper 和组合风控 | 阶段 2–5：使用同一实际数据与经济假设重放基准、压力与模拟成交，核验风险预算和成交差异；既有入口的存在不等于真实窗口验收 |
| 历史行情、退市/停牌、ST、限价、行业、市值、流动性和公司行为事实 | 阶段 0–1、4：按日历逐项核对来源、历史可见时点、字段覆盖和缺失；不把测试占位值、当前快照或人工推导当作已审查事实 |
| 正式质量与性能 | 阶段 5–6：补独立质量决定、来源/锁/代码绑定和必要的 release 矩阵；正常研究运行中记录优化后阶段耗时与峰值，不为计时单独重训 |

### 执行顺序和前置条件

0. **冻结口径并审计已用结果。** 将 2025-08-07..2026-09-07 已看过的两折作为探索性结果；固定此前模型、目标、成本和数据来源，逐项标记诊断事实中的 `is_st=False`、缺前收填充 `1.0`、行业缺失及公司行为覆盖限制，不以其通过正式质量审查。未来若依据这些结果调整模型或规则，不再把同一窗口作为独立确认集；须预先记录选择规则和试验次数，并使用未参与调整的后续样本验证。
1. **先解决公司行为和全窗口数据兼容性。** 直接复用现有两折目标做 v3 连续回放会触发全窗口公司行为排除冲突：按现有诊断公司行为面板，线性第一折 106 个决策日/71 个标的、LightGBM 第一折 115 日/101 个标的命中；第二折均为 0。数据经 market store 和 target-weight store 读回，但这只是执行前交集检查，未执行连续回放。不得事后删权重、放宽拒绝或将不同 universe 的结果称作同一实验。先核验预测制品是否可复用、目标生成时的事后全窗口排除及可见性，再准备覆盖完整 OOS 加两日的日历、价格、执行事实、公司行为数据和同经济参数配置。当前诊断执行事实及权利发行覆盖不足必须显式留证。
2. **补 v4 连续账户及 paper 能力后，再构造可比较目标。** 扩展 v4 事件下的跨折资金、持仓、应收、送股与终止清仓；paper 状态与对账须表达相同事件及时间口径，权利发行按已批准排除规则处理并披露其时点限制。保持旧 v3 contract，新增语义使用版本化契约。先用测试覆盖跨折事件和不可成交清仓，再在隔离根使用测试密钥试跑；事件源或完整性不足时继续 fail-closed。复用已发布预测只在其样本、过滤和血缘经核验后进行，否则需重建请求；不保证可以零训练完成。
3. **形成报告和交易验证闭环。** 基于经检查的同成本、同 universe、同账户口径分别生成两模型连续回放、等权等基准及配对年度报告；核验报告血缘、期间、两日执行/清仓延伸和净值重算。补行业/风格/个股收益贡献，运行成本压力、资金网格、组合风险和 paper 对账；不合格或数据不足要留存失败/不可判定，而不是要求数值通过。连续回放与 paper 结果不能替代历史来源和冲击成本审计。
4. **补数据和实现剩余功能。** 并行核验可用 TDX/Tushare 历史范围、复权和执行事实、退市与公司行为/权利发行覆盖；重新运行 readiness 和标签审计。现有 OOS 尾后有 33 个行情日，其中 28 日存在非空五日标签，仅作延长诊断候选；不足以单独支持一年独立验收。实现/验收基本面和资金行为因子、中性化与归因、验证集模型选择向受治理模板/产物的绑定，记录多重试验预算；行业/市值来源不可证明时保留缺失。若扩展分数加权、风险平价或行业中性，须先满足对应风险收益及行业来源绑定。
5. **冻结新协议后做独立评价。** 预先确定候选、阈值、成本/基准、市场状态规则和试验预算；再使用未参与调参或策略选择的足够长样本外窗口执行并报告。已观察的两折只能用于研发与可重复性核验。模型替换须同时满足计划中的扣成本超额、IR、贡献分散和风控要求；两种模型都不达标时保留不通过结论。
6. **完成正式质量和交付。** 核验历史 PIT/退市资格、公司行为与成本来源、样本及评审绑定，保留月度 universe 精度说明；需要正式质量结论时配置独立评审材料。补全代码回归、普通/Qlib 全量测试与相关文档，发布或认证前运行完整 gate 和所需矩阵。模拟执行在本次范围内，小资金真实下单仍依既有架构边界单独决策。
