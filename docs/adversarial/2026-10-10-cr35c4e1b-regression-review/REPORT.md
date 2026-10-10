# 对抗审查报告：commit 35c4e1b 回归检查（cr.md C1–C3 + H1–H13 修复）

- 审查对象：commit `35c4e1b`（"fix: close cr.md critical/high findings C1-C3 and H1-H13"，49 文件 +1230/−89）
- 审查目标：本次改动是否引入新问题（回归、语义破坏、治理旁路、测试盲区）
- 审查模式：adversarial，4 视角隔离审查 + 匿名交叉对抗（cross-1）+ 单项 cross-2
- 审查日期：2026-10-10；监工会话：unified-quant-c7（sid c4236205-1769-4c92-9749-b07cb201a1b4）

---

## 一、TL;DR（确认发现，按严重度排序）

本次修复**方向正确、16 项中 13 项闭合**，但引入 **1 个 BLOCKER 级回归** 与 **8 个 MAJOR** 级问题；3 项修复（H3/H5/H6）只部分闭合。

### BLOCKER（1）

- **U1｜H2 修复使文档化默认 ingest 路径（`uq-ingest daily`，不带 `--cross-validate`）每次运行必被拒。** cli.py:55 默认构造 `build_tdx_pipeline` → daily.py:104/:123/:323-326 装配 `bars_daily.research-v1.yaml`（compare_with:tushare）→ gate.py:129 新增的 comparator-unavailable error 分支在无 tushare comparator 时必 error → 必 rejected。旧 gate 同路径 accepted=True（git-show 探针），新 gate accepted=False；`build_tdx_pipeline` 零测试覆盖，592 全绿未捕获。**四路独立 A 级复跑确认。**

### MAJOR（8）

- **U2｜H11 指纹算法变更使全部存量三族 artifact（factor partition / model dataset / feature preprocessing）重读必失配。** 无迁移层、cr.md 第六节未声明兼容切断。机制锚点：raw_price.py:53-54（null_mask 独立前置哈希）vs store.py:549-551（读路径指纹比对）。
- **U3｜C1 快照 schema 原地 const 翻转 v1→v2（adjustment_snapshot.v1.json:26 `const: "adj_factor.exchange_v2"`）。** 存量 v1 快照 manifest 重读即 ContractError；无版本并存窗口、无重导出工具；文件名/snapshot_version 仍为 v1，名实不符。**违反 AGENTS.md「canonical schema 语义变更须出新版本」。**
- **U9｜H5 新增 execute 期决定核验可被跨 subject 重放击穿。** 签名字段集（model_layer.py:744-753）不含 subject；subject 在验签之后（:832-840）才由调用方自盖章；backtest 路径 checksum 被丢弃（adapters.py:719-727 `config_report, _ =`，对照 preprocessing 路径 :283-290 有比对）。三路独立探针确认。
- **U10｜H3 新聚合防线可被伪造行业 taxonomy 满足。** industry_mapping 是未绑定的调用方入参，从不与 policy 声明的 membership 快照核对（engine.py:256-263 只查存在性）。
- **U14｜H6 仅修 resolve 期崩点，provider 模式 execute 期仍必崩（runner.py:405 等 8 处无 subject 取决定点）。** 原洞后移未闭合。
- **U15｜H5 新增 bind 消费原始签名报告形态（11 字段裸集），与 resolver 对同一 mapping 键强制的 quality_decision.v1 信封形态字段互不相交。** CLI 单通道流程（cli.py:327-364 → resolver.py:597-607 信封强制）下新校验在字段集检查处即 fail-closed，永远到不了验签/验状态；闭合仅在绕过 resolver 的 adapter/runner 级流程成立。
- **U20｜C2 的 QlibRuntimeTrainer 半边零回归锁。** qlib_runtime.py:145 新校验全部 6 个调用点均在 qlib skipif 门控测试内且全是 happy-path；「恰好一个 train split」「export 日历不覆盖」的拒绝路径无任何断言——qlib 未安装环境（含本次 592 全绿）该校验零执行。（ModelTrainer 半边有真锁 test_model_trainer.py:243；RC U7 为此条的较弱形式，归簇。）
- **U21｜H9 的 worker datetime64 保留行在任何环境都无回归锁（含 CI qlib job）。** test_qlib_adapter.py:440 所谓 dtype preservation 测试只断言帧可被 worker 接受、不断言输出 dtype；pyarrow 对字符串列静默接受，worker 改回 str 转换全套测试照绿。（路径勘误：factors/qlib_adapter.py:83-85，非 models/。）

### MINOR（8）

