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
| 研究链结果读侧残留(本轮 CR) | `research_chain/runner.py:813` 仍硬编码 `{passed, warning}`。经探针核实,registry 的 `validate_report` 已拒绝 warning,故这是**一致性残留(防御性收紧),非可达绕过**;改为 `reviewed_allowed_statuses("research_run_result_v1")`,纳入锁测试 | test_reviewed_status_policy_has_a_single_source(源码锁);test_reviewed_registry_rejects_warning_status_for_passed_only_families(行为负例,2026-10-12 修复轮改名,原名 test_reject_all_read_paths_reject_warning_reports_behaviorally);test_model_qlib_export.py::test_warning_status_report_rejected_on_read(真实 store 读路径负例);已用突变验证两处回退均被锁测试捕获 |

未改动项与理由：`research_chain/adapters.py:435` 的 `{passed, warning}` 作用于 **factor_v1 owning report**——该族不走模型评审 registry（无 binding_type 条目），其状态语义由因子侧 quality_policy 决定（factors/store.py:161/:560 已按策略区分 passed/双态）。把模型 registry 套上去会误拒因子报告；因子侧「策略→状态」映射与模型 registry 统一为单一治理口径，登记为后续项，非本批缺陷。

`src/uq/contracts/model_layer.py` 变更后按约定重钉 `paper-execution-research-integration/phase-0` 索引记录。全量测试 632 passed / 22 skipped(22 个为 qlib 门控,本地 skip、在 CI `qlib-runtime` 单元运行)。此前写的 654 为 passed+skipped 合计,口径有误,已更正。

### 8.5 A500 pilot 证据收尾与自 CR（2026-10-12，覆盖 0755cae 之后的 dfc8237 + 23cb49a）

自 CR 在本批发现并处理的真实缺陷（含首版生成器自身的缺陷）：

| 缺陷 | 判定与处置 | 锁 |
|---|---|---|
| `uq-ingest daily` 只接 research-v1（builder 与 `research-whitelist.txt` 均硬编码） | **可达缺陷**：500 只 pilot 由此无法从仓库复现，"端到端"证据只能靠内联循环。新增 `--pipeline v2` + `--membership-root/--universe-id`，成员逐日走 PIT membership store；缺参 `parser.error`，快照不可见 `configuration_failure` 且不落分区。默认仍 v1，既有行为不变 | tests/test_pipeline_v2.py 三条 CLI 用例（wiring / 缺参 / 未见快照 fail-closed） |
| `evidence/a500-membership/evidence-index.json` 记 14 个快照、store 实为 17 | 证据滞后（回填 2025-05-30/06-30/07-31 后未刷）。改为生成器重导，旧 14 条 digest 逐条复验**无漂移** | scripts/build_store_evidence.py 出稿前逐条经 governed reader 校验 |
| 生成器首版对 store 相对路径调 `Path.relative_to(REPO_ROOT)` | 首版自身缺陷：直接 `ValueError` 崩。改为先 `resolve()` 再相对化，仓库外路径 fail closed | 生成器实跑（bars 23 分区 / membership 17+21） |
| 生成器首版调私有 `IndexMembershipStore._snapshot_dates` | 依赖私有 API。改为脚本侧只做"目录发现"，manifest/members 一律经 `read_manifest/read_members` 校验；非日期目录名直接失败，避免暂存目录被静默跳过 | 同上 |
| pilot 记录把 builder 模块写成 `uq.pipeline.builders` | 文档错。实际在 `uq.pipeline.daily`（已核 `rg`），并补 run report 落点 `<bars_root>/runs/<run_id>.json` 与"pilot 由内联循环产出"的诚实声明 | 人工核对 |
| `paper-execution-research-integration/phase-0` 索引 4 条 stale digest | 源码前进后未重钉（`model_layer.py`、`research_chain/{contracts,resolver,runner}.py`）。按既有重钉约定重钉，`phase-record.remote_gate` 绑定不动 | test_evidence_indexes_are_fresh_and_never_index_themselves；test_phase_0_evidence_index_hashes_are_complete（两条此前失败的测试现绿） |

| 索引生产者可产出被禁止的形态 | 自 CR 发现：`scripts/build_store_evidence.py --kind index` 允许把 `--out` 自身写进记录（正是 8.4 刚清理过的"自我引用永不可验"形态），也允许重复记录。改为生产侧直接拒绝（self-record / duplicate / 非仓库内路径 / 缺失文件），不再只靠 `test_evidence_indexes_are_fresh_and_never_index_themselves` 事后拦截；同时把 `repo_root` 由模块常量改为显式参数，使 fail-closed 分支可测 | tests/test_store_evidence.py（10 例：bars/membership digest 正确性、篡改 data.parquet 与 members.csv 均拒绝、manifest 语义被改而 anchor 仍自洽时由读回捕获、暂存目录残留拒绝、空窗口拒绝、self-record/duplicate/越界/缺失拒绝） |

未夸大声明：pilot 归档的 `local_gate` 为**单格本地**（macOS arm64 / Python 3.12，runner=uv，绑定 `23cb49a`）；远程矩阵在同 HEAD 自然跑绿（run 38197992719，11 jobs：6 base + 4 qlib-runtime + f4-certification），但**未按格归档** gate artifact，故 `remote_ci` 只记状态与绑定 commit，不当作 per-cell 证据；`publish_lag_days=5` 仍是未实测假设；v2 分区 lineage 全部 `compared_rows=0`（无对照源佐证）。全量测试 751 collected / 729 passed / 22 skipped / 0 failed（22 为 qlib 门控，本地 skip、CI `qlib-runtime` 单元运行）。

### 8.6 整体 CR（2026-10-12，覆盖 dfc8237…67e39ff 全部 7 个提交）

再审一遍本批全部改动，发现并修掉 3 处（均为本批自身引入或本批可锁）：

| 项 | 判定与处置 | 锁 |
|---|---|---|
| `bars_inventory` 读 `manifest.get("data_file", "data.parquet")` | 幻影键：canonical manifest 从未有 `data_file`（写侧固定 `data.parquet`）。当前无害，但它把"清单里出现同名字段"变成可左右读路径的开关。改为写侧常量 `BARS_DATA_FILE` | 现有 digest 用例覆盖 |
| governed 读回的 `ContractError` 裸逸 | 真缺陷：`ManifestFirstReader.read` / `read_manifest` / `read_members` 抛的 ContractError 未被包装，出稿脚本以 traceback 退出，运维拿不到"拒绝归档"这一句结论。新增 `_governed()` 统一转 `SystemExit` 并带上被拒对象与原因 | test_bars_inventory_refuses_a_manifest_the_reader_will_not_accept |
| CLI 断言不足以支撑记录里的声明 | `pilot-record` 声称每会话 ingest report 落 `<bars_root>/runs/<run_id>.json`，但原 3 例无一验证 v2 路径真的写文件；缺参用例只断 `SystemExit` 不锁提示内容。补 2 例（写盘内容与 run_id 前缀、退出码 2 与提示含两个缺失参数） | tests/test_pipeline_v2.py（15 例） |

复核性验证（不是推断）：`bars-inventory.json` 与 `evidence/a500-membership/evidence-index.json` 用改后脚本从活 store 重导，与已提交版本 `diff` 为空（逐字节可重导）；`pilot-record` 的 11 项数字与三份 artifact 逐条比对一致；归档的 `requirements.lock.txt` 无绝对路径/凭据；`paper-execution` 索引只改 4 条 digest（`layer/record_version/status` 未动，47 条记录）。

一条真实但**非本批引入**的治理弱点（登记后续项）：canonical manifest 的 `trust_anchor_sha256` 只是 `sha256(generation_id)`，而 `generation_id` 由 manifest 自身内容哈希得到——读侧不做外部校验，故 `lineage`/`source_versions`/`run_id` 这类"声明性"字段被改写后，重算 generation+anchor 即可自洽（数据字节、行列、dtype、row_count 仍被交叉校验，改不动）。本窗口的实际缓解是外部锚定链已存在：`evidence-index.json`(tracked) → `bars-inventory.json`(tracked) → 每分区 `manifest_sha256`，篡改本地 manifest 可通过"重跑生成器 + 与 tracked 文件 diff"检出。把 generation 改为内容重算或接入外部签名锚定，属 manifest 语义变更，需按规则开新版本。

### 8.7 pendding 5 联动断言 + pendding 6 dividend importer（2026-10-12，覆盖 87a6759 + 45dc5e3）

本批交付与自 CR 发现（均为本批自身引入，未混入无关改动）：

| 项 | 判定与处置 | 锁 |
|---|---|---|
| 因子/label 联动无门禁 | **可达缺口**：就绪门查了 bars/成员/停牌，却从不校验声明的因子分区是否越过 `usable_label_end`。新增 `assess_factor_label_linkage`（越窗 / 晚启动 / 空因子层 / 读到 label 之后四类 fail-closed），窗口本身不满足 horizon 时静默；`--factor-partition-set` 接入 CLI，新增 `factor_sessions`/`label_only_sessions` metrics | tests/test_training_readiness.py（15→27 例），真实 store 实测 passed/blocked 两态 |
| 联动规则把 `window_start` 当成交易日 | 自 CR 发现：月末/月初窗口起点常为周末/假期，原判定 `factor_values[0] > window_start` 会误报。改为与"窗口内首个会话"比较，并补月首非交易日用例 | test_linkage_allows_a_month_start_window |
| dividend 主键不唯一 | 实测同 `(ts_code,end_date,div_proc,ann_date)` 仍重复 34 行/17 组，且是内容修订（`cash_div_tax` 0→0.02）而非重复投递。改为**公告日为分区键 + 规范化整行 canonical SHA-256 为 `record_id`**：字节级重复收敛，内容修订各留一条 | test_identical_rows_collapse_but_revisions_survive；A500 实测 1714→1710 |
| 契约必填列把派生列当上游列 | 自 CR 发现（被本批测试抓到）：`required` 集合含派生 `instrument`，真实 Tushare 帧只有 `ts_code`，首版会直接拒绝全部输入。改为「原始列集合减去派生列，再并上 `ts_code`」 | 21 例 store 测试 + 真实数据抓出 |
| 披露阶段枚举漏 `预披露` | 真实数据打回（脚本首次实跑即失败）：Tushare 实际有 4 个阶段。已从"预披露/预案/股东大会通过/实施"取值并补用例；未知阶段仍 fail-closed | test_pre_disclosure_stage_is_a_disclosure_record；实测 2025-08 全月枚举确认恰为 4 值 |

实测数字（不是推算）：`bash scripts/run_gate.sh`（uv，单格本地 macOS arm64 / Python 3.12）在 `be672b8` 上 `766 passed / 22 skipped / 0 failed`（22 为 qlib 门控，本地 skip、CI `qlib-runtime` 运行）；归档报告与 requirements 同 HEAD，`pilot-record.local_gate.commit_bound` 已回绑 `be672b8`。dividend 真实窗口 A500 2025-08-29~09-30：28 个公告日、3337 条记录、证据清单由 `--kind dividend` 逐分区 governed 复读生成（`evidence/reference-data/dividend-events-2025-08-29-to-2025-09-30.json`）。

未夸大声明：联动断言校验的是"声明的因子分区"，不重算特征本身；`publish_lag_days=5` 仍是未实测假设。（dividend store 当时尚未被复权链路消费，已在 §8.8 补齐。）

### 8.8 dividend → 复权生产链路（2026-10-12，覆盖 87a6759/45dc5e3 之后的未提交切片）

本批把 `XdxrAdjustmentDeriver` 从"只有测试调用"接到生产：`DividendEventStore → implemented_events → XdxrAdjustmentDeriver → AdjustmentSnapshotStore`，并新增逐标的审查报告与批量脚本。

| 缺陷 | 判定与处置 | 锁 |
|---|---|---|
| 已实现快照**不可能**被选中（真实阻断） | `AdjustmentSnapshotReader.select_visible_generation` 要求 `sessions.csv` 等于 `pd.date_range(start,end,freq='D')`，即覆盖窗口内**每个自然日**；而 deriver 的 sessions 是交易日。任何跨周末窗口（真实窗口都有）都会被判 `invalid adjustment snapshot session coverage`——快照能写、永远选不中，生产链路必然不可用。改为与 save 侧同口径：严格递增、且首尾等于声明窗口（manifest checksum 仍钉住整张表） | tests/test_adjustment_chain.py::test_snapshot_publishes_and_is_selectable_across_a_weekend（周末间隙）+ 既有 runtime 用例 |
| 派生表 checksum 无法与快照对齐 | `AdjustmentDerivation` 只回传 `effective_date_table_checksum`，不回传表本身；producer 若自行重建 effective 表，任何一格差异都会让快照 checksum 与 factor 侧声明漂移。给 dataclass 补 `effective_dates` 字段，直接用 deriver 自己的表落盘 | 同一用例断言 `source_event_id` 逐行等于披露记录 id |
| 单位口径靠推断 | Tushare `cash_div/stk_div` 是**每股**还是每 10 股，直接决定因子错 10 倍。实测 CATL 2022 年度（每股现金 2.52 + 每股转增 0.8）复现官方 2023-04-26 比例 0.55214，确认为每股 → 映射 ×10 | tests/test_adjustment_chain.py::test_bonus_and_cash_use_per_share_rates_times_ten |
| provider 口径不一致 | A500 窗口 219 条实施披露中 3 只（601866/002831/300003）用公告金额算出的因子与 Tushare 自身 `adj_factor` 差 1.6e-4~8.2e-4；折合现金 0.002~0.0096 元/股，量级正是交易所参考价的 0.01 元分辨率，非映射错误。容差取实测最差之上的 1e-3，并把 `price_scale_gap_cny`/`within_public_resolution` 写进结果，超差仍 fail-closed | tests/test_adjustment_chain.py::test_provider_divergence_refuses_to_derive |
| 复权链路无端到端闸门 | 新增 `scripts/build_dividend_adjustments.py`：批量派生+发布+出报告，任一标的被拒即 exit 1（不是日志） | test_script_reports_and_exit_code_follow_refusals / test_script_refuses_a_window_without_bars |

实测（真实 store，不是构造）：A500 2025-08-29~09-30，23 个会话；窗口内 219 条实施披露 → 29 只标的有可分派事件 → **29/29 快照发布、0 拒绝**，最差 `max_abs_diff=8.2e-4`（601866），全部在 0.01 元/股分辨率内；证据 `evidence/reference-data/adjustment-chain-2025-08-29-to-2025-09-30.json`。

