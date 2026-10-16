# A500 训练数据接入待办(当前有效版本,2026-10-11)

决策:
- 成分来源:Tushare `index_weight(000510.SH)`,月度近似(非严格 PIT)。取数用当月第一天至最后一天。
- 扩展:已支持中证全指 `000985.CSI`(`scripts/fetch_index_membership.py --preset csi-all`),作为全 A 近似;成员数不固定(约 5000),不做数量校验。北交所统一使用后缀 `.XBSE`(Tushare `.BJ`),沪深仍为 `.XSHG`/`.XSHE`。
- 不实现事件表,不实现 v1 月度快照的单窗口读取。
- 成分取数结果落盘为不可变 raw artifact,记录 `source_fetched_at` 与 SHA-256,训练只读该 artifact(不在训练时实时查询 Tushare)。
- 详细生效规则与限制见下文"A500 成分:月度近似方案"。

状态(2026-10-12):本清单 1–9 项**全部关闭**。剩余只有两条前瞻性说明,都不在当前关键路径上:
- 财务数据 PIT(见下方"Tushare PIT 能力边界"的待办):等真正接入财务报表时再做 `f_ann_date` 样例与 `update_flag` 语义确认。
- 月度近似的升级路径(见"A500 成分:月度近似方案"):只有在需要严格逐日 PIT 时才另寻公告日/生效日数据源。

待办(按依赖顺序):
- [x] 1. 实测 `index_weight` 发布滞后,确定 `publish_lag_days`。
  - 结论(2026-10-12):`index_weight` **没有任何可见时间字段**,且只返回每月月末一个 `trade_date` 的快照,因此滞后无法事后回溯测量,只能**向前观测**。已实现可复跑探针 `scripts/probe_index_weight_visibility.py`(逐日运行会把 `first_seen`/`content_changed` 事件追加到 `evidence/reference-data/index-weight-visibility-log.jsonl`,并打印每个快照的会话数上界),首条观测:2026-10-12 查询时最新可得快照为 `2026-09-30`、当月(10 月)快照不可得 → 该快照的可见滞后 `<= 3` 个会话;且**当月快照在月内不可得**,证明快照只在月末之后才发布。注意:只有"正在观测中"的那个最新快照上界才有信息量,更早的快照因为起观测时早已发布,上界会偏大(首个观测中 2026-06-30 的上界是 68 个会话,属于这类)。
  - 决定:`PUBLISH_LAG_DAYS = 5` 保留,但性质从"未实测假设"升级为"高于已实测上界的保守值"——5 > 3,只会把快照的可用时间推迟、绝不会提前暴露,故不会引入前视;注释与证据路径已写入 `scripts/fetch_index_membership.py`。若未来需要更紧的界,继续累积该探针观测即可逐月收紧。
- [x] 2. 定义月度快照 raw artifact 与 manifest 契约(主键 `(snapshot_date, instrument)`,记录 `source_fetched_at`、SHA-256、`publish_lag_days`、`visible_from`)。
- [x] 3. 改造 `scripts/fetch_index_membership.py`:写入不可变快照 artifact(经 `IndexMembershipStore`),不再输出 CSV 作为训练输入。脚本与测试已完成;正式 store 为 `data/a500-store/`,待用该路径正式拉取后勾选。
- [x] 9. 北交所 instrument 支持:新增 `.XBSE` 后缀(映射、`StaticUniverseLoader` 正则、`index_membership_row.v1` 正则),并补测试。`adjustment_snapshot`/`golden_provenance` 已发布契约未改动。
  - [x] 9a. lifecycle 的北交所名单已接入(2026-10-12):`AkShareLifecycleProvider` 新增 `tushare_bse` 辅助源,经 Tushare `stock_basic(exchange=BSE)` 取北交所现挂名单(实测 2000 积分可用:349 只、全部带 `list_date`,当前代码前缀全为 `92`)。分类规则仍保守:名单里有 `list_date` 且缺口早于上市日 → `not_listed_expected_missing`;名单内缺口、无 `list_date`、名单外(退市与未上市不可区分,无北交所退市源)、非 43/83/87/92 前缀 → 一律 `unknown_requires_review` fail-closed;`research-v1` 的 `lifecycle_auxiliary_sources` 已加入 `tushare_bse`(仅对 `.XBSE` 查询)。测试 `tests/test_real_data_policy.py` 新增 10 例(含 AkShare 故障时北交所名单仍独立可用)。
- [x] 4. `as_of` 查询接入 research_chain(拆分为子步骤,按序实施):
  - 4a. [已完成] 绑定模型(已决定:快照集合绑定,方案 1;基础模块 `build_membership_set` / `verify_membership_set` 已实现并测试,尚未接入 resolver):
    - 新增绑定类型 `index_membership_set`,字段:`universe_id`、`index_code`、`window_start`、`window_end`、`snapshots`(按 `snapshot_date` 升序的列表,每项含 `snapshot_date`、`visible_from`、`generation_id`、`manifest_digest_sha256`、`members_checksum_sha256`)、`set_digest_sha256`。
    - `set_digest_sha256` = canonical JSON 的 SHA-256,覆盖 `snapshots` 全部字段与窗口;任何快照增删或内容变化都会改变集合 digest。
    - 窗口覆盖校验:窗口内每个交易日必须存在 `visible_from <= 该日` 的快照,否则拒绝(fail-closed)。
    - 旧 `universe_snapshot` v1 绑定保持不变,新旧绑定通过 `family` 区分,不互相替换。
  - 4b. [已完成:绑定校验已接入 resolver;按日过滤函数已实现并有测试,待 v4 执行路径开启后挂载到 dataset 阶段] `src/uq/research_chain/membership_input.py`:`verify_request_input_sets` 由 store 重建两类集合并与请求摘要比对;`filtered_factor_frame` 对每个交易日取该日可见成员(每个快照只读一次成员)并与当日因子分区求交。
  - 4c. [已完成 2026-10-12] 执行路径挂载与读回复核。v3/v4 运行时仍按设计 fail-closed 拒绝;真正可执行的版本是 **v5**(= v4 + paper execution 模板),因此按 v5 收口:
    - resolver:`resolve()` 现在对 `research_run_request_v5` 也跑集合摘要校验(抽成 `_verify_contract_version_inputs`,v4/v5 同一分支)。此前 v5 只校验 paper 绑定、**不校验** required 的 `index_membership_binding`/`factor_partition_set_binding`,等于漏掉 PIT 成员检查。
    - dataset 阶段:`DatasetStageAdapter._read_factor_frame` 抽成独立方法;v4/v5 一律走 `filtered_factor_frame`,且**缺治理输入直接报错**(此前 membership_store 为 None 会静默退回原始因子分区,会放进当日不在指数内的标的)。
    - runner:`_reconcile` 在发布结果前调用 `_reverify_input_sets`,对 v4/v5 用 owning store 重新推导两类集合摘要;resolve 之后若 store 被改动,run 直接失败,不再发布与请求血缘不符的结果。输入不可变,故一次复核即覆盖每个阶段的读回。
    - CLI:`uq-research-run` 新增 `--membership-root` 与 `--market-dataset-root`(必须成对);治理交易日历不再来自厂商实时日历,而是从已发布的 `trading_calendar_snapshot_v1` 家族(`MarketDatasetPublisher`)按研究窗口取会话(`_governed_trading_days`,无覆盖即 fail-closed),并把它同时交给 dataset 阶段与 resolver。
    - 测试:`tests/test_research_membership_input.py` 新增 7 项(v5 分派校验、v5 缺治理即拒、读回复核拒篡改、v1 跳过复核、dataset 阶段消费过滤帧、v4/v5 缺治理即拒、治理日历派生与 fail-closed、CLI runner 接线)。
    - 接线状态更正(2026-10-12 修复轮,原文两条断言均已过时):CLI `_dry_run` 已按 contract version 路由到 v1..v5 各自 schema(`cli.py:140-144`),Phase 0.5 家族已有生产发布脚本 `scripts/publish_market_datasets.py`。本批 CR 发现的真问题是 v5 EXECUTE 全死:resolver 漏导入 `research_stage_plan_v5_sha256`,任何 v5 `resolve()` 直接 NameError(cr.md §8.12 B1)。修复轮已补导入并把原 vacuous 测试重写为真实 store 端到端 resolve(变异验证:删导入即复现 NameError);模板四大 paper 绑定现于三处强制——resolver(族比对+绑定窗口覆盖请求窗口)、模板 schema(逐字段 family const)、执行侧(adapter 发布任何 artifact 前复核 store 与模板一致)。剩余边界:v5 CLI execute 在真实数据面上的端到端跑通尚未实测(测试/库调用已覆盖);真实跑通需先用发布脚本按请求窗口备齐 Phase 0.5 家族与 membership/factor 治理输入。
  - 4d. [已完成] `tests/test_research_membership_input.py`(11 项):跨月窗口逐日成员、快照日当天不可见、成员字节篡改、两个集合摘要漂移、窗口早于首个可见快照 fail closed、绑定窗口与请求窗口不一致、resolver 治理参数缺失、v1/v3 解析不受影响。
  - 不变量:任何训练窗口内的每个交易日,成员只能来自 `visible_from <= 该交易日` 的快照。
- [x] 5. 训练前覆盖校验 fail-closed(成员数 500、Alpha158 每只至少 60 个交易日、5 日 label 需未来数据、adj_factor/涨跌停/停牌/上市退市字段缺失率)。
  - 已实测(2026-10-11,`scripts/probe_reference_data_coverage.py`,中证全指约 5100 只,3 个抽样交易日,证据 `evidence/reference-data/csi-all-coverage-probe.json`):`adj_factor` 与 `stk_limit` 整市场按 trade_date 取,成员覆盖率 1.0,无非正/空因子;`suspend_d` 与 `dividend`(按 ann_date)为事件型,每日 4~22 只停牌、分红 1~217 条,低覆盖属预期而非缺陷。
  - 已做(2026-10-12):`src/uq/pipeline/readiness.py`(纯函数,9+2 项规则测试)+ `scripts/validate_training_readiness.py`(退出码 0/1/2)。规则:会话越出窗口、成员快照按日不可见、bars 会话缺口、universe 成员无 bars、单标的会话数下限(默认 60)、必需字段空值率、`adj_factor` 非正、价格非正、交易日缺 `limit_up/limit_down`、label 前视窗口(`usable_label_end`)。bars 经 `ManifestFirstReader` manifest-first 读取,成员经 `IndexMembershipStore.as_of` 取当日可见快照。
  - 真实数据验证(2026-08-03~08-09,`data/bars-store` 3 只 + `data/csi-all-store`):5 个会话、5129 成员、全字段空值率 0、`adj_factor` 与既有 golden 官方累计因子一致(000001=139.008、600000=17.3774);唯一阻断项为 5126 只成员无 bars(仅入库 3 只),即预期的 fail-closed。
  - 已做(2026-10-12):停牌与退市已进入就绪门。`suspend_d` 使缺口区分三态(全窗停牌 / 可交易日数不足 / 无解释缺口,另有 `suspension_bar_conflict`),`stock_basic(list_status=D)` 把整窗零日线且已退市的成员降为 informational。限制:2000 积分取不到 `out_date`,故退市豁免只覆盖「整窗无日线」;停牌记录本身也可能滞后于真实停牌日(实测 601989 的 `suspend_d` 只到 2025-09-04)。
  - 已做(2026-10-12):因子/label 联动断言。`assess_factor_label_linkage`(`src/uq/pipeline/readiness.py`)对声明的 factor 分区会话做 fail-closed 校验:分区不得越出窗口、必须覆盖窗口首个交易日、且**不得读到 `usable_label_end` 之后**(那些决策日的 5 日 label 还不完整);空 factor 层单列 `no_factor_sessions`。窗口不足 label horizon 时该规则静默,避免与 `label_horizon_unsupported` 重复报。`scripts/validate_training_readiness.py` 新增 `--factor-partition-set`(接受 `factor_partition_set` 绑定 JSON 或裸日期列表),新增 metrics `factor_sessions` / `label_only_sessions`。真实 store 实测(A500 2025-08-29~09-30):factor 会话止于 2025-09-23 → `passed`;含其后 5 个会话 → `blocked`(`factor_reads_past_label_end`),`factor_sessions=23, label_only_sessions=5`。测试 `tests/test_training_readiness.py` 15→27 项。
  - 注意:交易日历仍来自 Tushare `trade_cal`(与主源同商),`bar_session_gap` 只证明内部一致性。
