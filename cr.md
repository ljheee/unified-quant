# Unified Quant 全库逐层代码评审（cr.md）

- 评审日期：2026-09-15
- 评审范围：整个代码库（`src/uq/**`、`scripts/**`），按分层架构逐层进行：contracts → data ingest → factors → models → portfolio → backtest → risk → execution → research_chain → glue（pipeline/routing/quality/exporters/cli/scripts）。
- 方法：多角度查找（逐行、行为变更、跨文件追踪、语言陷阱、包装/代理、复用、简化、效率、抽象层次、约定）→ 逐条一票验证（CONFIRMED / PLAUSIBLE / REFUTED）→ 补漏扫描。静态阅读验证（沙箱内不可执行 python/pytest）。
- 结果：15 项正式发现（按严重度排序），另附次要观察与已证伪项。

---

## 一、正式发现（按严重度排序）

### 1.【高】标签层：先过滤后 shift，标签 horizon 语义被静默破坏
- 文件：`src/uq/models/labels.py:103`（过滤）→ `:106-108`（`shift(-horizon)`）
- 问题：`LabelBuilder.build` 先用 eligibility（停牌/涨跌停/上市不足 60 天/退市）过滤掉行，再在**过滤后**的帧上 `groupby(instrument).transform(values.shift(-5) / values - 1)`。`shift(-5)` 取的是"向后第 5 个**幸存行**"，而不是"向后第 5 个**交易日**"。
- 失败场景：某股票 D 日之后停牌 10 个交易日，D 日标签实际变成 `adjusted_close[D+15交易日]/adjusted_close[D]-1`，与 manifest 中 `formula_sha256` 声明的 `adjusted_close[D+5]/adjusted_close[D]-1` 及 `horizon_trading_days=5` 直接矛盾。不同股票停牌分布不同 → 标签时间网格逐股错位，训练/IC 被静默污染且 checksum 全部"合法"。

### 2.【高】研究链：多 decision_date 预测阶段必然发布冲突，execute 必失败
- 文件：`src/uq/research_chain/adapters.py:1202`（每个日期 publish 一次 state）+ `src/uq/research_chain/resolver.py:479-480`（immutable 路径已存在即 raise）
- 问题：`PredictionStageAdapter.run()` 每处理一个 decision_date 就 `publish_state(stage="prediction_publication")`，而 state 路径固定为 `stage=05/manifest.json`。第二个日期写入同一路径 → `ContractError("immutable research manifest already exists")`。对比：`PortfolioStageAdapter` 是聚合后只发一次（正确模式）。
- 失败场景：CLI `uq-research-run --mode execute`，`scores-json` 含 2 个及以上 decision_date（`cli.py:361` 会把所有 scores 日期传入）→ 第二个预测日期时整个 run 以 `stage_failed` 终止。多日期研究运行（文档明确支持的能力）100% 复现失败。

### 3.【高】风控发布门：allow 路径必然 AttributeError 崩溃
- 文件：`src/uq/risk/stores.py:99`（`payloads.items()`，payloads 为 None）+ `src/uq/risk/adapter.py:152`（`gate()` 调 `run_store.publish(run)` 不传 payloads）
- 问题：`RiskRunStore.publish(document, payloads=None)` 只在 `payloads is not None` 时做校验，但随后的 `for path, payload in payloads.items()` 对 None 直接抛 `AttributeError`。`risk_run` schema 不强制 `files` 字段，因此 `gate()` 构造的 run 能通过校验、然后崩在写文件循环。
- 失败场景：`PortfolioPublicationRiskGate.gate()` 对任何**非 block** 的决定（allow/warn/resize/de_risk…）在已发布 decision+event 之后抛 `AttributeError: 'NoneType' object has no attribute 'items'` —— 放行路径完全不可用，且留下半完成的证据链。现有测试只覆盖 block 路径（block 在到达该行之前已 raise），所以漏检。

### 4.【高】回测引擎：买卖合并单循环，现金排序错误导致买入被误跳过
- 文件：`src/uq/backtest/engine.py:183`（合并循环）、`:308`（卖出回款）、`:349-356`（买入现金检查）
- 问题：注释写 "Execute sells first"，实际是一个按股票代码字母序的**合并**循环，每只票各自走 sell 或 buy 分支。买入 `if total_cost > cash: skipped_insufficient_cash` 只能看到字母序更早的卖出回款。
- 失败场景：持仓 Z（拟卖出）+ 目标买入 A，现金不足。字母序 A 先处理 → A 被记 `skipped_insufficient_cash`；随后 Z 卖出回款到账却无处可买。组合轨迹与"先卖后买"语义系统性偏离（回测结果失真但确定性、可通过全部校验）。修复：卖出、买入分两趟循环。

### 5.【高】回测风控门：停牌股以 price=0.0 送入风控引擎，整个 gated 回测崩溃
- 文件：`src/uq/backtest/engine.py:201-221`（gate 调用在停牌短路之前）、`:210-211`（`execution_price=0.0`、`previous_close=NaN` 占位）+ `src/uq/risk/engine.py:347-350`（价格必须为正，否则 raise）
- 问题：风险门在 `is_suspended` 短路（`:247`）**之前**被调用；停牌时 open 为 NaN → `execution_price` 填 0.0（且 `float(open_price or 0.0)` 因 NaN 为真值而传入 NaN 的 previous_close）→ `RiskEngine.evaluate_order` 直接 `ContractError("execution price must be positive")`。
- 失败场景：`run_with_risk_gate` 包住的回测中，任何需要调仓的停牌股票 → 整个回测 run 抛 ContractError 中止，而不是记录 `skipped_suspended`。

### 6.【高】模型训练：特征 NaN 未过滤，静默产出 NaN 权重 artifact
- 文件：`src/uq/models/trainer.py:45-50`
- 问题：只用 `valid = ~np.isnan(y)` 过滤标签 NaN；特征矩阵 `X` 中的 NaN 完全不检查，`np.linalg.solve(X_v.T @ X_v + alpha*I, ...)` 要么传播出全 NaN 权重（被打包、签名、发布为"受治理" model artifact），要么直接 `LinAlgError`。
- 失败场景：数据集任一特征列含 NaN（预处理 min_group_observations 边界、merge 残留）→ 训练产出 NaN 权重且 checksum/治理链全部"合法"，推理阶段输出全 NaN 分数。

### 7.【中高】Tushare 适配器：空 instruments 退化为全市场抓取
- 文件：`src/uq/adapters/tushare_free.py:96-101`
- 问题：`batches = [...] or [[]]` 使空 instruments 产生一个空 batch；`ts_code=codes or None` 把空串变成 `None` → `client.daily(ts_code=None, start, end)` 在 Tushare API 语义下返回**全市场**该日期范围数据。
- 失败场景：任何空白名单/过滤后为空的请求 → 拉取全市场数千只股票数据进入下游 merge（量级错误、覆盖语义错误，且消耗 API 配额）。正确行为应为直接返回空 SUCCESS。