未覆盖：配股不在 Tushare `dividend` 端点内（`peigu/peigujia` 恒 0，已在 provenance 声明）；快照自身的不可变性/篡改用例沿用既有 runtime 测试；本链路只解决"因子侧有受治理的复权 lineage"，尚未把快照挂进 `FactorEngine.execute(factor_set='adjusted')` 的调用方（research_chain 的 v3/v4 执行路径，见 pendding 4c）。

### 8.9 pendding 6 收尾：v2 独立交易日历 + 9a 北交所名单（2026-10-12）

本批关闭 pendding 6 的两条独立后续项中的第一条（v2 链路独立交易日历），并带上已完成的 9a（lifecycle 北交所名单）。

| 项 | 判定与处置 | 锁 |
|---|---|---|
| v2 路由**永远**拿不到 `complete` | 真缺陷（根因）：`build_tushare_v2_pipeline` 解包成 `SourceRouter(contract, adapters)`，从不传 `calendar`，而 `SourceRouter.__init__` 在 `calendar is None` 时直接置 `self.calendar=None`；覆盖率判定读 `self.calendar.between(...)`，于是覆盖率恒为 `unverified`（`pendding.md` 里"v2 链路尚无独立交易日历"的机械原因）。research-v1 的交叉校验 builder 早已传 akshare 日历，只有 v2 漏了 | tests/test_pipeline_v2.py::test_v2_route_coverage_is_complete_with_an_independent_calendar（有历 → 无 unverified 警告）/ ..._without_a_calendar_stays_unverified |
| 日历可得性：akshare 不在任何 extra 里 | 事实核对：`uv.lock` 与 CI 的 `--extra dev --extra real` 都不含 akshare，本地也不可导入——照搬 v1 的 akshare 日期会把 v2 的既有可用通路（A500 pilot 就是这么灌的）改成"无 akshare 即失败"。改为新增零依赖的 `EastMoneySessionProvider`（沪深300指数日K→会话集合），厂商上真正独立于 Tushare/TDX，且不引入新依赖 | tests/test_eastmoney_calendar.py（8 例，含解析/缓存/证据） |
| 失败模式会**静默**放宽置信度 | 真缺陷（设计时即堵住）：覆盖率判据是 `observed_dates >= expected_dates`，若日历不可达/为空就返回空集合，判据会真空成立并把未验证覆盖率报成 `complete`。provider 因此 fail-closed：不可达、非 JSON、无 klines、**窗口末端超出已发布最新会话**一律抛 `IndependentCalendarError`（`status=upstream_error`、`retryable=True`），并由 `IngestReport` 落成 `source_upstream_error`、不落分区 | tests/test_eastmoney_calendar.py::{test_failed_transport_fails_closed, test_empty_payload_never_empties_the_expected_sessions, test_window_past_the_newest_published_session_fails_closed}；tests/test_pipeline_v2.py::test_v2_run_fails_closed_when_the_independent_calendar_is_unreachable |
| 覆盖率"完成"事后不可审计 | 自 CR 发现：报告 provenance 只记日历名字，不记它到底依据了什么。provider 暴露 `evidence`（payload_sha256、认证区间、会话数、URL），`DailyIngestPipeline._provenance` 把它写进每份 ingest report 的 `calendar_evidence` | tests/test_pipeline_v2.py::test_v2_run_records_the_calendar_evidence_in_provenance |
| 就绪门的会话表仍是同商 | 真缺口（部分收口）：`validate_training_readiness.py` 的会话来自 Tushare `trade_cal`，与 bars 同商，`bar_session_gap` 只证内部一致。新增 `--independent-calendar`：用同一 provider 复核窗口内每个会话，不一致即退出码 2；报告 `notes` 另记 `min_sessions_per_instrument`/`label_horizon_sessions`/`max_null_rate`/`skip_*`（阈值此前只在 pilot-record 的 reproduction 里，报告本体不可自证） | 真实重跑（见下）；`--min-sessions 17` 与 pilot-record.reproduction 一致 |
| 9a：`.XBSE` 一律 `unknown_requires_review` | 已知缺口补齐：AkShare 无北交所名单，故 lifecycle 对北交所代码只能 fail-closed。新增 `tushare_bse` 辅助源（Tushare `stock_basic(exchange=BSE)`，实测 2000 积分可用：349 只、全部带 `list_date`、前缀全为 92），仅对 `.XBSE` 查询；缺席早于上市日才豁免，名单内缺口/无 `list_date`/名单外一律仍 fail-closed（无北交所退市源，退市与未上市不可区分） | tests/test_real_data_policy.py（+10 例，含 AkShare 故障时北交所名单仍独立可用） |

实测（不是构造）：① 真实 v2 ingest（真实 Tushare + 真实 EastMoney）2025-09-02 两只标的 → `status=published`、`warnings=[]`（此前必有 `route coverage is unverified`）、provenance `calendar=eastmoney_index_kline_v1`、`calendar_evidence.payload_sha256=730c7ab6…`、认证区间 `2024-07-29..2026-10-12`、`sessions=532`（该 scratch 分区已删，不入库）。② 就绪门重跑 A500 2025-08-29~09-30（`--min-sessions 17 --label-horizon 5 --independent-calendar`）→ 仍 `passed`、仅 `delisted_members_excluded` informational、`independent_calendar_verified=true`（23/23 会话两历一致），`evidence/a500-pilot/.../training-readiness.json` 已按此次运行刷新，`evidence-index.json` 由生成器重导。

未夸大声明：归档的 23 个 pilot 分区是在本改动之前灌入的，它们的 route coverage 当时仍是 `unverified`——`pilot-record.json` 的 `ingest.calendar` 已如实写明，并说明该链路现在默认独立日历；`bar_session_gap` 本身仍是内部一致性检查，改的只是"会话表另有独立来源可复核"。

实测数字：`bash scripts/run_gate.sh`（uv，单格本地 macOS arm64 / Python 3.12）`806 passed / 22 skipped / 0 failed`（22 为 qlib 门控，本地 skip、CI `qlib-runtime` 运行）；本批新增 14 例（`tests/test_eastmoney_calendar.py` 8 + `tests/test_pipeline_v2.py` 6），另 9a 的 10 例在 `tests/test_real_data_policy.py`。

### 8.10 pendding 6 收尾（二）：XBSE 正则开新版本（2026-10-12）

| 项 | 判定与处置 | 锁 |
|---|---|---|
| 已发布契约只认 XSHG/XSHE，与 `index_membership_row.v1`（含 XBSE）不一致 | 真缺口：`.XBSE` 标的能进 universe/分区（pendding 9 已做），却进不了 `adjustment_snapshot`/`golden_provenance` 的 manifest——用中证全指这类含北交所成分的 universe 跑复权链路时，快照会在写侧被 schema 拒。按 AGENTS.md"放宽属语义变更"开 **v3**，不原地改 v1/v2 | test_xbse_widening_lands_as_v3_not_an_in_place_edit（v3 接受 `920098.XBSE`；同一 payload 标成 v2 被拒）；`contract-negative-cases.json` 新增 `xbse_suffix_is_not_part_of_v2` 与 `unknown_suffix_stays_rejected` |
| 版本分派一处遗漏就会静默拒绝旧快照 | 事实核对：`SNAPSHOT_SCHEMA_BY_VERSION` 是唯一分派点，`LATEST_SNAPSHOT_VERSION` 只影响**写**；v1/v2 仍可读，且 `select_visible_generation` 的血缘闸门（`adj_factor.exchange_v2`）不变，故 v2/v3 快照可等价消费。U3 的"原地翻转"教训不重演 | test_snapshot_store_writes_the_latest_version_and_reader_roundtrips；test_unknown_snapshot_version_is_rejected 改用版本 4（原先用 3 探测未知版本，3 现在合法） |
| 新版本只加分支、不加证据 | 自 CR 约束：`golden-provenance.v3.json` 与 v2 **逐字相同**，只改 `provenance_version`——v3 买到的只是"允许 `.XBSE`"这一条 schema 语义，不新增任何官方证据；北交所案例本身的取证仍未做，已在文档写明 | test 断言 v3 fixture 的四例与 v2 一致；`specs/layers/contracts/adjustment-official-source-access-v1.md` 已注明 |

实测数字：`bash scripts/run_gate.sh`（uv，单格本地 macOS arm64 / Python 3.12）`807 passed / 22 skipped / 0 failed`（新增 3 例：`test_xbse_widening_lands_as_v3_not_an_in_place_edit`，以及 v3 进入既有参数化/负例表后多出的 2 条）。未夸大声明：v3 不新增官方证据（`golden-provenance.v3.json` 与 v2 逐字同例），北交所案例的逐案官方参考价取证仍未做；`select_visible_generation` 的公式血缘闸门未变，故 v2/v3 快照可等价消费。

### 8.11 pendding 4c：能落地的那一半（2026-10-12）

4c 的剩余部分**不是缺陷而是依赖**：它要的挂载点（runner `_readback`、model/backtest 阶段消费 `filtered_factor_frame`、每阶段集合摘要复核）只有在 v3/v4 执行路径开启后才有语义，而开启它等于执行 `paper-execution-research-integration-plan` 的 Phase 1–3（resolver 解析 execution/calendar/suspension/corporate-action/initial-state/risk-policy 绑定 → `PaperExecutionStageAdapter` → result reconciliation）。本批只落可落地且可验证的部分，并把依赖写成机械事实。

| 项 | 判定与处置 | 锁 |
|---|---|---|
| `stage_plan_for_request` 对 V4 静默退回 V1 顺序 | 真缺陷（潜伏）：分派只看 `== 3`，V4 落到 `else` 分支拿到未含 `paper_execution` 的 V1 顺序。V4 的 normative plan 与 V3 相同（都插 `paper_execution` 于 `result_reconciliation` 前），因此一旦 v4 执行放开，`_state_relative_path` 编号、`list_state_snapshots` 顺序、失败态的 `failed_stage` 推导都会错位。改为显式映射 {1,2}→V1、{3,4}→V3，未知版本直接 `ContractError`（不再"猜成 V1"） | test_v4_and_v3_share_the_stage_order_and_unknown_versions_fail_closed |
| "v4 未开启"当时没有机械锁 | 自 CR 发现：`runner._validate_plan` 的 `research request v3/v4 paper-execution runtime is not enabled` 一句没有任何测试钉住，删掉守卫不会有测试变红——而删掉它意味着 v3/v4 请求会在**没有 paper_execution 阶段**的情况下跑完整条链并发布结果。补 fail-closed 锁 | test_v3_and_v4_execution_are_refused_until_the_paper_runtime_lands |


实测数字：`bash scripts/run_gate.sh`（uv，单格本地 macOS arm64 / Python 3.12）`809 passed / 22 skipped / 0 failed`（22 为 qlib 门控，本地 skip、CI `qlib-runtime` 运行）；本批新增 2 例（`tests/test_research_v4_contract.py` 的 `test_v4_and_v3_share_the_stage_order_and_unknown_versions_fail_closed`、`test_v3_and_v4_execution_are_refused_until_the_paper_runtime_lands`）。

未夸大声明：本批**没有**开启 v4 执行、没有新增 paper_execution 阶段输出、也没有把 `filtered_factor_frame` 接进任何生产调用点；`pendding.md` 的 4c 已从"[部分完成]"改为"[阻断:…Phase 1–3]"，并写明 4b 的函数"已实现并有测试"但"未挂载"。

### 8.12 整体 CR（2026-10-12，覆盖 2e45e19..7970eec 全部 61 提交：pendding 1–9 关闭 + paper-execution Phases 0R–3）

四路独立集群并行对抗审查（review 修复+membership/v5｜Tushare v2+就绪门｜dividend 复权链｜paper-execution 阶段），**全部 BLOCKER/MAJOR 由监工亲自复核**（读码 + 实跑复现），不接受代理单方面结论。基线：本地 862 passed / 23 skipped / 0 failed，HEAD CI 绿（run 38254476814）。

**BLOCKER（1，亲自实跑复现）：**

| 发现 | 锚点与判定 |
|---|---|
| v5 resolve 全线 NameError，本批交付的唯一可执行 paper 路径是死的 | `resolver.py:171` else 分支调用 `research_stage_plan_v5_sha256()` 但未导入（:12-22 只到 v4，函数在 `model_layer.py:384`；a2065fc 引入）。监工复现：`resolve(research_run_request_v5-valid.json)` → NameError。CLI `--mode execute` 任何 v5 请求 → exit 3；`docs/user-guide.md` 记录的流程实际不可运行。**绿门盲区**：无测试用 v5 走真 `resolve()`——membership 测试直调 `_verify_contract_version_inputs` 绕开崩溃点；唯一调 `resolve()` 的 phase-1 测试用裸 `except Exception` + 弱断言，**因崩溃而通过**，PE17 验收行由空测试支撑 |

**MAJOR（6，全部亲自读码确认，M2 另有 fixture 佐证）：**

| # | 发现 | 锚点 |
|---|---|---|
| M1 | `_verify_v5_paper_bindings` 迭代 `_PAPER_BINDING_FAMILIES` 却从不比对 `expected_family`（死变量）；四字段全绑日历族也过；phase-1 fixture 自己就把 market_data/corporate_action 绑成 suspension 族 | `resolver.py:314-326` |
| M2 | 模板四大 paper 绑定在执行侧零消费（adapter 从 market source 现取 + `_restate_family` 改名）；绑定窗口从不与请求窗口比对。**佐证：自带合法 fixture 运行窗口 2025-12-01..2026-01-29，六个集合绑定全钉 2025-01-01..2025-12-31**——验收样本本身就是窗口不覆盖实例；membership/factor 有 resolve+reconcile 双重复核，paper 输入只有 resolve 时对一个执行时被忽略的绑定的校验 | `stage_adapter.py:396-407` |
| M3 | `_daily_batch` 对 `response is None` 立即静默返回空帧（不重试不报错）；空帧路径有重试+UPSTREAM_ERROR，None 形态原样留着——d02c0ba 修的缺陷类经兄弟形态仍可达：短面板按 SUCCESS 发布 | `tushare.py:211-218` |
| M4 | 就绪门按「并集成员 × 全部窗口会话」求缺口：v2 CLI 按日 as_of 灌数，调出指数的成分调出后无 bars，任何跨成员变动边界的训练窗口对**正确数据**报 `unexplained_bar_gap` blocking。修正口径注意：不能缩到「仅成员会话」——成员末期决策的 5 日 label 需要调出后的 label 尾段；正确口径为「成员会话 ∪ label horizon 尾段」或由 CLI 补灌尾段 | `readiness.py:143,:207` |
| M5 | `risk_policy_binding` 模板必填（spec 称「指明本 run 经评审的 risk_policy generation」）但零强制：paper 阶段执行 risk store 当下任何策略，无人比对模板字段——评审钉 G1、执行用 G2，全链自洽不报 | `paper_execution_template_v2.json:21` vs 全树仅 v2 路径比对（`runner.py:259/:267`） |
| M6 | 日历分区选择三种窗口过滤不一致：`sessions()` 重叠即收、`calendar_binding()` 只收 window_start 在窗内者（跨界分区服务了会话却被排除，甚至「会话照喂、run 照死」）、集合绑定按 partition_date 在窗内取——同一 run 三个 artifact 可各报一套日历血缘 | `market_source.py:39-51/90-102` |