- [x] 6. 数据面补齐:`adj_factor`、`stock_basic`、`suspend_d`、`stk_limit`、`dividend` 的 importer,落为可校验 artifact。
  - 状态(2026-10-12,本项 importer 交付已闭环):五个端点的 importer 全部落地——`adj_factor`/`stk_limit`/`suspend_d` 随 bars v2 链路发布,`stock_basic` 用于退市判定,`dividend` 由独立、不可变的 `DividendEventStore` 发布;`dividend → XdxrAdjustmentDeriver → AdjustmentSnapshotStore` 复权生产链路也已打通并交叉校验。勾选仍留空,只因下方还有 2 条**独立范围**的后续项(v2 独立交易日历;`adjustment_snapshot`/`golden_provenance` 的 XBSE 正则需按语义变更开新版本),它们不是 importer 的欠账。
  - 修缺陷(2026-10-12,严重):`TushareAdapter.fetch` 允许 `daily` 的某个批次(50 只)静默返回空,整批标的丢失仍按 SUCCESS 发布。实测 A500 全量窗口 23 个分区中,2025-09-10 少 49 只、2025-09-19 少 50 只(恰为一个批次),分区状态均为 `published`;这些数据直到训练前就绪门才被查出。上游数据完整(同日重查 49/49 全部可得),属我方缺陷。现已由 `_daily_batch` 处理:非空批次返回空→重试一次→仍空则报可重试 `UPSTREAM_ERROR`,不再发布短面板。修复后重灌 23/23 分区,行数只剩 498/499(停牌所致),就绪门的批次缺口全部消失。
  - 附带修正:适配器错误路径此前一律回报 `schema_version="research-v1"`,v2 请求失败时会被 router 判成信封不符(`primary_source_unsupported_request`),掩盖真实的上游错误类型;现改为回报请求实际推导出的版本。
  - 说明:v2 请求中"标的无日线但 suspend_d 未覆盖"仅记为 warning,不在适配器阻断。原因实测:601989(中国重工,后续吸收合并退市)自 2025-09-04 起不再出现在 `suspend_d`,但持续停牌,若在此阻断会误拒 23 个会话中的 18 个。可解释/不可解释由 `src/uq/pipeline/readiness.py` 在全窗口上下文判定。
  - 已做:`TushareAdapter` 新增 `_call` 统一错误映射与 `adj_factor`/`stk_limit`/`suspension`/`dividend`/`stock_basic` 端点(实测 2000 积分均可用)。
  - 已做(决定:复用 `config/datasets` 契约,不新造存储):`TushareAdapter.fetch` 支持 `bars_daily` v2 字段集(`bars_daily.v2` 早已定义 `status`/`limit_up`/`limit_down`/`adj_factor`),按交易日拉整市场 `adj_factor`/`stk_limit`/`suspend_d` 并合并;`config/datasets/bars_daily.v2.yaml` 的 tushare `provides` 已补齐这些字段。实测 2026-08-05 三所标的(含 `920438.XBSE`)字段齐全,北交所涨跌停为 ±30%,证明必须用 `stk_limit` 而非硬编码 ±10%。
  - 已做(2026-10-12):除权除息披露的生产 importer。实测 Tushare `dividend` 不是稳定主键事件表:同一 `(ts_code, end_date, div_proc)` 会被原地修订(实测 `cash_div_tax` 0→0.02),且存在完全重复行;因此 `src/uq/contracts/dividend_events.py` 以**公告日为分区键、规范化整行的 canonical SHA-256 为 `record_id`** 存原始披露记录——字节级重复收敛为一条,内容修订各留一条,`ex_date` 只作为字段不参与分区(无前视)。契约 `config/schemas/contracts/dividend_event_partition.v1.json`;`scripts/fetch_dividend_events.py` 逐公告日拉取并发布(不可变、可断点续跑)。实测 A500 窗口 2025-08-29~09-30:28 个公告日、3337 条记录(仅 2025-08-29 就有 1714 行→1710 条,4 条完全重复被收敛),证据清单 `evidence/reference-data/dividend-events-2025-08-29-to-2025-09-30.json`(由 `scripts/build_store_evidence.py --kind dividend` 复读生成);无负值/`ex_date<ann_date` 违例。注意:该 store 是原始披露记录,**不会**自行推断 `ex_date` 事件分区,`XdxrAdjustmentDeriver` 仍是唯一的复权因子推导入口(目前仍仅被测试调用,尚未接生产调整链路)。
  - 已做(2026-10-12):dividend → 复权生产链路。`src/uq/market/adjustment_chain.py` 完成 `DividendEventStore(公告记录) → implemented_events(映射) → XdxrAdjustmentDeriver(adj_factor.exchange_v2) → AdjustmentSnapshotStore(不可变快照)` 的全链路。映射规则:仅取 `div_proc=实施` 且 `ex_date` 落在窗口内的记录;现金用 `cash_div_tax`(税前,与交易所公式口径一致),为空 fail-closed 不回退税后;**实测确认 Tushare `cash_div/stk_div` 为每股口径**(CATL 2022 年度 每股 2.52 现金 + 0.8 转增 复现官方 2023-04-26 因子比例 0.55214),映射时 ×10;同一 ex-date 有金额互斥的多个实施修订 → fail-closed;字节级重复已在 store 层收敛。配股(`peigu`)不在 Tushare `dividend` 端点内,置 0 并在 provenance 声明。每个派生结果与 canonical bars 里的官方累计 `adj_factor` 交叉校验:`derived[t] = official[t]/official[last]`,容差 1e-3(实测 A500 窗口最差 8.2e-4,折合 0.0021 元/股,源于交易所参考价 0.01 元分辨率,已写入 `price_scale_gap_cny` 让评审能看现金量级而非裸因子数)。`scripts/build_dividend_adjustments.py` 批量派生并发布快照,输出审查报告(含逐标的 generation/event lineage/provider_check),有拒绝即 exit 1。
  - 真实数据验证(2025-08-29~09-30):A500 窗口内 219 条实施披露 → 29 只标的 → 29/29 快照发布、0 拒绝,`max_abs_diff=8.2e-4`(601866),全部在 0.01 元/股分辨率内。修复顺带发现并修复的缺陷:`AdjustmentSnapshotReader.select_visible_generation` 原要求 sessions 覆盖**每个自然日**,与 deriver 的交易日语义冲突(窗口含周末必然失败),改为校验"严格递增且首尾与声明窗口一致"(save 侧同口径);`AdjustmentDerivation` 补 `effective_dates` 字段,producer 直接用 deriver 自己的表落盘,不再重算导致 checksum 漂移。证据:`evidence/reference-data/adjustment-chain-2025-08-29-to-2025-09-30.json`(sha256 `45680b409cb8086316b42c6565905ec112b0508214d6b4ed6e9d49f688d590bb`)。
  - 已做:`build_tushare_v2_pipeline`(`src/uq/pipeline/daily.py`)接入 v2 日线链路。因 `status`/`limit_*`/`adj_factor` 目前只有 Tushare 能提供,v2 的主源改为 tushare、tdx 降级为 close/volume 对照源(与 research-v1 相反);`config/datasets/bars_daily.v2.yaml` 的 tdx `provides` 相应收窄(不再声明 `status`/`adj_factor`),`MootdxSourceAdapter` 现按请求字段返回 `session_date`(v2)或 `datetime`(research-v1)。
  - 修缺陷:`config/schemas/bars_daily.v2.yaml` 的 instrument `pattern` 原写作 `'...\\.(XSHG|XSHE)$'`,YAML 单引号内是字面双反斜杠,正则等价于"反斜杠+任意字符",会拒绝全部真实代码;已改为 `'^[0-9]{6}\.(XSHG|XSHE|XBSE)$'`(与 `index_membership_row.v1` 一致,含北交所)。
  - 修缺陷:三个 pipeline builder 的默认 `project_root` 取 `Path(__file__).parents[2]`,而 `daily.py` 位于 `src/uq/pipeline/` 下三层,实际解析到 `<repo>/src`;此前只因 CLI 显式传 `--project-root` 未暴露。已改为 `parents[3]`。
  - 修缺陷:对照源若交付的帧缺少契约主键列(例如仍按 `datetime` 键),原实现会在 merge/verified-row 处 `KeyError`;现以 `comparator_key_missing` fail closed。
  - 已做(2026-10-12,v2 独立交易日历):`build_tushare_v2_pipeline` 默认给路由挂上厂商无关的会话日历 `EastMoneySessionProvider`(`src/uq/adapters/eastmoney.py`,provenance `eastmoney_index_kline_v1`,会话取自沪深300指数日K),此前 `router.calendar is None` 导致覆盖率恒为 `unverified`。实测(2025-09-02,真实 Tushare + 真实 EastMoney):`status=published`、无 `route coverage is unverified` 警告,报告 provenance 记录 `calendar` 与 `calendar_evidence`(`payload_sha256`、认证区间 `2024-07-29..2026-10-12`)。provider 是 fail-closed 的:`IndependentCalendarError`(`status="upstream_error"`,`retryable=True`)覆盖不可达/非 JSON/无 klines/**窗口超出已发布的最新会话**(空集绝不能被当成"无交易日",否则 `observed_dates >= expected_dates` 会静默成立);`independent_calendar=False` 或显式注入 `calendar=` 可分别退回旧行为/测试注入。测试 `tests/test_eastmoney_calendar.py`(8)+ `tests/test_pipeline_v2.py`(6)。
  - 已做(2026-10-12,就绪门交叉校验):`scripts/validate_training_readiness.py --independent-calendar` 用同一 provider 复核窗口内每个会话,与 Tushare `trade_cal` 不一致即退出码 2;报告 `notes` 现在还记录 `min_sessions_per_instrument`/`label_horizon_sessions`/`max_null_rate`/`skip_*` 等阈值(此前只写在 pilot-record 的 reproduction 里)。实测重跑 A500 2025-08-29~09-30(`--min-sessions 17 --independent-calendar`):仍 `passed`,仅 `delisted_members_excluded` informational,23/23 会话两历一致,归档 `evidence/a500-pilot/.../training-readiness.json` 已刷新,`pilot-record.json` 的 limitation 相应改写(不再声称"同商日历是唯一依据")。
  - 已做(2026-10-12,XBSE 正则新版本):`adjustment_snapshot` 与 `adjustment_golden_provenance` 新增 v3(`config/schemas/contracts/*.v3.json`),instrument 正则放宽为 `^[0-9]{6}\.(XSHG|XSHE|XBSE)$`(与 `index_membership_row.v1` 一致);v1/v2 保持原样且仍可读,v2 继续拒绝 `.XBSE`。写侧 `LATEST_SNAPSHOT_VERSION=3`,公式血缘不变(仍是 `adj_factor.exchange_v2`,故 v2/v3 快照均可消费)。取证:`config/schemas/fixtures/adjustment-snapshot-v3-valid.json`(instrument `920098.XBSE`)、`config/schemas/fixtures/adjustment/golden-provenance.v3.json`(与 v2 同四例、逐字复制,只改 `provenance_version`,不新增证据声明);新增 `contract-negative-cases.json` 的 v3 分支(含 `unknown_suffix_stays_rejected` 与 `xbse_suffix_is_not_part_of_v2`)与 `test_xbse_widening_lands_as_v3_not_an_in_place_edit`。文档同步 `specs/layers/contracts/adjustment-snapshot-storage.v1.md`(版本分派+后缀集)与 `specs/layers/contracts/adjustment-official-source-access-v1.md`(v3 与 v2 逐字同例);**故意不动** `specs/architecture.md`:它的 digest 被已 finalize 的 `evidence/paper-execution/release/evidence-index.json` 钉住,为一句示例去重钉一个已发布层的证据索引不划算(该行只是示例,并非完整枚举)。
- [x] 7. 20 只烟雾测试(2025-08-29 至 2025-09-30)端到端跑通。
  - 已完成(2026-10-12):A500 前 20 只成员 × 23 个交易日,`build_tushare_v2_pipeline` 23/23 分区发布,`status/limit_up/limit_down/adj_factor` 全部就位,就绪门全字段空值率 0。
- [x] 8. 500 只端到端与证据归档到 `evidence/`。
  - 已做(2026-10-12):A500 全 500 只成员 × 23 个交易日灌入本地 canonical store `data/bars-a500-store/`(gitignored,可重灌),23/23 分区发布,行数为 499(其余为停牌缺失);为补齐窗口,`data/a500-store/` 已追加 2025-05-30/06-30/07-31 三个月度快照,最早可见日提前到 2025-06-10。
  - 已做(2026-10-12):证据归档到 `evidence/a500-pilot/2025-08-29-to-2025-09-30/` —— `pilot-record.json`(结论/口径/限制)、`training-readiness.json`(就绪门原样输出,`status=passed`)、`bars-inventory.json`(23 个分区的 manifest/data sha256、generation、row_count、run_id、code_fingerprint、schema/quality/trust-anchor 摘要)、`evidence-index.json`。canonical store 是 gitignored 的,故归档的是**可重导的摘要清单**而非数据本身;清单由 `scripts/build_store_evidence.py` 生成,生成时逐分区经 `ManifestFirstReader` 复读并校验 checksum,不匹配即拒绝出稿。`evidence/a500-membership/evidence-index.json` 同步从 14 个快照补到 17 个(旧记录 digest 无漂移),`csi-all` 一并换用同一生成器。
  - 修缺陷:`uq-ingest daily` 此前只接 research-v1(硬编码 `build_tdx_pipeline`/`build_tushare_crosscheck_pipeline` + `config/universe/research-whitelist.txt`),v2 pilot 只能靠内联循环驱动,即"500 只端到端"不可从仓库复现。现加 `--pipeline {v1,v2}`(默认 v1 不变)、`--membership-root`、`--universe-id`:v2 成员逐日从 PIT membership store 解析;缺参 `parser.error`,快照尚不可见时 `configuration_failure` 且不落任何分区。ingest report 每会话写入 `<bars_root>/runs/<run_id>.json`(gitignored),其身份字段已进 `bars-inventory.json`。
  - 就绪门口径(2026-10-12 补):`suspensions`(来自 `suspend_d`,仅查询"有标的缺日线的会话")启用后区分三态 —— 全窗停牌(计入 `instruments_fully_suspended`)、可交易日数不足(`limited_tradable_history`, remedy 是拉长窗口)、日线确实缺失(`unexplained_bar_gap`, remedy 是修 ingest)。另有 `suspension_bar_conflict`(停牌日却有日线)。全窗口 A500 实测:批次缺口修复后仅剩 1 条 `unexplained_bar_gap`,即 601989。
  - 已做(2026-10-12):退市标的改判。`TushareAdapter.delisted_instruments()` 取 `stock_basic(list_status=D)`,就绪门据此把"整窗零日线且已退市"的成员记为 informational 的 `delisted_members_excluded`(不阻断),其余缺口仍为 blocking。同时引入 finding 严重度(`blocking`/`informational`),`status` 只看 blocking。
  - 实测否证了先前"接 `out_date` 即可"的设想:2000 积分下 `stock_basic` 即使显式传 `fields=...,out_date` 仍不返回该列(仅 ts_code/name/list_date/list_status);`namechange` 对 601989 无退市记录;`stk_premarket` 无权限。因此**拿不到退市日期**,只能做保守判定:仅当该标的在本窗口完全没有日线时才豁免;有部分缺口仍按缺陷处理。若将来需要精确到日,需要更高积分或交易所公告源。
  - 结果:A500 2025-08-29~09-30 窗口首次 `passed`(23 会话/500 成员/499 只有日线),唯一条目为 informational 的 601989(中国重工,已退市)。

# Tushare PIT 能力边界(2026-10-11,2000 积分账号实测)

可用:
- `income`、`balancesheet`、`cashflow`:返回 `ann_date` 与 `f_ann_date`,实测 `f_ann_date` 无空值。财务 PIT 以 `f_ann_date` 作为可见时间,`end_date` 仅为报告期,不得作为可见时间。
- `disclosure_date`:返回 `pre_date`(预约披露日)与 `actual_date`(实际披露日,部分为空)。空值行不得静默视为已披露。

不可用或受限:
- `income_vip`:无权限(需更高积分)。
- `fina_indicator`、`express`、`forecast`:无 `f_ann_date`,只能用 `ann_date`,且 `forecast` 实测在 600000.SH 上 2024 年无数据。不得在缺少 `f_ann_date` 时假定 PIT 可见。
- 指数成分(`index_weight`):无任何可见时间字段(前文已确认)。

版本问题:
- 同一 `end_date` 可有多行,`update_flag` 取值 0/1;文档未明确其语义,不得直接假设 `1` 即最新或可见版本。
- PIT 查询必须选择 `f_ann_date <= 查询日` 的最新版本,并在 manifest 中记录所用 `update_flag` 与版本选择规则。

待办(前瞻,不在当前关键路径):
- 在接入财务数据前,用 `f_ann_date` 与查询日构造 PIT 测试样例,并确认 `update_flag` 语义(需核实文档或用多版本样本推断)。
- 说明:当前训练数据面(日线/复权/涨跌停/停牌/成分)不使用财务报表,故本条不影响 A500 训练接入。

# A500 成分:月度近似方案(2026-10-11 决策)

决策:
- 采用 Tushare `index_weight` 月度数据作为 A500 成分来源,是"月度 PIT 近似",不是严格逐日 PIT。
- 取数按官方建议:`start_date` 为当月第一天,`end_date` 为当月最后一天;每个自然月对应一个快照(已核实 2025-08 至 2026-09 共 14 个月,每月恰好一个快照)。
- 快照的 `trade_date` 为指数公司发布的快照日,位于月末附近(样本:2025-08-29、2025-09-30、2025-11-28、2026-01-30 等)。快照日不等于披露日,也不等于生效日。

生效规则(防前视,强制):
- 快照不得在"当月第一天"生效。一个快照只能在其 `trade_date` 之后的第一个交易日起使用,并且再加 `publish_lag_days` 个交易日作为保守可见时间。
- `publish_lag_days` 取 5 个交易日(写入 manifest)。这是**高于实测上界的保守值**,不是未实测的假设:实测见待办第 1 条(`evidence/reference-data/index-weight-visibility-log.jsonl`,首条观测中最新快照上界为 3 个会话),5 只会推迟快照可用时间,不会提前暴露。
- 每个快照的有效区间为 `[visible_from, 下一快照 visible_from)`;最后一个快照的 `end` 为开放区间,需标注。

限制(必须写入所有研究报告与 manifest):
- 近似口径:快照日之后、下一快照日之前的调整无法区分,逐日 PIT 不成立。
- 调整可能发生在快照日前的盘中或公告中,本方案只能保证"不早于快照日可见"。
- 不能用于严格的调样事件研究(如调入/调出后的超额收益),这类研究需要更精确的公告时间。

升级路径:
- 若未来训练需要精确 PIT,另寻带公告日、生效日的成分数据源(如指数公司公告),届时再设计事件表并替换月度快照。

存储约定(2026-10-11 确认):
- 正式 store:`data/a500-store/`,由 `.gitignore` 的 `data/` 覆盖,不修改 `.gitignore`。
- 仓库内只登记 `manifest.json` 的 generation 与 SHA-256 到 `evidence/`,不提交 `members.csv` 本体。
- 快照为不可再生的历史抓取结果,需在 `data/` 之外另做备份,并记录 SHA-256。

集合绑定实施记录(2026-10-11):
- 已实现:`build_membership_set` 按窗口内交易日选择快照并生成 `set_digest_sha256`;`verify_membership_set` 校验 digest、generation、manifest 摘要、成员校验和、严格递增,以及每个交易日选中的快照是最新可见者。
- 已验证:真实 store 上构造确定性(两次 digest 相同),窗口 2025-10-10 至 2026-09-30 选中 13 个快照并通过校验。
- 决策点(已定,2026-10-12):窗口起点早于首个可见快照时**保持直接失败**(fail-closed),不做空段降级。理由:静默接受未覆盖的起始段会让"窗口内每个交易日都有可见快照"这条不变量失效,而调用方无法从结果里看出前段是空的;正确做法是把快照补齐(例如 `data/a500-store/` 已为此向前追加 2025-05-30/06-30/07-31 三个月度快照),而不是让运行时猜。若将来确有"窗口合法地早于首个已发布快照"的用例,再改为显式返回空缺段并让调用方决定。

# 全 A / A500 实盘训练前置缺口(2026-10-13 项目级 CR 复核)

来源:对最近数个 commit 的项目级 CR。下面每条都在本机复现过才写入;CR 里**未复现的指控单列在"本轮未复现项"**,防止后续读者按假问题施工。

## 已复现的真问题

- P1(阻断,机械)`scripts/publish_market_datasets.py:257` 直接 `dt.date.fromisoformat(args.start)`,日期写错即抛 `ValueError: Invalid isoformat string` 原始 traceback、exit=1。
  - 同类入口都已结构化拒绝:`src/uq/cli.py:71` 用 `parser.error`,`scripts/validate_training_readiness.py` 输出 `{"status":"configuration_failure",...}`,本脚本是唯一例外。
- P2(阻断,机械)`rg -l publish_market_datasets tests` 为空:该脚本 `main()` 无任何测试覆盖,包括 `cross_check_result=("agreement" if independent_sessions is not None else "skipped")`(`:283`)这条刚改过的装配线。也就是说本层唯一的"四族发布入口"没有回归防护。
- P3(阻断,需决策)已治理的因子链**无法消费 v2 bars 线**,而 A500/全 A 实盘训练实测产出的正是 v2:
  - 复现:`FactorStore.publish(..., input_dataset="bars_daily", input_schema_version="v2")` → `ContractError: factor input binding does not match reviewed factor set`(`src/uq/factors/store.py:85` 对 `basic` 硬要求 `research-v1`);`alpha158` → `ContractError: qlib factor input schema is not reviewed: v2`(`store.py:96-99`,读 `config/factor-sets/alpha158-v1.json` 的 `required_schema_versions.bars_daily == ["research-v1"]`)。
  - 实测 store:`data/bars-a500-store/canonical/bars_daily/v2/date=*/manifest.json` 的 `schema_version` 为 `v2`(23 个分区)。
  - 根因**不只是版本字符串比较**:`bars_daily.research-v1.yaml` 主键是 `(instrument, datetime)`、字段止于 OHLCV+amount,`basic-v1.json` 的 `required_columns` 同样要求 `datetime`;v2 主键是 `session_date`,并多出 `status`/`limit_up`/`limit_down`/`adj_factor`。列契约与主键都不同,放宽字符串也接不上。
  - 可选路线(未决策,不得擅自放宽已 reviewed 定义):
    - A. 新建 v2 因子集定义(`basic-v2.json`/`alpha158-v2.json`,`required_columns` 改 `session_date`,`input_bindings` 要求 v2,重算 `implementation_fingerprint`)——口径最干净,但是一次真正的重新评审。
    - B. 加受治理的 v2 → research-v1 投影(重命名 `session_date`→`datetime`、丢弃扩展列)后按现有 reviewed 定义发布——改动最小、保持 v1 定义与既有证据不变;代价是丢掉 `adj_factor`/状态列,`adjusted`/资格判定仍接不到 v2。
    - C. 并行维护一条 research-v1 bars 线(重复抓取,额外配额)。
  - 相关缺口:仓库内**没有**从 bars store 计算并发布治理因子的生产入口(`rg -l FactorStore scripts/` 为空,只有 `src/uq/factors/store.py`、`src/uq/models/accepted_store.py`、`src/uq/research_chain/cli.py` 引用),上面任一条路线落地时都要一并补上该入口。
- P4(次要,机械)`upstream_created_at` 传入 naive `datetime` 时抛 `TypeError: can't compare offset-naive and offset-aware datetimes`,而非带类型的 `ContractError`;失败形状与其余校验不一致。
- P5(次要,命名,**决定不改** — 2026-10-13)`src/uq/pipeline/readiness.py:239` 的 `observable_counts` 实际保存 required − suspended,名字与语义不符。
  - 已试过改名 `required_tradable_counts`,但 `evidence/a500-pilot/2025-08-29-to-2025-09-30/evidence-index.json:60` 钉了该文件的 SHA-256,钉的是「跑出该 pilot 证据的那份代码」。改名后 `tests/test_adv_review_fixes.py::test_evidence_indexes_are_fresh_and_never_index_themselves` 立即失败;若顺手刷新 digest,等于宣称「改名后的代码跑出了那份 pilot 结果」,属于伪造证据血缘。已回滚。
  - 处置:留待下次对 `readiness.py` 做**实质**改动时一并改名,并同步重跑 pilot、重出证据索引。