### 8.【中高】mootdx 适配器：长区间请求被静默截断到最近 800 根
- 文件：`src/uq/adapters/mootdx_source.py:105-117`（`_requested_bar_estimate` 恒返回 `page_size`）配合 `:145-176` 分页循环
- 问题：估算函数计算了天数却直接 `return self.page_size`（800），注释谎称"longer ranges remain covered by bounded pagination"。分页循环取满一页即停 → 超过 800 个交易日的请求只拿到最近 800 根，且状态为 SUCCESS。
- 失败场景：当前唯一生产调用方是日线 ingest（start==end，不受影响）；但任何人经 SourceRouter 发起多年回填 → 静默缺历史数据且报告成功。属于潜伏型数据完整性陷阱 + 误导性注释。

### 9.【中】契约层：模板 digest 不匹配时报错路径自身抛 KeyError
- 文件：`src/uq/contracts/model_layer.py:584`
- 问题：模板类 schema（model_definition_template 等）的 digest 字段是 `template_manifest_digest_sha256`，但 digest 不匹配时的错误 f-string 引用 `payload['manifest_digest_sha256']`（模板 payload 没有此键）→ 构造错误消息时抛 `KeyError`，替代应有的 `ContractError`。
- 失败场景：模板被篡改或 digest 算错时，调用方收到裸 KeyError，绕过所有 `except ContractError` 治理错误处理（研究链 CLI 会把它归类为 configuration_failure，丢失语义）。

### 10.【中】纸面执行存储：symlink 检查遍历未解析路径的全部祖先，macOS 上必然误伤
- 文件：`src/uq/execution/stores.py:61`（另 `:530` 同模式）
- 问题：`_safe_resolve` 先 `resolve()`，却对**未解析**的 `path` 及其**所有** `parents` 做 `is_symlink()` 检查。macOS 上 `/var → /private/var`、`/tmp → /private/tmp` 是符号链接；任何位于系统临时目录下的存储根（pytest `tmp_path`、默认 TMPDIR）其祖先链必含 `/var` → 读路径一律 `ContractError("symbolic links are forbidden in paper paths")`。
- 失败场景：在 macOS（本机及 macos-latest CI）上，任何 paper-execution store 的 publish→read 在 tmp 根下读取即失败；同仓库其它 store（如 owning_contracts.py 先 resolve 再检查）无此问题，说明是检查写法不一致而非有意策略。应只检查 root 内部的分量。

### 11.【中】复权快照：会话覆盖校验用自然日序列对比交易日序列
- 文件：`src/uq/contracts/adjustment_snapshot.py:156-163`
- 问题：`select_visible_generation` 用 `pd.date_range(start, end, freq="D")`（含周末/节假日的自然日）构造期望序列，与 `save()` 接受的**交易日** sessions CSV 比对，不一致即 raise。
- 失败场景：任何跨周末/假期的窗口（即一切真实窗口）调用 `select_visible_generation` 都抛 "invalid adjustment snapshot session coverage"。当前无生产调用方（测试仅用连续日期），属潜伏矛盾：接口在该实现下对真实数据不可用。

### 12.【中】指标计算：pooled IC 替代截面 IC 均值（PLAUSIBLE）
- 文件：`src/uq/models/definition.py:111,127-136`
- 问题：`MetricReport.compute` 把全部 (instrument, decision_date) 行合并成一个大样本算 Pearson/rank 相关（pooled），而非业界标准的"逐日截面 IC → 跨日取均值"。pooled 相关会被跨日标签水平漂移主导，不能刻画截面选股能力。
- 失败场景：标签整体随时间趋势上行时，即使每日截面排序完全随机，pooled IC 也可能显著为正 → 模型质量报告给出误导性结论。仓库内未找到锁定该语义的 spec，故标 PLAUSIBLE；若设计意图即 pooled，应在契约中显式声明。

### 13.【中】重复的 ContractError 类，抛出方与捕获方不匹配
- 文件：`src/uq/contracts/schema.py:18` 与 `src/uq/errors.py:1`；抛出方 `src/uq/contracts/config.py:8`；捕获方如 `src/uq/pipeline/daily.py:14,187`
- 问题：两个独立的 `ContractError` 类（都继承 ValueError）。`contracts/config.py` 抛 schema 版；`pipeline/daily.py` 等 catch 的是 errors 版 → schema 版异常绕过 `except ContractError` 的分类处理（靠外层 `except ValueError` 兜底，丢失 source_status 分类语义）。
- 失败场景：契约/配置加载失败时，ingest 报告不能按设计映射为对应 status；任何只 catch 其一的调用方都存在同类漏捕。应删除其中一个类并统一 import。

### 14.【低中】组合构建：max_turnover=0 且无前序权重时完全跳过换手约束
- 文件：`src/uq/portfolio/builder.py:122`
- 问题：`if max_turnover > 0 and previous_target_weights is None: raise` —— 边界值 `max_turnover == 0` 不要求 previous_target_weights，随后的换手检查也因 previous 为 None 而整体跳过 → 从零持仓直接建成满仓新组合，"零换手"约束形同虚设；而 `0.0001` 反而会 raise。边界不一致。
- 失败场景：定义 `max_turnover: 0`（意图锁定持仓）的 definition 在首期无 previous 时发布出全仓目标权重。是否应对"建仓期豁免"另作显式建模，至少边界行为需一致化。

### 15.【低中】数据集切分校验：purge/embargo 检查对输入顺序敏感，产生误报
- 文件：`src/uq/models/dataset.py:58-63`
- 问题：`ranges` 按输入列表顺序累积，每个 split 只与"已出现的" split 做 `start_idx <= prev_end + horizon + embargo_days` 检查。若输入不是按时间升序（如 validation 列在 train 前），较早 split 后处理时会以错误方向代入公式。
- 失败场景：合法但乱序的 split 列表（时间上 A 早于 B 且间隔充足）→ 处理 A 时检查 `A.start <= B.end + h + e` 恒为真 → 误报 purge/embargo violation。不会漏报真实违规，仅误报；调用方按时间顺序构造则不受影响。

---

## 二、次要观察（未进正式清单）

- `src/uq/factors/context.py:37-43`：`FactorContext.read_bars` 把 start/end 展开成每个自然日，且只 catch `FileNotFoundError`（ManifestFirstReader 对缺分区抛 ContractError）→ 任何跨非交易日的区间读取失败。无 src 内调用方（测试路径）。
- `src/uq/risk/engine.py:253`：`industry_weight` 指标取 `max(industry_mapping.values())`；类型标注是 `Mapping[str, str]`（instrument→行业名），若真传入字符串值则 `round(str, 12)` 抛 TypeError；若语义是"行业→权重"，则无校验。src 内无生产方。
- `src/uq/research_chain/adapters.py:943`：Qlib cache 越界证据检查用 `any(...)`，混有界外文件时仍通过（应为 `all`）；且 cache 文件本就由 `rglob(cache_root)` 收集，检查近于 vacuous。
- `src/uq/research_chain/runner.py:304`：`research_stage_plan_v3_sha256` 未 import（ latent NameError），但 v3 在 `:291` 已被拒绝，分支不可达。
- `src/uq/models/accepted_store.py:89-96`：分页 cursor 用排序元组 `>` 比较；排序键不含唯一字段时，并列项跨页被跳过。
- `src/uq/adapters/demo.py:41`：未知字段先在 `selected[fields]` 抛 KeyError，`PARTIAL_FIELDS` 状态不可达（测试适配器）。
- `src/uq/risk/adapter.py:296`：run files 的 `byte_size` 用 `len(str)+1` 而非 UTF-8 字节数；payload 含非 ASCII 时 readback 校验失败。
- `src/uq/factors/adjusted_price.py:72`：`assert_not_raw_close` 参考序列未按 instrument 分组，多 instrument 帧触发 broadcast ValueError（测试辅助）。
- `src/uq/market/lifecycle.py`：`suspension_window` 异常时返回 {}（fail-open）；在 pipeline 中被兜成 fail-closed（未解释缺失→reject），可接受但值得显式记录。
- `src/uq/execution/engine.py:271-276`：reject reason 先置 `quantity` 后被后续非 elif 的 suspended 覆盖，原因优先级语义模糊（装饰性）。
- `scripts/cleanup_retention.py:33-34`：对 staging **文件**用 `shutil.rmtree(ignore_errors=True)`，错误被吞、文件型 staging 永远清不掉。
- 效率：`FactorStore.read_manifest`/`FileResearchRunStore.read_published_document`/owning stores 的 `rglob` 全树扫描（每次读 O(全库)）；`models/qlib_export.py` 逐行 read-modify-write `.bin`（O(n²) I/O）；`execution/planner.py` 循环内重复计算 decision_nav（O(n²)）；`portfolio/builder.py` publish 中 `_serialize` 调用两次。
- 抽象层次：`contracts/model_layer.py` 模块 import 时实例化 Registry/TrustAnchor（import 期文件 I/O）；`runner.py` 依赖 `run_store.last_plan`、各 adapter 的 `last_result` 隐式状态传参。