**MINOR（14）：** m1 reference 端点错误被拍平成不可重试 AUTH_FAILED（`tushare.py:290-292+353-354`）；m2 独立日历只守 newest 端，早于首根 K 线的窗口返回空会话集（`eastmoney.py:107-114`）；m3 空 instruments 触发整市场 `daily(ts_code=None)` 并可发布，目前只靠 membership 契约 `row_count>=1` 挡住（`tushare.py:277`）；m4 `bar_session_gap` 比计数不比集合（`readiness.py:158-165`）；m5 就绪门脚本 suspension/delisting 上游故障裸 traceback 退出（`validate_training_readiness.py:101-114`）；m6 非交易日 ex_date 事件静默不生效、容差内交叉校验照过（`adjustment.py:65-67`，影响被 1e-3 限界）；m7 ad2bea1 新不变量接受窗口中段缺交易日的快照（`adjustment_snapshot.py:206-209`）；m8 provider_check=skipped 仍发布且汇总行把 skipped 显示为 0.0（`build_dividend_adjustments.py:88-90`，测试祝福的有意行为）；m9 「行为锁」名不副实（只直调 `registry.validate_report`；配套源码锁经变异验证确实抓住两处历史回退，换序集合/反向断言可规避）；m10 已发布 `research_run_result/state.v1` 原地拓宽 contract_version 枚举（向后兼容、旧消费者 fail-closed、有计划书背书与兼容锁，但与「语义变更开新版本」字面规则不一致，**记为有意先例**）；m11 `_governed_trading_days` 部分覆盖静默收窄会话表（`cli.py:300-324`）；m12 `--skip-independent-calendar` 时 manifest 硬编码 `cross_check_result:"agreement"`（`market_dataset_publisher.py:82`）；m13 phase-0r 重名测试遮蔽，PE6 证据引用的断言路径从未在 CI 执行（`test_paper_execution_research_integration_phase0r.py:101/:123`）；m14 pendding 4c「仍未接线」双向过时（v5 dry-run 已通、生产 publisher 已存在；真正死的 v5 EXECUTE 反而没记）。

**观察项（择要）：** factor_v1 适配器不对称（`adapters.py:462` 持有 manifest 却不按其 quality.policy 区分，reject_all 因子族的 warning 在 adapter 被接受、在 store 被拒）；无 `ResearchChainRunner.execute` 的 v5 端到端测试（pendding 已明示）；停牌源与 bars 同商（相关故障可掩盖 ingest 洞）；`verify_paper_binding_tampering` 为仅测试引用的生产模块死代码；风控拒绝决策在中止前先落 store（行为正确，宜注释防误「修」）；`_no_symlink` 拒绝 macOS 默认 tmpdir。

**验证成立（四路一致 + 监工抽查）：** ① 读侧状态策略类缺陷**全数闭合**——19 个 registry 族全部经 registry 派生状态，dfc21cf 修复的 qlib_export 洞此前真实可达（`validate("model_quality_report")` 不查 registry），4e8c83e 修正正确（§8.4 原先把 runner.py:813 误记为 factor_v1，本批已自我纠正）；② pendding 4c 的 v5 治理缺口真实存在过且已闭合；③ 可执行性地图：v1/v2 可执行，v3/v4 行为锁拒绝，v5 可执行（V3 阶段序+paper 阶段，风控 block 即 halt 不透出后续日期）；④ dividend/复权链公告日锚定无前视、`ex_date>=ann_date` 代码强制、XBSE v3 干净开新版；⑤ 证据卫生：96 个 {path,sha256} 对全匹配零自指、release 索引 55 条 + phase-0 索引 47 条经代理重算匹配、无数据文件入库。

**处置建议（监工口径）：** B1+M1/M2/M5/M6 同属「v5 paper 链路语义」一簇**必须同批修**——只修 B1 的 import 会把五个语义缺口从死代码变成活漏洞；M3/M4 独立可修；m12 建议随 v5 簇（评审信任面）；m13 改名即修复证据引用；m14 更正 pendding。

未夸大声明：① B1/M1–M6 为监工亲自复核（B1 实跑复现，其余读码+M2 fixture 佐证）；MINOR/观察项为各路代理 CONFIRMED（多数注明 executed），监工未逐条复跑。② release 证据 55+47 条摘要匹配为代理重算结论，监工抽查了 freshness 守护测试全绿这一独立证据。③ 集群 A 留有三项「无法判定」（评审管道实际能否签发 reject_all 族的 warning 报告——决定 qlib:248 类闸门的生产载荷而非正确性；两个 registry 条目是否有规划消费方；phase-2 源码断言是否故意脆弱）。④ 本报告对 61 提交的覆盖按集群分工，evidence/docs-only 提交（约 20 个）只做绑定正确性抽查未逐行审。

### 8.13 修复轮（2026-10-12，四路并行修复 §8.12 确认发现，全部未 commit 待用户裁决）

四路各自修复自己 review 出的问题，严格文件分片无交叉，零 commit；B1+M1/M2/M5/M6 按 §8.12 建议同批落地。监工逐簇复核 diff 并对两处关键修复做了独立变异验证。

| 簇 | 修复内容 | 验证 |
|---|---|---|
| A（评审/读侧残留） | 探针指纹记录改 `seen[date]=fingerprint`（`probe_index_weight_visibility.py:121`）；m9 行为锁补强——真实 store 读路径负例 + 决议信封负例；改名过宽测试为 `test_reviewed_registry_rejects_warning_status_for_passed_only_families` | 5 测试，变异验证（代理自报）；改名后 cr.md:343 旧引用已由监工更正 |
| B（Tushare v2 + 就绪门） | M3 None 批次并入重试后报错路径、空请求零调用（`tushare.py:207-228`）；M4 就绪门必需集改为「成员日 ∪ label 尾段」（`readiness.py:213-271`，含整段停牌免尾段、部分缺口不豁免退市）；m1 ContractError 状态/retryable 不再拍平（:362-365）；m2 eastmoney 最老端守卫 + 认证区间载荷派生（`eastmoney.py:115-136`）；m4 集合比对（`readiness.py:158-185`）；m5 suspension/delisting 上游故障结构化 exit 2（`validate_training_readiness.py:247-259`）；O1 `_unsupported` 按请求推导版本 | 16 新测试。**监工变异验证**：`required=set(session_values)` 回退变异杀死 4 条新测试（rebalance 通过/member-span gap 命名/缺尾段阻断/joiner 归类），恢复后全绿。真实 store 复跑门输出与归档 training-readiness.json 逐字段一致（无 independent-calendar 一腿，见未决②）。a500-pilot 索引重钉 2 条（监工抽查 sha 匹配） |
| C（dividend 复权链） | m6 非交易日 ex_date 在 chain 接缝拒绝（`adjustment_chain.py:311-329`）+ stk_div=bo+co 一致性 1e-9 外拒绝 + imp_ann_date<=ex_date + 逐分量负值检查；m8 provider_reconciliation 报告分 verified/skipped/skipped_instruments 三段，max_abs_diff 只覆盖 verified；m7 spec 标注 v3 现行写版本 + 接受窗口注释（`adjustment_snapshot.py:200-215`）；证据 instrument_count 从 verified 帧重算拒漂移；store.save 增 expected_effective_checksum + 写侧列校验；fetch 脚本逐日 source_fetched_at + 不可变跳过漂移告警 | 12 测试（代理自报 86/86 簇内）；a500-pilot 索引重钉 build_store_evidence.py 1 条 |
| D（v5 paper 链路） | **B1** 补 `research_stage_plan_v5_sha256` 导入（`resolver.py:21`），phase-1 空测试重写为真实 store 端到端 resolve（六绑定+v5 计划摘要断言）；**M1** resolver 族比对 fail-closed（:335-340）+ 模板 schema 逐字段 family const（allOf 组合）+ phase-1 harness 按字段绑正确族；**M2** resolver 窗口覆盖校验（:341-350）+ adapter 发布前 `_verify_template_bindings` 复核 store（`stage_adapter.py:372`，先于任何 4N artifact）+ 三个 phase-0r fixture 重生成（绑定窗口覆盖运行窗口，身份摘要重算）；**M5** 逐决策日 risk_policy_binding 与模板严格相等（:252-256）；**M6** 日历分区统一谓词 `_calendar_partitions`（`market_source.py:39`：partition_date 在窗内 + 内容窗口 +1 邻接覆盖全窗），sessions()/calendar_binding(session) 同源；**m11** CLI `_governed_trading_days` 单源化委托 `PublishedPaperMarketData.sessions()`；**m12** cross_check_result 增 skipped、not_checked 不得记 agreement；**m13** 重名测试改名，PE6 引用路径恢复可执行；**OBS10** `_reconcile_paper_bindings` 与内存权威 bindings 比对+防篡改校验（`runner.py:782-793`）；**OBS13** 拒绝决策先落 store 的保护性注释 | **监工变异验证 B1**：删导入后新测试复现原 NameError（resolver.py:171），证明测试真走 v5 resolve；**监工核验** 重生成 fixture 四绑定窗口全部覆盖运行窗口且族正确。phase-0 索引重钉 resolver/runner 2 条、release 索引 PE17 引用改指新测试 1 条 |

**用户复评追加修复（2026-10-13，监工实施）：**

| 发现 | 修复 | 验证 |
|---|---|---|
| P1（低危真口子）：v5 链路 `date.fromisoformat` 未包 typed error——schema 日期只钉形状（`^\d{4}-\d{2}-\d{2}$`，无 `format:"date"`），`"2026-99-99"` 类形状合法非日期过校验后抛裸 ValueError，CLI（`cli.py:130`）取不到 `.reason` 降级为 configuration_failure，丢失 input_unresolved/request_invalid 语义。共两簇解析点：`resolver._verify_v5_paper_bindings` 的请求窗口与绑定窗口四处；**更早触达的** `membership_input._window_days`（v4/v5 输入集校验先于 paper 绑定校验执行） | resolver 请求窗口→request_invalid、绑定窗口→input_unresolved（`resolver.py:329-356`）；`_window_days` 抛 ContractError，由 `_verify_v4_input_sets` 既有接缝（`resolver.py:397-404`）转为 input_unresolved——避免 membership_input→resolver 循环导入 | 2 新测试（真 resolve 端到端 + 直调绑定校验，phase1 文件）。**监工变异验证**：两处 except 改 KeyError → 两测试分别以裸 ValueError 失败（`membership_input.py:33`/`resolver.py:352`），恢复后全绿。phase-0 索引 resolver.py 记录按约定重钉，freshness 绿 |
| P2（极低危）：两个 negative fixture 的 identity 摘要无测试锁（只验拒绝理由） | `test_request_v5_rejects_superseded_stage_plan` 末尾对两 fixture 重算 `research_contract_identities` 并比对 pinned 值——内容编辑不刷 identity 即失败 | 随 phase0r 套件绿 |

**全量套件**：监工独立复跑两轮均 exit 0（零失败）；D 末轮自报 932 collected / 909 passed / 23 skipped / 0 failed（qlib 门控 skip）。P1/P2 后全量一轮：仅 2 条 evidence-index stale-pin 失败（resolver.py 重钉前的预期状态），重钉后两条单独复跑全绿，其余 900+ 在该轮全过。

**未决项（需用户裁决）：**
1. **schema 原地拓宽（D 申报的范围外扩展）——已裁决（2026-10-13，用户授权监工定夺）：保留 `trading_calendar_snapshot_v1.json:35` 的 `cross_check_result` 枚举 +`"skipped"`，记为与 m10 同类的有意先例**。理由：① 该字段仓库内零消费方（仅 publisher 写、publish 脚本传，监工 grep 核实无读者穷举），对一个纯审计字段开 v2 强推版本分派是零行为收益的治理成本；② 拓宽向后兼容——旧记录全部仍合法，旧消费者 fail-closed（枚举外值被 schema 校验拒绝）而非误读，与 m10 兼容性形态一致；③ 已有写侧锁（`not_checked` 不得记 `agreement`）与两条行为测试充当兼容锁。**重访触发条件**：出现仓库外消费方按 schema 校验该字段，或 `cross_check_result` 获得语义消费者时，开 `trading_calendar_snapshot_v2`。
2. **pilot 证据日历腿未复验——已关闭（2026-10-13）**：EastMoney 恢复可达，`validate_training_readiness.py --independent-calendar` 对 A500 试点窗口全标记复跑仍 `passed`（23/23 会话两历一致）；归档 `training-readiness.json` 已用新载荷证据刷新（仅 certified_end/fetched_at/payload_sha256/sessions/url 五个探针字段更新，判定字段逐项不变），evidence-index 同步重钉。
3. **按设计未修**：O2（就绪门对大面积停牌的过宽报告，已有文档化限制）、O4（停牌源与 bars 同商，治理后续项）、A 的三项无法判定（§8.12 未夸大声明③）。

未夸大声明：B1、M4、P1 的变异验证，fixture 窗口核验，cr.md:343 更正，两轮全量 exit 0，P1 后两簇 except 的变异杀伤确认与重钉后两条证据测试复绿为监工亲做；各簇测试计数与证据索引全量遍历结论为代理自报（B/D 的重钉经监工抽查匹配）；C 簇修复未做监工级变异验证（12 测试为代理自报，全量两轮 exit 0 覆盖其回归）。P1 的 `_window_days` 分支在 v5 路径上由 `_verify_v4_input_sets` 接缝报 input_unresolved（而非 resolver 内的 request_invalid），属接缝既有映射，监工未改其语义。

### 8.14 R1 修复轮（2026-10-13，因子层质量口径单一化）

用户点头后落地 pendding R1（`FactorEngine.create_result` 报告 taxonomy 与发布契约不兼容）。改动半径仅因子层 + 一个发布脚本。

