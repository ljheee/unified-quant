# 交易级训练升级验收台账

目标和完整验收范围以 [trading-grade-training-plan.md](trading-grade-training-plan.md)
为准。本台账区分功能、测试与真实实验；测试通过不等于收益有效或实盘验收。
2026-10-29 开始实施，工作树基点 `2d713260eef73d595bf75bb1bd06899a8929dad1`。

| 范围 | 当前证据 | 尚待完成 |
| --- | --- | --- |
| 贯穿前置：数据 onboarding、PIT、依赖与生产评审 | 历史数据默认根目录约定为被 gitignore 的 `data/trading-grade-history/`；LightGBM 4.7 进入 `gbdt` extra 与 `dev` 锁定测试环境 | 该目录尚未创建、无历史行情；市值/行业/财报/预期/资金数据契约与 adapter；分析师预期源暂缺；历史退市及新股完整性证据；生产锚 provisioning |
| 阶段 0：冻结原基线 | `evidence/trading-grade/baseline-v1.json` 保存真实 request/run/result、split、特征、模型、成本、制品摘要和数据根；原回测三份制品经 store 读回核验 | 原执行代码/锁摘要为零、原上市日期映射未保存，旧结果仍不能宣称可从零复现；修复后全 A 性能基线尚未测量 |
| 阶段 0：绑定机制 | `provenance.py` 核验 CLI 实际源码/配置和锁文件；`listing_snapshot.py` 保存可离线重放的上市日期映射；`telemetry.py` 保存独立 attempt 与导出文件写入、清单校验、评审绑定、发布前/owning 读回、receipt 绑定和阶段发布计时；新增不可变实验归档入口，读回核验研究结果、配对评估和环境/计时绑定 | 真实全 A 实验归档、性能基线和源码字节完整重放验证 |
| 阶段 1：可信评估 | 原基线已冻结；新增配对已扣成本回测的超额/IR、两种胜率、年度/指定市场状态、回撤恢复、换手及重叠修正 IC/RankIC；报告归档输入并读时重算；新增滚动/扩展 fold 规划、时间隔离、一年覆盖门槛、已评审 request 批量执行/读回与完成结果重试；真实 Qlib 短单折集成已测；标签三问的书面审计与原始行情逐值核算已记录；新增 v3 实际持仓的单票/行业/现金暴露和前日成交额流动性代理诊断 | 扩展历史及真实暖机数据；退市 PIT 源/退出价格/样本缺口解释；至少一年、三状态的真实多折实验；跨 fold 连续资本/持仓与统一基准；等权/市值/行业中性基准构造；受治理历史市值暴露与行业/流动性来源核准；完整 OOS/PIT 证据 |
| 阶段 2：交易与成本 | v3 原始价格和可见执行事实契约；显式参与度与双边部分成交、历史板块/ST/除权参考限价、冲击/最低佣金、行业/单票/现金/调仓/换手限制；已评审基线重放与三种成本压力、资金网格容量诊断入口；旧版本保留 | 历史执行事实源的维护者裁决及真实数据 onboarding；冲击/成交假设校准；真实保守成本下结论稳定性与可承载资金证据；公司行为持仓/现金事件仍未建模，相关标的继续排除 |
| 阶段 3：模型 | `qlib_linear` 保留；LightGBM 已有 model-definition v2、Qlib 训练、受治理 artifact store、日期推理读取；Research Chain v7 request/template 现可选择 LightGBM；另有最多三个候选的验证段 RankIC 选择器 | 与线性基线同特征、样本、split、成本和组合约束比较；至少一年 OOS 的 IR、超额和贡献分散证据 |
| 阶段 4：因子 | 保留 157 个 Alpha158 特征；新增受治理因子日频 IC/RankIC + HAC 稳定性、横截面 Spearman 共线性、top-N 成员换手及绑定已评审 v3 成本配置的成本代理（均为诊断，不自动筛选） | 按披露时间的估值/盈利/成长/杠杆及预期修正/资金流/北向/龙虎榜等数据源；分析师预期数据当前不具备；行业/市值/流动性中性化；单因子增量、实际成交成本和跨时期样本外贡献 |
| 阶段 5：组合与风险 | v1 等权兼容；v2 softmax 分数加权；v3 风险平价（历史样本协方差、严格前序收益、完整窗口/正定校验）与行业中性（top-N 内行业等额预算、行业内等权）已接入；离线收益源绑定交易日历，行业来源绑定 v3 回测配置的开盘前可见 execution facts；v3 industry cap 按比例缩减并留存现金；Research Chain v8 使用 portfolio-definition template v3；v3 最大回撤停止与目标波动率控制可选启用 | 当前无真实历史风险收益面板；实际 PIT 行业源质量和覆盖审计；风险平价/行业平衡在真实成本和多期样本下验收；风险预算与回撤/波动控制的年度实证效果 |
| 阶段 6：执行验证 | 现有确定性 paper 回放保留；新增单日及完整窗口 paper/v3 回测成交差异入口，要求同一目标权重、完整决策日覆盖、下一交易日和五项显式预算 | 使用真实历史同源结果进行逐日对账与预算裁决；生产评审 provisioning；小资金真实成交率/滑点/冲击/权重偏差证据（broker/live 路径仍受既有禁止规则约束，不能自行开放） |
| 性能与总体完成定义 | 已有粗粒度阶段计时入口，计时不改变逻辑结果 | 当前 HEAD 全 A 基线与可重复加速；一年多状态稳定超额、容量和风险合格、标签审计、paper 与小资金验证及下一轮调仓结论全部成立 |

