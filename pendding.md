# A500 训练数据接入待办(当前有效版本,2026-10-11)

决策:
- 成分来源:Tushare `index_weight(000510.SH)`,月度近似(非严格 PIT)。取数用当月第一天至最后一天。
- 扩展:已支持中证全指 `000985.CSI`(`scripts/fetch_index_membership.py --preset csi-all`),作为全 A 近似;成员数不固定(约 5000),不做数量校验。北交所统一使用后缀 `.XBSE`(Tushare `.BJ`),沪深仍为 `.XSHG`/`.XSHE`。
- 不实现事件表,不实现 v1 月度快照的单窗口读取。
- 成分取数结果落盘为不可变 raw artifact,记录 `source_fetched_at` 与 SHA-256,训练只读该 artifact(不在训练时实时查询 Tushare)。
- 详细生效规则与限制见下文"A500 成分:月度近似方案"。

待办(按依赖顺序):
- [ ] 1. 实测 `index_weight` 发布滞后,确定 `publish_lag_days`;未实测前使用 5 个交易日并标注为假设。
- [x] 2. 定义月度快照 raw artifact 与 manifest 契约(主键 `(snapshot_date, instrument)`,记录 `source_fetched_at`、SHA-256、`publish_lag_days`、`visible_from`)。
- [x] 3. 改造 `scripts/fetch_index_membership.py`:写入不可变快照 artifact(经 `IndexMembershipStore`),不再输出 CSV 作为训练输入。脚本与测试已完成;正式 store 为 `data/a500-store/`,待用该路径正式拉取后勾选。
- [x] 9. 北交所 instrument 支持:新增 `.XBSE` 后缀(映射、`StaticUniverseLoader` 正则、`index_membership_row.v1` 正则),并补测试。`adjustment_snapshot`/`golden_provenance` 已发布契约未改动。
  - [ ] 9a. lifecycle 的北交所名单未接入(akshare 无北交所列表接口已确认前不改):当前 `.XBSE` 一律返回 `unknown_requires_review`,fail closed。接入名单后再加前缀规则 `43/83/87/92`(Tushare 当前上市北交所代码全为 `92` 开头)。
- [ ] 4. `as_of` 查询接入 research_chain(拆分为子步骤,按序实施):
  - 4a. [已完成] 绑定模型(已决定:快照集合绑定,方案 1;基础模块 `build_membership_set` / `verify_membership_set` 已实现并测试,尚未接入 resolver):
    - 新增绑定类型 `index_membership_set`,字段:`universe_id`、`index_code`、`window_start`、`window_end`、`snapshots`(按 `snapshot_date` 升序的列表,每项含 `snapshot_date`、`visible_from`、`generation_id`、`manifest_digest_sha256`、`members_checksum_sha256`)、`set_digest_sha256`。
    - `set_digest_sha256` = canonical JSON 的 SHA-256,覆盖 `snapshots` 全部字段与窗口;任何快照增删或内容变化都会改变集合 digest。
    - 窗口覆盖校验:窗口内每个交易日必须存在 `visible_from <= 该日` 的快照,否则拒绝(fail-closed)。
    - 旧 `universe_snapshot` v1 绑定保持不变,新旧绑定通过 `family` 区分,不互相替换。
  - 4b. [已完成:绑定校验已接入 resolver;按日过滤函数已实现并有测试,待 v4 执行路径开启后挂载到 dataset 阶段] `src/uq/research_chain/membership_input.py`:`verify_request_input_sets` 由 store 重建两类集合并与请求摘要比对;`filtered_factor_frame` 对每个交易日取该日可见成员(每个快照只读一次成员)并与当日因子分区求交。
  - 4c. [部分完成] resolver 已在 v4 分支校验两个集合摘要;runner `_readback` 与 model/backtest 阶段的集合摘要校验待 v4 执行路径开启后接入。
  - 4d. [已完成] `tests/test_research_membership_input.py`(11 项):跨月窗口逐日成员、快照日当天不可见、成员字节篡改、两个集合摘要漂移、窗口早于首个可见快照 fail closed、绑定窗口与请求窗口不一致、resolver 治理参数缺失、v1/v3 解析不受影响。
  - 不变量:任何训练窗口内的每个交易日,成员只能来自 `visible_from <= 该交易日` 的快照。