| 项 | 修复 | 验证 |
|---|---|---|
| 第二套 checks 口径 | `engine.py:create_result` 删除自算 checks，**委托** publish 重推导所用的 `_validate_factor_frame`；status 只跟随 reviewed 阈值，advisory 缺失列降级为 `FactorResult.warnings`（不进 durable 报告，也不再把 `accept_with_warnings` 集合降成 `warning`——那会让 engine 状态 ≠ publish 期望状态而被拒） | 正向锁 `test_factor_engine_quality_report_publishes_end_to_end`：`result.quality_report == _validate_factor_frame(...)` 且真 `publish` 成功 |
| 空帧语义 | `intent="publication"` → `rejected` + 错误消息（旧为直接抛）；`intent="dry_run"` → `empty` 不变；publish 侧对空帧仍直接抛（未放宽） | 既有的 dry-run/publication 两例分别锁 `empty`+`staging_only` 与 `ContractError` |
| 外部报告一致性校验 | policy/status/checks 三项校验保留；checks 比对由「必须非空 + 集合比对」简化为字典相等，使零 checks 的空帧报告不被误伤 | `test_external_quality_report_must_match_local_decision` 两例仍杀（status / checks 各一） |
| `null_policy` 静默丢弃 | 尊重调用方传入，缺省才回退默认 | 既有 `staging_only` 断言覆盖默认分支 |
| 发布入口 | `scripts/publish_factor_partitions.py` 改走 `FactorEngine`（`intent="publication"`、run cutoff=分区日 15:00 Asia/Shanghai），非 `passed` 带 status/errors 失败；不再 import 私有校验函数 | 真实 A500 store 全新 factor root 重发 2025-09-25~09-30：4 分区 × 498 行，零 skip/失败 |
| 治理倒退风险 | 未在发布侧放宽任何比较；R1 的 pin 换成正向锁，另加 `test_downgraded_engine_report_still_rejects_publication`（schema 合法但 status 降级的报告仍被拒）。CanonicalV2Store 的 R2 pin **故意保留** | 该文件 3 例全绿 |

**generation 是否漂移（实测，非推定）**：`result.quality_report` 在无外部报告时即校验器返回的同一文档，实测 A500 2025-09-30 分区发布报告与 `_validate_factor_frame` 输出逐字段相等（`warnings` 为空），故 R1 不改既有分区身份。

**未夸大声明**：全量套件监工亲跑 `python -m pytest tests/`= 941 passed / 24 skipped（qlib 门控本地 skip）；generation 不变仅对 2025-09-30 一个分区做了逐字段核对，其余 3 个分区按同一路径推定；真实 A500 重发为监工亲跑。R2/R3 未动。

### 8.15 R2 修复轮（2026-10-13，canonical manifest 版本收敛 + 身份判定单源）

pendding R2：`CanonicalV2Store` 写 `manifest_version: 2`，而 `ManifestFirstReader` 无条件按 `canonical-v1.json` 校验，凡由前者产出的分区在**所有层共用的受治理读路径**上直接不可读；同时未带版本的文档会静默走 v1 分支。

| 项 | 修复 | 验证 |
|---|---|---|
| 读侧版本分派 | `contracts/manifest.py:load_canonical_manifest`：**先读 `manifest_version` 再选契约**，白名单 `(1, 2)`（`bool`/缺失/其他值一律 `unsupported canonical manifest_version` 并打印支持集，无 v1 回退）；`1` 走原 `canonical-v1.json` 规则（既有分区字节不变），`2` 走 `canonical_manifest.v2.json` + `canonical_v2_identities` 重算 generation/digest/trust anchor | `tests/test_canonical_manifest_versions.py` 5 例： v1 仍可读、`CanonicalV2Store` 分区经 `ManifestFirstReader` 读出、`manifest_version: 3` 与删字段两例 fail-closed、篡改 `row_count` 触发 generation mismatch |
| 身份判定单源 | 新增 `validate_canonical_v2_manifest`（契约层）；`canonical_v2._validate_manifest` 与 `research_chain/owning_contracts.py` 的手写副本**均改为委托**（后者原先是第三份 generation/digest 手写实现，且不校验 trust anchor 等式）。`gate_contracts` 懒加载避免 manifest↔gate_contracts 模块环 | 全量套件在**加严** adjusted-price 读（多出 trust anchor 等式）后仍 945 passed / 24 skipped，说明不存在依赖旧宽松口径的既有数据 |
| 严格度差异登记 | 未把两条读路径强行合并：`ManifestFirstReader` 证身份 + data checksum + row count + 列序 + dtype + 内嵌 anchor；`read_canonical_v2` 额外要求带外 expected anchor + 绑定 quality report。差异与原因写入 `specs/layers/factor-layer-spec.md` §3.1.3，并登记命名坑：`bars_daily/v2`（schema 版本）实际活在 `manifest_version: 1` | spec 文本；两条读路径各自测试均绿 |
| pin 清理 | `tests/test_known_contract_divergences.py` 两条 pin 全部撤除（R1→正向锁、R2→上述新锁），文件删除，锁分拆为 `tests/test_factor_quality_single_source.py` / `tests/test_canonical_manifest_versions.py` | 两文件 2 + 5 例绿 |

**未修（有意，登记不隐藏）**：`src/uq/exporters/qlib.py:41`（legacy QlibExporter）把自己重读的 canonical manifest 原样嵌进导出快照 manifest，未走契约校验（帧本身已由 `ManifestFirstReader` 校验过），属 legacy 隔离面的 TOCTOU-ish 弱点，不在本轮半径；`factor-layer-implementation-plan` Phase 的「带外 expected anchor + 物理路径三元组校验」仍是未完成项。

**未夸大声明**：全量 `python -m pytest tests/` = 945 passed / 24 skipped（监工亲跑，qlib 门控本地 skip）；本轮不改任何写侧身份，故无需迁移已发布分区；生产写侧今天没有任何入口使用 `CanonicalV2Store`（grep 核实：仅 contracts 自身与 research-chain 测试引用），该结论为代码检索而非运行时枚举。R3 未动。

### 8.16 外部评审处置轮（2026-10-13，`code-review-unified-quant.md` 20 条候选逐条重验）

外部报告基线 `44a0286`。监工对本 HEAD 逐条重验（读码 + 可达性判定 + 关键项运行时复现），**每条已修项都做变异验证**：临时回退被修文件后新测试必失败，恢复后全绿。锁集中在 `tests/test_external_review_20261013.py`（14 例，报告条目 ID 写在文件 docstring）。

| ID | 判定 | 修复 | 变异验证（监工亲做） |
|---|---|---|---|
| B4 | 真问题（可达） | 回测按**日初持仓**分类 sell/buy/hold 后分批执行，取代"注释说先卖后买、实为一个字母序循环" | 回退 `backtest/engine.py` → 新测试 `AAA` 买入永不成交（`[] == ['AAA']`） |
| B6 | 真问题（可达） | 预测阶段改为**逐日期构建 + 阶段一次性发布**（state 路径按 stage 编号，循环内发布必撞不可变路径）；`run()` 变薄封装 | 回退 `research_chain/adapters.py` → 复现 `immutable research manifest already exists: .../stage=05/manifest.json` |
| B7 | 真问题（可达，低中） | `contracts/schema.py` 删除自持 `ContractError`，复用 `uq.errors.ContractError` | 回退 → 两条锁失败（类型同一性 + 可捕获性） |
| B8 | 真问题（可达） | 模板 digest 不匹配的消息改读 `payload[digest_field]`（原读 `manifest_digest_sha256` → 治理拒绝变成裸 `KeyError`） | 回退 → 锁失败 |
| B9 | 真问题（可达） | 新增单源 `contracts/paths.py::reject_symlinks_within(root, path)`：根以上不查、根以内逐分量查、解析后仍在根内；接入 `market_dataset_publisher`×4、`execution/stores`×2、`dataset_writer`、`model_layer`（有 accepted root 时）、`owning_contracts` | 变异（把守卫改回遍历祖先分量）→ 根内符号链接被漏检，锁失败 |
| B10a/b | 真问题（可达） | builder 新增显式 `initial_state`（仅运行首份分区可为真，真时豁免且由 `previous_target_weights_generation_id: null` 留痕）；`max_turnover == 0` 不再两侧都不进 | 回退 `portfolio/builder.py` → 两条 B10 锁失败 |
| B12 | 真问题（可达，低） | purge/embargo 改为先按时间排序再对相邻区间判重叠与隔离带（顺序无关；相邻满足则非相邻必然满足） | 回退 → 两条 split 锁失败 |
| B1/B2/B3/B5/B11/B13 | 真问题但**当前不可达/待裁决** | **本轮不修**，按依赖与决策条件登记（见 pendding 同名条目）：B1 需先定 risk_run `files[]` 载荷与 `de_risk/flatten` 是否算阻断；B2 是"写 label 生产者之前必须先修"的硬前置；B3 触发条件是模板改用 `declared_fill`；B5 与 B1 同批；B13 触发条件是多年回填经 SourceRouter；B11 属 metrics 语义裁决 | 不适用（未改动） |
| N1/N2/N5、N3/N4/B14 | 非缺陷 / REFUTED | 未据此改动 | 不适用 |

**证据与残余**：`evidence/paper-execution-research-integration/phase-0/evidence-index.json` 对 `model_layer.py` 的钉值按既有约定重钉（该文件被 B8/B9 两批改到，一个会话内重钉两次本身是"被钉文件是热路径"的信号，已在此记录）。B9 同类残余两点未收敛并已注释说明：`contracts/artifacts.py:228` 与未配置 root 的 `ModelContractLoader`。`code-review-unified-quant.md` 保持未跟踪（外部输入件，是否入库由用户定）。

**未夸大声明**：全量 `python -m pytest tests/` = 960 passed / 24 skipped（监工亲跑；qlib 门控本地 skip）；变异验证为监工逐条亲做；"不可达"结论来自代码检索（调用方枚举），非运行时覆盖统计。

### 8.17 7970eec..11a11c9 十提交 CR 与修复轮（2026-10-14，发布入口与契约读路径）

监工对 7970eec..11a11c9（10 提交，70 文件 +4489/−349）两路独立代理分簇审查，**全部 10 条 finding 逐条亲开锚点对 HEAD（及 R1 diff 旧侧 `git show 226370a^`）读验**，得 2 MAJOR + 3 MINOR + 5 OBS（contracts/R1R2 簇 APPROVE，无 BLOCKER/MAJOR）。用户指示修复，9 条落地，每条修复均做**变异验证**（stash 单文件回滚 → 新测试必失败 → 恢复全绿，监工亲做）。

| ID | 档 | 位置（引入提交） | 修复 | 变异验证 |
|---|---|---|---|---|
| feat-1 | MAJOR | `publish_factor_partitions.py:253-288`（44a0286） | 报告落盘改为存在性感知：盘上报告经 `read()` 篡改校验并与本次重算**逐字节比对**——一致方复用以完成中断的发布（自愈），不一致响亮失败；异常分类器收窄为仅匹配分区不可变错误，杜绝"报告已存+分区不存在"被误记 `already_published`+`covered`（旧形态对该基准日永久无分区且 headline 成功） | 回滚 → 孤儿自愈与分歧孤儿两测试双双失败（旧码复现误标） |
| feat-2 | MAJOR | `publish_universe_snapshots.py:55-78` × `index_membership.py:160-172`（9cb8a6b） | `_tiles` 增加单调性拒绝：snapshot_date 在 visible_from 序上必须严格递增（切瓦与 `as_of` 逐点一致的充要条件），混合 publish lag fail-closed | 回滚 → 混合 lag 用例以 published 落盘分叉切瓦，测试失败 |
| contracts-1 | MINOR | `publish_market_datasets.py:321`（126e462） | `--family corporate-action` 缺 `--dividend-root` 的拒绝前移至任何 family 发布之前，结构化 `configuration_failure`+exit 2；内层 SystemExit 删除 | 回滚 → 复现 :321 SystemExit，测试失败 |
| contracts-2 | MINOR | `owning_contracts.py:56`（11a11c9/R2） | 候选循环重构：JSON 解析失败仍跳过；**id 命中但校验失败 → 响亮** `invalid manifest for published adjusted price generation`，不再静默报 "unpublished" | 回滚 → 篡改用例报 unpublished，regex 不匹配，测试失败 |
| feat-3 | MINOR | `tests/test_factor_store.py:200-260`（7617362） | 注释叙述的 research-v1 过关路径改为实际执行：探针实证其撞 `QlibNotInstalledError`（ContractError 子类），锁定 schema 关卡先于任何 qlib 计算 | 探针实证（非变异）：`pyqlib==0.9.7 is required for qlib factor computation` |
| contracts-3 | OBS | `store/reader.py:32`（R2 放大） | 受治理读路径新增 manifest `partition_date` 与请求路径日期比对（两个 governed writer 恒写该字段，v1/v2 同覆盖；`read_canonical_v2` 早有同级校验） | 回滚 → 搬迁分区被当错误日期读出（DID NOT RAISE），测试失败 |
| contracts-4 | OBS | `factors/engine.py:243-246`（226370a/R1） | 恢复外部供给报告空 checks 拒绝（仅限 `quality_report is not None`；本地空帧 dry-run 路径不受影响，与旧码 `not supplied_checks` 语义对等） | 回滚 → 空 checks 外部报告被接受，测试失败 |
| contracts-5 | OBS | `factors/engine.py:191-195` | `check_results` 畸形条目（缺键/非数值 threshold）包成 ContractError，不再裸 KeyError/ValueError 逃逸治理错误通道 | 回滚 → 复现裸 `ValueError: could not convert string to float` |
| feat-5 | OBS | `publish_factor_partitions.py:141-152`（44a0286） | 定义加载后即拒表达式定义集（`qlib_expression_set`/factor `expression` 键，数据驱动非名称匹配），一句 `configuration_failure` 指明 qlib 入口，取代 N 条相同逐日失败 | 回滚 → alpha158 不再前置拒绝，测试失败 |

**未修（登记不隐藏）**：feat-4（CR#9，开放式尾瓦片无退役/取代机制——`artifacts.py:217-218` 的 `valid_to=None` 窗口短路是明写设计 "deliberately open-ended"，真正修复需请求侧陈旧检测或治理退役协议，属设计裁决非代码缺陷，待用户定夺）；contracts-5 附带项（外部报告 `observed` 字段不比对：与发布侧 `_reports_agree` 同层级，精确比对有浮点表示风险，身份与结论已由两层 (name,threshold)→result 比对 + 校验和兜住）。

**测试**：新增 10 例（孤儿自愈/分歧孤儿/alpha158 前置/混合 lag/dividend 前置/篡改响亮/v1+v2 搬迁/空 checks/畸形 threshold/非 dict manifest 留在 unpublished 分支），目标 7 文件 65/65 绿；全量 **971 passed / 23 skipped / exit 0**（基线 961+10 精确吻合）。