## 三、验证后被证伪（REFUTED）的候选

- **回测 T+1 锁跨日残留**（原疑 `t1_locked.clear()` 只在调仓日执行）：证伪。执行恒为决策日次日，单循环内同一股票不会同日买卖；当日买入次日即可卖，语义正确，`t1_locked` 实为冗余但无害。
- **paper 质量报告绑定代际不匹配**（疑 publish 重算 generation 后 read 必败）：证伪。`paper_execution_identities` 的 generation 明确排除 `quality_report_checksum_sha256`，前后一致。
- **回测最后决策日 break 丢失末日净值**：证伪。equity 行在 break 前已追加，末日决策无法执行属正确语义。
- **质量门 max_abs_diff_pct / max_rel_diff_pct 双键语义分歧**：两分支数学等价（均为相对主源百分比），属命名冗余而非错误。

## 四、重点复核后确认健壮的路径

- `store/pit_store.py`（flock + 双检查的幂等发布）、`market/adjustment.py`（后复权因子方向与循环正确）、`models/dataset_writer.py`（分面校验完整）、`portfolio/builder.py` 的 `TargetWeightStore.read`（fail-closed 校验链完整）、`risk/contracts.py` 的签名验证与治理链、`research_chain/contracts.py` 的路径/layout 校验与 stage-plan 签名验证、`execution/state.py` 的 T+1 卖出校验与现金对账、`quality/gate.py` 主辅源对齐合并逻辑。

---

## 五、核查结论（2026-09-15 由第二评审组追加）

> 本节回答：cr.md 哪些是真问题？方法：对 15 条正式发现逐条复核（2 条运行时复现、1 条运行时类身份检查、12 条静态精读），并与两组独立评审交叉比对——本会话 9 个分层复审 agent（cr-*）与平行会话 13 个查找 agent（find-*），共产出约 100 条原始发现。cr.md 的每条正式发现均至少被一个独立 agent 复达。

### 5.1 15 条正式发现逐条判定

| # | 判定 | 复核证据 |
|---|---|---|
| 1 | ✅ 真问题（高） | 运行时复现：10 行面板、D1–D4 停牌，2026-01-05 标签实测 **0.5**（取第 5 个幸存行 01-16，close=15），而 manifest 声明公式应为 **0.1**（第 5 个交易日 01-12，close=11）。cr-models 独立复达（labels.py:106）。附带击穿 SplitValidator purge 不变式（拉伸窗口可伸入验证期）。 |
| 2 | ✅ 真问题（高） | cr-research-chain 独立复达（runner.py:166）并有运行时复现：第二个 decision_date 在 resolver.py:479-480 撞 immutable 路径，整个 run 必失败。另发现同源潜伏问题 runner.py:623（见 5.4-M 清单）。 |
| 3 | ✅ 真问题（高） | cr-risk 运行时复现：gate() 对非阻断 decision 调 `publish(run)` 不传 payloads → `AttributeError: 'NoneType' object has no attribute 'items'`。放行路径 100% 崩溃。 |
| 4 | ✅ 真问题（高） | cr-portfolio-backtest 独立复达（engine.py:183）：满仓轮换 Z→A 时约半数日度轮换误执行，equity/turnover 系统性失真。 |
| 5 | ✅ 真问题（高） | 静态复核确认：risk_gate 在 is_suspended 短路（:247）之前以 `execution_price=0.0`（:210）调用，risk/engine.py:347-350 必 raise，gated 回测遇停牌股整 run 中止。 |
| 6 | ✅ 真问题（高） | cr-models 独立复达（trainer.py:45）：X 的 NaN 直进 `np.linalg.solve`，产出 NaN 权重且治理链全部"合法"。另：train() 从不感知 split（见 5.4-C2）。 |
| 7 | ✅ 真问题（定级下调至低） | cr-ingest 复达：空 instruments → `ts_code=None` → 全市场拉取按 SUCCESS 返回；当前 gate 端 reindex 丢弃多余行，落库污染暂未发生，但请求量级与语义均非预期、消耗 API 配额。 |
| 8 | ✅ 真问题（中高，潜伏） | cr-ingest 复达（mootdx_source.py:105-117）：估算函数算完天数仍 `return page_size`；另 EMPTY 误标 retryable=True（重试永不可能成功）。当前日线 ingest start==end 不受影响，任何长区间回填静默缺历史。 |
| 9 | ✅ 真问题（中） | cr-contracts 复达：模板 payload 无 `manifest_digest_sha256` 键，错误路径抛 KeyError 逃逸所有 `except ContractError` 分类。 |
| 10 | ✅ 真问题（中） | 静态复核确认：:61 对**未解析**路径做祖先 symlink 检查；macOS `/var→/private/var` 致 tmp 根下读路径全灭；:87/:243/:326/:420 四个读调用点均受影响。 |
| 11 | ✅ 真问题（中，潜伏） | cr-contracts 复达：save() 只校验端点（:60-62），select 用自然日 `date_range`（:156）比对交易日序列，任何跨周末窗口永不可选。 |
| 12 | ⚠️ 存疑（维持 PLAUSIBLE） | 代码事实确认（definition.py:104-136 全样本合并后单次 `s.corr(y)`，:134）；但仓库无 spec 锁定截面 IC 语义，无法判定是否违背设计意图。建议契约显式声明口径。 |
| 13 | ✅ 真问题（中） | 运行时检查：`uq.contracts.schema.ContractError is uq.errors.ContractError` → **False**；config.py:8 抛 schema 版、pipeline/daily.py:14,187 捕 errors 版，schema 版异常绕过分类处理。 |
| 14 | ✅ 真问题（且比原判更重） | 边界两侧都错：`==0` 跳过检查（原判成立）；`>0` 且 previous=None 必 raise——真实调用方 adapters.py:614-617 首期恰传 None，任何 max_turnover>0 的 definition 在首个调仓日 ContractError，研究链无法完成第一天（cr-portfolio-backtest 复达）。 |
| 15 | ✅ 真问题（低，仅误报方向） | cr-models 复达（dataset.py:59）：乱序但合法的 split 列表被误拒；不会漏报真实违规，调用方按时间序构造则不受影响。 |