- [x] 5. 训练前覆盖校验 fail-closed(成员数 500、Alpha158 每只至少 60 个交易日、5 日 label 需未来数据、adj_factor/涨跌停/停牌/上市退市字段缺失率)。
  - 已实测(2026-10-11,`scripts/probe_reference_data_coverage.py`,中证全指约 5100 只,3 个抽样交易日,证据 `evidence/reference-data/csi-all-coverage-probe.json`):`adj_factor` 与 `stk_limit` 整市场按 trade_date 取,成员覆盖率 1.0,无非正/空因子;`suspend_d` 与 `dividend`(按 ann_date)为事件型,每日 4~22 只停牌、分红 1~217 条,低覆盖属预期而非缺陷。
  - 已做(2026-10-12):`src/uq/pipeline/readiness.py`(纯函数,9+2 项规则测试)+ `scripts/validate_training_readiness.py`(退出码 0/1/2)。规则:会话越出窗口、成员快照按日不可见、bars 会话缺口、universe 成员无 bars、单标的会话数下限(默认 60)、必需字段空值率、`adj_factor` 非正、价格非正、交易日缺 `limit_up/limit_down`、label 前视窗口(`usable_label_end`)。bars 经 `ManifestFirstReader` manifest-first 读取,成员经 `IndexMembershipStore.as_of` 取当日可见快照。
  - 真实数据验证(2026-08-03~08-09,`data/bars-store` 3 只 + `data/csi-all-store`):5 个会话、5129 成员、全字段空值率 0、`adj_factor` 与既有 golden 官方累计因子一致(000001=139.008、600000=17.3774);唯一阻断项为 5126 只成员无 bars(仅入库 3 只),即预期的 fail-closed。
  - 已做(2026-10-12):停牌与退市已进入就绪门。`suspend_d` 使缺口区分三态(全窗停牌 / 可交易日数不足 / 无解释缺口,另有 `suspension_bar_conflict`),`stock_basic(list_status=D)` 把整窗零日线且已退市的成员降为 informational。限制:2000 积分取不到 `out_date`,故退市豁免只覆盖「整窗无日线」;停牌记录本身也可能滞后于真实停牌日(实测 601989 的 `suspend_d` 只到 2025-09-04)。
  - 已做(2026-10-12):因子/label 联动断言。`assess_factor_label_linkage`(`src/uq/pipeline/readiness.py`)对声明的 factor 分区会话做 fail-closed 校验:分区不得越出窗口、必须覆盖窗口首个交易日、且**不得读到 `usable_label_end` 之后**(那些决策日的 5 日 label 还不完整);空 factor 层单列 `no_factor_sessions`。窗口不足 label horizon 时该规则静默,避免与 `label_horizon_unsupported` 重复报。`scripts/validate_training_readiness.py` 新增 `--factor-partition-set`(接受 `factor_partition_set` 绑定 JSON 或裸日期列表),新增 metrics `factor_sessions` / `label_only_sessions`。真实 store 实测(A500 2025-08-29~09-30):factor 会话止于 2025-09-23 → `passed`;含其后 5 个会话 → `blocked`(`factor_reads_past_label_end`),`factor_sessions=23, label_only_sessions=5`。测试 `tests/test_training_readiness.py` 15→27 项。
  - 注意:交易日历仍来自 Tushare `trade_cal`(与主源同商),`bar_session_gap` 只证明内部一致性。