## 已交付入口与复现约定

先运行 `.venv/bin/python scripts/research_environment.py --project-root .` 获取环境
receipt，把其中 `code_fingerprint` 和 `environment_lock_digest_sha256` 写入新 request
的 `environment`，再按原流程计算 identity、进行外部评审与签名。修改源码或配置后要重新
生成绑定，不能替换已发布 request 的字段。dry-run 仍只验证计划，不证明环境匹配。

标签首次发布保存 `listing_date_snapshots/generation=<sha256>.json`，重放时将快照保留
在指定 `--label-root` 并传 `--listing-snapshot-generation <sha256>`。映射缺项或校验
不一致会拒绝发布，不回退实时查询；上市日期被原标签输入 checksum 覆盖，标签 v1 的
公式和资格语义未改变。原基线未保存此映射，禁止用今天的 stock_basic 冒充原输入。

CLI 调用 runner 正常结束时返回独立 `telemetry_path`，Python 异常退出也保存失败记录；
同一 run 的重试使用不同 attempt 文件，不覆盖旧测量。该文件包含实际 HEAD、源码清单、
锁摘要和运行环境观察，计时不会
写进逻辑制品。总耗时从构造计时器开始，不含此前请求解析、上游 store 解析和 CLI
初始化；真实全 A exporter 性能验收仍未完成。

实验命名约定：`<universe>-<oos-start>-<oos-end>-<protocol>-<model>-<variant>`。
每个实验使用新 request identity，重试沿用同一个 request/run 并追加 attempt；新实验
使用新 run。归档置于 `evidence/trading-grade/experiments/<name>/`，应包含 request、
result binding、环境 receipt、计时、评估协议和指标重算入口、外部评审与限制声明；
市场数据与 Qlib bin 仍仅放 gitignored store。当前原基线归档独立于实验目录，不覆盖。

## 验证范围

新增测试覆盖脏工作树/配置/锁变动、零摘要拒绝、CLI 真实观测绑定、独立失败/重试计时、
静态评审路径、快照篡改/路径/覆盖缺项，以及同一映射离线重放生成相同标签 identity。
Qlib 测试覆盖真实阶段计时不改变结果 manifest，以及失败 export 停止后续阶段。
本轮实际执行：

- `.venv/bin/python -m pytest`：1304 passed / 31 skipped。
- `.venv-qlib/bin/python -m pytest`：1334 passed / 1 skipped；该跳过项为
  `tests/test_inference.py::test_inference_refuses_without_the_qlib_extra`，安装 Qlib
  时不适用，已用 `-r s` 单独核验原因。
- 完整套件之后补强根目录软链接用例，单独重跑
  `tests/test_listing_snapshot.py tests/test_training_provenance.py`，两环境均通过；
  Qlib 环境连同 `tests/test_inference.py` 为 17 passed / 1 skipped。
- 在独立 Python 进程中禁用 `verified_runner_identity` 的比较条件，两条摘要不匹配
  用例均因未抛异常而失败，确认守卫回归能检出失效；未修改磁盘源码。
- `git diff --check` 通过；`scripts/research_environment.py --project-root .`
  命令行 smoke 成功，扫描 287 份源码/配置文件并验证输出身份。

首轮两套完整测试在证据索引更新前读取了 runner 的旧摘要，导致两项 freshness 检查
失败；完成 re-pin 后单独检查和上述完整重跑均通过。未变更依赖或锁文件。
本轮验证的是上述 HEAD 的未提交工作树；原有 `AGENTS.md` 修改及规划文件保持原样。
全 A 重训、实时源、认证 gate/远程矩阵及实盘未运行，未作发布或生产可用声明。

## 第二轮：可重算评估入口

实现和完整接口见 [training-evaluation.md](subsystems/training-evaluation.md)。新增
`scripts/evaluate_training.py` 从两个受治理回测 store 读取结果，对比相同的成本、资金、
成交/限价规则以及价格、日历、universe、停牌、除权绑定。回测配置/结果摘要与签名会
再核验；日历不一致、窗口缺失、配置差异均拒绝，不内连接后静默截取。

报告为明确的 `diagnostic_only_no_quality_authorization`，不替代质量批准或生产评审。
需要 IC 时同时绑定已发布预测、标签和其已评审输入数据集 manifest，核对标签 generation
及预测日期落在声明的 test split；数据集特征 payload 不重新读取，也不声称重演原训练。
若预测行含 `datetime`，还必须与所发布的 decision date 一致，避免把别日分数改记为
样本外预测。lower-better 分数按显式语义取负。

每次报告读取都会核验输入表 checksum/shape 并重算全部指标。测试包括重哈希后的伪造
指标、篡改制品/配置/签名、原子写入失败、重复发布和软链接边界。HAC 手算用例区分
名义股票行数与有效交易日；缺失截面保留交易日间距，不压缩为相邻日期。

本地原基线作过只读 smoke：以同一回测结果作为两侧输入、显式选取
`2025-12-10..2025-12-24`，得到 11 个观测区间、10 个持仓区间，两个胜率分别为
45.4545% 和 50%；同结果对比的超额为 0、IR 为 null，符合定义。这只是读回/计算验证，
不是新的基准实验或策略有效性证据，没有改写原 store。