**小计：14 条真问题 + 1 条存疑，0 条证伪。** 定级建议调整 2 条（#7 降至低；#14 升至中——首期必崩比边界豁免更严重）。

### 5.2 次要观察抽查

对第二节次要观察抽样复核：8 条被独立 agent 复达确认（context.py:37、adapters.py:943、accepted_store.py:94、demo.py:41、adjusted_price.py:72、execution/engine.py:271-276 等）。2 条应升级：risk/engine.py:253（fail-open 与 TypeError 均已运行时复现，升入 5.4-H3）、market/lifecycle.py（原判"pipeline 兜底可接受"——实际兜底方向判反，升入 5.4-H1）。runner.py:304、risk/adapter.py:296、cleanup_retention.py:33 未获独立复达，维持观察级。

### 5.3 「确认健壮」清单更正

第四节 8 项中 **4 项被推翻、1 项保留意见**：

- ❌ **market/adjustment.py**（原判"后复权因子方向与循环正确"）——推翻。运行时复现：10派5、pre_close=11.0 登记于除息日 → factors=[1.0476, 1.0]；closes=[11.0, 10.5] 经 `calculate_adjusted_factors` 得 return_1d=**−8.88%**，真实全收益为 0%（cr-ingest 以 pre_close=9.31/分红0.42 独立复现 −8.8%）。因子挂在除息日错误一侧（:51-56），且下游 adjusted_price.py:54 / labels.py:105 以**乘法**消费**除法基准**因子。每次分红送转都向 return_1d/5d/20d 与模型标签注入 phantom crash。现有测试（test_adjustment_snapshot_runtime.py:64-75）只比对相邻因子比值，反而锁死了错误时序。
- ❌ **models/dataset_writer.py**（原判"分面校验完整"）——推翻。(a) :58 `timestamp.strftime` 在 qlib_adapter 产出的字符串日期分区上必崩（见 5.4-H9）；(b) SplitValidator 以 `purge_trading_days` 充当 horizon 调用，从不校验 purge ≥ label horizon（5），拉伸标签可穿透 purge 间隙；(c) write() 不做 row_count 对账而 read() :298 强制 `len(frame)==row_count`——读写契约不对称。
- ❌ **risk/contracts.py**（原判"签名验证与治理链"）——推翻。`verify_risk_review_decision`（:181-210）只验 review_type/subject/签名，从不要求 `review_status=="approved"`（:198 仅在 approved 时附带查 errors）。运行时复现：签名合法的 **rejected** review 通过 validate_risk_policy_governance，engine 产出 enforceable decision。policy/exception/de-risk 三类治理全线 fail-open。对比 research_chain/contracts.py:256-257 强制 approved——同仓库两种治理标准。
- ❌ **quality/gate.py**（原判"主辅源对齐合并逻辑"）——推翻。四处缺陷（cr-infra 复达）：:112-113 辅源缺列静默 `continue`，该字段永不校验；:118 主源为 0 时 rel-diff=NaN 永不冲突（volume 0 vs 4,000,000 也"交叉验证通过"）；零重叠时 compared_rows=0 仍 accepted，minimum_coverage 只进 lineage 标签不作拒绝条件；辅源键重复时 reindex 抛 ValueError 逃逸 except 分类。
- ⚠️ **execution/state.py**——未发现直接反例，但同层 planner/engine 费用语义不一致（planner.py:161 对零数量订单按最低佣金计费 vs engine.py:313 fee=0，运行时复现），"现金对账健壮"的结论不能外推到 plan 层。
- ✅ 其余 3 项（pit_store.py、TargetWeightStore.read、research_chain/contracts.py）复核无异议。注意 TargetWeightStore.read 健壮不等于 builder.build 健壮（见 #14）。

### 5.4 漏报清单（cr.md 未发现的问题，按严重度）

**Critical（系统性污染研究结果或击穿治理）：**

| ID | 位置 | 问题 | 证据 |
|---|---|---|---|
| C1 | market/adjustment.py:123 × factors/adjusted_price.py:54 × models/labels.py:105 | 复权体系方向倒挂：deriver 产**除法基准**因子（除息日前 >1），两处消费方**乘法**使用；叠加挂接日期错侧 | 运行时复现 return_1d=−8.88%（真实 0%）；cr-ingest 独立复现 −8.8% |
| C2 | models/qlib_runtime.py:137（trainer.py:45 同类） | 训练从不读 split_policy：`segments={'train': (calendar[0], calendar[-1])}` 在全导出日历（train+valid+test）上拟合，purge/embargo 形同虚设；export 阶段（adapters.py:918-923）全量带标签发出；src 全库无 split 的训练期消费方 | cr-models 静态确认；numpy baseline 同样在全量帧上拟合 |
| C3 | risk/contracts.py:198 | 见 5.3：rejected review 被当批准，policy/exception/de-risk 三类治理全线 fail-open | cr-risk 运行时复现 |

**High：**

| ID | 位置 | 问题（一句话） |
|---|---|---|
| H1 | market/lifecycle.py:41-48 | 用**当前**上市/退市名单解释**历史**缺行：退市股其退市前缺口误判 not_listed_expected_missing，带缺口的 partition 以 verified 身份落库，verified_only 闸门失效（cr-ingest） |
| H2 | quality/gate.py:112-151 | 见 5.3：gate 接受其从未校验的数据（规则静默跳过/零重叠接受/最小覆盖率不强制/空主源接受）（cr-infra） |
| H3 | risk/engine.py:253 | industry_weight 取未验证调用方入参 `max(values)`：实测实际 60% 单行业 vs 阈值 0.25，传 `{"tech":0.10}` → allow；按注解 Mapping[str,str] 传则 TypeError（均运行时复现） |
| H4 | execution/planner.py:161 | 现金模拟对 requested_quantity==0 的订单照收最低佣金 5 元：plan 数量被虚假压缩；reject 策略 + 现金<5 时一个零数量订单流产整个 run（运行时复现） |
| H5 | research_chain/resolver.py:596 | label_set_v1/backtest_config_v1 质量决定只做 envelope 校验、execute 期从不取回核验 → 伪造/被拒决定可通过，governed-binding 被绕过 |
| H6 | research_chain/cli.py:341 | --decision-provider-json 模式必崩：resolver 对 callable 做 `dict()` → TypeError；spec 要求的生产决策提供方边界不可用 |
| H7 | factors/adjusted_price.py:58 | `pct_change` 默认 `fill_method='pad'` 跨缺失 bar 伪造收益：实测 [10,NaN,11]→[NaN,0.0,0.1]，违反 factor-layer-spec §8.3（运行时复现） |
| H8 | risk/state.py:317-356 | prior_payload 仅"非 None"即被消费，完整性检查只在 prior_state 非 None 时做 → 伪造 cooldown state 可抑制 loss-control（静态确认） |
| H9 | factors/qlib_adapter.py:83 | worker 输出 datetime 列 strftime 成字符串 → 分区日期列 object dtype；下游 dataset_writer.py:58（`.strftime`）与 adapters.py:918（`.dt.strftime`）必崩 |
| H10 | factors/store.py:248（:520 同） | FactorRegistry 从**当前源码树**重建（`Path(__file__).parents[3]`）而非注入 registry：发布期校验与计算期可见的 registry 可不一致，治理旁路 |
| H11 | factors/raw_price.py:50 | logical_fingerprint 对 NaN `fillna(-0.0)`：NaN 与 -0.0 的 sha256 相同，两种语义不同的帧指纹碰撞（运行时复现） |
| H12 | contracts/artifacts.py:190 | universe_id 未约束字符集即拼路径 → 路径穿越可读出任意目录 manifest（运行时复现） |
| H13 | contracts/model_layer.py:217 | initial 模式下 provenance 校验被跳过，首个 artifact 可无血缘发布 |