- P6(文档)`docs/user-guide.md` 未说明研究链 v5 请求所需六类**预发布**制品,而这些 family 在生产代码里只读(生产者仅存在于测试 helper),纯按用户指南走会在发布前一步卡住。

## 本轮 CR 未复现项(不要按这些施工)

- "pandas 3 下 `Series.fillna` 被移除 / `dataset.py`、`labels.py` 会崩":本机 pandas 2.3.3,`Series.fillna` 与 `ffill(axis=1)` 均可用;`rg "\.fillna\(|ffill\(axis" src/uq/models src/uq/backtest src/uq/portfolio` 零命中;所引行号超出文件长度(`dataset.py` 167 行、`labels.py` 161 行)。
- "非交易日触发 `RuntimeError(\"no expected sessions\")` 回归":`src/uq/routing/router.py` 无该 raise,`TradingCalendar.between` 返回空集,`DailyIngestPipeline.run` 捕获 `OSError/RuntimeError` 后返回报告的契约不变;所引测试文件与测试名不存在。
- "`daily.py:57` 新增 `fromisoformat` 校验":`pipeline/daily.py`、`store/reader.py`、`store/pit.py`、`store/pit_store.py` 内 `rg fromisoformat` 零命中。
- "EastMoney `_ensure` 缓存捷径不可靠":`min(fetch_start, self._certified[0])` 只会在已认证区间之外扩大抓取窗口,方向保守,不是缺陷。
- "`readiness._session_tail`":该函数不存在(仅存在 P5 的命名问题)。