- **U4｜H12 白名单使 schema 合法但不符合 `[a-z][a-z0-9_-]{1,63}` 的 universe_id（单字符/大写/含点）运行时被拒；契约 schema 未同步收紧。** 仓内现有 id 全部兼容，属前瞻性不一致。
- **U5｜H1 fail-closed 过度覆盖：已退市股票"上市前"缺口（修复前正确豁免 not_listed）也被改判 unknown_requires_review，历史回补合法流程被误杀。**（lifecycle.py:47-51）
- **U6+U17 归簇｜C1 的版本闸门是哑的：`_LINEAGE_VERSION` 升为 exchange_v2 但全库零引用、formula_version 只写不读**——v1/v2 语义区分仅靠快照 schema const 在读端兜底（U3），calculate_adjusted_factors 自身无法识别喂入因子基准。两视角独立发现，档位收敛 MINOR。
- **U11｜H2 零基期修复只堵 |primary|==0；|primary|==inf 在两个分支都静默豁免。**
- **U12｜H2「最小覆盖率强制」生产不可达：唯一生产构造点（daily.py:323-326）不传 minimum_coverage，契约配置亦无此键**（config.py:17 `_CONFIG_KEYS` 全列无 minimum_coverage，亲查）。
- **U16｜H10 注入点未延伸到兄弟消费方：accepted_store.py:40/:113 两处 read_factor_partition(partition) 仍走源码树重建 registry**——发布期/接受期 registry 不一致的同类窗口在 accept 侧残留。
- **U22｜H5 新增「not approved」断言被先存的 registry 状态检查（model_layer.py:709-710）满足，adapters.py:279/:726 的状态行删除后测试仍全绿。** 定性经攻防修正为「配置条件性冗余」（allowed_statuses 配置下可激活），非纯逻辑死代码。
- **U23｜cr.md 6.1 表三行归因错误（锁定测试挂错对象）：** C1 行引用 test_adjusted_price_engine.py 实为 H7 之锁；C3 行 phase1/3 实为 H3/H8 之锁；H1 行引用的 test_cr_fixes.py 对 H1 零覆盖（真锁在 test_real_data_policy.py:56）。

### 观察（4）

- **U8｜H5 为 Dataset/BacktestStageAdapter.run 新增必填参数**：绕过 CLI 的程序化 execute 调用方若持最小 decision 映射（缺 label_set/backtest_config 两族）执行期被拒。影响面经两条款收窄登记。
- **U13｜H13 provenance 只查存在性**：任意合法 64hex 即通过，唯一下游核对是与运营者自撰 config 的相等性。
- **U18+U24 归簇｜H11 修复后 +inf 与 −inf 仍互相碰撞**（null_mask 在 inf→NaN 替换之前采样，两者同落 (None, mask=False)）；commit message「NaN/-0.0/inf no longer collide」对 inf 类内不成立。系存量行为未扩大。两视角独立发现。
- **U7｜见 U20 归簇**（C2 qlib 校验零执行的观察级表述）。

### cross-2 单项（1）

- **X1｜cr.md:239 H6 修复记录「provider-json 模式不再必崩」无端到端测试证据**——其唯一回归测试（test_research_chain_phase1.py:520）只锁 resolve 半段，原理上无法见证 execute 期崩点。**cross-2 终裁：MINOR 确认**——RC/GB/FC 三方独立复核一致确认（:520 测试体通读、全 tests/ provider 通道 grep 零命中、证伪判据独立重跑不成立），零反驳零新增；RC 补机制根佐证锚点 cli.py:203-215（provider 模式无条件返回 callable 闭包 → runner._decision 无 subject 必抛）。GB 措辞锐化附注：按端到端口径「不再必崩」**事实不成立**（崩点从 resolve 期 TypeError 后移至 execute 首站 runner.py:405 ContractError），不改动定档——本条登记文档-证据缺口，实质不可用由 U14（MAJOR）承载。

### 16 项修复闭合总账

**13 项闭合 + 3 项部分闭合：**
- H3【部分】max(values)/TypeError 原路径已堵；伪造 taxonomy 稀释未堵（U10）。
- H5【部分】伪造决定/状态翻转已堵；跨 subject 重放未堵（U9）+ CLI 通道形态不兼容（U15）。
- H6【部分】resolve 期已修；execute 期仍必崩（U14）。

---

## 二、争议项

无未决争议。cross-1 全部 24 条（含归簇）达成收敛：22 条至少两路独立一致确认；U19 经攻防后作者自撤（见撤回记录）；档位/归簇类调解 4 起（U6≈U17、U7→U20、U18≈U24、U22 定性修正）均不构成事实层反驳。cross-2 单项 X1 经 RC/GB/FC 三方独立复核一致「确认」（MINOR 维持，零反驳零新增），对抗全部收敛。