隔离进程中分别禁用基准可比性及重叠标签 lag 门槛，四条不公平对比用例和一条 IC
overlap 用例全部按预期失败，确认回归能检出守卫失效；磁盘源码未改动。
本轮没有新增依赖或修改锁文件。历史扩窗、walk-forward、基准组合、暴露、标签执行腿
差异和退市 PIT 审计等完整要求继续保留，不以诊断报告发布代替这些验收。

最终工作树验证：`.venv/bin/python -m pytest` 为 1339 passed / 31 skipped，
`.venv-qlib/bin/python -m pytest` 为 1369 passed / 1 skipped，`git diff --check`
及新 CLI 的 `--help` smoke 通过。版本仍为 `2d713260eef73d595bf75bb1bd06899a8929dad1`
的未提交工作树；精确运行源码摘要、锁摘要、实现/测试文件摘要和检查结果已记录在
`evidence/trading-grade/evaluation-implementation-v1.json`。跳过项沿用可选运行时条件；
未运行认证 gate/远程矩阵、历史重训或实盘。

## 第三轮：walk-forward 协议与逐折执行

新增 `scripts/run_walk_forward.py`，`--mode plan` 从已发布的全窗口日历生成不可变
rolling/expanding 计划，`--mode execute` 批量调用既有研究入口处理已评审的逐折 request。
完整入口与限制见 [training-evaluation.md](subsystems/training-evaluation.md)。

计划固定至少 60-session 暖机、覆盖标签 horizon 的 purge、额外 embargo、不重叠且
连续的测试日期及观察标签所需尾段；不足一个完整末折时明确列出未分配日期，或由
配置显式启用 partial fold。默认执行门槛同时检查 240 个样本外 session 和 365 个
日历日，短实验必须显式 `--allow-short-diagnostic`，仍不通过一年覆盖项。这个门槛
和日历预算不证明实际 bars/factor 历史完整，也不证明多市场状态，相关字段保持 false。

第一折前批量核验 request identity、阶段计划评审、split/window、固定模型/组合/缺失
策略/环境、源码与锁摘要、标签 horizon/摘要、回测配置签名/窗口/日历，以及成本、
成交、限价和初始资金一致性。执行关闭外部分数，沿用 owning runner 的拟合和推理。
每折结果重新读取完整 dataset payload、模型 run/artifact、预测、回测及其跨层绑定；
所有 test 日期必须恰好出现一次，实际特征/因子版本/标签语义不得漂移。

失败会停止后续折，保留已核验前缀、失败折的 request/run/data-root 上下文，重试产生
独立 attempt。已完成折先从原 request/run 唯一结果读回，并重复 owning-layer 核验；
不再进入结果发布器，不改 ledger 或 run id。未完成折沿用既有 stage resume。计划读取
会按日历/spec 重建，attempt 读取检查 checksum、scope、覆盖与折顺序；重新哈希的
伪造一年资格或缺折 passed 记录也会拒绝。

各折回测初始资金独立，当前不拼接其收益或宣称组合连续。自动产出/评审所有 fold
数据输入、实际一年历史多折运行、跨折持仓/现金/末期清仓及统一基准仍待实现/验证。
新增的真实 Qlib 测试是短合成单折，覆盖拟合、自动推理、回测、完整读回及完成结果
重试；多折控制流与预检另有测试，不能据此宣称真实年度策略验收完成。

接入时确认旧 `backtest_result.v1` 在全空仓/零方差下拒绝引擎输出的 null Sharpe；
已加行为复现并记录在 `cr.md` 本轮核查，后续版本化回测升级必须处理，未原地改旧契约。
隔离内存 mutation 分别禁用 purge、一年门槛、完成结果重试，三条目标回归均失败。
未新增依赖或变更锁文件，未写入真实数据 store，未调用实时源或实盘。

本轮验证：`.venv/bin/python -m pytest` 为 1372 passed / 32 skipped；
`.venv-qlib/bin/python -m pytest` 为 1403 passed / 1 skipped。新增两文件 targeted
suite 在普通环境为 33 passed / 1 skipped，在 Qlib 环境为 34 passed。
`git diff --check` 与新 CLI `--help` smoke 通过。基点仍为
`2d713260eef73d595bf75bb1bd06899a8929dad1` 的未提交工作树；运行源码/锁及实现/测试
摘要、实际命令结果和未完成项已归档于
`evidence/trading-grade/walk-forward-implementation-v1.json`。未运行认证 gate/远程矩阵，
没有作发布、年度策略有效性或生产批准声明。

## 第四轮：v3 交易约束、成本与容量诊断

新增 `backtest_config.v3` / `backtest_result.v3`，保持 v1/v2 的已发布字段和行为。
v3 使用原始 `bars_daily/v2`，按 T 收盘决策、T+1 开盘执行、当日收盘记账；参与度
是必填配置，使用前一 session 成交量，买卖均可部分成交。旧 v1 的空仓 null Sharpe
拒绝回归继续保留，v3 明确支持未定义统计量的发布与读回。