## 待办(按依赖顺序)

- [x] 1. 修 P1:`--start/--end` 校验失败输出 `{"status":"configuration_failure","error":...}` 并 exit=2,与 `validate_training_readiness.py` 同形。
- [x] 2. 修 P2:新增 `tests/test_publish_market_datasets.py`,覆盖非法日期、`--skip-independent-calendar` 时 `cross_check_result == "skipped"`、重复运行按不可变语义跳过。
- [x] 3. 修 P4:naive `upstream_created_at` 改抛 `ContractError`(只收紧失败形状,不改已接受输入的 identity 语义),并补回归测试。
- [x] 4. P5 决定不改(理由见上文 P5:化妆性改名会破坏 A500 pilot 证据的 digest 锁)。
- [x] 5. 修 P6:`docs/user-guide.md` 列出六类预发布 family 并指明生产者尚未随仓库提供,交叉引用 `docs/layer-architecture.md`。
- [~] 6. P3 决策:**已定 A(2026-10-13,用户拍板)**,契约层已落地,剩 A-2 执行入口:该决策改变 canonical 口径,必须由用户确认,不得单方面放宽已 reviewed 定义;决策与理由追加到本节。
  - 已做的决策无关部分(2026-10-13):两处拒绝信息改为可操作——`basic` 现在报 `... basic/1.0.0: got bars_daily/v2, reviewed binding is bars_daily/research-v1`,Qlib 分支现在报 `... (reviewed schema versions for bars_daily: ['research-v1'])`。**已 reviewed 的定义未放宽**,行为仍是 fail-closed。回归测试 `tests/test_factor_store.py::test_unreviewed_input_binding_names_the_reviewed_line`。


实施记录(2026-10-13):
- 1:`scripts/publish_market_datasets.py` 的日期解析改 `try/except ValueError` → 打印 `{"status":"configuration_failure","error":...}` 并 `return 2`;`--end` 早于 `--start` 同样结构化拒绝(原先是 `SystemExit`,exit 1 且无 JSON)。
- 2:新增 `tests/test_publish_market_datasets.py`(6 例):非法日期、倒序窗口、`--skip-independent-calendar` → `cross_check_result="skipped"` 且 `independent_source="not_checked"`、两家一致 → `"agreement"`、两家分歧 → `ContractError` fail-closed、重跑记 `already_present` 且不重复发布(set binding 逐字节不变)。
- 3:`_iso_datetime` 在入口处拒绝 naive `datetime` 与 naive ISO 串(`ContractError: upstream_created_at must be timezone-aware`)与不可解析串;字符串入参仍原样返回,已发布分区的 identity 字节不受影响。回归测试 `tests/test_factor_store.py::test_naive_upstream_timestamp_is_a_typed_refusal`。
- 5:`docs/user-guide.md` §6 新增「execute 前必须已发布的上游制品」表(六类绑定 + 谁读它 + 仓库内是否有生产者),并说明 dry-run 不解析这些 store、v5 日历快照的 `skipped`/`agreement` 口径。

P3 实施记录(2026-10-13,方案 A)
- 决策:新增 reviewed 定义版本,而不是放宽旧的、也不是再灌一条 research-v1 线。旧定义与其已发布分区语义不变。
- 已核实的等价性(为什么这是"新版本"而非"改名"):`config/schemas/bars_daily.v2.yaml` 与 research-v1 同单位(`volume` share、`amount` CNY、价格 raw CNY),只把主键 `datetime` 改名为 `session_date` 并新增 `status/limit_up/limit_down/adj_factor`;两条线共用 `src/uq/adapters/tushare.py` 里同一次换算(`vol*100`、`amount*1000`),实测 A500 store `amount ≈ close×volume` 成立,故因子值单位一致。
- 已落地:
  - `config/schemas/factor-sets/factor_set.v3.json`:在 v2 基础上 (1) `input_bindings` 仍必填,(2) `qlib_name`/`expression`/`reviewed_engine_versions` 改为**仅在声明 `qlib_expression_set` 时必填**(原生 pandas 集合没有 qlib 表达式与 qlib 引擎版本;原生运行环境溯源仍由分区的 `python_version` + `dependency_lock_digest_sha256` 承担)。
  - `config/factor-sets/basic-v2.json`:`basic/2.0.0`、`set_definition_version: 3`、statuses reviewed、`input_bindings` 只允许 `bars_daily/v2`;factors/fingerprint/thresholds/required_columns 与 1.0.0 逐字相同(有测试锁)。
  - `src/uq/factors/store.py`:输入线守卫改为**由定义驱动**(有 `input_bindings` 就走它),v1 两个集合的名称分支原样保留;顺带把"raw-price 输入不得绑复权血缘"从只覆盖 `basic` 扩到所有 bars_daily 输入。
  - `specs/layers/factor-layer-spec.md` §3.1.1 登记该 reviewed 输入线与两条边界。
  - 测试:`tests/test_factor_set_input_line_v2.py`(7 例,含"加 2.0.0 没有放宽 1.0.0"的双向拒绝、v3 条件必填、unreviewed dataset 报出 reviewed 清单)。
- 已落地(A-2,2026-10-13):`scripts/publish_factor_partitions.py` + `src/uq/factors/inputs.py`。
  - 流程:`ManifestFirstReader` 读 canonical 分区(manifest-first + checksum 校验)→ 投影到 reviewed 输入契约 → `calculate_raw_price_factors` → 逐 session `FactorStore.publish`,并把同 session canonical 分区的 `generation_id`/`data_checksum_sha256` 绑成上游;可选输出 `factor_partition_set` 绑定(覆盖**实际发布**的 session,不假装覆盖请求窗口)。
  - 行资格规则写在库里而不是脚本里:`v2` 可能带 `status != trading` 的零成交量行,而 research-v1 对停牌标的**根本没有行**;不剔除就会把 0 吸进 20 日均值、让 `volume_ratio_20d` 报 `0.0` 而不是缺失。所以投影时剔除非交易行,并有测试区分"剔除后为 NaN"与"保留后有限值"。
  - 可见性口径**必填无默认**(`--input-visibility {session_close,ingest_time}`):契约把 `run_visible_cutoff` 钉在 `分区日 15:00 Asia/Shanghai` 且要求所有输入在此之前可见。实测:`session_close` 可发布;`ingest_time` 绑定 canonical manifest 的真实下载时间(收盘后),直接 `upstream partition visibility violates factor run cutoff` fail-closed。这是口径选择,不是缺陷。
  - 真跑验证(A500 实测 store):`--factor-set basic --factor-version 2.0.0 --input-visibility session_close` 在 2025-08-29~09-30 窗口发布 4 个分区(09-25~09-30,每分区 498 行),其余 19 个 session 记为 `insufficient_history`(lookback=20);重跑全部 `already_published`、set digest 不变。**教训**:研究窗口前必须先灌够 ≥20 个 session 的暖机历史,否则 A500 全窗口只有尾巴能出因子。
  - 测试:`tests/test_publish_factor_partitions.py`(7 例,含因子值逐位断言 range_ratio_1d=0.2、close_location_1d=0.5、amount_20d=1000、volume_ratio_20d=1.0)。