**提交形态**：修复本体由并行会话提交为 **`4bb29b1` 并已推送**（"fix: close nine second-pass CR findings on the R1/R2/review commits"）。该提交在监工修复基础上含两处协同增补，监工已逐行复核语义正确且更健壮：① owning_contracts 候选循环加 `isinstance(manifest, dict)` 守卫（非对象 JSON 文档留在 "unpublished" 分支而非裸 AttributeError）并配行为测试；② F5 的 qlib 缺失断言改为 `monkeypatch.setitem(sys.modules, "qlib", None)` 钉住缺失条件（四个认证 CI 环境会经 qlib extra 安装 pyqlib，采样式断言在该类环境会失效）。

**未夸大声明**：6 处源修复的变异杀伤为监工逐一亲做（stash 单文件回滚法，先于 4bb29b1 落地）；增补两处的验证依赖 4bb29b1 后全量套件（971/23/exit 0，监工亲跑），未对其单独做变异杀伤。代理执行类结论（R2 探针矩阵、23 在盘 manifest 运行时核对、17 快照实数）本轮未重跑，但与监工读验一致。修复期间 `11a11c9` 被 amend（70da6a6→11a11c9，本轮 CR 与修复均基于 amend 后版本）、外部评审 8 项 B 类修复由并行会话提交为 `cd7cf10`（其 owning_contracts.py B9 区与本轮 contracts-2 区域不重叠，git diff 证实无冲突）。`code-review-unified-quant.md` 保持未跟踪（§8.16 既定口径）。

### 8.18 latent 批次（B1/B2/B3/B5/B13）修复轮（2026-10-14，§8.16 登记的五条潜伏项）

§8.16 把 B1/B2/B3/B5/B13 判为"机制为真但当前不可达"并挂起（B1 需先裁决两件事、B2 是写 label 生产者的硬前置）。本轮按登记的条件落地这五条，工作区起点是一份**半成品**（源码已改、测试只覆盖 B13、B5 用例红），监工补齐红用例、发现并修掉该轮自己引入的一处读回回归，并为每条补上变异验证过的锁。

| ID | 判定 | 修复 | 变异验证（监工亲做） |
|---|---|---|---|
| B1 | 真问题（不可达→已闭合） | `PortfolioPublicationRiskGate.gate` 的 allow 路径原 `run_store.publish(run)` 漏传 payloads：decision/event 分区已落盘后抛 `AttributeError`，留下无 run 的半个会话。现改为 **allow/warn 之外一律拒发**（`EXECUTABLE_ACTIONS`，spec 7.3：`de_risk`/`flatten`/`halt_strategy` 在消费方定义执行契约前不可执行；`escalate_review` 及未来新增枚举同样拒），并把决策载荷钉进 run 的 `files[]` 后再算身份。`RiskRunStore.publish` 的 `payloads` 默认 `None` 改为**必填**（缺参不再落盘半个 run） | 删 `action not in EXECUTABLE_ACTIONS` 分支 → 三条 `de_risk/flatten/escalate_review` 用例 `DID NOT RAISE`；把 store 守卫退回 `payloads is None: {}` → 守卫用例改报 "count does not match" |
| B1 附带 | 该轮引入的回归 | 同轮把 portfolio 路径也写进 `files[]`，但 `read_verified_risk_run` 仍只按 order 形状读 `payloads[path]["candidate_order"]` → 合法 portfolio run 读回时裸 `KeyError: 'candidate_order'`。改为按 `run_scope` 分派：order 校验候选单血缘 + 决策体，portfolio 只校验决策体 | 把分派强制走 order 分支 → 新用例 `risk run candidate order payload missing` 失败 |
| B2 | 真问题（不可达→已闭合） | `LabelBuilder` 原**先按 eligibility 过滤再 `shift(-horizon)`**，使 horizon 变成"第 N 个幸存行"而非"第 N 个交易日"（一次停牌把 5 日收益悄悄变成 6 日），manifest 却仍声明 `horizon_trading_days`。现改为**先在完整交易行网格上算 label，再过滤**；`formula_sha256` 文本显式写明 D 计数在过滤前 | 恢复"先过滤后 shift" → 新用例 checksum 判别失败（旧/新标签向量确实不同） |
| B3 | 真问题（不可达→已闭合） | `ModelTrainer.train` 原只用 `~np.isnan(y)` 剔空标签，`inf` 或特征 NaN 会被正规方程传播成 NaN 权重，再被 artifact checksum 当作合法发布物记录。现补 `np.isfinite` 校验（标签、特征、结果权重三处） | 删两条 `isfinite` 守卫 → 新用例改报 "trained weights are not finite" |
| B5 | 真问题（不可达→已闭合） | 回测引擎在**停牌队列**（执行日无有效开盘价）上仍把 `price=0.0` 交给预交易门，门要求正价即整轮崩；停牌本不可能成交。现把停牌判定移到风控调用**之前**（`... and not is_suspended`），停牌分支照记 `skipped_suspended`。附带：某执行日全部候选皆停牌时无任何决策，`run_with_risk_gate` 返回 `run=None`（schema 要求 `decision_bindings` 至少 1，空 run 是伪造行） | 去掉 `not is_suspended` → 原缺陷复现（`candidate order execution price must be positive`），两条 B5 用例失败 |
| B13 | 真问题（不可达→已闭合） | `MootdxSourceAdapter._requested_bar_estimate` 原对任何区间都答 `page_size`，既向调用方掩盖截断、又让分页循环一页即止。现改为**返回请求的自然日跨度**：跨度大于单页即 `UNSUPPORTED_REQUEST` 拒发（不再把最近一页当作请求窗口交付）；另加**满页覆盖校验**——满页说明窗口背后还有历史，若最老一根仍晚于请求起点则拒绝 | 把估算改回 `page_size` → 跨度拒绝用例失败；`if False and saw_full_page` → 覆盖校验用例失败 |

**附带清理**：本轮改动遗留的死导入（`risk/adapter.py` 的 `json`/`file_sha256_bytes`、`risk/stores.py` 的 `file_sha256_bytes`）删除。

**测试**：新增 13 例——`test_risk_control_plane_phase2.py` 8 例（portfolio run 落盘+读回、载荷篡改拒绝、`de_risk/flatten/escalate_review` 三参数拒发、store 必填守卫、全停牌无 run、停牌出场不入门）、`test_model_labels_dataset.py` 1 例（中段不合格行不改写 horizon）、`test_model_trainer.py` 1 例（非有限特征/标签拒训）、`test_real_chain.py` 3 例（跨度超页拒、满页不达起点拒、满页达起点照常交付）。原红用例（B5 fixture）按新语义重造：用 weight 1.05 + 500bps 滑点让**只有** `order_insufficient_cash` 失败。全量 **984 passed / 23 skipped / exit 0**（基线 971+13 精确吻合；qlib 门控本地 skip）。

**未夸大声明**：6 处源修复 + 1 处回归修复的变异杀伤为监工逐一亲做（临时改单文件后新锁必失败，改回全绿），B5 的"停牌出场"变异复现了 §8.16 描述的原始崩溃。全量套件监工亲跑。B11（pooled vs 截面 IC）仍属 metrics 语义待裁决，本轮未动；`code-review-unified-quant.md` 保持未跟踪。

### 8.19 外部评审 N1 硬化（2026-10-14，PIT 窗口参数改必填）

§8.16 把外部报告 N1 判为**非缺陷**（REFUTED，"不可达的一致性缺口"是报告自身措辞，非本台账结论）；报告建议"顺手把窗口参数改必填"，本轮据此落地。

| 项 | 修复 | 变异验证（监工亲做） |
|---|---|---|
| N1 契约层 | `UniverseSnapshotStore.read_members` 的 `requested_valid_from` 由 `date \| None = None` 改为**必填** `date`，删除"回退到快照自身 `valid_from`"的默认（该默认让 `UniverseSnapshotReader.read` 的有效性守卫对忘记传窗口的调用方恒真） | 把签名与默认改回旧形 → 新锁 `test_universe_snapshot_read_members_requires_the_pit_window` 的 `TypeError` 断言失败 |
| N1 调用方 | `PortfolioStageAdapter.run` 与数据集阶段同口径传 `plan.request` 的 `window_start_date`/`window_end_date` | 把窗口改回 `universe_manifest["valid_from"]` → 新锁 `test_portfolio_stage_refuses_a_snapshot_outside_the_request_window` `DID NOT RAISE` |
| N1 附带（测试夹具） | 硬化后暴露 `tests/test_research_chain_phase4.py::_extended_plan` 的请求窗口（沿用裸 fixture 的 2025-12-01）**本就在快照有效区间（2026-01-05 起）之外**，旧默认把它掩盖成"通过"。已把该夹具窗口对齐到 2026-01-05~01-30，与 `_dataset_plan` 一致 | 不适用（夹具修正本身即证明旧默认在掩盖不一致） |

**测试**：新增 2 例（`test_factor_governance.py` 的必填参数 + 窗口外拒绝；`test_research_chain_phase4.py` 组合阶段窗口外拒绝）。全量 **986 passed / 23 skipped / exit 0**（基线 984+2 吻合；qlib 门控本地 skip）。

**未夸大声明**：N1 的两处源改动 + 一处夹具修正的变异杀伤为监工亲做；全量套件监工亲跑。N2（因子血缘完整性）、N5（可见性声明可证伪性）仍为既定口径观察项，未动。`code-review-unified-quant.md` 保持未跟踪。

### 8.20 外部评审 B11 裁决与落地（2026-10-14，IC 口径：截面为默认 / pooled 显式可选）

§8.16 把 B11（pooled IC vs 截面 IC）判为 PLAUSIBLE、需治理裁决。用户裁决：**截面 IC 为默认，pooled 作为显式可选**。落地如下。

| 项 | 修复 | 变异验证（监工亲做） |
|---|---|---|
| 口径分派 | `MetricReport` 原对 `ic`/`rank_ic` 一律做全样本单次相关（pooled）。现改为按 metric **名**分派：`ic`/`rank_ic` = 逐 `decision_date` 截面相关再取无权重均值；`ic_pooled`/`rank_ic_pooled` = 全样本单次相关（旧行为，须显式命名）。`ic` 家族不再把日间共同移动算进相关性 | 把 `ic`/`rank_ic` 的 basis 改回 pooled → **6 条新锁失败**（§8.24 复核更正：原记 4 条系少报） |
| 报告可审计 | 每条结果新增 `basis`（`cross_sectional`/`pooled`）与 `cross_section_count`，口径不再靠推断 | 同上下（basis 断言） |
| 退化截面 | 常量截面（分数或标签无变异）相关未定义：**跳过**而非让均值变 NaN；无任何可用截面则 fail-closed | 删 `nunique()<2` 跳过分支 → 2 条新锁失败（NaN / 未报错） |
| 契约 | metric `name` 在 `model_definition`/`model_definition_template` 均为自由字符串（schema 无 enum），故**无需升版**；口径约定写入 `specs/layers/model-layer-spec.md` §8.1 | 不适用 |

**测试**：`tests/test_model_definition.py::TestMetricReport` 由 1 例扩为 7 例（净 +6，默认截面 + 截面=逐日相关均值 + pooled 显式且与截面不同 + rank 变体 + 退化截面跳过 + 无可用截面 fail-closed + 未支持指标 fail-closed）。全量 **992 passed / 23 skipped / exit 0**（基线 986+6 吻合；qlib 门控本地 skip）。

**未夸大声明**：口径分派与退化截面两处源改动的变异杀伤为监工亲做；全量套件监工亲跑。`MetricReport` 目前在 src 内仍无生产调用方（仅测试），故本轮不改任何已发布制品身份。`code-review-unified-quant.md` 保持未跟踪。

### 8.21 自 CR：§8.18–§8.20 三轮提交的提交后复核（2026-10-14）

三轮修复（§8.18 latent 批次、§8.19 N1、§8.20 B11）以三个独立提交落地并推送后，对**合并后的树**做了一轮对抗自 CR（逐提交读码 + 对可触达路径做运行时探针 + 变异验证）。**发现并修复 1 处自引入回归**，其余结论为无新增缺陷。

| 项 | 结论 | 证据 |
|---|---|---|
| **B13 回归（自引入，已修）** | `_requested_bar_estimate` 被提升到 `fetch` 的 per-instrument `try` **之外**，于是畸形 ISO 日期不再走 per-instrument 的 `errors.append(...)`，而是把 `ValueError` **抛出适配器**。`fetch` 的既有契约是"请求级错误也返回 typed `FetchResult` 信封，绝不外抛"。已改为捕获该 `ValueError` 并返回 `UNSUPPORTED_REQUEST` 信封 | 变异：去掉捕获 → 新锁 `test_mootdx_malformed_dates_return_an_envelope_not_an_exception` 复现裸 `ValueError`；修复后 993 passed |
| B1 风险门 | 读码复核：`_LEGACY_BLOCKED_ACTIONS` 与 `EXECUTABLE_ACTIONS` 分派正确（block 系保留历史文案，其余非可执行动作 `not executable`）；`RiskRunStore.publish` 全部调用点已传 payloads；`read_verified_risk_run` 按 `run_scope` 分派，portfolio 载荷（决策体）与 order 载荷（候选单+决策体）各自校验 | 全量套件 + 既有篡改锁 |
| B2 标签 | `output` 的 `data_checksum` / `logical_fingerprint` 均不依赖 index，故新增的 `.reset_index(drop=True)` 不改分区身份；`row_count` 与过滤前一致 | 既有 label 锁 + 新 B2 锁 |
| B3 trainer | 三处 `isfinite` 均在权重求解前后，顺序无副作用 | 新 B3 锁 |
| B5 回测门 | 停牌判定移到门调用之前；全停牌日 `run=None` 且 `risk_run` schema 的 `minItems:1` 不被触发 | 新 B5 两锁 |
| N1 | 其余 `read_members` 调用点（`scripts/build_store_evidence.py`、`scripts/publish_universe_snapshots.py`）均为 **`IndexMembershipStore`**（另一类，签名不同），不受必填参数影响；`UniverseSnapshotStore` 仅数据集/组合两阶段 + 测试调用 | grep 调用点枚举 |
| B11 | `groupby("decision_date")` 依赖 merge 后的键列（恒存在）；metric 名非白名单值 fail-closed；`rank_ic` 先 rank 再 pearson，与 pooled 变体共用同一 `_correlate` | 新 6 锁 + 变异 |
| 提交卫生 | 三个提交的改动文件集互不重叠（仅 `cr.md`/`pendding.md` 分段落叠加）；无 `.pyc`/`data`/`evidence` 误入；提交前分别在 r1/r2 中间态各跑一次全量（984 / 986） | `git show --stat` + 中间态全量 |