**Medium（摘要，均经独立复核）：**

- backtest/engine.py:308 卖出回款未扣滑点（现金高估；买入却按含滑点 net_price 扣款，两侧不对称）
- backtest/engine.py:264 涨跌停基准 prev_close 首执行日退化为当日 open、非日频调度滞后一日
- backtest/engine.py:334 买单超量能上限被裁剪记为 filled（spec 要求 skip），卖单正确 skip，两侧不对称
- backtest/engine.py:249 停牌买单以 side=sell、target=0 记账，fills 台账说谎
- risk/engine.py:28 _ACTION_RANK 与 spec §7.3A 相反（block 压过 halt_strategy/flatten/de_risk）
- risk/adapter.py:103 发布门禁阻断集合缺 de_risk/flatten（修好 #3 后即成 fail-open）
- risk/state.py:390 cooldown 用日历日而非交易日（周五触发周一即可重新进入）
- research_chain/runner.py:623 多日期绑定时旧 prediction_set 被逐出，portfolio/backtest 静默只覆盖最后一日（被 #2 崩溃掩盖的潜伏 fail-open，需与 #2 同修）
- research_chain/runner.py:813 失败阶段归因错误（部分进度后失败记到下一未运行阶段）
- research_chain/cli.py:319 provider_config_ref 从不对照信任根校验
- execution/engine.py:275 行情行整体缺失误标 suspended，no_market_data 分支成死代码
- execution/planner.py:124 execution_open_replay 基准下 target_quantity 仍除 close 而非 order_price（spec §5.4）
- execution/planner.py:132 sellable 截断发生在板手取整后，产出非整手卖单
- factors/store.py:148 logical_fingerprint 行序依赖 vs 落盘排序，写读两侧 canonicalization 不一致
- factors/engine.py:220 engine 产出 N 个重名 null_rate 检查，publish 端 _reports_agree 恒拒
- factors/engine.py:31 WindowSelector.as_dates 塌缩为两端点，执行计划丢中间会话
- contracts/model_layer.py:424 schema enum 允许的 'running' 状态被校验器拒绝
- routing/router.py:128-146 coverage=complete 是超集判定而非 股票×日期 笛卡尔积，交错缺口放行
- adapters/mootdx_source.py:199 PARTIAL_ROWS 按页帧数而非股票数判定，整票缺失可被掩盖成 SUCCESS
- models/qlib_export.py:336 init receipt 在 identity 字段定稿前落盘，盘上凭证永久为 "0"*64 占位
- models/predictions.py:108 分数行 datetime 从不与 decision_date/visible_cutoff 对账
- scripts/python-for-gate.sh:4-9 拒绝 3.13+（与 requires-python>=3.11 及已认证 3.13 CI 矛盾）
- scripts/nightly_regression.sh:6 解释器硬编码到隔壁仓库 `../one-stop-quant/.venv`
- src/uq/cli.py:15-30 exit 2 同时承载 argparse 用法错误与 expected_empty，且 expected_empty 永不被发出

**Low（摘选）：** adjustment.py:70 对签名声明的 list[date] 输入崩溃（`session.date()`）；execution/planner.py:141 碎股卖出整 run 流产 vs engine 订单级拒绝不一致；execution/stores.py:239 OrderPlanStore.read 缺 row_count/列/dtype 校验（兄弟 store 均有）；execution/engine.py:379 dust 权重误判缺 plan 流产；risk/adapter.py:93 同一 policy 二次 gate 必抛 partition exists；risk/engine.py:272 `_resize_value` 死代码（两分支同值、零调用点）；risk/engine.py:587 resize 约束未按 spec 归一化；factors/qlib_adapter.py:84 instrument 大小写单侧归一化；factors/store.py:188 staging 残留致永久 ambiguous generation；factors/context.py:37 区间读取跨周末必失败；contracts/schema.py:115 invariant-ref 检查死代码、:51 enum 仅对字符串 dtype 生效、artifacts.py:148 resolve-then-check 死代码；scripts/run_gate.sh 不 cd 到 ROOT（错仓库 `git rev-parse` 污染 gate 证据）；scripts/cleanup_retention.py 负数天数未校验（默认删除模式可一键清空 raw/quarantine）；routing/router.py:83 去重守卫 str vs 对象列表恒假；backtest/engine.py:144 day_fills 随天数×成交数平方增长。

### 5.5 总结

- cr.md 15 条正式发现：**14 条为真问题（其中 2 条运行时复现、1 条运行时类身份检查），1 条存疑（#12），0 条误报**。作为首轮静态评审查准可靠；但 #7 定级偏高、#14 定级偏低。
- 「确认健壮」清单不可信：8 项中 4 项被对抗复核推翻（adjustment、dataset_writer、risk/contracts、quality/gate），且推翻项包含全库最严重缺陷（C1/C3）——"已复核健壮"的标签本身构成风险，建议废弃该清单，或改为"未发现反例 + 复核深度"的克制标注。
- 漏报规模：去重后 **50+ 条独立问题**，其中 3 条 critical、13 条 high（5.4 全列）。两条 critical（C1 复权倒挂、C2 全日历训练泄漏）系统性污染所有收益序列与模型评估，危害超过原 15 条之和。建议修复优先级：**C1 ≈ C2 > C3 > H1–H13 > 原 15 条**。
- 评审覆盖：本节合并 22 个独立评审 agent 的约 100 条原始发现（去重后约 60 条）；high 及以上均有运行时复现或双人独立静态确认。

## 六、修复记录（2026-09-16，Critical + High 共 16 项）

修复范围经确认：**C1–C3 + H1–H13 全部 16 项**；方式为逐批修复并跑测试——修正锁定错误行为的既有测试、为每项新增回归测试、每批跑相关 pytest。全部变更保留在工作区，**未提交 git**。

### 6.1 逐项修复与回归测试