- 新发现(本轮实测暴露,未修):
  - **两个 canonical 发布器与 reader 的 manifest 版本不兼容**:`uq.store.pit_store.CanonicalStore`(ingest pipeline 用的、也是 `data/bars-a500-store` 实际形状)写 `manifest_version: 1` + `schema_version: "v2"`;`uq.contracts.canonical_v2.CanonicalV2Store` 写 `manifest_version: 2` + `package_provenance`。而 `ManifestFirstReader` 固定按 `canonical-v1.json` 校验,读 CanonicalV2Store 的分区会直接 `manifest schema validation failed`。后果:凡是只用 `CanonicalV2Store` 造的数据,受治理读路径都读不了(本层测试改用 `CanonicalStore` 以对齐真实 store)。需要单独评审决定是收敛写侧还是让 reader 支持双版本分派。**→ 已于 2026-10-13 收敛(reader 按 manifest_version 白名单显式分派),见下面 R2 实施记录。**
  - **`FactorEngine.create_result` 的报告 taxonomy 与发布契约不兼容**:engine 每列各发一个同名 `null_rate`,而 `FactorStore._reports_agree` 遇到重名直接 `duplicate quality checks reject publication`,且发布侧还要求 `null_rate__<col>`/`finite_values__<col>` 命名。后果:engine 产出的 `quality_report` 永远无法用于发布,发布侧报告必须由 `_validate_factor_frame`(publish 内部用的同一函数)生成——A-2 入口就是这么做的。修它要改因子层 Phase 4 的验收语义,应另开一轮。**→ 已于 2026-10-13 修复,见下面 R1 实施记录。**
  - **因子分区只绑一个上游 generation**:滚动窗口实际用了 20 个 canonical 分区,但 manifest 的 `inputs` 只记录决策 session 那一个 `generation_id`/checksum,其余 19 个不在因子血缘里(research-v1 时代同样如此,不是本次引入)。要精确复现需要把窗口内的 bars 集合绑定进来。
- 这三条的修复排期与前置决策(2026-10-13 定;先把两条无决策可加固的变成 CI 锁 `tests/test_known_contract_divergences.py`,它们**故意断言"问题仍然存在"**,真修时必须同一次改动里删掉对应 pin):
  - **R1(已完成 2026-10-13)engine 报告 taxonomy 与发布契约不兼容**:已改为单一口径,见下面 R1 实施记录。
  - **R2(已完成 2026-10-13)canonical manifest 版本收敛**:已让受治理 reader 按 `manifest_version` **白名单显式分派**,见下面 R2 实施记录。
  - **R3(有明确触发条件才做)因子只绑决策 session 的上游 generation**:改它就是改因子 manifest 身份(canonical 变更 → 现有分区要按新版本重发)。触发条件:出现"必须逐窗口精确复现/审计因子输入"的实际需求(例如监管式回溯、或跨数据源对账对不平)。在那之前用 bars 侧 inventory(`scripts/build_store_evidence.py` 逐分区 checksum)+ `factor_partition_set` 做近似审计。**在此之前不要动。**

R1 实施记录(2026-10-13,单一质量口径)
- 决策:`FactorEngine.create_result` 不再自算第二套 checks,而是**委托** `FactorStore` 发布时重推导所用的同一个 `_validate_factor_frame`。因此"计划时的报告"与"发布时的报告"由构造保证一致,R1 的洞从根上消失,而不是在发布侧放宽比较。
- 状态语义(有意收敛,替代旧的自造口径):
  - status **只跟随 reviewed 阈值**(null_rate / coverage / 逐列 null_rate / 逐列 finite_values)。`reject_all` 集合下部分缺失但仍在 `null_rate_maximum` 内 → `passed`,并把这些列以 `insufficient history emits nulls: <col>` 记进 `warnings`(advisory,不改 status)。旧的 "policy==accept_with_warnings 才降 warning" 分支删除:warning 只来自校验器本身,保证 engine 状态永远等于 publish 重推导的状态,否则发布会被 `expected_report_status` 拒绝。
  - 空帧按意图分派:`intent="publication"` → `rejected` + `empty factor results cannot be published`(旧行为是直接抛);`intent="dry_run"` → `empty`(仍可作为暂存产物)。publish 侧对空帧的行为不变(直接抛),没有放宽。
  - 失败原因由 checks 派生成可读消息(`coverage below minimum`、`null rate above maximum: <col>`、`non-finite values: <col>`),不再维护第二套文案。
  - 外部传入 `quality_report` 的一致性校验保留(policy/status/checks 三项必须与本地一致);checks 比对改为直接字典相等,空帧报告(零 checks)不再被"必须非空"这条误伤——空帧本来就不可发布,由意图分派负责。
  - `null_policy` 参数此前被静默丢弃,现在尊重调用方传入,缺省才回退默认。
- 已落地代码:`src/uq/factors/engine.py`(委托 + 状态语义)、`scripts/publish_factor_partitions.py`(改走 `FactorEngine`,`intent="publication"`,run cutoff = 分区日 15:00 Asia/Shanghai;非 `passed` 直接带 status/errors 失败,不再 import 私有 `_validate_factor_frame`)。
- 身份影响(已实测,结论是不变):因子分区 manifest 身份含 `quality_report_checksum`,直觉上担心 engine 报告换内容会改 generation。实测相反——无外部报告时 `result.quality_report` **就是** `_validate_factor_frame` 返回的那个文档(policy/status/checks/errors/warnings 逐字段相同,实测 A500 2025-09-30 分区为 `True`),所以 R1 不改任何既有分区的 generation。advisory 的 `insufficient history emits nulls` 只留在 `FactorResult.warnings`,**故意不写进 durable 报告**:这样"计划时报告 ≡ 发布时重推导报告"是字节级等价,身份也因此稳定;代价是发布侧不留 advisory 语境,需要时由运行日志承担(已加测试锁这条等价)。
- 测试:`tests/test_known_contract_divergences.py` 的 R1 pin 换成**正向锁**——(1) engine 报告与 `_validate_factor_frame` 逐 check 一致且能端到端 `publish`;(2) 一份 schema 合法但 status 降级的报告仍被发布拒绝。同文件里 CanonicalV2Store 那条 pin 随 R2 一起撤除,两个锁现分拆为 `tests/test_factor_quality_single_source.py` 与 `tests/test_canonical_manifest_versions.py`(pin 文件本身已删)。
- 验证:全量 `python -m pytest tests/` → 941 passed / 24 skipped;真实 A500 store 走 engine 路径重发 2025-09-25~09-30(4×498 行)成功。

R2 实施记录(2026-10-13,canonical manifest 版本收敛)
- 决策(收敛读侧,不改写侧身份):**受治理 reader 先读 `manifest_version`,再按该版本自己的契约与身份校验**。白名单 `(1, 2)` 之外(含缺失、非整数、bool)一律 fail-closed 并打印支持集;不再有任何"默认按 v1 校验"的回退。
  - `1` → 仍走 `canonical-v1.json` + 原 generation/trust-anchor 规则(既有 store 与分区**逐字节不变**);
  - `2` →走 `canonical_manifest.v2.json` + `canonical_v2_identities` 重算(generation / manifest digest / trust anchor 三项全验)。
- 为什么不选"只保留一个写侧":`CanonicalV2Store` 是 plan 明确要求的 **canonical-v2 增量契约**(带 `package_provenance` 与 run-local `manifest_digest_sha256`),不是 accidental 副本;而 `CanonicalStore`(manifest_version 1)是 ingest 流水线与磁盘上真实 store 的形状。删任一边都会毁掉已有能力,而两边身份规则本来就没冲突——缺的只是读侧的分派。
- 关键细节:`validate_canonical_v2_manifest` 放在 `src/uq/contracts/manifest.py`(契约层),`canonical_v2._validate_manifest` 改为**委托**它,发布侧与读取侧共用同一身份判定,不会再各自漂移(`gate_contracts` 在函数内懒加载,避免 `manifest ↔ gate_contracts` 模块环)。
- 两条读路径的严格度差异是**有意保留**并在 spec 登记:`ManifestFirstReader` 证 manifest 身份 + data checksum + row count + 列序 + dtype + 内嵌 trust anchor;`read_canonical_v2` 额外要求带外 expected anchor 与绑定的 quality report。
- 命名坑登记(spec §3.1.3):`bars_daily` 的 **schema** 版本 `v2` 与 canonical **manifest** 版本 2 是两回事,`bars_daily/v2` 分区实际活在 `manifest_version: 1`。R2 之前的混乱正是这两个 `v2` 撞名造成的。
- 测试:`tests/test_canonical_manifest_versions.py`(5 例:v1 仍读、v2 能读、`manifest_version: 3` 与缺失均 fail-closed、篡改 v2 `row_count` 触发 generation mismatch)。`tests/test_known_contract_divergences.py` 两条 pin 都已撤除(R1→正向锁、R2→上面的锁),该文件已删除。
- 生产写侧现状记录(不做代码改动):今天**没有任何生产入口写 canonical-v2**(`CanonicalV2Store` 只被 contracts 自身与 research-chain 测试使用),所以本次不存在"已发布分区需要迁移"的问题。


- 仍未 reviewed 的输入线:
  - `alpha158/1.0.0` 仍只接受 `research-v1`;要做 qlib 线过 v2,需另开一个 reviewed 版本(它牵涉 qlib 引擎版本与表达式字段口径),不在本次范围。
  - `adjusted/1.0.0` 仍绑 `bars_adjusted/adjusted-v1`。v2 上虽然带了 `adj_factor`,但"带字段"不等于"该线已 reviewed",复权线要单独走一次评审。