`publish_backtest_execution_facts.py` 归档离线日级执行事实，`publish_backtest_config.py
--contract-version 3` 将确切身份和 checksum 纳入外部配置评审。表中保存 pre-open
可见的板块、ST/停牌、限价带、除权参考和原始前收、整手/最低买入、行业；不按今日
状态或固定 10% 推断历史规则。事实表必须覆盖整个日历，交易标的缺记录即拒绝。
源审批引用只保留 provenance，不伪装成独立签名或已完成历史源准入。

新增线性/平方根冲击、最低佣金、双边滑点、卖出税、数量相关资金检查、实际换手、
调仓频率和成交后净值上的单票/行业/现金限制。原始开盘价超出已发布限价为数据错误，
滑点/冲击后的假设成交价也不能越带。被动收盘价格造成的风险超限单独记录，不声称
在停牌/封板下仍能强制满足收盘预算。公司行为的持仓/现金事件尚未建模，相关标的
继续在原路径中排除。正式研究回测 adapter、评估和 walk-forward 预检已接入 v3。

`run_backtest_stress.py` 先从真实 owning stores 重放既有 v3 基线，逐值核对三份制品和
summary，再对同一权重/行情运行佣金（含最低费）翻倍、滑点翻倍、成交率减半和显式
资金网格。每个配置/结果分别取得外部评审，禁止改旧签名配置或复用旧审批；重试
读回已有且唯一匹配的已评审制品。`backtest_stress.v1` 只发布诊断，读取时重算指标、
精确变体与结论；重哈希伪造容量、篡改原始数据、未定价订单不能通过资格判断。
最大资金仅指给定阈值下合格的已测网格点，实际容量仍未校准，收益与交易资格不作
推断。源准入、真实保守成本结论与资金规模证据继续待完成。

验证包括真实 canonical 质量读回、事实表 immutable/篡改/软链接、生产 CLI 参数与
外部 reviewer、实际回测阶段发布/评估读回、成本批次/容量/重试和逐折 v3 源预检。
独立进程内分别注入卖出漏扣滑点、使用未来流动性、风险净值忽略费用，三条目标回归
均实际失败；源码未落盘修改。未新增依赖或变更锁文件，未写真实数据根或触碰实时源。

修复尾数清仓及研究阶段的目标权重 owning 读回后，最终普通环境完整套件为
`1425 passed / 32 skipped`（90.72 秒）；Qlib 环境完整套件为
`1456 passed / 1 skipped`（156.09 秒）。相关定向套件 `76 passed`，
`git diff --check` 与三个新/扩展 CLI 的 `--help` smoke 均通过。精确 HEAD、源码/
锁摘要、文件摘要、命令、反证与限制已归档于
`evidence/trading-grade/backtest-v3-implementation-v1.json`。这是本地未提交工作树
验证；认证 gate、远程矩阵、历史真实数据发布和交易结论仍未执行。

## 第五轮：标签三问的基线只读审计

用原脚本的 `ManifestFirstReader` 读取 `data/root-2025` 的 160 个旧清单 bars，
`LabelStore` 读取原标签；780,955 条非空标签全部按原 `close × adj_factor` 公式逐值
重算成功。新增 [`trading-grade-label-audit.md`](trading-grade-label-audit.md) 说明五日
重叠、自相关的 HAC 最低四 session 纪律，以及收盘标签与下一开盘入场/出场之间的
实际样本差异。780,955 条可比入场腿平均差为 −3.1075bps、35,468 条正负号翻转；
可比完整开盘腿 775,795 条平均差 +0.0680bps、50,822 条翻转。数值是标签行差，
没有交易成本或成交约束，不当作策略收益。原数据 116 个标的早于窗口尾消失，
567 条 null 标签早于全局末五个 session；没有逐日历史退市可见状态和退出价格，
历史 universe/PIT 完整性仍未验收。报告与摘要在
`evidence/trading-grade/label-design-baseline-v1.json`，真实数据根只读。

脚本默认原始 `bars_daily/v2`，所以此前对原基线“重复乘复权因子”的怀疑经核查
**不成立**；但显式 `--schema bars_adjusted.adjusted-v2.yaml` 会使 v1 公式错误，现于
发布入口提前拒绝。该修复不改变旧标签 generation 或 v1 公式。

本轮源码状态的完整测试：`.venv/bin/python -m pytest` 为
`1427 passed / 32 skipped`（84.18 秒），`.venv-qlib/bin/python -m pytest` 为
`1458 passed / 1 skipped`（136.57 秒）；标签定向测试 `18 passed`，
`git diff --check` 通过。精确 HEAD、源码/锁摘要、文件摘要、命令和限制见
`evidence/trading-grade/label-audit-implementation-v1.json`。这是本地未提交工作树；
认证 gate/远程矩阵及生产发布未运行。

## 第六轮：Qlib 导出细分计时

新 attempt telemetry 将 Qlib 导出的 feature-bin 写入、文件清单/校验和、持久发布
分别计时；失败写入会记录 `failed` 子阶段，已复用的完整导出阶段仍记为 `reused`，
不会编造子阶段耗时。计时 callback 不写入导出 manifest、Qlib 二进制文件或研究结果
identity。直接 exporter 对比测试证明有/无计时得到相同 manifest；真实 Qlib 短链路
测试核对所有子阶段及同一结果。尚未测量真实全 A export 性能，也未细分外部评审
绑定或 adapter 读回时间。

导出器曾对 staging 目录分别执行空文件扫描和文件清单扫描；现合并为一次遍历，
保留空文件拒绝、每个文件的哈希/字节数记录、发布前读回及 fsync。新边界回归
证明合并后零字节文件仍拒绝且记录失败的清单子阶段；未测量全 A 的实际加速幅度。