**测试**：新增 1 例（mootdx 畸形日期信封）。全量 **993 passed / 23 skipped / exit 0**。

**未夸大声明**：本自 CR 的探针与变异为监工亲做；三轮各自的变异验证见 §8.18–§8.20。合并树全量监工亲跑。`code-review-unified-quant.md` 保持未跟踪。

### 8.22 b81b44a..HEAD 五提交 CR（2026-10-14，对 §8.18–§8.21 修复轮的对抗复审）

对 `10d2d0e`（B1/B2/B3/B5/B13）、`0d0df5b`（N1）、`b52f669`（B11）、`fd2048e`（信封回归）、`1e81940`（本节前的台账）做修复轮复审：监工逐行亲读全部 src diff，两个专项 agent（测试体逐条 / 台账规格溯源），全量套件监工亲跑 **993 passed / 23 skipped / exit 0**（与提交声明逐字吻合）。结论：**7 处源码修复全部验证安全，无代码级缺陷；1 MAJOR（测试判别力缺口）+ 7 MINOR + 8 OBS，均未修、逐条登记如下。**

| ID | 档 | 位置 | 发现 | 处置 |
|---|---|---|---|---|
| CR22-1 | **MAJOR** | `backtest/engine.py:257/:274/:307/:357` | 回测门 resize **消费路径**零端到端覆盖：白名单放行、缺 allowed_shares 拒、min 限幅三个分支均无测试驱动 resize 决策经 `run_with_risk_gate`；变异（白名单删 resize / 忽略 allowed_shares）全绿。代码逐行读验正确，缺的是锁 | **已修**（§8.24：3 例端到端 resize 锁；两个变异均被杀） |
| CR22-2 | MINOR | `risk/contracts.py`、`risk/adapter.py`、`backtest/engine.py` 注释 + §8.18 + `pendding.md:242` | 规格引用 "spec 7.2" 应为 **7.3**：§7.2 是 `risk_state.v1`，所引 fail-closed 规则原文在 §7.3（spec:345-346）。规则真实存在，编号错位，审计锚点断链（台账 agent 评 MAJOR，监工定 MINOR：无运行时影响，但波及 5 处含台账） | **已修**（§8.24：5 处改 7.3） |
| CR22-3 | MINOR | `pendding.md:255` | "N1 已修"与同节收尾"N1/N2/N5 是既定口径……本轮未据此改动任何东西"自相矛盾（旧轮残留句） | **已修**（§8.24：收尾句改为 N2/N5 既定、N1/B11 已落地） |
| CR22-4 | MINOR | §8.20 | "由 1 例扩为 6 例"与同行括号自列 7 项矛盾；实际 7 个测试方法（净 +6，986+6=992 总数不错），应为"扩为 7 例" | **已修**（§8.24：改"扩为 7 例（净 +6）"） |
| CR22-5 | MINOR | §8.19 | "§8.16 把 N1 判为'不可达的一致性缺口'"转述失真；§8.16 原文是"非缺陷 / REFUTED"，该措辞是外部报告自身用语 | **已修**（§8.24：改为 §8.16 判 N1 非缺陷，措辞系报告自身） |
| CR22-6 | MINOR | `backtest/engine.py:257` | 回测门下 de_risk/flatten/escalate_review 的 `skipped_risk_blocked` 无桩测试；当前连线 `RiskEngine.evaluate_order` 只产 allow/resize/block_order 故不可达，但"未知 action 一律 blocked"的修复主旨无锁 | **已修**（§8.24：桩 gate 喂 `halt_strategy`，必落 `skipped_risk_blocked`） |
| CR22-7 | MINOR | `mootdx_source.py:112-113` | "end must not precede start" 分支无测试；删该分支（负 span 放行超页检查）不红 | **已修**（§8.24：倒置区间锁；`days<=0`→`days==0` 变异被杀） |
| CR22-8 | MINOR | `tests/test_model_definition.py` | rank_ic 数值语义无判别：夹具线性使 rank==pearson，`_correlate` rank 分支（`definition.py:163`）变异不红 | **已修**（§8.24：平方分位夹具，spearman=1.0 / pearson≈0.9615；删 rank 分支变异被杀） |

**OBS（8 条，均已于 §8.25 收口）**：① `test_risk_control_plane_phase2.py:225` 恒真断言（`decision_digest == decision_digest(payloads)`，X==X；真锁是 :224 的 `failed_findings` 精确断言）；② `sample_count` 字段全仓库无断言（既有字段，非本轮新增）；③ §8.20 称 B11 变异"4 条新锁失败"，抽样复做实杀 6 例——少报（保守方向）；④ 同名 `ic` 语义变更仅靠 spec §8.1 记录，历史报告跨期可比性注意（src/scripts 无生产消费方、无 config 钉 "ic"，不冲击在管管线）；⑤ mootdx refusal 测试未断言 `retryable=False`，`_FixedPageMootdxAdapter` 复用于多页场景会掩盖分页 bug；⑥ `test_model_trainer.py:264` 测试放错类、`test_research_chain_phase4.py:525` 变量名 `dataset_adapter` 实为 portfolio 适配器；⑦ `read_verified_risk_run` 语义比对层无负向测试（字节链已篡改 evident，该层只防发布方写错内容）；⑧ trainer 训练后 "weights are not finite" 实际不可达（输入有限且 alpha>0 解必有限），该处无测试锁。

**验证安全（监工亲验）**：B1（readback 按 `run_scope` 分派覆盖 schema 枚举全部两值；`RiskRunStore.publish` 两调用方均传 payloads；读路径逐文件验 byte_size+sha256）；B2（mergesort 稳定排序先算后滤；`_formula_sha256` 仅构建时写入，无旧哈希校验方）；B3（三处 isfinite 含 inf）；B5（`is_suspended` 判定集 ⊇ 一切非正价格路径；`risk_run` schema `decision_bindings minItems:1`，空决策返回 None 是必需）；B13+fd2048e（跨度拒绝与满页回检在锚定最近交易日语义下自洽；oldest 边界双向有锁）；N1（两 stage 同口径传窗；守卫不再恒真）；B11（merged 必含 decision_date 键列；常数截面跳过与无截面 fail-closed 均为强判别；spec §8.1 与实现逐条吻合）。

**未夸大声明**：993/23/exit 0 为监工亲跑（提交态）。审查期间工作树出现并行会话的**区间外**改动（artifacts.py、model_layer.py、paths.py、runner.py + 未跟踪 `test_external_review_20261014.py`），脏树上 2 个 evidence 新鲜度测试失败——属在途新工作，不计入本节结论。变异验证：B1/B13/B11 由台账 agent 在干净克隆抽样复做；CR22-1 的变异推理由监工复核成立（未实际执行回退）；测试体 agent 复跑 7 文件 128 例全绿。两 agent 由同一份修复清单派发，**非隔离审查**；全部 MAJOR/MINOR 锚点经监工逐一亲验定级，与 agent 定级分歧处（CR22-2）已注明双方档位与理由。本轮仅评审未改码，故无"修复-变异"记录；`code-review-unified-quant.md` 保持未跟踪。

**§8.22 更正（2026-10-14）**：上段把工作树里的 4 个改动记为"区间外并行会话在途工作"，其中 2 条判断有误。`model_layer.py` / `runner.py` 的改动确是本轮 B9-B 修复的一部分；它们改动了 `evidence/paper-execution-research-integration/phase-0/evidence-index.json` 钉住的两个 sha256，pin 未同刷，**2 个 evidence 新鲜度测试红是本轮引入的、不是"区间外"**。已在 §8.23 一并修复。

### 8.23 B9 三残余修复与 B9-C 自引入洞的收口（2026-10-14）

对 §8.22 登记的三处 B9 同族残余（工作树未提交改动）做修复轮复审。**结论：三处修复方向正确，但其中 B9-C 的第一版兜底自身引入新洞（B9-D），且本轮把套件弄红。两条均已修，全量 1000 passed / 23 skipped / exit 0。**

| ID | 档 | 位置 | 发现 | 处置 |
|---|---|---|---|---|
| CR23-1 | **MAJOR** | `evidence/.../phase-0/evidence-index.json:174,186` | 本轮改了 `model_layer.py`/`runner.py`，两个 sha256 pin 未刷新 → `test_evidence_indexes_are_fresh_and_never_index_themselves`、`test_phase_0_evidence_index_hashes_are_complete` 红（本轮态 2 failed/996 passed；HEAD 态 4 failed）。刷 pin 是既有约定（cd7cf10 同文件刷过） | **已修**（刷为 53ebe423…/a57e3d5d…） |
| CR23-2 | **MAJOR** | `contracts/paths.py:43-58`（§8.22 工作树版） | "两侧拼写任一即可"兜底：`(resolved_root, resolved_path)` 命中即 `break`，而 resolve 已折叠在库链接 → 调用方预解析路径时在库软链被静默放行（运行时复现 ACCEPTED） | **已修**（恢复一致拼写要求） |
| CR23-3 | MINOR | `research_chain/runner.py:1099` | `_read_receipt` 的 loader 作用域修复无测试锁：单独回退该行全量仍绿 | **已修**（补 `test_qlib_receipt_readback_scopes_the_loader_to_the_run_store`） |
| CR23-4 | OBS | `pendding.md:248` | "B9 残余同类点…仍未修"已过时（B9-A/B/C 本轮均收口） | **已修**（改写为"已收口"） |
| CR23-5 | OBS | `tests/test_external_review_20261014.py:16` | 测试文件 docstring 仍把 B9-C 描述为"调用方解析、守卫应接受" | **已修**（改写为一致拼写口径 + B9-D 说明） |

**B9-A/B/C 修复安全（监工亲验，逐条变异）**：
- **B9-A** `contracts/artifacts.py:237` 改 `reject_symlinks_within(self.root, directory)`——回退成父目录全扫，`test_universe_snapshot_reads_through_a_symlinked_store_root` 必失败。
- **B9-B** `research_chain/runner.py:805/:1099` 传 `self.run_store.root`——两处分别回退为无 root，`test_run_state_readback_scopes_the_loader_to_the_run_store` / `test_qlib_receipt_readback_scopes_the_loader_to_the_run_store` 必失败（后者为本轮新补）。
- **B9-C** `research_chain/owning_contracts.py:95` 不再预解析——回退为 `canonical_root.resolve()`，`test_adjusted_price_dataset_reads_through_a_symlinked_store_root` 必失败。

**B9-D 修复口径**：`reject_symlinks_within` 要求 `path` 能 `relative_to(root)`（两侧同拼写：都未解析或都已解析）；"已解析路径 + 未解析根"一律 `path is outside the governed root` 拒发，不再有"走解析路径"的放行分支。负向锁：`test_reject_symlinks_within_refuses_a_resolved_path_that_hid_a_link`（复现原 ACCEPTED 形状，现必拒）、`test_reject_symlinks_within_still_refuses_a_link_inside_the_store`（在库链接、越根、`..` 逃逸三向仍拒）。口径写入 `contracts/paths.py` 模块与函数 docstring。

**测试**：`test_external_review_20261014.py` 由 5 例扩为 7 例（改 1 例口径 + 净增 2 例：B9-D 走私锁、receipt 回读锁）。全量 **1000 passed / 23 skipped / exit 0**（监工亲跑）。

**未夸大声明**：本节的变异验证由监工亲做（逐条回退源码 → 新锁必失败 → 还原）；三处 B9 源码修复经监工逐行亲读；CR23-1 的"本轮引入"结论由 stash 对照实测（HEAD 态 vs 本轮态）。`code-review-unified-quant.md` 保持未跟踪。

### 8.24 §8.22 台账项收口轮（2026-10-14，CR22-1..CR22-8 全部落地）

对 §8.22 登记的 1 MAJOR + 7 MINOR（此前全部"未修"）逐条修复。**8 条全部闭合，每条新增锁均做变异验证（回退源码 → 新锁必失败 → 还原）。全量 1006 passed / 23 skipped / exit 0。**

| ID | 修复 | 变异验证（监工亲做） |
|---|---|---|
| CR22-1（MAJOR） | `tests/test_risk_control_plane_phase2.py` 新增 3 例端到端 resize 锁：(a) 白名单放行 → 引擎消费 allowed_shares 并成交；(b) min 限幅（把引擎自身 participation 设 **高于** 风控 cap，使忽略约束会填 50,000 而非 10,000，断言可判别）；(c) `resize` 决策缺 `allowed_shares` → `ContractError` | 变异 A：`ORDER_GATE_EXECUTABLE_ACTIONS` 去掉 `resize` → 3 例失败；变异 B：买入路径 `min(shares_to_buy, risk_allowed_shares)` 改 `pass` → 2 例失败（首版夹具 participation=0.05 时风险 cap 与引擎 cap 同值，变异 B 不红——已改为 0.50 使二者分离） |
| CR22-2 | `risk/contracts.py:43`、`risk/adapter.py:117`、`backtest/engine.py:35` 注释 + `pendding.md:242` + `cr.md` §8.18 的 "spec 7.2" → **7.3**（fail-closed 规则原文在 spec §7.3 `risk_decision.v1`，行 345-347） | 不适用（纯台账/注释锚点） |
| CR22-3 | `pendding.md` 收尾句改为"N2/N5 是既定口径、N3/N4/B14 为 REFUTED，未据此改动；N1 与 B11 已按裁决落地" | 不适用 |
| CR22-4 | `cr.md` §8.20 计数改为"由 1 例扩为 7 例（净 +6…）" | 不适用 |
| CR22-5 | `cr.md` §8.19 改为"§8.16 把 N1 判为**非缺陷**（REFUTED，'不可达的一致性缺口'系报告自身措辞）" | 不适用 |
| CR22-6 | 同 CR22-1 文件新增桩 gate 用例：喂 `action="halt_strategy"`（引擎从未执行的枚举）→ 必落 `skipped_risk_blocked`、`filled_shares=0` | 该锁锁住 `engine.py:257` 的 `not in ORDER_GATE_EXECUTABLE_ACTIONS` 白名单分支（与 CR22-1 变异 A 同源） |
| CR22-7 | `tests/test_real_chain.py` 新增倒置区间锁：`start=2026-08-21, end=2026-06-01` → `UNSUPPORTED_REQUEST` + "precede" | 变异：`days <= 0` → `days == 0` → 新锁失败（返回 `EMPTY` 而非 envelope） |
| CR22-8 | `tests/test_model_definition.py` 新增 `test_rank_ic_is_rank_correlation_not_pearson`：分数=秩²（严格递增非线性），spearman=1.0、pearson≈0.961524，二者必不相等 | 变异：`definition.py:163` 的 rank 转换改 `pass` → 新锁失败（rank_ic 塌成 ic=0.9615） |