universe snapshot 生产者(2026-10-13)
- `scripts/publish_universe_snapshots.py`:把受治理的 `index_membership` 快照铺成 `universe_snapshot.v1` 制品,覆盖区间严格 `[visible_from, 下一 visible_from)`(最后一份 `valid_to=null` 且在 evidence 里标注),`membership_evidence` 必带 `source/index_code/pit_precision/index_snapshot_date/publish_lag_days/membership_generation` 与"月度近似、非严格逐日 PIT"的限制语。两个快照 `visible_from` 相同(无法平铺)时直接拒绝,不猜先后。
- 真跑:`data/a500-store` 17 个快照 → 17 个制品,`UniverseSnapshotStore.read_manifest` 全部读回通过,区间**无缺口无重叠**(最早 `valid_from=2025-06-10`,最新 `2026-10-15` 开放)。
- 顺带把 `IndexMembershipStore.snapshot_dates()` 提为公开 API(脚本不该依赖私有 `_snapshot_dates`)。
- 测试:`tests/test_publish_universe_snapshots.py`(6 例:平铺正确+读回、幂等、窗口过滤、未知 universe 拒绝且不落盘、visible_from 冲突拒绝、边界参数拒绝)。
- 剩余四类预发布制品仍只有测试 helper:`adjusted_price`、`feature_schema`/`feature_preprocessing`、`label_set`、`backtest_config`。其中 `label_set`/`adjusted_price` 还需要**新写受治理写入器**(现有 `LabelStore`/`AdjustedPriceDatasetStore` 只有 read_* 方法)。

外部评审 2026-10-13 复核与处置(报告文件 `code-review-unified-quant.md`,基线 `44a0286`)
- 方法:报告 20 条候选逐条对本 HEAD 重验(读码 + 可达性 + 必要时运行时复现),**每条已修项都用变异验证确认新增锁在修复前必失败**(临时回退源码跑新测试)。锁集中在 `tests/test_external_review_20261013.py`(14 例)。
- 已修(8 条,全部是真问题):
  - **B4 回测先卖后买未真的生效**:买卖在同一字母序循环里,买入按字母序早于卖出时会把本可成交的买入记成 `skipped_insufficient_cash`。改为按**日初持仓**分类 sell/buy/hold 后分批执行(组内仍按标的序,确定性不变)。锁:构造 `AAA`(买)/`ZELT`(卖)资金依赖场景,修复前 `AAA` 永不成交。
  - **B6 多决策日研究链必崩**:state 路径只含 `stage=NN`,而预测阶段在逐日期循环内 publish,第二个日期撞 `immutable research manifest already exists`。改为**逐日期构建、阶段一次性发布**(与 portfolio 阶段同形),`run()` 变成单日薄封装。锁:两日期 `run_for_dates` 产出 2 份 prediction + 1 份 state 两条 binding;修复前直接复现撞路径。
  - **B7 两份 `ContractError`**:`contracts/schema.py` 自持一份,导致 schema 拒绝绕过 ingest 的 `except ContractError` 分类。改为复用 `uq.errors.ContractError`(全仓无人 import 旧类)。
  - **B8 模板 digest 报错路径自身 KeyError**:错误消息读 `manifest_digest_sha256`,而模板族只有 `template_manifest_digest_sha256`。改为读 `payload[digest_field]`。
  - **B9 symlink 守卫误伤合法 store 根**:守卫遍历**未解析路径的全部祖先**,macOS 的 `/tmp`、`/var` 都是符号链接 → 落在其下的根必拒;`pytest` 的 `tmp_path` 已解析所以测试全绿掩盖了它。新增单源 `src/uq/contracts/paths.py::reject_symlinks_within(root, path)`:**根以上是操作系统的事,根以内不许有链接,解析后仍须在根内**;已接入 `market_dataset_publisher`(4 处)、`execution/stores`(2 处)、`models/dataset_writer`、`contracts/model_layer`(仅当配置了 accepted root)、`research_chain/owning_contracts`。
  - **B10a `max_turnover > 0` 首个调仓日必崩** + **B10b `max_turnover == 0` 静默跳过约束**:builder 现在要求显式 `initial_state`(仅一次运行的首份分区可为真;真时豁免并因 manifest `previous_target_weights_generation_id: null` 可审计),adapter 传 `initial_state=previous_frame is None`。`max_turnover=0` 且有前序状态时真正冻结。
  - **B12 split purge/embargo 对声明顺序敏感**:改为**先按时间排序再逐相邻对**判重叠/隔离带,顺序无关且不漏报(相邻满足则非相邻自动满足)。
- 已核实为真、已修复(2026-10-14 latent 批次,详见 cr.md §8.18;每条均做变异验证):
  - **B1 风控发布门 allow 路径必 AttributeError**:`PortfolioPublicationRiskGate.gate` 现把 allow/warn 之外一律拒发(spec 7.3:执行契约未定前 `de_risk`/`flatten`/`halt_strategy` 不可执行,`escalate_review` 及未来枚举同拒),决策载荷钉进 run `files[]`;`RiskRunStore.publish` 的 payloads 改必填。裁决结论:**`de_risk/flatten` 算阻断**(fail-closed,不是 fail-open);`files[]` 放**逐决策 `decision-N.json`**(portfolio 路径为决策体、order 路径为候选单+决策体)。附带修掉该轮自己引入的 `read_verified_risk_run` 按 order 形状硬读 portfolio 载荷的回归。
  - **B2 标签先过滤后 `shift(-horizon)`**:改为先在完整交易行网格上算 label 再过滤,`formula_sha256` 写明 D 在过滤前计数。
  - **B3 trainer 不校验 X 的 NaN**:补 `np.isfinite` 三处(标签/特征/结果权重)拒训。
  - **B5 风控门对停牌标的传 `price=0.0`**:停牌判定移到风控调用之前;全停牌日返回 `run=None`(不伪造空 run)。
  - **B13 mootdx 长区间被静默截断到 800 根**:`_requested_bar_estimate` 返回请求跨度,超单页即拒发;另加满页覆盖校验(最老一根晚于请求起点则拒绝)。
- 已收口(2026-10-14 B9 三残余,详见 cr.md §8.23):
  - **B9-A** `contracts/artifacts.py` universe snapshot 目录:改用 `reject_symlinks_within(self.root, directory)`,不再走父目录全扫。
  - **B9-B** `ModelContractLoader` 未配置 root 时的严格扫描:两个生产调用方(`research_chain/runner.py` 的 state 回读与 Qlib receipt 回读)现传 `self.run_store.root` 作为作用域;未配置 root 的调用仍保守拒发(注释写明)。
  - **B9-C** `research_chain/owning_contracts.py` 的 `AdjustedPriceDatasetStore` 预解析路径:改为保留 spelled 路径交给守卫,避免解析折叠在库链接、以及"已解析路径 + 未解析根"的拼写错配。

外部评审 N1/N2/N5 处置(2026-10-14)
- **N1 已修**:`UniverseSnapshotStore.read_members` 的 PIT 窗口参数由 `requested_valid_from: date | None = None` 改为**必填** `date`,不再回退到快照自身 `valid_from`(那会让 `UniverseSnapshotReader.read` 的有效性守卫对"忘记传窗口"的调用方退化成恒真)。组合阶段 `PortfolioStageAdapter.run` 现与数据集阶段同口径传请求窗口。附带发现并修正:`tests/test_research_chain_phase4.py` 的 `_extended_plan` 请求窗口(2025-12-01)本就在快照有效区间(2026-01-05 起)之外,旧默认把它掩盖了,已对齐到 2026-01-05~01-30。变异验证:两处改动分别回退后,新锁 `test_universe_snapshot_read_members_requires_the_pit_window` / `test_portfolio_stage_refuses_a_snapshot_outside_the_request_window` 必失败。
- **N2 非缺陷(既定口径)**:因子分区血缘只声明"当日"一个输入,`specs/layers/factor-layer-spec.md` §3.1.2 明写"绑定同一会话"。要完整血缘需连 `factor_manifest` schema 一起开版本,属治理裁决,未动。
- **N5 非缺陷(既定约定)**:`session_close` 的输入可见时间是合成断言、在 artifact 侧不可证伪,属 close-to-close 既定约定,仅提示,未动。
- **B11 已裁决并落地(2026-10-14)**:IC 口径定为**截面 IC 为默认、pooled 为显式可选**。`MetricReport` 现按 metric 名分派:`ic`/`rank_ic` = 逐决策日截面相关再取无权重均值(剔除日间共同移动),`ic_pooled`/`rank_ic_pooled` = 全样本单次相关(旧行为,须显式命名)。报告每条结果记录 `basis` 与 `cross_section_count`;常量截面(相关未定义)跳过而不污染均值,无可用截面则 fail-closed。口径写入 `specs/layers/model-layer-spec.md` §8.1。metric `name` 是自由字符串,schema 无需升版。
- 处置边界声明:N2/N5 是既定口径(非缺陷)、N3/N4/B14 为 REFUTED,未据此改动;N1 与 B11 已按裁决落地(见上两条)。`code-review-unified-quant.md` 保持**未跟踪**(是外部输入件,不入库;要入库由你决定)。

B9 复核收口(2026-10-14,cr.md §8.23)
- **触发**:对 §8.22 修复轮的对抗复审发现三处 B9 同族残余(见上),其中 B9-C 的第一版"两侧拼写任一即可"兜底**自身引入新洞**:调用方预解析路径时,在库软链已被 `resolve()` 折叠,守卫走解析路径必然放行(实测 ACCEPTED)。
- **裁决**:`reject_symlinks_within` 恢复**要求两侧拼写一致**(`path` 须能 `relative_to(root)`),不再接受"已解析路径 + 未解析根";`owning_contracts` 调用方改为不预解析。口径写明在 `contracts/paths.py` 模块与函数 docstring。
- **变异验证**:回退 `paths.py` 一致拼写规则 / 回退 `owning_contracts` 调用方 / 回退 `runner.py` 两处 loader 作用域,对应新锁必失败。
- **附带**:`evidence/paper-execution-research-integration/phase-0/evidence-index.json` 刷新 `model_layer.py`、`runner.py` 两个 sha256 pin(§8.22 曾漏刷,导致 2 个 evidence 新鲜度测试红)。全量 **1000 passed / 23 skipped / exit 0**。

全A 实盘训练前置工作(2026-10-15,首次试跑)
- **整窗入库入口**:新增 `scripts/ingest_window.py`。此前 `uq-ingest daily --pipeline v2` 一次只发一个会话,A500 pilot 是**内联循环**驱动,仓库里没有可复现整窗的命令。该脚本逐会话从 `IndexMembershipStore` 取 PIT 可见成员、走同一个 `build_tushare_v2_pipeline` 发布不可变分区;分区已存在即记 `already_published` 跳过(可断点续跑);独立日历(EastMoney)带重试(`--calendar-attempts`/`--calendar-retry-seconds`,该端点实测约 1/6 概率掉连接),会话间隔默认 25s(单会话约 104 次 Tushare 请求,免费档 300/min)。**中断也写 summary**(`status=interrupted`、已落盘会话在列),不留半截 run。
- **就绪门 exclusion 机制**:`assess_readiness` 增 `excluded_instruments`(instrument→reason)+ `excluded_provenance`,`validate_training_readiness.py` 增 `--excluded-instruments <json>`。用于 `delisted` 规则表达不了的情形——供应商中途停发(`suspend_d` 不标记长停牌、或供应商空洞),任何窗口长度都换不到足够可交易会话。设计:**只对"确实缺陷"的标的生效**——名必须在 `short_all`(少于会话下限)或存在 susp 无法解释的必需缺口,否则该条剔除本身报 `unjustified_exclusion`(blocking),防"静默缩表";剔除永远以 informational 的 `excluded_instruments` 报告(报告文件里的每一条)。
- **全A(csi-all,中证全指 000985.CSI,~5070 成员)试跑数据**:`/tmp/uq-csiall` 42 个 canonical 会话分区(2025-08-11→2025-10-15)、universe 快照(5068 成员,valid 2025-09-08→2025-10-15)、`basic/2.0.0` 因子分区 21 个(2025-09-09→2025-10-15,绑 `bars_daily/v2`)。就绪门 21 会话窗(9/9–10/15,`--min-sessions 15`)`passed`。
- **仍未决(见 cr.md §8.26 OBS-B/C/D 与本节下方)**:①更宽的 42 会话窗被**离场成员 label 尾段**阻断(逐日 PIT 入库不为离场成员抓 5 会话尾段,属设计缺口,需裁决"补尾段 vs 接受更窄 label 覆盖");②`adjusted_price`/`label_set` 两个预发布族仍只有测试 helper、无生产发布器(训练链路关键路径);③因子线 `alpha158/1.0.0` 仍绑 `research-v1`,过 v2 需另开 reviewed 版本(治理裁决)。