最终本轮源码状态的完整测试：`.venv/bin/python -m pytest` 为
`1430 passed / 32 skipped`（87.76 秒），`.venv-qlib/bin/python -m pytest` 为
`1461 passed / 1 skipped`（132.82 秒）；两环境导出/telemetry/研究链定向测试与
`git diff --check` 均通过。精确 HEAD、源码/锁和文件摘要见
`evidence/trading-grade/qlib-export-observability-v1.json`。这仍是本地未提交工作树，
没有运行认证 gate、远程矩阵或真实全 A 导出。

## 第七轮：不可变实验归档入口

新增 `scripts/archive_trading_experiment.py` 和 `trading_experiment_archive.v1`。
入口用固定命名组件归档已通过的研究结果、原 request、环境源码/锁 receipt、
passed attempt 计时、已扣成本的配对评估、外部评审 checksum 引用和明确限制。
发布及每次读回都会通过 owning stores 复查 request/result/backtest/config，
重算评估表指标，再以现有策略和基准回测重新构造日收益与可比性绑定。
同名不同证据拒绝覆盖；合成五日 scratch 实验验证相同重试、CLI、错位计时、
源评估篡改、错误模型命名和重算 checksum 后伪造指标/回测绑定的拒绝。
归档还保留策略、基准两侧回测与配置的评审摘要引用。

归档是证据索引，不是质量批准。它没有复制全部外部评审正文，没有从历史仓库
重建当时源码字节，也没有重读 IC 来源的预测/标签 payload；一年 OOS、PIT
完整性及真实全 A 重放仍未证明。旧基线的零代码/锁摘要不能作为新实验的
可复现绑定通过此入口。

最终本轮源码状态的完整测试：`.venv/bin/python -m pytest` 为
`1437 passed / 32 skipped`（87.93 秒），`.venv-qlib/bin/python -m pytest` 为
`1468 passed / 1 skipped`（132.96 秒）。实验归档定向测试 `7 passed`；
`compileall`、归档 CLI `--help` 与 `git diff --check` 通过。精确 HEAD、源码/锁
摘要、文件摘要与验证范围见
`evidence/trading-grade/experiment-archive-implementation-v1.json`。认证 gate、
远程矩阵及真实全 A 归档未执行；本轮为本地未提交工作树。

## 第八轮：Qlib 导出评审与预发布读回计时

在原 `feature_files`、`file_inventory`、`durable_publish` 三个子阶段之间，
新增 `review_binding` 与 `prepublish_readback`。前者测量 export identity、
外部评审绑定和评审报告持久化；后者测量 staging manifest 写入与所有文件的
发布前 checksum 读回。两项只进入 attempt telemetry，不改变逻辑 manifest；
已有有/无计时导出对比及真实 Qlib 短链路重跑均通过。外部评审 provider 注入
失败时记录失败子阶段并清理 staging。adapter 的再次读回与 receipt 绑定仍在
粗粒度 `qlib_export` 耗时内，真实全 A 性能尚未测量。

本轮最终源码状态的完整测试：`.venv/bin/python -m pytest` 为
`1438 passed / 32 skipped`（85.13 秒），`.venv-qlib/bin/python -m pytest` 为
`1469 passed / 1 skipped`（131.91 秒）；相关定向测试分别为 `41 passed` 和
`52 passed`。`compileall` 与 `git diff --check` 通过。最终统一的 HEAD、源码/
锁及文件摘要见
`evidence/trading-grade/experiment-archive-and-qlib-observability-v1.json`；
第七轮的归档收据记录的是增加两个计时点之前的工作树。认证 gate、远程矩阵、
真实全 A 性能测量和一年样本外实验仍未执行。

## 第九轮：Qlib adapter 读回、receipt 与状态发布计时

研究链 Qlib 导出 adapter 新增 attempt 级 `owner_readback`、
`receipt_binding`、`stage_publication`，以及仅在 immutable export 恢复路径出现的
`retry_readback`。`receipt_binding` 包含缓存边界检查及 receipt builder；失败时
记录异常类型和耗时，后续模型训练不启动。真实 Qlib 短链路验证新增计时仍得到
完全相同的结果 manifest，注入 receipt 失败用例核对失败阶段与后续停止。
这些计时没有写入逻辑制品，阶段总耗时仍包含输入数据准备等未拆分开销。
真实全 A 耗时和从零重放仍待历史数据与评审输入。

最终本轮源码状态的完整测试：`.venv/bin/python -m pytest` 为
`1438 passed / 33 skipped`（122.55 秒），`.venv-qlib/bin/python -m pytest` 为
`1470 passed / 1 skipped`（185.40 秒）。研究链定向测试两环境均通过；
`compileall` 与 `git diff --check` 通过。精确 HEAD、源码/锁和文件摘要见
`evidence/trading-grade/qlib-adapter-observability-v1.json`。未运行认证 gate、
远程矩阵或真实全 A 导出；本轮为本地未提交工作树验证。

## 第十轮：paper 与 v3 回测成交差异诊断