**排查记录（工装而非代码）**：CR22-7 复核时一度看到倒置区间返回 `EMPTY`、`days<=0` 分支"失效"。定位为**变异实验遗留的 `.pyc`**：回退源码时我保留了原 mtime，Python 的 mtime 缓存未失效，加载到的是变异字节码（`dis` 显示 `COMPARE_OP ==`）。清 `__pycache__` 后行为恢复、源码 `git diff` 为空——**源码自始正确**。该坑记此以备后续变异流程：**回退后须同时失效 pyc（或改 mtime）**。

**测试计数**：`test_risk_control_plane_phase2.py` +4 例（3 resize + 1 未知 action）、`test_real_chain.py` +1、`test_model_definition.py` +1 = 净 +6（1000→1006 吻合）。

**未夸大声明**：CR22-1（两个变异）、CR22-7、CR22-8 的变异杀伤为监工亲做；CR22-2..CR22-6 为台账/注释锚点或与 CR22-1 同源的白名单分支。全量套件监工亲跑。本轮不改任何已发布制品身份（无 canonical schema 语义变更、无版本升迁）。`code-review-unified-quant.md` 保持未跟踪。

### 8.25 §8.22 八条 OBS 收口轮（2026-10-14）

对 §8.22 登记的 8 条 OBS 逐条处理。**6 条补锁/纠正（均做变异验证），2 条为文档化观察（非缺陷）。全量 1008 passed / 23 skipped / exit 0。**

| ID | 原观察 | 处置 | 变异验证（监工亲做） |
|---|---|---|---|
| OBS-1 | `test_risk_control_plane_phase2.py:225` 恒真断言 `decision_digest == decision_digest(payloads)`（X==X） | **改真锁**：`decision_digest()` 助手改为**重算** `sha256_json({action, constraints, findings})`（与 `RiskEngine` 同式），断言比对发布值与重算值 | 变异：给 `RiskEngine` 的 digest payload 加一个字段 → 目标用例失败 |
| OBS-2 | `sample_count` 字段全仓无断言 | **补断言**：默认截面用例断言 `sample_count==30`；pooled 用例断言 `sample_count==18` 且与 `cross_section_count==0` 独立 | 变异：`"sample_count": len(merged)` → `0` → 2 例失败 |
| OBS-3 | §8.20 称 B11 变异杀"4 条新锁"，实为 6 | **更正台账**：§8.20 改为"**6 条新锁失败**（原记 4 条系少报）" | 抽样复做：`ic`/`rank_ic` basis 改回 pooled → 实杀 6 例，确认 |
| OBS-4 | 同名 `ic` 语义变更仅靠 spec §8.1 记录，跨期可比性需注意 | **文档化**（非缺陷）：spec §8.1 增 "Comparability note"——报告自身的 `basis` 字段为准，跨约定边界比较须比同期而非同名；并核实无 config 钉 `ic`、无生产消费方 | 不适用（检索确认唯一 `"ic"` 引用在分派表自身） |
| OBS-5 | mootdx refusal 测试未断言 `retryable=False` | **补断言**：超页跨度、倒置区间、畸形日期三条 refusal 锁各加 `result.retryable is False` | 变异：三条 refusal 的 `retryable=False` → `True` → 3 例失败 |
| OBS-6 | `test_model_trainer.py:264` 测 trainer 却放在 `TestArtifactStore`；`test_research_chain_phase4.py:525` 变量名 `dataset_adapter` 实为 portfolio 用途 | **归位/改名**：3 个 trainer 用例移入 `TestModelTrainer`；`phase4` 中仅供 `universe_store` 的适配器统一改名 `universe_adapter`（14 处引用） | 不适用（结构/命名，全量套件仍绿） |
| OBS-7 | `read_verified_risk_run` 语义比对层无负向测试（字节链已覆盖篡改，该层只防发布方写错内容） | **补负向锁**：发布**自洽**（`files[]` 摘要与文件一致）但决策体写 `block_order`、而 decision store 存 `allow` 的 run → 必须 `decision payload mismatch` 拒 | 变异：删除 `payload["decision"] != expected_decision` 分支 → 该用例失败 |
| OBS-8 | trainer "trained weights are not finite" 无测试锁（有限输入 + alpha>0 时解必有限，自然不可达） | **补直接锁**：monkeypatch `np.linalg.solve` 返回 NaN 权重 → 必须拒（该守卫是求解器变更/未来核溢出的最后防线，非死代码） | 变异：删除 `if not np.isfinite(weights).all()` 守卫 → 该用例失败 |

**关于 OBS-8 的取证**：先用 `1e154`/`1e200` 量级的有限特征实测——Gram 矩阵会溢出为 `inf`，但 `np.linalg.solve` 仍返回有限（0）权重，**自然路径确实到不了该守卫**。故采用 monkeypatch 直接驱动，并在注释中写明这是"最后防线"而非可达缺陷，避免把它误记成"已由真实输入触发"。

**测试计数**：新增 2 例（`test_non_finite_solver_output_fails_closed`、`test_risk_run_rejects_a_payload_the_decision_store_disagrees_with`）+ 就地强化 5 处断言；3 例跨类移动（计数不变）。1006→1008 吻合。

**未夸大声明**：OBS-1/2/5/7/8 的变异杀伤为监工亲做；OBS-3 为抽样复做确认；OBS-4/6 为文档化与结构性处置，无运行时语义变更。全量套件监工亲跑。本轮不改任何已发布制品身份（无 canonical schema 语义变更、无版本升迁）；`specs/layers/model-layer-spec.md` 仅增可比性说明，不动规范条款。`code-review-unified-quant.md` 保持未跟踪。

### 8.26 全A 窗口入库驱动 + 就绪门 exclusion 机制轮（2026-10-15，提交 self-CR 与替身复审）

**触发**：完成全A（中证全指 `000985.CSI`）实盘训练的前置工作——补上"整窗入库"入口、把供应商中途停发（长停牌 `suspend_d` 不标记 / 供应商空洞）的成员从 universe 里**显式、可审计**地剔除——并起一轮更短的全A 窗口做首次试跑。本轮改动：新增 `scripts/ingest_window.py`、`assess_readiness` 增 `excluded_instruments`/`excluded_provenance`、`scripts/validate_training_readiness.py` 增 `--excluded-instruments`、`evidence/a500-pilot/.../evidence-index.json` 重钉 2 条 sha256。

**本轮提交后再做对抗复审（职责分离：一名替身只读审码，一名替身做变异验证，监工复验关键项）**，发现并修复以下问题——**这些是真问题，不是台账措辞**：

| ID | 问题（复审确认） | 处置 | 变异验证（监工亲做） |
|---|---|---|---|
| CR26-1 | **`unjustified_exclusion` 守卫可被绕过**：文件里写一个"既非窗口成员、也无 bars"或"非成员但 bars 齐全"的标的，`short_all` 与 gap 集皆为空 → 判定为"有正当理由"，静默通过（实测：`excluded non-member full bars` → `passed`）。docstring 却声称"无缺口的剔除本身就是阻断项"，与实现不符 | 守卫改为**两个条件都必须证伪**才算正当：名在 `short_all` **或** 名存在 susp 无法解释的必需缺口；否则一律 `unjustified_exclusion`（blocking）。把 gap 判据提为 `_has_unexplained_gap()` 单源 | 回退守卫 → 新锁 `test_exclusion_is_reported_even_for_a_name_outside_the_window` 必失败（实测杀） |
| CR26-2 | **exclusion 报告会漏项**：`reported_excluded` 只保留 `name in members ∪ with_bars`，文件里"本窗口既不在此 universe、也无 bars"的条目**在报告里完全消失**（既无 `excluded_instruments` 也无 `unjustified_exclusion`），`metrics.instruments_excluded` 少报。docstring 声称"exclusion 永远会被报告"，与实现不符 | 报告改为 `sorted(excluded)`（**报告文件里的每一条**），并改写 detail 文案为"instruments"而非"members" | 回退为按成员过滤 → 同一条新锁必失败（实测杀） |
| CR26-3 | **两条修复分支无测试锁**：`short` 里的 `and instrument not in excluded` 过滤、`members_without_bars` 里的 `if name not in excluded` 过滤，**回退后全套仍绿**（替身变异 d/e 均存活） | 新增 `test_excluded_instrument_does_not_appear_in_the_short_history_finding`（真夹具：被剔名 5/20 会话，**保证它本会进 `insufficient_history`**，并断言同窗未被剔的短名仍 blocking）+ `test_excluded_member_without_bars_is_not_reported_as_missing_bars` | 变异 d（去掉 short 过滤）→ 杀 test3；变异 e（去掉 member_without_bars 过滤）→ 杀新锁。两者监工复做，均红 |
| CR26-4 | **一条新测试是恒真的**：原 `test_excluded_instrument_does_not_appear_in_the_short_history_finding` 的夹具给被剔名**正好 `min` 条**（`count < min` 为假），`short` 列表为空，断言在空列表上恒过，不能区分修复前后；`test_excluded_instrument_is_informational...` 首句 `"excluded_instruments" not in codes or all(informational)` 亦恒真 | 前者按 CR26-3 重写为真夹具；后者删去恒真子句，改为断言"无任何 blocking finding" | 变异 f（删掉整个 `excluded_instruments` 块）→ 原 test1 与 test4 杀；重写后 test3 另由变异 d 杀 |
| CR26-5 | **`evidence-index.json` 被手工重排**：`build_store_evidence.py` 固定键序（`layer, record_version, status, note, records`），工作副本却是 `sort_keys=True` 的字母序（`layer, note, ...`）。虽不影响 digest，但破坏了"索引由生成器产出"这一可追溯性 | **改由生成器重导**（`--kind index` + 原 14 条 `--record`、原 `--note`），键序恢复规范；diff 收敛为**仅 2 条 sha256** | 不适用（生成器产物 + 新鲜度测试绿） |
| CR26-6 | **`scripts/ingest_window.py` 多小时后半途中断会丢审计记录**：循环里 `KeyboardInterrupt`/意外异常在写 summary 之前抛出，一次在 104 个会话中第 100 个被打断的run，**"哪些会话已落盘"的记录随之丢失** | summary 写入提为 `_write_summary()`，包 `try/except BaseException`：先写 summary（`status="interrupted"`、`interrupted=true`、已落盘会话在列）再重抛；新增 `tests/test_ingest_window.py` 3 例（中断仍写 summary、重跑跳过已发布分区、日历无法认证则 fail-closed） | 实测中断路径：summary 落盘且 `published=["2025-09-01"]`，确认后重抛（监工亲跑） |

**纳入台账、本轮未改（OBS，非缺陷或需用户裁决）**：
- **OBS-A**：`unjustified` 守卫里 `name not in short_all` 这一半在数学上可达性低（`count < min` 通常蕴含必需集有缺），但它**不是恒真**——CR26-1 的"非成员/无 bars"用例正是靠它之外的 gap 分支补齐判定；保留两个析取是为了同时覆盖"短"与"有洞"两类缺陷，且新文案不再声称"有足够会话"。
- **OBS-B**：`scripts/ingest_window.py` 目前未进任何 evidence-index。它是**可重导的入口脚本**（逻辑受测、无 store 身份），按 §8.4 惯例入口脚本可在对应 pilot 证据里 pin；本轮未新增 pilot 证据目录，故未 pin。若后续把全A 试跑归档为 evidence，应连同 `ingest_window.py` 一起 pin。
- **OBS-C**：`excluded_instruments` 的 reason 只作**审计字符串**入报告，代码不解析其语义（"供应商停发"这类判据靠人工证据）。这是有意的：机器可判定的部分（短/有洞）已由守卫强制，语义真伪留给研究侧证据，避免把供应商行为硬编码进就绪门。
- **OBS-D**：`ingest_window.py` 对"分区文件存在但损坏"记 `already_published` 跳过（不做读回）。该行为继承 `CanonicalStore` 的不可变+manifest-first 契约（发布时已校验），非本轮引入；记此备忘。

**测试计数**：`tests/test_training_readiness.py` +2（净，重写 1 例、删 1 假例并补 2 真例）、`tests/test_ingest_window.py` +3 = 净 +5 → **1014 passed / 23 skipped / exit 0**（基线 1012）。

**未夸大声明**：CR26-1/2 的守卫与报告修复、CR26-5 的生成器重导、CR26-6 的中断路径为监工亲验（读码 + 运行时/变异复现）；CR26-3/4 的变异杀伤由替身亲做、监工复做关键两条（d/e 均红）。本轮不改任何已发布制品身份以外的语义（无 canonical schema 变更、无版本升迁）；`evidence-index.json` 仅 2 条 digest 随源码变更刷新。`code-review-unified-quant.md` 保持未跟踪。

### 8.27 §8.26 提交（302b228）独立复审轮（2026-10-15，监工亲审）

**触发**：用户指示对 `6ddf567..HEAD` 新提交 + 工作树未提交改动做复审（原 5 小时定时任务取消，立即执行）。实际范围：区间内仅 `302b228` 一个提交；工作树无在范围内的未提交改动（`AGENTS.md` 时间戳头、`code-review-unified-quant.md` 为既定排除项）。**方法**：全部 src/scripts diff 逐行精读（`readiness.py` 5 个 hunk 坐标 102–391、validator 3 个 hunk、`ingest_window.py` 全文 217 行、两个测试文件全部新增用例逐条变异推理）；全量套件亲跑；关键机制变异验证 ×5；evidence 重钉哈希亲算；台账声明逐条对账。

**发现（按严重度）**：无 BLOCKER、无 MAJOR。

| ID | 问题（复审确认） | 档位 | 处置建议 |
|---|---|---|---|
| CR27-1 | **台账与提交信息测试计数失真**：提交信息称"+5 net (1012 → 1017)"，§8.26 称"净 +5 → **1014** passed（基线 1012）"——三处不一致。实际：父提交 6ddf567 基线 **1008**（前轮亲跑），本提交两测试文件**纯增 9 例**（6+3，零删除），实测 **1017**。§8.26 的 1014 与本提交信息的 1017 自相矛盾；"基线 1012"在提交历史中从未存在；§8.26"+2 净（重写 1 例、删 1 假例）"描述的是提交前开发过程，与提交 diff（纯 +6 例）不符。代码实质无影响（1017 全绿为真） | MINOR（台账防失真口径） | 后续轮次以"提交父节点实测数"为准基线；本条作为既往记录保留，不回改 §8.26（历史台账只增不改） |
| CR27-2 | **`excused_excluded` 是死变量**（`readiness.py:272/:294`）：收集后从不消费，与 `excused_delisted`（产出 metric + informational finding，:297-307）不对称。suspension 源在场时报告看不出**本窗口**哪些 exclusion 实际抵消了 gap（仅有含窗口外条目的全文件级 informational finding）。**变异实证 M4a：删掉 `excused_excluded.append(...)` 后全套 44 例仍绿** | MINOR（死代码 + 可观测性缺口） | 二选一：补 `instruments_exclusion_excused` metric + informational finding（与 delisted 对齐），或删变量 |