| ID | 修复要点 | 主要变更文件 | 回归测试 |
|---|---|---|---|
| C1 | deriver 改为乘法基准复权因子并挂接正确日期侧，快照语义升级 v2（schema/fixture 同步），消除两处消费方乘法/除法倒挂 | market/adjustment.py、contracts/adjustment_snapshot.py、factors/adjusted_price.py、adjustment_snapshot.v1.json、formula-golden.json | test_adjustment_snapshot_runtime.py、test_adjustment_provider.py |
| C2 | trainer/qlib_runtime 强制显式 train_period（缺省即拒），按 split_policy 窗口拟合，杜绝全导出日历训练泄漏 | models/trainer.py、models/qlib_runtime.py | test_model_trainer.py、test_model_end_to_end.py、test_model_predictions.py；既有调用点补 train_period（phase4、model_store_isolation） |
| C3 | risk_decision 强制 review_status==approved，rejected 不再被当批准 | risk/contracts.py | test_risk_control_plane_phase0.py |
| H1 | lifecycle 以上市/退市窗口判定历史缺行，退市前缺口不再误判 not_listed_expected_missing | market/lifecycle.py | test_real_data_policy.py::test_gap_before_delist_date_is_not_excused_as_unlisted |
| H2 | gate 四处 fail-open 全修：主源/参照源重复键拒绝、空主源拒绝、comparator 缺失/零重叠报错、最小覆盖率强制、零基期相对差按冲突计 | quality/gate.py | test_cr_fixes.py（4 项新增）；test_contract_validation.py 补 tushare 行 close/volume |
| H3 | industry_weight 改由目标权重经 instrument→industry 映射实测聚合（不再取调用方入参 max(values)）；存在 industry_weight 规则时正权重标的缺行业归属即 fail-closed | risk/engine.py | test_risk_control_plane_phase1.py |
| H4 | 零数量订单免收最低佣金（卖出现金贷记与买入费用两侧对齐 engine） | execution/planner.py | test_paper_execution_phase1.py::test_zero_quantity_rows_do_not_incur_minimum_commission |
| H5 | label_set_v1/backtest_config_v1 质量决定在 execute 期取回并 bind 核验（签名/binding_type/subject/status），伪造或被拒决定 fail-closed | research_chain/adapters.py、runner.py | test_research_chain_phase2.py（label）、phase4.py（config）、phase1/3/5 同步 |
| H6 | _MappingDecisionProvider 支持 callable 按 subject 求值，非 Mapping 返回 fail-closed，resolve 半段不再必崩；「provider-json 模式不再必崩」按端到端口径当时不成立（崩点后移至 execute 首站 subject-less 决策取回），execute 半段由 U 批 U14 补齐（见 §7.3） | research_chain/cli.py | test_research_chain_phase1.py::test_mapping_decision_provider_invokes_callable_per_subject（resolve 半段）；execute 半段覆盖见 §7.3 U14 行 |
| H7 | pct_change 显式 fill_method=None，NaN close 在绑定校验即拒，不再跨缺失 bar 伪造收益 | factors/adjusted_price.py | test_adjusted_price_engine.py::test_nan_close_is_rejected_before_return_calculation |
| H8 | prior_state 与 prior_payload 必须成对提供（单独携带 payload 即拒），payload 完整性校验前置，伪造 cooldown state 不再抑制 loss-control | risk/state.py | test_risk_control_plane_phase3.py |
| H9 | qlib worker 输出保留 datetime64，分区标签与结果对账统一按规范化日期字符串，下游 .dt 消费不再崩 | factors/qlib_adapter.py | test_qlib_adapter.py（dtype 保持 + 对账接受 datetime64） |
| H10 | factor_generation/read_partition 支持注入 FactorRegistry 并贯穿 publish 参数，不再从当前源码树重建，发布期与计算期 registry 一致 | factors/store.py | test_factor_store.py::test_store_honors_injected_registry_at_generation_and_read |
| H11 | logical_fingerprint 移除 fillna(-0.0)，独立哈希前置 null_mask：NaN / -0.0 / ±inf 三者指纹互异且确定性保持 | factors/raw_price.py | test_raw_price_engine.py::test_logical_fingerprint_distinguishes_nan_from_zero_and_infinity |
| H12 | universe_id/generation_id 白名单（`[a-z][a-z0-9_-]{1,63}` / 64 位 hex）前置校验，save/read/read_manifest 三处路径拼接不再可穿越 | contracts/artifacts.py | test_factor_governance.py::test_universe_path_segments_are_whitelisted |
| H13 | initial 模式无条件要求 initial_state_provenance_sha256，且禁止携带 input_state_binding（此前携带 binding 即跳过血缘校验） | contracts/model_layer.py | test_paper_execution_phase1.py::test_initial_transition_cannot_waive_provenance_with_input_binding |

### 6.2 修复过程中同步处理的测试基建

- `config/schemas/fixtures/raw-price-golden.json`：H11 修正指纹算法后，旧金向量锁定的是含碰撞的指纹——按修正后算法重算并原地单行替换（未重排文件格式）。
- `tests/test_model_store_isolation.py`：C2 遗漏调用点，补 `train_period`（同 phase4 处理）。
- `evidence/paper-execution-research-integration/phase-0/evidence-index.json`：按仓库既有惯例（源码变更随附 index 重钉，见 e4d7e45/56d9750）将 model_layer.py、runner.py 两条记录重钉为当前字节 sha256；phase-record 的 remote_gate 绑定不变。

### 6.3 验证结论

- 每批相关套件绿：cr_fixes 13；quality 邻接 72；paper execution 82；research chain phase1–5 全绿；factors 及下游消费套件全绿。
- 最终全量：**592 passed, 22 skipped**（skip 均为 qlib 门禁用例，本地无 pyqlib）。
- 未动 Medium/Low 清单（5.4 余下约 34 条）与原 15 条正式发现，留待下批。

## 七、对抗评审修复记录（2026-10-10，U 批次）

2026-10-10 对 35c4e1b 的四路对抗评审（报告：docs/adversarial/2026-10-10-cr35c4e1b-regression-review/REPORT.md，U 编号）确认 **1 BLOCKER + 8 MAJOR + 8 MINOR** 及若干观察项，本批全部修复。本节登记归因勘误、兼容性切断与逐项修复清单。

### 7.1 6.1 表归因勘误（U23）

- **C1 行**：回归测试列原引 test_adjusted_price_engine.py——35c4e1b 在该文件仅新增 H7 之锁（test_nan_close_is_rejected_before_return_calculation）；C1 真锁为 test_adjustment_snapshot_runtime.py 与 test_adjustment_provider.py。行已订正，变更文件列同步补上消费侧 factors/adjusted_price.py。
- **C3 行**：原引 test_risk_control_plane_phase0/1/3.py——phase1/phase3 之锁分属 H3/H8；C3 真锁在 phase0（test_validly_signed_rejected_review_is_not_an_approval、test_approved_policy_binds_verified_external_review_decision 等）。行已订正。
- **H1 行**：原引 test_cr_fixes.py——该文件 91 行新增均属 H2，对 H1 零覆盖；真锁在 test_real_data_policy.py::test_gap_before_delist_date_is_not_excused_as_unlisted。行已订正。

### 7.2 兼容性切断声明

本批含以下不向后兼容变更，程序化调用方需同步适配：

