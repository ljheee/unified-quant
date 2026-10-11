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
  - 4b. [未开始,等待接入 resolver] factor 阶段成员过滤:对每个交易日用 `IndexMembershipStore.as_of` 取成员,替代 `UniverseSnapshotStore.read_members` 的单窗口读取。
  - 4c. 读回与绑定校验:runner `_readback`、model/backtest 阶段绑定校验改为校验快照集合的 digest。
  - 4d. 测试:覆盖跨月窗口、PIT 边界(快照日当天不可用)、篡改快照被拒绝、与旧 v1 绑定的隔离。
  - 不变量:任何训练窗口内的每个交易日,成员只能来自 `visible_from <= 该交易日` 的快照。
- [ ] 5. 训练前覆盖校验 fail-closed(成员数 500、Alpha158 每只至少 60 个交易日、5 日 label 需未来数据、adj_factor/涨跌停/停牌/上市退市字段缺失率)。
- [ ] 6. 数据面补齐:`adj_factor`、`stock_basic`、`suspend_d`、`stk_limit` 的 importer,落为可校验 artifact。
- [ ] 7. 20 只烟雾测试(2025-08-29 至 2025-09-30)端到端跑通。
- [ ] 8. 500 只端到端与证据归档到 `evidence/`。

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