三项收口(2026-10-15,用户裁决后落地)
- **Q1 离场成员 label 尾段(裁决:默认 0 严守 PIT,显式传 N)**:联网调研确认业界口径。① 微软 Qlib(`Alpha158`/`Alpha360`)的默认学习流水线第一道处理器就是 `DropnaLabel`,实现是 `df.dropna(subset=['label'])`,且 `is_for_infer()` 注释明写"按标签丢样本,故不可用于推理"——标签缺失即丢样本,完全没有"为已退出 universe 的标的补取"这一步;`filter_pipe` 默认 `None`(qlib/data/dataset/loader.py:215)即无成分股退出过滤。② CRSP/Wermers 与 Shumway(1997)的一族做法是"坚持持有至退市事件、把退市收益计入最后一段",Shumway 在退市原因缺失时给该样本赋 **−30%** 月收益(≈−100% 的月频率1/4)而非丢弃;动机正是"丢弃末段观测会引入偏差"。③ 两组不冲突:分歧只在**谁来提供末段价格**——约束是"末段收益必须有",不是"必须为离场者向供应商取数"。落地:`scripts/ingest_window.py` 增 `--tail-sessions N`(默认 **0**),显式传 N 时把"最近 N 个会话曾是 PIT 可见成员、本会话已非"的标的并入当日取数集合,并在 run summary 的 `tail_sessions` / 逐会话 `tail_members` 里记录该扩窗(PIT 契约的显式加宽,不因默认值而变)。锁:`tests/test_ingest_window.py::test_tail_sessions_widen_the_universe_only_when_asked`(默认 0 不取、N=2 仅取最近两场、summary 逐会话留痕)。
- **Q2 复权线在 v2 上重新 reviewed(裁决:质量规则"够",即三条)**:原 `bars_adjusted/adjusted-v1` **连 schema 文件都不存在**(`config/schemas/` 只有两个 `bars_daily`)——老线从来不可用、无已发布分区需迁移。新增 `config/schemas/bars_adjusted.adjusted-v2.yaml`:字段集/单位/不变量/主键/排序从 `bars_daily/v2` 逐字继承,唯一语义变更是四个价格列 `adjustment: raw` → `back_adjusted`;按"语义/单位变更开新版本"规则命名 `adjusted-v2`,v1 一字节未动。新增生产者 `src/uq/pipeline/adjusted_price.py`(该线唯一生产者,restate 定义单源)。三条强制规则:① `adj_factor` 必须存在且为正,② 复权价必须能由 `raw × adj_factor` 重算复现(容差 1e-3),③ 复权价非零非 NaN。**关键设计**:`publish_adjusted_partition` 要求必填 `expected_restated`——因为"自己 restate 再校验自己的输出"是恒等,会给任何输入盖上 `reject_all/passed`;没有独立复权源可校验时**拒绝发布**。另修两处实现缺陷:(a) `verify_restatement` 用 `reindex` 只对齐 label 未对齐行列,导致 A 标的与 B 标的错位比较(实测可把 1% 漂移判为通过),改为 `reindex(...).loc[raw.index]` + 行集合不一致即拒;(b) `set(_PRICE_COLUMNS) | {"adj_factor"} - set(frame.columns)` 优先级错导致恒报缺列。锁:`tests/test_adjusted_price_pipeline.py`(10 例)。
- **Q3 exclusions 归档 + alpha158 在 v2 上新 reviewed(裁决:一并做)**:`evidence/csi-all-pilot/2025-09-09-to-2025-10-15/` 三件——`excluded-instruments.json`(validator CLI 契约原样的扁平 instrument→reason,外发文件必须满足该形状,否则 fail-closed)、`exclusions-record.json`(窗口级元数据/机制/复现命令)、`training-readiness.json`(真跑输出)。provenance 从 `file:/tmp/...` 变为仓库内路径,可审计。alpha158 新增 `config/factor-sets/alpha158-v2.json`(`set_definition_version=3`、`factor_version=2.0.0`、`input_bindings` 改 `bars_daily/v2`),因子表达式/指纹/minimum_history/排除项(`VWAP0`)/引擎版本 `0.9.7` 全部**逐字继承**;**`required_columns` 保持 `datetime` 不变**——输入线更名由 `src/uq/factors/inputs.py` 的投影处理,且 qlib adapter 与 `_validate_factor_frame` 都要求 `datetime`(以已 reviewed 的 `basic-v2` 为准,它也是 `datetime`)。定义文件按 `factor_set.v3.json` 封闭 schema 校验(`additionalProperties: false`),故变更说明只入台账不入定义。v1 定义一个字节未动。锁:`tests/test_factor_set_input_line_v2.py`(alpha158 v2 绑 v2 线、v1 未被放宽、喂 research-v1 输入拒绝且无分区落盘)、`tests/test_qlib_adapter.py`(v2 定义通过 adapter 文档校验)。
- **仍未决**:①`adjusted_price` 的独立复权源(`expected_restated`)尚无生产提供者——当前 producer 已 fail-closed,但要真发布还需定"第二个口径从哪来"( dividend 事件链 `src/uq/factors/adjusted_price.py` + `AdjustmentSnapshotStore` 已在库,见 pendding 6 的 dividend importer 缺口);② `label_set` 仍无生产发布器(`LabelBuilder` 在 `src/uq/models/labels.py`,只读 `LabelStore`);③ 42 会话宽窗的离场尾段现在可用 `--tail-sessions 5` 跑,但供应商停发的 10 只仍需 exclusion 文件。

TDX 独立复权源(2026-10-16,前判推翻 + 真跑落地)
- **前判错误纠正**:此前记「TDX 42 主机全不可用」「Tushare dividend 重算差 30.6%」**两条均错**。前者错在探测命令(`bars` 被网络拦、`xdxr` 未被拦,同一连接上 `xdxr` 返回 88 行真数据,含默认主机 `115.238.90.165`);后者错在除权公式(送股项应入分母)与量纲(TDX xdxr 全部按每 10 股)。教训:**探测必须用真正要发的命令**。
- **正确除权公式**:`除权参考价 = (前收盘 - 每股现金 + 配股价×配股比例) / (1 + 送股比例 + 转增比例)`,`ratio = 除权参考价 / 前收盘`(< 1,乘基,锚定最新会话 1.0,与 canonical 派生器同向)。83 事件大样本(6 标的):重算跳变 vs vendor 因子跳变 max 0.109%、mean 0.023%。(**口径与事件集合于 cr.md §8.36 更正**——本节曾写 `ratio = 前收盘 / 参考价` 并让 `restate_from_events` 做除法、事件集合也取错,批量真跑后已修;上句的 83 事件**跳变**吻合度不受影响,因跳变对两种口径互为倒数。)
- **落地**:`src/uq/adapters/mootdx_source.py` 增 `fetch_xdxr_events`/`_xdxr_client`(按 xdxr 探测)/`connected_server`;新 `src/uq/pipeline/adjustment_events.py`(xdxr 归一化 + 除权 ratio);`adjusted_price.py` 的独立源支持 TDX 事件重算,新增 `verify_jump_cross_check`(按 instrument 对齐、两侧各除**自己**的前一帧)。
- **真跑**:三只真实标的在 2025-09-19 发布 `bars_adjusted/adjusted-v2` 成功,四价格列 vendor 跳变 vs 事件跳变差异全 0.000000。
- **仍未决**:①`label_set` 生产发布器(训练链路最后一洞);②复权线发布器目前由调用方提供 `closes`(事件 ratio 所需的前收盘),生产入口(`scripts/`)尚未接线;③`adjusted-v2` 尚未在 A500/全A 试跑窗上批量发布。

TDX xdxr 派生链 + 数据源文档(2026-10-16,cr.md §8.31)
- **先纠正一处台账措辞**:spec 要求的 `derive_adj_factor()` **早已存在**——`src/uq/market/adjustment.py::XdxrAdjustmentDeriver`(provenance 默认 `mootdx.xdxr`)+ `market/adjustment_chain.py` 的 build/publish + provider check + effective-date 表。真正缺的是"TDX 原始 xdxr 事件直接入链"(原路径需从 Tushare dividend 公告映射)与 lineage 落地。
- 落地 `build_tdx_adjustment_chain`(lineage `adj_factor.derived_v1`、provenance `tdx:xdxr`):事件原样使用,只过滤 `category==1`、只取窗口内、按日排序,不经过任何公告解释——这正是第二派生链的意义。逐事件生成 `source_event_id`。
- 新增 `config/schemas/contracts/adjustment_snapshot.v4.json`(**未改 v3**,因其 `formula_version` 是 const,就地放宽等于给已发布快照重新贴标);v3 逐字节不变且继续拒绝 `derived_v4`,v4 继续拒绝 `exchange_v2`(双向互斥已加锁)。`CONSUMABLE_FORMULA_VERSIONS` 扩为二元组(同语义不同来源,不用通配);`save()` 血缘改为声明式并按声明的 schema 版本校验(修掉"始终按 LATEST 校验"的 bug)。
- 真跑:600000/000001 窗口 2025-06-01~08-01(含真实除权日),TDX xdxr → `derived_v4` 快照发布成功,provider check passed(max|diff| 3.3e-07 / 1.69e-04)。
- `docs/user-guide.md` 新增「## 5. 可用数据源」(行情源/复权两条链/新浪/排查指引),后续小节顺延重编号。新浪**如实标注** 185/190 一致、2 处仅分类差异、3 处真实数值分歧,并写明勿装 akshare(pandas 3.x 与锁定的 2.3.3 冲突)。
- **仍未决**:①新浪 3 处真实分歧成因未查,故未接入(仅文档登记);②复权线生产入口脚本仍未接线(目前需调用方提供 bars 与 closes);③`label_set` 生产发布器仍是训练链路最后一洞。