新增 `scripts/reconcile_paper_backtest.py` 和
`src/uq/evaluation/paper_reconciliation.py`。单日对账读回 v3 回测、回测日历和目标
权重，以及 paper 配置、订单计划和结果；只有目标权重 generation/digest 一致、
paper 执行日恰为决策日下一回测 session 才比较。逐订单记录缺失、请求/成交股数、
已成交价格及费用差异；预算五项必须显式给出且非负。CLI 只输出诊断，不发布
质量决定。paper 的全有全无执行与 v3 的部分成交/冲击模型不同，预算合格不等于
经济假设一致，更不证明真实成交。

本轮本地回归包括纯数值/错误输入用例，以及真实已评审 v3 回测、目标权重、日历
读回配合隔离 paper reader 的绑定控制流测试；尚未在同源真实历史 paper/v3 制品
上运行对账，也未覆盖小资金实盘。最终未提交工作树的完整套件：普通环境
`1444 passed / 33 skipped`（132.24 秒），Qlib 环境 `1476 passed / 1 skipped`
（207.31 秒）；定向对账测试 `6 passed`，`compileall`、CLI `--help`、
`git diff --check` 通过。精确 HEAD、源码/锁摘要、文件摘要和限制见
`evidence/trading-grade/paper-backtest-reconciliation-v1.json`。认证 gate 和远程
矩阵未运行。

## 第十一轮：完整窗口 paper/v3 成交对账

在单日入口上新增 `--paper-generations-json`。窗口模式从已发布 v3 回测的目标权重
绑定确定预期决策日集合，逐一核验 paper 结果、计划、配置、目标权重和下一交易日；
缺结果、重复结果、跨日冒充或回测成交落在未覆盖决策对时拒绝。每个回测结果/日历
只读一次，成交表按决策/执行日分组，避免随 paper 日期数重复全表扫描。逐日差异
仍可见，五项预算改对完整窗口的累计订单/股数/费用和最大价格差判断；全窗口
无订单单列，不作预算通过声明。

隔离测试用真实已评审 v3 回测/目标权重/日历，配合受控 paper store reader 验证
覆盖、汇总和错误路径。尚未取得同源真实历史 paper/v3 成对制品，一年样本外与
实盘执行结论仍未作出。最终未提交工作树完整回归：普通环境
`1445 passed / 33 skipped`（89.83 秒），Qlib 环境 `1477 passed / 1 skipped`
（148.51 秒）；定向对账测试两环境均为 `7 passed`，`compileall`、CLI `--help`
和 `git diff --check` 通过。精确 HEAD、源码/锁及文件摘要见
`evidence/trading-grade/paper-backtest-window-v1.json`。认证 gate、远程矩阵、
真实历史同源对账与实盘未运行。

## 第十二轮：v3 实际持仓暴露诊断

新增 `scripts/evaluate_portfolio_exposures.py`。入口从受治理 v3 回测结果、原始
行情和已绑定的开盘前执行事实读回数据，以实际成交而非目标权重重建每日持仓，
并与已评审净值表的持仓金额逐日核对。输出单票最大权重、权重 HHI、行业权重、
现金权重及持仓金额相对上一交易日成交额的流动性代理。缺失行业或上日流动性
以权重单列，不静默填补；没有历史市值数据时不编造市值暴露。诊断不作质量批准
或实际容量结论。

合成双标的用例覆盖行业/集中度/流动性数值及缺源标记；真实已评审 v3 scratch
回测用例覆盖 owning 读回、CLI 和原始行情篡改拒绝。历史执行事实源审批、真实
全 A 暴露及一年样本外稳定性仍未验收。最终未提交工作树完整测试：普通环境
`1450 passed / 33 skipped`（92.81 秒），Qlib 环境 `1482 passed / 1 skipped`
（157.09 秒）；暴露定向测试两环境各 `5 passed`，`compileall`、CLI `--help`
和 `git diff --check` 通过。精确 HEAD、源码/锁和文件摘要见
`evidence/trading-grade/actual-exposures-v1.json`。认证 gate、远程矩阵、
真实历史同源源准入、全 A 年度实验和实盘未运行。

## 第十三轮：LightGBM 验证段候选选择

新增 `src/uq/models/lightgbm_selection.py` 和
`scripts/select_lightgbm_candidate.py`。入口只接受 `DatasetWriter.read` 已校验的
数据集，重验 train/validation/test 切分和 purge/embargo；候选数限制为 1–3，
以验证段日均截面 RankIC 选择，固定种子和线程后仅在 train 段重训，
只导出 test 预测。回执绑定数据集 generation、manifest 和 data checksum、特征顺序、
split 窗口、试验值以及模型和预测校验和。测试改变 test 标签后断言候选、模型字节
和预测不变，证明测试标签没有参与选择。LightGBM 4.7 加入 `gbdt` extra 与 `dev`
测试环境；数据根路径记为 `data/trading-grade-history/`，该目录尚未创建，也不会自动填充。

该候选仍未进入已评审 `model_artifact`/推理发布链，也尚未与 `qlib_linear` 用同一
组合和成本做年度 OOS 对比。当前分析师预期数据源缺失，特征未伪造；paper 保持确定性
模拟，只用于流程及成交差异检查，没有真实历史同源年度运行或小资金真实成交。