1. **质量决定签名覆盖 subject（U9）**：`create_reviewed_quality_decision` 新增必填 `subject_generation_id`（content≠generation 的 flow 另需 `subject_content_sha256`）；缺 subject 的 11 字段旧形态决定在 bind 处一律 fail closed。
2. **label/backtest 决定必填（U8，H5 影响面登记）**：`DatasetStageAdapter.run` / `BacktestStageAdapter.run` 新增必填 quality decision 参数（label_set_v1/backtest_config_v1 两族）；绕过 CLI 的程序化 execute 调用方若持最小 decision 映射（缺此两族）将在执行期被拒。
3. **provider 决策通道协议（U14/U15）**：runner 仅对 subject 可知的 flow eager 解析——输入绑定族（factor_partition/label_set/feature_preprocessing，content=generation）与 runner 合成的 portfolio_definition（content=manifest_digest，一参 provider 退化为 provider(g)、raw 形态 fail closed、信封形态可用）；stage 自有 subject 的 flow（model_dataset/model_run/prediction_set/backtest_config/backtest_result/qlib export/receipt/target_weights）原样透传，由 stage flow 以 (generation, content) 两参解析——其中 model_run/prediction_set/backtest_* 的 content==generation（一参 provider 可用），真正 content≠generation 的只有 qlib export/receipt 与 target_weights（一参 legacy provider 仅在这两族 fail closed）。静态决策可为签名裸决策或 quality_decision.v1 信封（含 owning_report）。
4. **行业规则须绑 membership artifact（U10）**：policy 启用 industry_weight 规则时，`evaluate_portfolio`/gate 必须提供 policy `industry_membership_binding` 绑定的 industry_membership_v1 artifact；裸 industry_mapping 降为信息参考，与绑定 artifact 冲突即拒。
5. **initial_state provenance 内容承诺（U13）**：execution_config initial 模式的 `initial_state.provenance_sha256` 必须是 {cash, holdings, valuation_price_basis} 的 canonical sha256 承诺（`initial_state_provenance_sha256`），自撰 64hex 在校验期即拒；paper-execution fixture 与 identity golden vectors 已重钉。
6. **universe_id schema 收紧（U4）**：universe_snapshot.v1 的 universe_id 收紧为 `^[a-z][a-z0-9_-]{1,63}$`，与 H12 运行时白名单一致；此前 schema 期可过、运行期才拒的 id 现在 schema 期即拒。

### 7.3 逐项修复与回归测试

除注明外，新增回归锁均在 tests/test_adv_review_fixes.py。

| ID | 修复要点 | 回归测试 |
|---|---|---|
| U1（BLOCKER） | 修复前文档默认 `uq-ingest daily`（不带 --cross-validate）因强制参照源比对而每次必拒；修复后默认路径免比对可发布，显式请求比对仍 fail closed | test_default_tdx_pipeline_publishes_without_comparator、test_requested_cross_validation_stays_fail_closed |
| U2+U18 | 指纹算法版本化（v1 fillna / v2 null_mask 前置）+ 读取路径全版本兼容；±inf 指纹互异 | test_fingerprint_distinguishes_signed_infinities、test_fingerprint_read_path_accepts_all_known_versions |
| U3+U6+U17 | 复权快照 schema v1/v2 并存（v1 可读不可消费，未知版本 fail closed）；_LINEAGE_VERSION 单一来源成活 | test_snapshot_store_writes_v2_and_reader_roundtrips、test_legacy_v1_snapshot_stays_readable_but_is_not_consumable、test_unknown_snapshot_version_is_rejected、test_v1_snapshot_reexports_through_current_deriver、test_lineage_constant_is_single_sourced |
| U4 | universe_id schema pattern 与 H12 运行时白名单对齐 | test_universe_id_schema_pattern_matches_runtime_whitelist；contract-negative-cases 补三例 |
| U5 | lifecycle 已退市股票上市前缺口不再误判 unknown_requires_review | test_real_data_policy.py::test_pre_listing_gap_of_delisted_symbol_is_expected_missing（+窗口内/未知上市日两 companion） |
| U9 | 质量决定签名覆盖 binding_type+subject_generation_id+subject_content_sha256，跨 subject 重放与 11 字段旧形态均 fail closed | test_quality_decision_rejects_cross_subject_replay、test_subject_less_legacy_decision_fails_closed_at_bind、test_bound_report_signature_verification_covers_subject |
| U10 | industry_weight 规则激活时 taxonomy 必须来自 policy 绑定的 industry_membership_v1 artifact，裸 industry_mapping 仅信息参考 | test_risk_industry_rule_requires_policy_bound_artifact；test_risk_control_plane_phase1.py 同步 |
| U11+U12 | gate 零基期 ±inf 不再豁免（inf vs inf 保持干净）；minimum_coverage 入契约且生产可达 | test_gate_nonfinite_primary_is_a_conflict_both_branches、test_gate_equal_infinities_stay_clean、test_contract_minimum_coverage_parsed_and_validated、test_default_contract_minimum_coverage_is_zero |
| U13 | initial_state.provenance_sha256 升级为内容承诺，校验期重算比对 | test_execution_config_initial_state_provenance_commits_to_content；paper-execution fixture 与 golden vectors 重钉 |
| U14 | runner 决策取回按 flow 分流（eager vs 透传），provider arity 自适应；CLI `_decision_provider_for_family` 升两参透传 subject digest，provider-json 模式 execute 全链打通（H6 行 X1 证据缺口闭合） | test_resolve_subject_quality_decision_is_arity_adaptive、test_cli_decision_provider_forwards_subject_digest、test_cli_provider_wrapper_compose_with_runner_resolution；research_chain phase0–4 全量转换 |
| U15 | quality_decision.v1 信封在 bind 单一收口验签拆解，裸/信封双形态统一 | test_bind_adopts_verified_resolver_envelope |
| U16 | AcceptedFactorIndexRuntime 支持注入 FactorRegistry，publish/accept 两侧 registry 不再可能分叉 | test_accepted_runtime_reads_through_injected_registry |
| U20 | C2 train split 校验抽纯函数 resolve_train_split（免 qlib 回归锁） | test_qlib_train_split_validation_is_pure_and_rejects |
| U21 | qlib worker datetime64 保持真锁（fake-qlib 全链路，变异验证） | test_qlib_adapter.py::test_qlib_worker_output_preserves_datetime64 |
| U22 | 重复状态行删除，allowed_statuses 单一收口于 review registry | research_chain/model_layer 邻接套件 |

## 八、残留项处置记录（2026-10-10，U 批次复核后的 10 项残留）

U 批次合入（12d44af）并经 code-review 复核（46ba276）后，复核方给出 10 项残留分析。逐条核验结论：**6 项代码修复（R1/R2/R3/R4/R9 + CI 回归），4 项文档化接受（R5/R6/R7/R8）**。CI 回归（qlib 门控全链首跑失败，run 38053013828）定位出两个本地不可见缺陷并已修复验证（844acc4 全格绿）。除注明外，新增回归锁均在 tests/test_adv_review_fixes.py。

### 8.1 代码修复项