- [ ] 6. 数据面补齐:`adj_factor`、`stock_basic`、`suspend_d`、`stk_limit`、`dividend` 的 importer,落为可校验 artifact。
  - 修缺陷(2026-10-12,严重):`TushareAdapter.fetch` 允许 `daily` 的某个批次(50 只)静默返回空,整批标的丢失仍按 SUCCESS 发布。实测 A500 全量窗口 23 个分区中,2025-09-10 少 49 只、2025-09-19 少 50 只(恰为一个批次),分区状态均为 `published`;这些数据直到训练前就绪门才被查出。上游数据完整(同日重查 49/49 全部可得),属我方缺陷。现已由 `_daily_batch` 处理:非空批次返回空→重试一次→仍空则报可重试 `UPSTREAM_ERROR`,不再发布短面板。修复后重灌 23/23 分区,行数只剩 498/499(停牌所致),就绪门的批次缺口全部消失。
  - 附带修正:适配器错误路径此前一律回报 `schema_version="research-v1"`,v2 请求失败时会被 router 判成信封不符(`primary_source_unsupported_request`),掩盖真实的上游错误类型;现改为回报请求实际推导出的版本。
  - 说明:v2 请求中"标的无日线但 suspend_d 未覆盖"仅记为 warning,不在适配器阻断。原因实测:601989(中国重工,后续吸收合并退市)自 2025-09-04 起不再出现在 `suspend_d`,但持续停牌,若在此阻断会误拒 23 个会话中的 18 个。可解释/不可解释由 `src/uq/pipeline/readiness.py` 在全窗口上下文判定。
  - 已做:`TushareAdapter` 新增 `_call` 统一错误映射与 `adj_factor`/`stk_limit`/`suspension`/`dividend`/`stock_basic` 端点(实测 2000 积分均可用)。
  - 已做(决定:复用 `config/datasets` 契约,不新造存储):`TushareAdapter.fetch` 支持 `bars_daily` v2 字段集(`bars_daily.v2` 早已定义 `status`/`limit_up`/`limit_down`/`adj_factor`),按交易日拉整市场 `adj_factor`/`stk_limit`/`suspend_d` 并合并;`config/datasets/bars_daily.v2.yaml` 的 tushare `provides` 已补齐这些字段。实测 2026-08-05 三所标的(含 `920438.XBSE`)字段齐全,北交所涨跌停为 ±30%,证明必须用 `stk_limit` 而非硬编码 ±10%。
  - 已做(2026-10-12):除权除息披露的生产 importer。实测 Tushare `dividend` 不是稳定主键事件表:同一 `(ts_code, end_date, div_proc)` 会被原地修订(实测 `cash_div_tax` 0→0.02),且存在完全重复行;因此 `src/uq/contracts/dividend_events.py` 以**公告日为分区键、规范化整行的 canonical SHA-256 为 `record_id`** 存原始披露记录——字节级重复收敛为一条,内容修订各留一条,`ex_date` 只作为字段不参与分区(无前视)。契约 `config/schemas/contracts/dividend_event_partition.v1.json`;`scripts/fetch_dividend_events.py` 逐公告日拉取并发布(不可变、可断点续跑)。实测 A500 窗口 2025-08-29~09-30:28 个公告日、3337 条记录(仅 2025-08-29 就有 1714 行→1710 条,4 条完全重复被收敛),证据清单 `evidence/reference-data/dividend-events-2025-08-29-to-2025-09-30.json`(由 `scripts/build_store_evidence.py --kind dividend` 复读生成);无负值/`ex_date<ann_date` 违例。注意:该 store 是原始披露记录,**不会**自行推断 `ex_date` 事件分区,`XdxrAdjustmentDeriver` 仍是唯一的复权因子推导入口(目前仍仅被测试调用,尚未接生产调整链路)。
  - 未做:把 dividend 记录接入 `XdxrAdjustmentDeriver` 生产链路(需要按 ex_date 取 `pre_close`,并定义"实施"记录选择规则)。
  - 已做:`build_tushare_v2_pipeline`(`src/uq/pipeline/daily.py`)接入 v2 日线链路。因 `status`/`limit_*`/`adj_factor` 目前只有 Tushare 能提供,v2 的主源改为 tushare、tdx 降级为 close/volume 对照源(与 research-v1 相反);`config/datasets/bars_daily.v2.yaml` 的 tdx `provides` 相应收窄(不再声明 `status`/`adj_factor`),`MootdxSourceAdapter` 现按请求字段返回 `session_date`(v2)或 `datetime`(research-v1)。
  - 修缺陷:`config/schemas/bars_daily.v2.yaml` 的 instrument `pattern` 原写作 `'...\\.(XSHG|XSHE)$'`,YAML 单引号内是字面双反斜杠,正则等价于"反斜杠+任意字符",会拒绝全部真实代码;已改为 `'^[0-9]{6}\.(XSHG|XSHE|XBSE)$'`(与 `index_membership_row.v1` 一致,含北交所)。
  - 修缺陷:三个 pipeline builder 的默认 `project_root` 取 `Path(__file__).parents[2]`,而 `daily.py` 位于 `src/uq/pipeline/` 下三层,实际解析到 `<repo>/src`;此前只因 CLI 显式传 `--project-root` 未暴露。已改为 `parents[3]`。
  - 修缺陷:对照源若交付的帧缺少契约主键列(例如仍按 `datetime` 键),原实现会在 merge/verified-row 处 `KeyError`;现以 `comparator_key_missing` fail closed。
  - 未做:v2 链路尚无独立交易日历(覆盖率仍为 `unverified`,报告带警告);v1 交叉校验链路使用 akshare 日历。
  - 未做:`adjustment_snapshot`/`golden_provenance` 等已发布契约的 instrument 正则仍只允许 XSHG/XSHE,与 `index_membership_row.v1`(含 XBSE)不一致;放宽属语义变更,需按规则开新版本。
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

待办:
- 在接入财务数据前,用 `f_ann_date` 与查询日构造 PIT 测试样例,并确认 `update_flag` 语义(需核实文档或用多版本样本推断)。

# A500 成分:月度近似方案(2026-10-11 决策)

决策:
- 采用 Tushare `index_weight` 月度数据作为 A500 成分来源,是"月度 PIT 近似",不是严格逐日 PIT。
- 取数按官方建议:`start_date` 为当月第一天,`end_date` 为当月最后一天;每个自然月对应一个快照(已核实 2025-08 至 2026-09 共 14 个月,每月恰好一个快照)。
- 快照的 `trade_date` 为指数公司发布的快照日,位于月末附近(样本:2025-08-29、2025-09-30、2025-11-28、2026-01-30 等)。快照日不等于披露日,也不等于生效日。

生效规则(防前视,强制):
- 快照不得在"当月第一天"生效。一个快照只能在其 `trade_date` 之后的第一个交易日起使用,并且再加 `publish_lag_days` 个交易日作为保守可见时间。
- `publish_lag_days` 在实测前取保守值(待定,建议 5 个交易日),并写入 manifest。
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
- 决策点:窗口起点早于首个可见快照时当前直接失败(fail-closed),不做空段降级。是否改为显式报告空缺段待确认。