**纳入台账、本轮未改（OBS，非缺陷或需用户裁决）**：
- **OBS-A**：exclusion 的 reason 字符串在门内只写不读（`excluded` 的 value 从未被 `readiness.py` 使用，finding 只带 provenance）；per-name reason 仅存于 validator 的 `notes`。设计可接受（报告=裁定、notes=审计），但 docstring 未点明。
- **OBS-B**：`ingest_window.py:176` 节流仅在"本次运行已成功发布 ≥1 分区"后生效——连续失败会话之间不 sleep，失败会话本身可能已消耗近百次请求，失败连击下有击穿 300/min 的残余风险。提交信息"paces sessions"略宽于实现。
- **OBS-C**：`ingest_window.py:213` 的 `return 1 if failed or interrupted` 中 `interrupted` 不可达（:202 已重抛）。防御性死条件，无害。
- **OBS-D**：驱动失败路径无测试锁：membership 失败分支（:168-175）与非 published 状态分支（:180-188)。逻辑直白，风险低。
- **OBS-E**：validator 用裸 `raise SystemExit(...)` 报 JSON 形状错误（:273-281），邻近代码风格为 `return 2`。只读校验器无副作用，风格层面。
- **OBS-F（遗留，未变）**：`QualityReportStore.read` 死祖先守卫（`artifacts.py:168-169`）、`_run_gate_once` 的 `mkdtemp` 未清理——前轮已报，仍开放，不重复计。

**验证安全清单（本轮亲验）**：
1. **守卫语义正确且 fail-closed**：非窗口成员、满面板健康成员均被 `unjustified_exclusion` 拒绝（与 docstring 一致）；"缺口全被 suspension 解释"的健康名同样不可剔（防静默缩表边界正确）。`_has_unexplained_gap` scoping 安全：`present_sessions`(:221)、`required_by_instrument`(:252) 无条件计算，`suspended_by_instrument` 来自 `suspensions or {}`(:127)，suspension 两分支均安全。
2. **变异验证 ×5（监工亲做、亲恢复、`git status` 确认树净）**：M1 守卫置空→恰杀 2 例（healthy-blocking + 窗口外报告）；M2 删 short 过滤→恰杀 `test_excluded_instrument_does_not_appear_in_the_short_history_finding`；M3 删 member_without_bars 过滤→恰杀 `test_excluded_member_without_bars_is_not_reported_as_missing_bars`；M4b 删 suspension 分支 excuse→恰杀 `test_exclusion_excuses_a_gap_the_suspension_source_cannot_explain`；M5 报告改"仅窗口内"→恰杀窗口外报告例。另 M4a 证明 CR27-2 死变量（删 append 全绿）。五条机制均有独立测试锁，无恒真残留。
3. **evidence 重钉合规**：新 sha256 与当前树两文件逐一吻合；旧 sha 在索引中 0 残留；键序恢复生成器规范序（`layer, record_version, status, note, records`）；diff 仅 2 行。
4. **`ingest_window.py` 审计属性成立**：中断（`except BaseException`）先写 summary 再重抛（已落盘会话在列）；日历重试 fail-closed（含空 sessions 显式拒绝 :123-124）；续跑跳过已发布分区（manifest+data 双件检查）；`SystemExit` 仅用于副作用前的参数/日历错误；3 个新测试锁中断/续跑/日历失败三条关键路径。
5. **§8.26 自检台账独立复核**：CR26-1..6 的机制性声明**全部复现为真**（M1/M5 复现 CR26-1/2，M2/M3 复现 CR26-3，M2 反证 CR26-4 恒真已消除，哈希+键序核对 CR26-5，读码+断言核 CR26-6）；OBS-A..D 披露属实。唯计数行失真（CR27-1）。
6. **全量套件亲跑**：**1017 passed / 23 skipped / exit 0**（与提交信息终值一致）。

**测试计数**：本轮零代码改动（纯复审）；套件维持 **1017 passed / 23 skipped**。

**未夸大声明**：5 个变异均为监工逐个编辑→跑测→`git checkout` 恢复→确认树净（最终树仅余两项既定排除文件）；全量套件、哈希计算、台账对账均亲跑亲读，未委托子代理；读码覆盖本提交全部 hunk，`readiness.py` 392–579 行不属本提交 diff 未重读（前轮已覆盖其主体）；CR27-1 的"基线 1008"依据监工前轮亲跑记录 + 本提交 diff 纯增性质，非引用台账自述。**本轮实质缺陷为零，两个 MINOR 均不阻断使用。**`code-review-unified-quant.md` 保持未跟踪。

### 8.28 §8.27 处置轮（2026-10-15，用户判定"有问题直接改并推送"）

**触发**：§8.27 报出 CR27-2（`excused_excluded` 死变量 + 可观测性缺口，MINOR）与 CR27-1（台账计数失真，MINOR）。用户裁决"有问题直接改并推送"。本轮处置两条，并补锁。

| ID | 处置 | 变异验证（监工亲做） |
|---|---|---|
| CR27-2 | **补可观测性（采纳 §8.27 建议的第一支）**：`readiness.py` 的 `excused_excluded` 不再只收集。在 suspension 源在场的 gap 循环后新增 `instruments_exclusion_excused` metric + informational finding `excluded_instruments_excused`，与 `excused_delisted`/`delisted_members_excluded` 严格同形。它与文件级 `excluded_instruments`（全文件）**分开**，所以陈旧条目不能躲在活跃条目后面；只有"确实抵消了本窗口一条 susp 无法解释的必需会话"的剔除才会计入。`test_exclusion_excuses_a_gap_the_suspension_source_cannot_explain` 就地强化断言（finding 内容 + metric==1.0），并新增 `test_a_healthy_exclusion_does_not_claim_to_have_excused_a_gap` 负向锁（健康剔除不得出现该 finding/metric，且必须仍 `unjustified_exclusion`）。 | **M4a（§8.27 的原变异）复做**：删 `excused_excluded.append(...)` → 强化后的用例必失败（`assert ([])`）。修复前同变异全套 44 例绿，现必红——死变量已转活并上锁。负向锁由 `unjustified_exclusion` 分支独立覆盖。 |
| CR27-1 | **不回改历史台账**（采纳 §8.27 处置）：§8.26 与提交信息 302b228 的计数按"历史台账只增不改"保留；本条记录口径纠正供后续轮次遵循——**基线一律取"提交父节点实测数"**。核对确认：`test_training_readiness.py` 35→41（+6）、`test_ingest_window.py` 0→3（+3）= 父提交 6ddf567 到 302b228 **净 +9**，终值 1017；§8.26 的"1014/基线 1012"为提交前开发过程数（含未提交态），与提交 diff 不符。 | 不适用（台账） |

**本轮新增/变更**：`src/uq/pipeline/readiness.py`（+20 行：excused finding 块）、`tests/test_training_readiness.py`（就地强化 1 例 + 新增 1 例）、`evidence/a500-pilot/2025-08-29-to-2025-09-30/evidence-index.json`（`readiness.py` 单条 sha256 随源码刷新，生成器重导）。

**真实窗复跑（全A 21 会话,csi-all,5068 成员,`--min-sessions 15` + 10 条 exclusion）**:`passed`,findings = `delisted_members_excluded`(1) + `excluded_instruments`(10) + `excluded_instruments_excused`(1),`instruments_exclusion_excused=1.0` —— 报告现在指出 10 条剔除里**哪 1 条**真正抵消了缺口。

**测试计数（以父节点 302b228 实测为基线，遵循 CR27-1 口径）**：父节点 **1017**（监工亲跑）→ 本轮 **1018**，净 **+1**（就地强化不增计数,新增 1 例）。`exit 0`。

**未夸大声明**：CR27-2 的变更为监工亲做，M4a 变异亲跑亲恢复（`cmp` 确认字节还原）；全量套件、真实窗复跑、evidence 重钉哈希均亲跑。本轮不改任何已发布制品的身份语义（无 canonical schema 变更、无版本升迁）；`evidence-index.json` 仅 1 条 digest 随源码刷新。`code-review-unified-quant.md` 保持未跟踪。

### 8.29 三项收口轮（2026-10-15，用户裁决后落地；调研 + 治理裁决 + 实现）

**触发**：§8.28 之后用户对三项待决拍板——①尾段问题「业界怎么做」派调研；②「在 v2 上重新 reviewed」；③「alpha158 在 v2 上另开 reviewed 版本，一并做」。本轮全部落地并全量测试。多代理工具本轮不可用，调研由主线直接执行（源码 + 文献）。

**Q1 调研结论（决定尾段口径）**
- **Qlib（微软）**：`Alpha158`/`Alpha360` 的默认学习流水线首处理器即 `DropnaLabel`，实现 `df.dropna(subset=['label'])`（`qlib/data/dataset/processor.py:105`）；`is_for_infer()` 注释「按标签丢样本，故不可用于推理」——**标签缺失即丢样本**，无任何"为已退出 universe 的标的补取"步骤；`QlibDataLoader.filter_pipe` 默认 `None`（`loader.py:215`）即无成分股退出过滤。
- **CRSP/Wermers + Shumway (1997)**：口径是"持有至退市事件、把退市收益计入最后一段"；Shumway 在退市原因缺失时给该样本赋 **−30%** 月收益（≈−100% 的月频率 1/4）而非丢弃。动机即"丢弃末段观测会引入偏差"。
- **两者不冲突**：约束是"末段收益必须有"，分歧只在**谁提供末段价格**。Tushare 有这 5 只全部 42 个会话，故 (A) 数据上可行，代价是让入库偏离逐日 PIT 契约。
- **裁决**：`--tail-sessions N` 默认 **0**（严守 PIT），显式 N 时扩窗并在 summary 留痕。

**Q2 复权线 reviewed（用户：质量规则"够"）**
- 新现状：`bars_adjusted/adjusted-v1` **连 schema 都不存在**（老线从来不可用、无已发布分区需迁移）→ 按"语义变更开新版本"新增 `config/schemas/bars_adjusted.adjusted-v2.yaml`，字段/单位/不变量从 `bars_daily/v2` 逐字继承，唯一语义变更 = 四价格列 `adjustment: raw` → `back_adjusted`；v1 未动。
- 生产者 `src/uq/pipeline/adjusted_price.py`（该线唯一生产者）。三条强制规则按用户确认落地：①`adj_factor` 存在且为正；②复权价可由 `raw × adj_factor` 重算复现（容差 1e-3）；③复权价非零非 NaN。
- **关键设计（本轮最重要）**：`publish_adjusted_partition` 要求**必填** `expected_restated`。"自己 restate 再校验自己"是恒等，会给任何输入盖上 `reject_all`/`passed` —— 没有独立复权源可校验时**拒绝发布**。quality report 的 check 名受 `QualityReportStore._validate_check_taxonomy` 封闭 taxonomy 限制，故三项检查经 `finite_values__*` 槽位落名。

**Q3 alpha158 新版本 + exclusions 归档**
- `config/factor-sets/alpha158-v2.json`：`set_definition_version=3`、`factor_version=2.0.0`、`input_bindings` → `bars_daily/v2`；157 个因子的表达式/指纹/`minimum_history`/排除项 `VWAP0`/`reviewed_engine_versions: [0.9.7]` **逐字继承**。**`required_columns` 保持 `datetime`**（输入线更名由 `inputs.py` 投影处理，qlib adapter 与 `_validate_factor_frame` 均要求 `datetime`；以已 reviewed 的 `basic-v2` 为同一口径）。v1 未动；`factor_set.v3.json` 是封闭 schema，故变更说明只入台账。
- exclusions 归档到 `evidence/csi-all-pilot/2025-09-09-to-2025-10-15/`（外发文件扁平 instrument→reason、元数据分文件、真跑报告），provenance 由 `/tmp` 变仓库内路径。

**本轮自查发现并修复的实现缺陷（非用户要求，但属必改）**
| ID | 缺陷 | 处置 |
|---|---|---|
| CR29-1 | `verify_restatement` 用 `reindex(raw.index)` 只对齐 label、未对齐行列，A 标的与 B 标的错位比较（实测 1% 价格漂移可被判**通过**——跨标的错配正是该检查要防的） | `reindex(...).loc[raw.index]` + 行集合不一致即拒 |
| CR29-2 | producer 原设计"restate 后退自己的产出去校验"= 恒等，质量门形同虚设 | 改为要求独立 `expected_restated`，缺失即拒发 |
| CR29-3 | `set(_PRICE_COLUMNS) \| {"adj_factor"} - set(frame.columns)` 优先级错（`-` 先于 `\|`），恒报缺列 | 加括号 |
| CR29-4 | alpha158-v2 初版误把 `required_columns[1]` 改成 `session_date`（会使投影找不到列、qlib 缺 `datetime`） | 以 `basic-v2` 为准改回 `datetime`；锁 `assert required_columns[1] == "datetime"` |

**开放**（已入 pendding）：`adjusted_price` 的**独立复权源**尚无生产提供者（producer 已 fail-closed，真发布需定第二个口径从哪来——dividend 事件链在库但 importer 见 pendding 6 缺口）；`label_set` 仍无生产发布器。

**测试计数（以父节点 a61cb1d 实测为基线）**：父节点 **1018** → 本轮 **1032**（净 +14：`test_ingest_window` +1、`test_adjusted_price_pipeline` +10、`test_factor_set_input_line_v2` +2、`test_qlib_adapter` +1）。全量 **1032 passed / 23 skipped / exit 0**。

**未夸大声明**：本轮全部源码改动、三处治理新文件、四轮实现缺陷自查由主线亲做；Qlib 结论来自直接读取其源码（`processor.py`、`loader.py`），Shumway −30% 来自二次文献转述（未读原文）。多代理工具不可用（`unsupported call`），故未派 subagent；CR29-1/2 的缺陷由我在写测试时实测暴露并复验。本轮不改任何已发布制品身份（无 canonical schema 语义变更既有分区迁移、alpha158 v1 与 adjusted-v1 冻结未动）。`code-review-unified-quant.md` 保持未跟踪。