少数意见附注：无（多数确认+少数反驳且多数方未回应反证的情形未出现）。

---

## 三、撤回记录

- **U19（FC F6）【明确撤回】** H2 gate.py:124-125「field_name not in accepted.columns: continue 静默跳过，配置 typo 可致规则永不校验」。作者依自设证伪判据自撤：config.py:88-94 加载期已强制 cross_validation 字段 ⊆ schema.fields，pit_store.py:37 / schema.py:42-44 发布期拒绝缺失字段——「规则指向主源不存在字段」在加载/发布两道闸前不可达。作者自撤时**尚未看到**另外三路独立反驳（RC 指 config.py:92-93、TI 补 pit_store.py:37、GB 指 daily.py:323 唯一生产调用方），四方结论收敛。判「明确撤回」，未占用 cross-2 轮次。**此条是审查过程可信度的核心自证：证伪判据机制按设计工作。**

---

## 四、覆盖对账（16 材料 × 4 视角）

单元格为该视角落在该材料锚点上的 finding 数（round1+cross-1 回应合并计数；0 = 未触及）：

| 材料 | RC | GB | FC | TI | 主要落点 |
|---|---|---|---|---|---|
| C1（adjustment 快照/复权） | 2 | 2 | 2 | 2 | U3、U6/U17 |
| C2（trainer/qlib_runtime） | 1 | 1 | 2 | 1 | U20（+U7 归簇） |
| C3（risk/contracts） | 1 | 1 | 1 | 1 | 无新发现（闭合） |
| H1（lifecycle） | 1 | 1 | 1 | 1 | U5 |
| H2（quality/gate） | 1 | 1 | 1 | 1 | U1、U11、U12、U19（撤回） |
| H3（risk/engine） | 1 | 1 | 1 | 1 | U10 |
| **H4（execution/planner）** | **0** | **0** | **0** | **0** | **盲区→监工补查（见下）** |
| H5（research_chain adapters/runner） | 1 | 2 | 2 | 2 | U8、U9、U15、U22 |
| H6（research_chain cli/provider） | 0 | 2 | 2 | 3 | U14、X1 |
| H7（factors/adjusted_price） | 1 | 1 | 1 | 1 | 无新发现（闭合） |
| **H8（risk/state）** | **0** | **0** | **0** | **0** | **盲区→监工补查（见下）** |
| H9（factors/qlib_adapter） | 1 | 1 | 1 | 1 | U21 |
| H10（factors/store） | 1 | 1 | 1 | 1 | U16 |
| H11（factors/raw_price 指纹） | 1 | 0 | 1 | 1 | U2、U18/U24 |
| H12（contracts/artifacts 白名单） | 1 | 0 | 1 | 1 | U4 |
| H13（contracts/model_layer） | 1 | 1 | 1 | 1 | U13 |

**盲区补查（监工亲查，未经对抗，如实标注）**：H4 与 H8 四视角零锚点。监工亲查两处 diff（各仅 2 处改动）：H4 sell 循环零数量行 continue（该循环仅更新现金、无其他副作用；负数量不可达因 planner 自产 mod100/≤sellable 非负）、buy 侧 fee=gross>0?event_fee:0 与 engine filled>0 才收费对齐；H8 成对守卫前置 raise、旧 None 检查降级为 isinstance(Mapping)（None 已被前置守卫排除），非 Mapping payload 的 `.get` AttributeError 系修复前既有行为非新引入，payload-only 调用方被拒系 H8 本意 fail-closed。**补查结论：两项未见新问题。** 此结论未经隔离对抗，置信低于矩阵其余部分。

---

## 五、视角清单

| 视角 | 通道 | 审查清单 | 排除项 | 标注 |
|---|---|---|---|---|
| RC（regression-compat 回归兼容性） | subagent（用户批准「我直接 spawn」） | 行为变更对存量数据/默认路径/程序化调用方的兼容冲击；cr.md 声明与实际切断的一致性 | 不评修复方向正确性本身；治理伪造面归 GB | 4 路全上（用户裁决） |
| GB（governance-bypass 治理旁路） | 同上 | 新增校验可否被伪造输入/重放/自盖章旁路；fail-closed 是否有残留豁免路径 | 存量数据兼容归 RC；测试充分性归 TI | 同上 |
| FC（fix-closure 修复闭合性） | 同上 | 16 项修复是否真闭合原洞；cr.md 断言与代码证据是否相称；修复是否延伸到全部同类点 | 不重复 RC 的存量兼容面 | 同上 |
| TI（test-integrity 测试完整性） | 同上 | 回归测试是否真锁住修复（拒绝路径断言、环境门控盲区、锁定测试挂错对象） | 不评治理可伪造性（归 GB） | 同上 |