本轮定向测试两环境均 `2 passed`；普通环境完整测试 `.venv/bin/python -m pytest`
为 `1452 passed / 33 skipped`，Qlib 环境 `.venv-qlib/bin/python -m pytest` 为
`1484 passed / 1 skipped`。LightGBM CLI `--help`、离线 lock 解析及
`git diff --check` 均通过；认证 gate、远程矩阵和真实历史 OOS 未执行。
本轮代码指纹 `d4d08f6f8758e1b120e249c156f74d085a7507a922e8d9236eb119550ca6c602`、
HEAD `2d713260eef73d595bf75bb1bd06899a8929dad1`、锁摘要及文件校验和见
[`lightgbm-candidate-implementation-v1.json`](../evidence/trading-grade/lightgbm-candidate-implementation-v1.json)。

## 第十四轮：LightGBM 正式制品与日期推理

新增 `model_definition.v2`，保留 v1 线性算法契约不变。`qlib_lightgbm` 在 v2
下通过 `QlibRuntimeTrainer` 训练，只使用声明的训练 split；制品经现有
`ArtifactStore` 外部质量评审与读回路径发布。运行清单绑定 Qlib 版本、LightGBM
版本、导入路径及 joblib serializer。`score_decision_dates` 现在能加载此 artifact，
检查两侧 runtime 版本并对 Qlib 特征进行预测。Qlib 全链路测试覆盖定义 v2、实际
LightGBM 训练、artifact publish/readback 和指定交易日推理。

候选选择器在验证段选参后只用 train 段重训，与 qlib_linear 基线保持相同训练
窗口。研究链 request v1–v6 仍固定使用 model-definition template v1，尚不能在
正式 request 中选择 LightGBM；新增 request/template 与对应编排接入待完成。年度
OOS、同成本组合对照、真实市场样本和收益贡献证据仍未取得。

定向验证：普通环境 `tests/test_model_definition.py` 为 `14 passed`；Qlib 环境
`tests/test_model_end_to_end.py::TestEndToEndPipeline::test_full_chain_from_factor_to_prediction`
为 `1 passed`，覆盖 LightGBM 训练、评审发布和 Qlib 导出推理。完整测试在 v7 接入后
重跑；认证 gate 和远程矩阵尚未运行。

## 第十五轮：Research Chain v7 LightGBM 请求接入

新增 `research_run_request_v7` 与 `model_definition_template.v2`。v7 显式约束
`qlib_lightgbm` 和 `joblib-v1`，并继承 v6 的样本构造、PIT membership 与 paper
输入。执行阶段顺序没有变化，因此 request 绑定现有 v6 stage-plan digest 与其已签
激活评审；v1–v6 request 和 model-definition template v1 保持原样。解析、runner、
dataset stage、模型定义创建、state/result 版本枚举和 CLI 路由均接入 v7。

新增回归验证 v7 schema 与请求 identity、v6 stage-plan 签名复用、runner 计划检查、
LightGBM v2 model definition 生成，以及线性/v1 template 被拒绝。现有 Qlib 模型端到端
回归仍覆盖实际 LightGBM 训练、受评审 artifact 发布/读回和日期推理。完整 v7 真实
请求执行、一年 OOS 及线性基线同成本组合比较仍待真实、完整历史数据与已发布输入。

## 第十六轮：组合 v2 分数加权

`portfolio_definition.v1` 与 template v1 保持 top-N 等权；新增 v2 schema/template，
允许等权兼容或 `score_weighted`。分数加权选择固定 top-N 后按
`softmax((score - max(score)) / temperature)` 分配权重，拒绝非有限分数和无效温度；
之后应用单票上限、现金预算和既有换手限制。单票封顶后可保留额外现金，不将超出的权重
未经声明地重分给其他标的。Research Chain v7 固定使用 template v2。

新增测试覆盖 softmax 比例、现金预算、非有限分数拒绝、v7 template 路由及合成组合定义
读回。普通环境定向测试 `25 passed`；Qlib 环境 v7/LightGBM/研究链定向测试 `6 passed`。
当前普通环境完整套件为 `1460 passed / 33 skipped`；Qlib 环境为
`1492 passed / 1 skipped`；`git diff --check` 通过。环境/源码指纹、锁摘要和文件摘要见
[`lightgbm-score-weighted-chain-v1.json`](../evidence/trading-grade/lightgbm-score-weighted-chain-v1.json)。

风险平价仍需受治理历史收益/协方差输入；行业中性仍需行业 PIT 分类输入。最大回撤和目标
波动调仓仍需独立策略控制语义与历史组合状态。本轮没有接入这些输入，也没有声称真实样本
收益验收；至少一年 OOS、同成本线性模型对照、真实 paper/回测逐日预算裁决与小资金成交
仍未完成。

## 第十七轮：v3 回撤停止与目标波动率控制

v3 回测配置新增可选 `max_drawdown_stop` 与 `volatility_target`。回撤相对本次回测以来
最高收盘净值计算；达到阈值后状态锁存为退出，并在后续 session 持续按既有 T+1、成交量、
涨跌停、停牌、换手和费用规则尝试清仓。目标波动率用决策日可见的已完成日收益估计样本
标准差，按 `sqrt(252)` 年化，仅在最少历史够用后将目标权重缩至不高于原仓位；不会加杠杆。
两者均进入外部评审绑定的 backtest config，输出到每日指标与结果摘要；没有绕过执行限制，也
不替代 Risk Control Plane policy/state/action 治理。