lineage 更正(2026-10-16,cr.md §8.32)
- 用户质疑 `derived_v4` 是否测试反例 → **不是**。它只出现在 `adjustment_snapshot.v1.json` 负向用例组里,该组语义是"v1 快照不得冒用别的东西"(同组还有 v2_formula_const_under_v1_schema 等);`factor-layer-spec.md:673` 明确写着它是"当前实现 lineage"。此前台账把它描述成"应被拒绝的值"是错的,已更正。
- **但版本号选错了**:`hybrid-tushare200-mootdx.md:42` 为 TDX xdxr 链指定 `adj_factor.derived_v1`;`derived_v4` 属于公告派生的修订序列(v2→v3 修过现金分红量纲),TDX 派生是全新第 1 版、无此历史可继承——贴 v4 等于冒充演进史。
- 已改:`TDX_DERIVATION_VERSION` → `adj_factor.derived_v1`;快照 schema 由 v4 改 **v5**,旧 `adjustment_snapshot.v4.json` 删除(**整号退役不复用**,避免"4"带上不同语义);未知版本哨兵 5→6;负向用例改名 `v5_formula_const_under_v1_schema` 并加 note 说明其语义。
- 两处 spec 已对齐,写明 TDX 链是 `derived_v1`(schema v5)、`derived_v4` 仅指公告派生、两者互不可跨版本发布。

收尾三项(2026-10-16,cr.md §8.33)
- **`label_set` 发布器**:新增 `src/uq/models/label_store_writer.py::LabelSetWriter`(布局 `label_sets/generation=<id>/{manifest.json,data.parquet}`)。顺带修两处真缺陷:①`LabelBuilder.build` 只返回 manifest、丢弃帧 → 新增 `build_with_frame()`;②`LabelStore.read_frame` 从不校验 `data_checksum_sha256`(改写 label 后仍干净读回)→ 补校验。该校验暴露 8 个既有 fixture 长期用 `assign(label=0.01)` 伪造标签帧,已改为发布真实输出;phase2 因子/标签窗完全重合致末 5 日无标签被 fail_closed 拒,已把标签历史延伸过因子窗(符合生产口径)。
- **复权线生产入口**:新增 `scripts/build_tdx_adjustments.py`(TDX xdxr → `adj_factor.derived_v1` 快照 + 可复核报告,refused 即 exit 1)。真跑全 A 21 会话窗发布 2 只,0 refused,provider 对账 max|diff| 3.5e-4 且 within public resolution。
- **新浪 3 处分歧裁定**(用 vendor `adj_factor` 跳变反推每股派息):中国平安 2018-06-07 应有≈1.206 → **TDX(1.2)对**;五粮液 1999-06-08 应有≈1.243 → **新浪(1.25)对、TDX(1.0)低估**;平安银行 2007-06-18 送股一致、现金差 0.009 可忽略。**结论:没有来源处处正确**(TDX/新浪/Tushare dividend 各有错)。TDX 那处错在 2015 前、不在 83 事件验证窗内,故该验证不可外推全历史。已入 `docs/user-guide.md` §5.3。
- **仍未决**:新浪未接入(裁定口径已定:三源分歧以 vendor 跳变裁定);复权线未在 A500/全A 试跑窗批量发布;`adjusted_price_binding` 的 `expected_restated` 生产提供者仍未接线。

自 CR 上一轮交付(2026-10-16,cr.md §8.34)
- 对 `a047370` 三项交付做实现级走查,**6 项真缺陷**已修并逐条变异验证:①`LabelSetWriter` 每次发布**泄漏 1 个 fd**(双 `os.open` 只关其一,循环约 60 次即 EMFILE)——改用 `contracts/canonical_v2.fsync_tree` 单一实现;②质量报告只过 schema/status、**从不与本次 subject 对齐**(`bound_generation_id="0"*64`、异 `binding_type` 一律接受,provider callable 直接 `TypeError`)——改走 `resolve_subject_quality_decision` 并校验 family/subject/status;③缺主键列抛 `KeyError` 而非 `ContractError`;④回读 dtype 与**内存帧**比而非与 **manifest** 比(manifest 谎报 dtype 能发布、只在受治理读侧炸);⑤`adjusted_price._report` 交叉核验名**写死**,`independent_restated` 路径因此发布了一份**断言了未执行检查**的报告——改为按实际跑过的检查命名,无交叉核验即拒绝出报告;⑥`events_from_xdxr` 把 `suogu`(缩股)与送股/转增同号折进分母,方向相反会算出倒数型比率;canonical 派生器从不读 `suogu` ⇒ 非零值**拒绝**而非臆造符号(拒绝是定义性的,不依赖"TDX 是否填过该列"的统计——本机未装 mootdx,该类统计本轮无法复核)。
- 另补 2 条既有实现的锁:`_formula_sha256` 必须把"行基准"计入身份;finiteness 收紧**不得吞掉**声明的 null 策略(NaN 标签仍在检查前丢弃)。
- 删除未跟踪的 `tests/test_external_review_20261014_latent.py`(其机制在 `10d2d0e` 已分别锁入既有测试;去重前逐条核对,独有 2 条已补入)。
- **仍未决(新增一条)**:`backtest/engine.py` 首执行日 `prev_close` 退化为当日 open ⇒ 首执行日涨跌停永不触发。会改变已发布 PnL 与既有 golden 证据数值,属需裁决的口径变更,未与本轮"修缺陷"混做。

接线 label_set 入口 + 事件互校(2026-10-16,cr.md §8.35)
- **`scripts/build_label_sets.py`**:canonical bars → 5 日前视标签 → `LabelSetWriter` 发布 + 报告,refused 即 exit 1。真跑 12 会话 ×2 标的:发布 2 份、0 refused、各 12 行含 5 个 null(horizon 尾段),读回通过。接线时修掉 3 处必错:binding checksum 误用 `hashlib(json)`(应为 `sha256_json`)、binding 形状不满足 `label_set.v1` 封闭契约、canonical 列名 `session_date` 而 builder 要 `datetime`。
- **`listing_date` 真实缺口**:canonical v2 无该字段而资格规则要 60 天上市年龄 → 从 `stock_basic(list_date)` 取,取不到即拒(不猜)。**局限已写入报告**:`limit_up/limit_down` 在 canonical 是价格非布尔,涨跌停事件无法识别、该类不剔除。
- **`src/uq/market/corporate_action_reconcile.py`**:两份事件源逐除权日逐字段直接对撞,结论分 `agreed`/`amount_diverged`/`coverage_diverged`。真跑 6 只标的**自动复现**人工裁定:601318 报 `amount_diverged` @ 2018-06-07(公告 2.0/10股 vs TDX 12.0/10股),而 vendor 跳变反推应有 ≈12.06/10股 → TDX 对、公告错。600000 另报 `coverage_diverged`(TDX 多 2026-07-16)。报告不裁决谁对,交给 vendor 因子跳变那道门。
- **仍未决**:①复权线未在 A500/全A 试跑窗批量发布;②`adjusted_price_binding` 的 `expected_restated` 生产提供者未接线;③新浪仍未接入(裁定口径已定)。

复权线批量发布 + 事件互校常规门(2026-10-16,cr.md §8.36)
- **批量真跑暴露 3 项真缺陷**:①`restate_from_events` 的 hfq 方向**整体反了**(取 `ratio = 前收盘/参考价` >1 后**除以** product,且事件集合用 `> target`),与 canonical 派生器(**乘基**、事件作用于除权日、除权日当日本身不缩放)**不同基准**——修正后 000001 在 2025-10-14→10-15 的 vendor 跳变 1.006178 vs 事件跳变 1.005823(差 0.0355%),修正前 gap 达 4.24%;全 A 4932 个"有 vendor 跳变"标的的事件跳变最大相对差 **0.091%**。**是我的代码错、vendor 对**。②无事件标的此前**拒绝整个会话**(全 A 里"从无公司行为"的名字必然存在,会让宽窗永不可发布)⇒ 改为 product=1.0 **平凡可比**。③跳变互校要求两分区行集合**完全相同**(相邻会话 universe 本就变,每个真实窗都拒)⇒ 改为在**两分区共有标的**上比较。
- **事件互校常规门 `scripts/reconcile_corporate_actions.py`**:披露侧取**已发布公告 store**(与派生链同一份记录,不打 API)、TDX 取 `xdxr`,任一分歧即 exit 1。区分 `uncovered`(纯送股披露无法映射成交易所事件形状,结构性排除,**不进失败计数**——算进去会让门在多数真实标的误红)与 `refused`(整只无可比事件,才失败);另修"降级后第二次映射仍失败会让 traceback 中断整轮"⇒ 改干净 refusal。真跑 6 只:3 `amount_diverged`、3 `coverage_diverged`,**两个方向都出现**(601318 公告错 / 000858 TDX 错)。**结构性缺口已登记**:Tushare `dividend` 端点**完全不含配股**(`peigu`/`peigujia`),配股分歧是"披露侧无数据"而非金额争议。
- **复权线批量发布 `scripts/publish_adjusted_window.py`**:逐会话发布 `bars_adjusted/adjusted-v2`,每分区带质量报告(cross-check `tdx_corporate_actions`);**注入一跳基线**(窗内首会话无前驱 ⇒ 向前取一会话作 baseline,报告 `jump_baseline` 且**不发布**);已发布记 `already_published` 可重跑续做;refused 即 exit 1。真跑全 A 21 会话窗:`2025-10-14`(5059 行,gen `129d775d66e3…`)与 `2025-10-15`(5059 行,gen `5b1528220dbd…`)发布成功,**重跑幂等**(published 0)。
- **测试**:新增 `tests/test_publish_adjusted_window.py`(4)、`tests/test_reconcile_corporate_actions.py`(6);逐项变异(每次先清 `__pycache__`)——除法↔乘法、事件边界 `<=`→`<`(**初次未杀**,补非平凡 ratio 的边界测试)、无事件改回拒绝、共有集改回全量、`uncovered` 折回 `refusals`、删第二次映射 guard,全部被杀。全量 **1113 passed / 23 skipped / exit 0**(父节点 `493c0e2` 实测 1103/23,净 +10)。
- **仍未决**:①`adjusted_price_binding` 的 `expected_restated` 生产提供者未接线;②复权线仍未在**更宽的**全 A 窗(42 会话)批量发布;③新浪仍未接入(裁定口径已定)。

自 CR §8.36 提交(2026-10-16,cr.md §8.37,3 项真缺陷)
- **CR37-1(最重要)**:`verify_jump_cross_check` 的跳变容差是**纯相对量**,吸收不了交易所把除权参考价四舍五入到分位产生的残差(≈`tick/前收盘`,是**价格网格的物理分辨率**)。5.56 元名字已用掉 1e-3 预算 **89%**、3 元名字**超预算** ⇒ 一个合法标的会让**整会话被拒**;且旧口径下"四舍五入残差"与"真实 1% 漂移"**不可区分**。修正:加物理下界 `tick/前收盘`(调用方从 `previous_raw` 取),3 元残差放行、同名字 1% 真漂移仍拒。**是我在 §8.36 引入的**。修正后重跑全 A 窗,**generation id 逐字节不变**(下界只放宽门、不改内容)。
- **CR37-2**:`publish_adjusted_window.py` baseline 注入有**死分支**(`end=2100` 回退永不触发)且注释与代码不符 ⇒ 删除死分支,注释改为事实,新增"窗从最早分区开始时首会话被拒(exit 1)、其余照常发布"的锁。
- **CR37-3**:报告的 `baseline_note` 断言了不存在的分支(注入的 baseline 恒在窗外)⇒ 改为描述真实行为。
- **测试**:全量 **1115 passed / 23 skipped / exit 0**(父提交 `9c811ac` 实测 1113/23,净 +2);CR37-1 的下界做了变异验证(删下界即红)。