| 残留 | 处置 | 回归测试 |
|---|---|---|
| CI 回归 D1 | phase5 的 `create_reviewed_quality_decision` 漏带必填 subject（U9 转换时该文件被 qlib 门控遮挡）→ 补 subject_generation_id/subject_content_sha256 | tests/test_research_chain_phase5.py（CI qlib 格验证） |
| CI 回归 D2 | `validate_research_layout` 硬编码 V3 十阶段序，而可执行请求全是 V1/V2 九阶段（V3 runtime 在 runner._validate_plan 禁用）——失败 run 在 result_reconciliation 的状态发布报 08 vs 09 版式错并掩盖 D1。阶段序单一来源化于 `RESEARCH_STAGE_ORDER_V1/V3`（model_layer），layout 校验器收 `stage_order` 参数、_atomic_write 传文档自身 plan、runner._state_relative_path 全线穿 plan | test_v1_state_publishes_at_v1_stage_number、test_research_layout_stage_number_follows_explicit_stage_order |
| R1 | registry 全部 18 个 reject_all binding 的 allowed_statuses 由 [passed, warning] 收紧为 [passed]（此前 warning 决定可铸造并 bind，与因子/规范侧同策略只认 passed 相悖）；feature_preprocessing_v1 因非 reject_all 策略保持双态；registry 加载期拒绝自相矛盾口径；信任锚 registry_sha256 已重锚 | test_reject_all_registry_bindings_admit_only_passed_status |
| R2 | research-v1 契约新增 `cross_validation_mode: optional|required`（默认 optional）：声明即现实——optional 时默认路径合法跳过比对；required 时跳过比对的 pipeline 在构造期即拒。U1 语义债清偿，下轮评审不会再把它当 H2↔U1 摆回 | test_cross_validation_mode_is_declared_and_enforced |
| R3 | build_tdx_pipeline / build_tushare_crosscheck_pipeline 装配直接上锁（适配器集、日历、cross-validation 三旗标），不再只靠 U1 手工重建间接覆盖 | test_pipeline_builders_lock_their_wiring |
| R4 | provider arity 契约收紧为严格 {1,2} 位置参数：*args 误路由、3 参晚爆 TypeError、不可内省签名静默退化两参，三者均在解析期即拒 | test_decision_provider_arity_is_strict |
| R9 | canonical schema 的 float64 字段禁非有限值（±inf）——CanonicalStore 发布与读取同走 Schema.validate，inf 行从此写不进也读不出；NaN 仍归 nullable 规则，gate 的 U11 行为（inf vs inf 干净）不变 | test_schema_rejects_non_finite_float_values |

### 8.2 文档化接受项（经核验为既定架构口径，非缺陷）

- **R5（外部评审信任边界）**：`create_reviewed_quality_decision` 在 src/scripts 零生产调用方，外部评审仍是库调用——「谁能签」的信任边界在部署层（评审私钥的保管与签名环境的隔离），代码层只能保证「签什么」可验证（U9 已加固）。本批确认此为既定部署模型并登记于此；若未来引入远程评审服务，信任边界需随部署架构重述。
- **R6（industry_membership_v1 无生产者）**：taxonomy artifact 目前只有 engine 侧内容绑定校验（U10：policy 绑定 + 内容一致性），无生产者/存储层为其内容背书——taxonomy 的正确性由 policy 评审承担（policy 本身经外部评审签名）。登记为已知治理缺口，待行业分类数据源接入时补齐生产者。
- **R7（指纹版本未记录）**：manifest 未记录 fingerprint_version，v1 语义（NaN≡-0.0）永久可用；因 v1/v2 payload 结构不同，跨版本伪造路径不成立（U2/U18 已验证 ±inf 指纹互异），但排障时无法从 manifest 直读命中版本。接受为兼容性窗口的既定代价；下次 schema 版本化时连同登记。
- **R8（provisional zero-checksum 身份约定）**：backtest_config/target_weights 评审签的是发布前合成 manifest（quality_report_checksum_sha256 置零参与身份），内部自洽。此为发布序依赖（报告校验和需在绑定后回填）的既定解法，非伪造路径——绑定态 manifest 的完整身份在 readback 时全量重算验证。

### 8.3 自 CR 复核记录（2026-10-10，覆盖 844acc4 + 9a91cd2）

对两个残留处置提交做了一遍对抗式自审（修复正确性 / 旁路消费方 / 边界形态 / 语义一致性 / 锁的真实性），结论：**发现 1 项遗漏并当场修复，1 项复核为有意保留**。

- **已修复：V3 状态快照列表静默丢弃**。`list_state_snapshots` 仍按硬编码 V1 序排序——D2 修好后 V3 状态可经 store 直接发布（runner 虽禁 V3 执行，store 是公开 API），paper_execution 快照会从列表中消失。改为从状态文档自身的 contract_version 派生排序（同一次 run 的状态共享请求版本）。回归锁：test_v3_state_snapshots_list_in_v3_order_without_drops。
- **有意保留：publish_result 闭包校验的 V1 硬编码**。与 runner._validate_plan 的 V3 执行禁用一致——V3 result 不可能合法存在，从 payload 派生只会为伪造文档松闸。已加注释防止下轮评审再标。
- 复核通过的疑点：execution/stores.py 直读 registry bindings 但仅消费 allowed_checks（无 allowed_statuses 旁路）；R4 严格 arity 下 cli 两参带默认包装、functools.partial、bound method、__call__ 实例的路由均保持；R9 的 NaN 语义未受有限性禁令影响；R2 provenance 增补不破坏既有重放证据。

### 8.4 外部 CR 复评修补（2026-10-10，覆盖 844acc4 + 9a91cd2 + 8d3db60）

外部复评确认三条残留，本批全部处理（含一处比原报更精确的定位）：

| 残留 | 处置 | 回归测试 |
|---|---|---|
| 证据索引自我引用 | `paper-execution-research-integration/phase-0` 索引把自己登记成一条记录，其 sha256 逻辑上永不可验；测试用「跳过自身路径」的豁免掩盖而非删除。已删除自指记录并取消测试豁免 | test_evidence_indexes_are_fresh_and_never_index_themselves；test_paper_execution_research_integration_evidence 无条件验摘要 |
| 读侧状态集合未随 R1 收紧（定位修正） | 原报为「重复策略口径」；实测更严重——prediction/target_weights/backtest_result 读路径**根本未查 registry**，只比硬编码 {passed, warning}，registry 收紧后仍会接受 warning 报告。新增单一来源 `reviewed_allowed_statuses(binding_type)`，替换 8 处硬编码(dataset_writer ×2、trainer、predictions ×2、portfolio/builder、backtest/engine、qlib_export 读路径);feature_preprocessing_v1（非 reject_all）仍合法接受 warning | test_reviewed_status_policy_has_a_single_source |
| 证据索引新鲜度无守护 | 22 个索引结构各异（records/entries/items，仅 2 个带摘要），故按递归遍历任意 `{"path","sha256"}` 配对做全量校验：当前 96 对全部匹配、零自指;守护额外封堵 `./`/`..`/绝对路径自指、非法摘要长度、目录描述符缺失三类规避缝。索引结构统一属后续治理项 | test_evidence_indexes_are_fresh_and_never_index_themselves |

未改动项与理由：`research_chain/adapters.py:435`、`runner.py:813` 的 `{passed, warning}` 作用于 **factor_v1 owning report**——该族不走模型评审 registry（无 binding_type 条目），其状态语义由因子侧 quality_policy 决定（factors/store.py:161/:560 已按策略区分 passed/双态）。把模型 registry 套上去会误拒因子报告；因子侧「策略→状态」映射与模型 registry 统一为单一治理口径，登记为后续项，非本批缺陷。

`src/uq/contracts/model_layer.py` 变更后按约定重钉 `paper-execution-research-integration/phase-0` 索引记录。全量测试 632 passed / 22 skipped(22 个为 qlib 门控,本地 skip、在 CI `qlib-runtime` 单元运行)。此前写的 654 为 passed+skipped 合计,口径有误,已更正。