新增 v3 回归覆盖回撤触发后的锁存清仓、目标波动率只用已完成数据降仓，以及 CLI 参数和 schema。
普通环境完整测试 `1462 passed / 33 skipped`（80.19 秒），Qlib 环境完整测试
`1494 passed / 1 skipped`（126.28 秒）；组合/v7/回测定向测试 `56 passed`，发布 CLI
`--help`、schema JSON 解析和 `git diff --check` 通过。精确 HEAD、源码指纹、锁摘要和本轮
文件摘要见 [`backtest-risk-controls-v1.json`](../evidence/trading-grade/backtest-risk-controls-v1.json)。
历史数据根仍为空，因此一年 OOS、同成本线性基线对照、风险平价、行业中性与真实历史 paper
对账未验收；认证 gate、远程矩阵和实盘也未运行。

## 第十八轮：受治理因子的单因子筛查诊断

新增只读 `scripts/screen_factors.py` 与 `factor_screen_report.v1`。入口要求每个交易日恰有
一个已发布因子分区，并绑定标签、完整交易日历、源码指纹和锁摘要；报告列出逐因子日频
IC/RankIC、考虑标签重叠的 HAC 稳定性、日频横截面 Spearman 共线性及等权 top-N 成员变动
换手代理；可选绑定已评审 v3 回测成本配置，按双边比例佣金、卖出印花税、双边滑点和
配置参与率上限的冲击成本，生成每次调仓的 bps/Nav 成本估计。该代理忽略最低佣金、真实
成交/持仓约束、价格、整手和容量，不能替代回测成本。缺失日期保留在日历上并中断换手
转换；报告为诊断专用，既不判断因子方向，也不自动筛选或删除因子。

普通环境因子筛查定向测试 `12 passed`；普通环境完整套件 `1474 passed / 33 skipped`
（94.02 秒），Qlib 环境完整套件 `1506 passed / 1 skipped`（149.80 秒）。CLI `--help`、
schema JSON 解析、两个证据索引测试（`2 passed`）与 `git diff --check` 通过。源码/锁指纹和本轮文件摘要见
[`factor-screening-v1.json`](../evidence/trading-grade/factor-screening-v1.json)。已有 157 个
因子现可按发布分区执行诊断，但新基本面/分析师预期/资金源、PIT 中性化、单因子增量及至少
一年跨状态 OOS 仍未完成；分析师预期源缺失，没有合成替代数据。

## 第十九轮：风险平价接入

组合定义 v3 与 Research Chain request v8 已实现风险平价路由。先按现有 score policy
选 top-N，再使用已发布日收益的样本协方差求等风险贡献权重；默认建议窗口为 60 个交易日，
窗口长度必须由评审请求明确固定。新增离线收益发布器要求精确列 `date/instrument/return`，
记录数据源与审批引用，校验日期、代码、数值、日历 generation、日历 manifest 和数据 checksum。
解析阶段校验绑定身份、回看历史及完整研究日历；组合阶段仅使用决策日前已收盘的窗口，缺失
收益、非有限得分、非正定协方差或 solver 未收敛时拒绝发布。单票、现金和换手约束在求解后
应用，可能使最终组合偏离等风险贡献。

本轮覆盖离线 publisher、不可变 store/tamper、风险预算解算、严格前序可见、缺失数据拒绝、
v8/v3 契约与阶段顺序兼容、既有 v7 与 portfolio 回归。实际验证：

- `.venv/bin/python -m pytest`：1488 passed / 33 skipped。
- `.venv-qlib/bin/python -m pytest`：1520 passed / 1 skipped；唯一跳过是已安装 Qlib 时
  不适用的“缺少 Qlib extra 时拒绝推理”用例。
- `git diff --check` 通过。

工作区没有真实历史风险收益面板；测试只证明算法和治理失败路径，不证明实际策略收益。
下一步仍需导入经评审的 adjusted-close 日收益、扩展窗口暖机数据并跑同成本 OOS 验收。

## 第二十轮：行业预算与上限

portfolio v3 新增 `industry_neutral`：按 score policy 选 top-N，在当日代表行业间均分预算，
行业内等权。该口径是组合内行业平衡，不声称相对基准中性。`max_industry_weight` 现在对
v3 按比例缩减超限行业并留作现金；如果 turnover blending 会重新突破行业上限，构建器拒绝
发布，避免在两个约束冲突时静默违反上限。v1/v2 原行业上限拒绝行为保持不变。

行业来源必须绑定同一 request 的 v3 backtest config 中的 `ExecutionFactsStore` generation，
且 `industry_column=industry`。execution facts 的 `visible_at` 必须早于当日开盘，解析阶段读取
并重核 manifest/checksum、请求窗口覆盖及分类存在性，portfolio stage 按每个 decision date
读取对应标签；任一已选标的缺分类就拒绝。定向回归覆盖 sector equal budget、比例 cap、缺标签、
事实 generation 不匹配与历史 v1/v2 失败语义。

定向 portfolio/v7/v8/execution-facts 测试：38 passed。最终全量测试：普通环境 1490 passed /
33 skipped；Qlib 环境 1522 passed / 1 skipped；`git diff --check` 通过。Qlib 唯一跳过项是
已安装 Qlib 时不适用的缺 Qlib extra 拒绝推理用例。

本轮和风险平价共用 Research Chain v8 与 portfolio v3；仍没有真实历史分类面板供行业收益或
行业风险预算验收，因此只证明功能路径，不证明数据源全面或 OOS 行业效果。