备选区无转正；无用户移除视角。round1 结束后无新增视角诉求。

---

## 六、强制性披露

1. **匿名化边界**：cross-1 分发包以并集编号（U1–U24）匿名，发现 1 处署名泄漏（RC F6 正文引「视角①」）已在发包前中和为通用措辞；N=4，匿名化非形式统一。防内容跟随而非完全盲审。
2. **隔离残余边界**：worker 技术上可读 .supervisor/ 下分片文件，协议明确指令「只读点名分发包」且无激励越界；隔离目标是「不因正常执行而泄漏」。未发现越界迹象。
3. **锚点抽查结果（merge 期监工亲查 18/18 通过）**：批次1（8/8）：README.md:22 默认 ingest 命令行、gate.py:127-131 comparator-unavailable 分支、adapters.py:272-281 label bind、runner.py:403-411 三族取决定点（label_set 带 subject、另两族不带——不对称实锤）、model_layer.py:226-229 H13 双条款、lifecycle.py:47-51 退市判定、qlib_runtime.py:143-149 单 train split+日历覆盖、factors/qlib_adapter.py:83-85 H9 datetime64 行。批次2（10/10）：raw_price.py:53-54/:72 指纹机制、engine.py:256-263 聚合存在性校验、artifacts.py:23-33 白名单正则、config.py:17 _CONFIG_KEYS 无 minimum_coverage、adjustment_snapshot.v1.json:26 const=exchange_v2、store.py:549-551 读路径指纹比对、accepted_store.py:40/:113 两调用点、model_layer.py:744-753 验签调用、cli.py:55+daily.py:323-326 默认装配链、resolver.py:597-607 信封强制。另有 merge 前 6 项核查（echo 一致性 4/4、U21 路径勘误亲验、cr.md 行号锚点 3 处、adapters.py:725-726 if/raise 行号订正、tests/ 全库 grep provider 三符号零命中）。**未发现锚点造假。**
4. **零反驳强制重审执行情况**：**未触发**——cross-1 存在 3 起事实层反驳 + 1 起撤回 + 2 起裁决修订（FC 对 H5/H3 改判部分闭合）+ 1 起条款收窄（RC F8），事实层非零反驳，兜底重审条件不成立。
5. **紧凑回显偏离**：cross-1 下发动用逃生口（大 findings 集全量回显有撑爆 subagent 上下文的实跑风险），以「编号+档位+标题」紧凑回显替代全量；偏离已记 decisions，事后四方 echo 抽查 4/4 一致。
6. **subagent 通道记要**：①~4KB idle 通知截断 1 起（TI Finding 5 正文被截，尾标缺失触发点名索取，重发件原文回填入账）；②流式停摆 1 起（GB/FC 批间空闲 40 分钟——subagent 回合结束不续跑，轻量回执作续跑触发后恢复，此后每批即时回执）；③模型重开决策：原 adv-* 四方以 adv-*-2 重开（原因记 decisions）；④长内容一律专用分发包+指针消息，worker 只读点名包。
7. **Loop Guard**：未触发（cross 最多 2 轮内收敛；cross-2 仅单项一轮）。
8. **降级/减员**：无。4 视角 round1+cross-1 全齐；cross-2 由 RC/GB/FC 三方执行，TI 作为 X1 作者方依协议回避。
9. **勘误记录**：U21 锚点路径 models/qlib_adapter.py → factors/qlib_adapter.py:85（TI 一手确认）；U23 行号勘误；adapters.py:724→:726 raise 行号订正（三方分发包同步更新）。
10. **U19 撤回故事**：见撤回记录——作者依自设证伪判据自撤，与三路未见面的独立反驳收敛，判「明确撤回」。
11. **H4/H8 覆盖盲区**：见覆盖对账——监工补查未经对抗，报告如实降级标注。
12. **peer 安全**：全程无 worker 消息被当作用户授权；无权限 launder。

---

## 附：审查统计

- 视角 4 路，全部 subagent 通道（用户明示批准）；round1 findings 24 条（RC 8 / GB 5 / FC 6 / TI 5），cross-1 回应登记 72 条（RC 16 / GB 19 / FC 18 / TI 19，均含「共 N 条+END」尾标核验），cross-2 回应登记 3 条（RC/GB/FC 各 1，尾标核验通过；FC 重发件按去重纪律不重复入账）。
- 收敛结果：BLOCKER 1、MAJOR 8、MINOR 8（含 2 起归簇）、观察 4（含 1 起归簇）、撤回 1、cross-2 单项 1（确认，MINOR）。
- 监工锚点亲查累计 24 处（18 抽查 + 6 前置核查），零造假。
