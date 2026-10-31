# 交易级训练升级验收台账

目标和完整验收范围以 [trading-grade-training-plan.md](trading-grade-training-plan.md)
为准。本台账区分功能、测试与真实实验；测试通过不等于收益有效或实盘验收。
2026-10-29 开始实施，工作树基点 `2d713260eef73d595bf75bb1bd06899a8929dad1`。

| 范围 | 当前证据 | 尚待完成 |
| --- | --- | --- |
| 贯穿前置：数据 onboarding、PIT、依赖与生产评审 | gitignore 的 `data/trading-grade-history/` 已含 2024-01-01..2026-10-30：4 类全市场日频 raw Tushare 输入、SW2021 Y/N 行业成员、37 个 `csi-all` 月度成分快照和 683 个 v2 canonical bars 分区；7 类财报/预测端点已做单股真实权限试查；新增 immutable Tushare candidate builder，可复核源快照与治理日历绑定 | readiness 阻断：17 个成员行情缺口不能被 `suspend_d` 解释，2 个标的不足 60 个行情日；14/17 当前出现在退市名单但缺历史退市日期，不能据此豁免。新候选历史快照未保存当时 provider vintage，清单明确 strict PIT=false，尚无经审评的 PIT exposure/factor panels；SW 有效期不等于 transaction-time PIT；`report_rc` 无法支持完整面板；生产 reviewer key provisioning |
| 阶段 0：冻结原基线 | `evidence/trading-grade/baseline-v1.json` 保存真实 request/run/result、split、特征、模型、成本、制品摘要和数据根；原回测三份制品经 store 读回核验 | 原执行代码/锁摘要为零、原上市日期映射未保存，旧结果仍不能宣称可从零复现；修复后全 A 性能基线尚未测量 |
| 阶段 0：绑定机制 | `provenance.py` 核验 CLI 实际源码/配置和锁文件；`listing_snapshot.py` 保存可离线重放的上市日期映射；`telemetry.py` 保存独立 attempt 与导出文件写入、清单校验、评审绑定、发布前/owning 读回、receipt 绑定和阶段发布计时；新增不可变实验归档入口，读回核验研究结果、配对评估和环境/计时绑定 | 真实全 A 实验归档、性能基线和源码字节完整重放验证 |
| 阶段 1：可信评估 | 原基线已冻结；新增配对已扣成本回测的超额/IR、两种胜率、年度/指定市场状态、回撤恢复、换手及重叠修正 IC/RankIC；报告归档输入并读时重算；新增滚动/扩展 fold 规划、时间隔离、一年覆盖门槛、已评审 request 批量执行/读回与完成结果重试；真实 Qlib 短单折集成已测；标签三问书面审计；`audit_label_design.py` v2 绑定独立治理日历、逐日核对原始分区并验证每个非空标签的精确五 session bars grid；新增 v3/v4 实际持仓单票/行业/现金暴露与前日成交额流动性代理；新增等权/市值/行业中性 benchmark owning panel 与同配置 v3/v4 发布入口；连续共享账户 OOS 回放与期末清仓当前限定 v3 | 新 2024-2026 canonical bars 上的 readiness/质量复核通过后，重发 factor/label/export 并完成至少一年、三状态的真实多折实验；退市 PIT 源/退出价格/样本缺口解释；停牌/涨跌停/参与率下未完成清仓时的真实历史处置验收；受治理历史市值暴露与行业/流动性来源核准；完整 OOS/PIT 证据 |
| 阶段 2：交易与成本 | v3 原始价格和可见执行事实契约；显式参与率与双边部分成交、历史板块/ST/除权参考限价、冲击/最低佣金、行业/单票/现金/调仓/换手限制；已评审基线重放与三种成本压力、资金网格容量诊断入口；新增 v4 公司行为事件面板：除息日应收入 NAV、支付日现金可用、送股上市日入账且次 session 才可卖，权利发行标的全窗口排除，缺覆盖/关键日期/碎股 fail-closed；旧版本保留 | 历史执行事实、分红与权利发行源的真实全窗口审计；冲击/成交假设校准；真实保守成本下结论稳定性与可承载资金证据；v4 仅 synthetic 验证，未有真实事件源验收；paper 对账与连续跨折回放仍限 v3 |
| 阶段 3：模型 | `qlib_linear` 保留；LightGBM 已有 model-definition v2、Qlib 训练、受治理 artifact store、日期推理读取；Research Chain v7 request/template 现可选择 LightGBM；另有最多三个候选的验证段 RankIC 选择器；新增 `compare_training_models.py`，验证共享数据 checksum、特征、标签、split、组合模板和同成本 v3 回测，再发布 candidate-minus-baseline 诊断 | 至少一年真实 OOS 的 IR、超额和贡献分散证据；整个研究程序的多重比较预算仍未定义 |
| 阶段 4：因子 | 保留 157 个 Alpha158 特征；新增已有受治理因子 IC/RankIC + HAC、Spearman 共线性、top-N 换手和 v3 成本代理；新增 Tushare 8 个估值/换手/量比/资金流候选特征及 market-cap/liquidity exposure 物化，绑定逐日 raw generations 与 canonical 日历；新增候选 IC/HAC screen 与基于 JSON policy 的验证集 shortlist；现有 PIT exposure panel owning store、`feature_preprocessing.v2`/`feature_schema.v2` 和 Research Chain 重算链路保留 | Tushare 历史响应没有原始 provider vintage，候选 bundle 严格 PIT=false，不能升级到 owning store或用于 OOS 结论；需要历史公告时间与全市场基本面覆盖、分析师预期来源、行业 transaction-time PIT、外部源质量批准后才可发布；单因子增量、真实成本和跨时期样本外贡献仍待验收 |
| 阶段 5：组合与风险 | v1 等权兼容；v2 softmax 分数加权；v3 风险平价（历史样本协方差、严格前序收益、完整窗口/正定校验）与行业中性（top-N 内行业等额预算、行业内等权）已接入；离线收益源绑定交易日历，行业来源绑定 v3 回测配置的开盘前可见 execution facts；v3 industry cap 按比例缩减并留存现金；Research Chain v8 使用 portfolio-definition template v3；v3 最大回撤停止与目标波动率控制可选启用 | 新 bars 尚未转成并通过质量审评的历史收益/exposure panels；实际 PIT 行业源质量与覆盖审计；风险平价/行业平衡在真实成本和多期样本下验收；风险预算与回撤/波动控制的年度实证效果 |
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

## 第二十一轮：基准权重构造与因子中性化算法原语

新增 `build_benchmark_weight_partitions`，按每日 point-in-time membership 生成全成员等权、
正市值归一权重或“行业等预算、行业内等权”权重；它拒绝重复成员、非法日期、缺失的成员
暴露及非正/非有限市值，结果形状可直接传给 `BacktestEngine.run`。调用方必须通过 owning
readers 提供 PIT 成员、市值和行业面板；本轮没有新增市值/行业数据 adapter，也没有发布真实
benchmark 回测。

新增 `neutralize_factor_panel`，同一 session 内按行业哑变量、log 市值和 log1p 流动性做 OLS
残差化。因子/暴露键必须精确相同，缺失暴露、非数值值、控制矩阵不满秩或残差自由度不足都会
拒绝；因子空值保留。该原语尚未接入已评审 `feature_preprocessing`：暴露契约/清单绑定和
Research Chain 新版本路由仍待实现，当前不能把其输出当作已治理训练输入。

专项回归 `tests/test_evaluation_benchmark.py`、`tests/test_factor_neutralization.py`、
`tests/test_factor_screening.py`、`tests/test_experiment_archive.py` 合计 `43 passed`。
最终全量：`.venv/bin/python -m pytest` 为 `1523 passed / 33 skipped`；
`.venv-qlib/bin/python -m pytest` 为 `1555 passed / 1 skipped`；`git diff --check` 通过。
归档 checksum 回归同时锁定完整 telemetry 与绝对来源路径。全 A 年度数据、市场市值/行业
source onboarding、训练链中性化集成、benchmark owning publisher、跨 fold 末期清仓、真实
OOS/PIT 证据和执行验证仍未完成；本轮未运行 release gate 或远程矩阵。

## 第二十二轮：跨折连续 v3 回放

新增 `scripts/run_walk_forward_continuous_backtest.py` 与
`evaluation/walk_forward_continuous.py`。入口只接受完整 passed attempt；逐折读取已通过的
研究结果、v3 回测和经评审 target-weight 分区，通过 owning readers 核对结果 lineage 与每个
OOS session 的唯一权重。统一 v3 配置必须由外部评审，覆盖连续 OOS 决策窗口和末折后一个
执行 session，并与所有折的成本、执行、初始资金、风险设置完全一致。输入日历、原始行情、
execution facts、停牌和公司行为表全部从配置绑定的 owning stores 读回校验。

回放由单次 `BacktestEngine` 执行，现金与持仓跨 fold 延续；新不可变 receipt 绑定 plan、attempt、
统一配置/结果、综合目标序列及每个源 target-weight store。receipt readback 重新校验逐折研究/回测
 lineage、权重源、回测制品和外部质量评审。相同 attempt 重试读取既有 receipt/result，不重复运行
引擎。末折最后决策在下一 session 开盘执行，但期末剩余持仓不自动清仓；receipt 显式标记
`terminal_liquidation: false`，不代表策略已获交易验收。

合成三折回归证明一次连续引擎运行跨折保留账户状态、准确覆盖所有决策日，并可经 owning stores
读回及幂等重试；失败 attempt 在读取目标权重前拒绝。专项 `tests/test_walk_forward_continuous.py`
为 `3 passed`，相关 walk-forward/归档/benchmark/中性化集合为 `69 passed, 1 skipped`。完成年度
真实历史多折、末期清仓协议、统一 benchmark、PIT 数据完整性和成本容量校准仍待真实数据与后续
功能设计；本轮没有碰真实数据根、实时源或实盘。

最终全量验证：`.venv/bin/python -m pytest` 为 `1526 passed / 33 skipped`；
`.venv-qlib/bin/python -m pytest` 为 `1558 passed / 1 skipped`。`git diff --check` 和
`scripts/run_walk_forward_continuous_backtest.py --help` 均通过。精确 HEAD 为
`cd1befb4b5fcbe954e16aea148cc2332dbc1dc21`，这些结果针对其未提交工作树；没有运行认证 gate、
远程矩阵、年度真实数据或实盘验证。实现文件摘要与命令记录见
[`walk-forward-continuous-implementation-v1.json`](../evidence/trading-grade/walk-forward-continuous-implementation-v1.json)。

## 第二十三轮：基准 PIT 成员读取

`read_pit_benchmark_membership` 新增 owning-store 读取路径：要求有序、唯一的治理日历及合法
窗口端点，逐 session 使用 `IndexMembershipStore.as_of` 构造当日成员，并在返回前重验完整
membership-set 及其 snapshot generation、manifest digest 与成员 checksum。来源合同将精度声明为
`monthly_approximation`，该 helper 明确保留此限制，不声称逐日历史成分真实。

回归覆盖月度 snapshot 切换、不可见 snapshot、窗口端点不在日历和错误排序。定向运行
`.venv/bin/python -m pytest tests/test_evaluation_benchmark.py tests/test_index_membership.py -q`
为 `37 passed`。普通全套 `.venv/bin/python -m pytest` 为 `1529 passed / 33 skipped`；
Qlib 全套 `.venv-qlib/bin/python -m pytest` 为 `1561 passed / 1 skipped`。两套全量有既有
pandas `SettingWithCopyWarning`；Qlib 另有其依赖包 NumPy timedelta deprecation warning。
`git diff --check` 通过。benchmark 不可变发布与 v3 回测入口、历史 PIT 市值输入、真实同成本
benchmark 比较仍待完成；当前真实数据根没有这些数据，本轮没有生成模拟行情。

## 第二十四轮：跨折连续回放的期末清仓

连续 walk-forward 回放现在要求 OOS 最后一个决策日之后有两个 governed sessions：第一日执行末折
最后权重，第二日用 v3 引擎的 T+1 开盘流程强制目标清零。终端出售仍执行原有停牌、价格带、参与率、
风险和费用约束，不绕开任何保护；停牌或其他约束导致未成交时，receipt 的 `terminal_liquidation`
为 false，并保存剩余收盘持仓市值与未成交状态。该状态随 portfolio-sequence generation 和结果身份绑定。

连续回放 receipt 升至 v2，记录清仓决策/执行日期、是否尝试、最终剩余持仓及未成交状态；v1 receipt
仍可按旧的单一后续 session 语义读回。回归覆盖完整退出、终端停牌未退出、v1 receipt 兼容、T+1 日期
与同 attempt 幂等重试。

专项 `.venv/bin/python -m pytest tests/test_backtest_v3.py tests/test_walk_forward_continuous.py -q`：
41 passed。普通全套 `.venv/bin/python -m pytest`：1533 passed / 33 skipped；Qlib 全套
`.venv-qlib/bin/python -m pytest`：1565 passed / 1 skipped。既有 pandas `SettingWithCopyWarning`
与 Qlib NumPy timedelta deprecation warning 仍存在。真实历史停牌/涨跌停清仓成功率和退出成本尚未实证；
这项模拟回放不构成真实成交保证。

## 第二十五轮：PIT 暴露与预处理 v2 接入

新增 `factor_exposure_panel.v1` owning store 和离线 Parquet 发布入口。面板按
`(datetime, instrument)` 精确键、规范控制列、有限正值和行业标签校验；`available_at` 必须带时区且不晚于
该 session 上海时间 15:00。`feature_preprocessing.v2` 绑定暴露 generation、manifest digest、数据
checksum 和控制顺序，产出 `feature_schema.v2` 的 `neutralized` 状态。Research Chain 的 resolver、stage
runner 和 dataset writer 已按预处理清单版本分流；dataset 发布时重新读取暴露并重算 OLS，不能只信任
预处理输出帧。

新增集成回归覆盖暴露发布/读回、v2 manifest 发布/读回、schema v2、adapter replay、DatasetWriter 重算、
篡改拒绝及 PIT 时间边界。测试临时注册新 review policies；活动 `model-quality-reviews.v1.json` 与
trust-anchor 摘要保持不变，因此真实发布仍 fail-closed，待维护者批准 `factor_exposure_panel_v1` 和
`feature_preprocessing_v2` 策略及锚摘要更新。仓库仍没有真实 PIT 市值/行业/流动性源；测试数据不代表历史
暴露，不构成 OOS 结果。

本轮首次全量普通测试曾发现并修复 CLI 决策映射的未定义变量及旧测试运行时缺少可选 exposure store 的
兼容性问题；关联 evidence-index 的源码摘要已同步。最终 `.venv/bin/python -m pytest` 为
`1537 passed / 33 skipped`，`.venv-qlib/bin/python -m pytest` 为 `1569 passed / 1 skipped`；两环境仍有
既有 pandas `SettingWithCopyWarning`，Qlib 环境另有依赖包 NumPy timedelta deprecation warning。没有运行
release gate、远程矩阵、真实年度行情或实盘验证。

## 第二十六轮：基准权重不可变发布与 v3 回测

新增 `benchmark_weight_panel.v1` 和 `BenchmarkWeightStore`，将 benchmark 权重、
membership snapshot-set 与重建的日成员面板摘要、策略调仓序列、universe、已评审 v3 config、
暴露 generation 和期末清仓模式绑定到不可变制品；读回核验 schema、内容 checksum、逻辑指纹、
逐日 partition 身份及成员归属。发布使用文件锁与原子目录切换；同一 generation 重试读回核对，
已存在但不一致时拒绝。

新增 `scripts/run_benchmark_backtest.py`。它从已评审策略 v3 结果读取 config 和决策日，按
治理 calendar 及 `IndexMembershipStore` 重建逐日 PIT membership，在策略 universe 和公司行为
过滤后生成等权、市值权重或行业等预算权重，再以同一 v3 引擎、价格、执行事实、费用和风险限制
发布独立 `backtest_result.v3`。连续 walk-forward 输入必须带对应 v2 receipt；publisher 先经
owning reader 重验 receipt、结果与 config 绑定，再按相同引擎约束尝试期末清仓。子系统页新增
CLI 示例与参数说明。

新增集成回归以真实合成行情执行 v3 引擎，覆盖普通同配置发布、连续回放 receipt 的终端清仓、
不可变 panel 读回/幂等/tamper、完整 PIT 成员覆盖以及缺失 PIT 暴露拒绝。定向
`tests/test_evaluation_benchmark.py` 为 `18 passed`。普通完整测试 `.venv/bin/python -m pytest`
为 `1541 passed / 33 skipped`；Qlib 完整测试 `.venv-qlib/bin/python -m pytest` 为
`1573 passed / 1 skipped`。另外
`compileall`、CLI `--help`、CLI 模块导入及 `git diff --check` 均通过。测试警告为既有 pandas
`SettingWithCopyWarning` 与 Qlib NumPy timedelta deprecation warning。

membership 源仍明确声明 `monthly_approximation`，没有可用历史 PIT 市值/行业源；因此本轮未发布
真实基准、未跑年度同成本 OOS。新的 exposure/preprocessing review family 尚未获得 trust-anchor
维护者批准，相关生产发布仍 fail-closed。验证基于 HEAD
`cd1befb4b5fcbe954e16aea148cc2332dbc1dc21` 的未提交工作树；没有运行认证 gate、远程矩阵、真实历史
行情或实盘。

## 第二十七轮：标签审计绑定独立治理日历

`audit_label_design.py` / `audit_label_legs` 升为审计报告 v2，CLI 要求提供已发布
`BacktestMarketDatasetStore` calendar generation 与 data checksum。审计重验日历 manifest digest、
数据 checksum 和基于行内容的 generation；请求窗口内的原始 bars 分区必须逐 session 与治理日历
完全一致。每条非空标签还必须有连续 `T..T+5` 五个后续 session 所对应的标的行，防止证券级数据
缺口被 `groupby.shift(-5)` 静默解释成五日 horizon。close 标签及 `T+1`/`T+5`/`T+6` 价格腿现按
治理日历 session 位移重算，不按存活行位移。

同时校正计划 §2 的状态：v3 显式参与率、冲击/最低佣金、容量网格、调仓/换手及组合约束已实现；
剩下的是历史事实源审计、行业数据质量与真实成本/资金校准，不再把已有引擎功能列为“需要新增”。

回归覆盖内部分区缺失、标的单日 bars 缺口、篡改 bars checksum，以及 calendar binding 和版本化报告
字段。相关 `.venv/bin/python -m pytest tests/test_label_design_audit.py tests/test_build_label_sets.py -q`
为 `19 passed`；CLI `--help`、`compileall`、`git diff --check` 通过。普通全量为
`1543 passed / 33 skipped`；Qlib 全量为 `1575 passed / 1 skipped`。两套仍有既有 schema 表达式
`SettingWithCopyWarning`；NumPy generic timedelta 相关 deprecation 来自依赖/测试环境。

历史 `label-design-baseline-v1.json` 未用新审计器重跑，也未触碰历史数据根；增强代码不能追溯
认证旧报告。原始 bars 仍由旧 `ManifestFirstReader` 读回，PIT 退市状态完整性仍未证明。当前验证
针对 HEAD `cd1befb4b5fcbe954e16aea148cc2332dbc1dc21` 的未提交工作树；未运行 release gate、远程
矩阵或真实数据发布。

## 第二十八轮：同样本线性与 LightGBM 成本比较

新增 `paired_model_backtest_inputs` 与 `scripts/compare_training_models.py`。入口只接受
`qlib_linear` 基线和 `qlib_lightgbm` 候选；从双方 passed research result 追溯 owning request、
model dataset 与 backtest。两份 dataset 都经 `DatasetWriter.read` 校验 Parquet 和 sidecar，且必须对齐
特征顺序、因子/标签/PIT universe/preprocessing、split、样本行数、逻辑 fingerprint 和数据 checksum。
OOS 窗口必须位于共同 test split，回测绑定必须回到各自 request 的 reviewed config；随后调用现有
paired reader 强制比较成本、执行模型、市场数据、universe 和风险字段。报告把 candidate 作为
strategy 侧、linear 作为 baseline 侧，故超额收益和 IR 方向明确为 candidate 减 baseline，并留下
`one_year_oos_met` 时长标志。

回归覆盖同协议可比较及标签、特征、数据 checksum 漂移拒绝。完整真实年度运行尚不可做：本地缺少足够
历史数据和稳定 regime 标签；`one_year_oos_met` 只检验时间跨度/交易 session 数，不代表三态覆盖。
LightGBM 的最多三候选限制仅约束一次 selector 调用，研究程序级的多重比较预算仍是未定义项。
定向模型比较/批预检/归档集合为 `32 passed, 1 skipped`；普通全量 `.venv/bin/python -m pytest`
为 `1547 passed / 33 skipped`，Qlib 全量 `.venv-qlib/bin/python -m pytest` 为
`1579 passed / 1 skipped`。模型比较 CLI `--help`、`compileall` 和 `git diff --check` 通过；未运行
认证 gate、远程矩阵、真实历史数据或实盘。验证基于未提交工作树 HEAD
`cd1befb4b5fcbe954e16aea148cc2332dbc1dc21`。

## 第二十九轮：v4 公司行为口径、维护者裁决与最终复核

维护者批准 v4 公司行为账务规则并明确签批两项既有治理决定：研究 state/result schema 的版本枚举
7/8 原位纯追加；实验归档 checksum 覆盖完整 telemetry 与绝对来源路径。另批准
`factor_exposure_panel_v1`、`feature_preprocessing_v2` 的 passed-only review policies，并更新
model-quality trust-anchor 摘要至 `9287455599efbfb481e9e113aec1ef9b70acb4cd1f7b68d3b2739bd42d63eec4`。

v4 配置绑定完整窗口 `corporate_action_events`：登记日收盘持仓确定分红权益；除息日现金/送股应收计入
NAV 且不可下单；现金到 pay-date session 才可用；送股除息日开始计价，listing session 入账且该 session
仍受 T+1 限制；碎股、缺关键日期、缺覆盖声明一律拒绝；权利发行无支持的完整数据，必须显式传入经审阅的
全窗口排除清单。此功能为新 contract v4，旧 v1/v2/v3 不改语义。v4 的 benchmark 与 stress runner 现在
也分别有事件面板路由回归；连续跨折 replay 与 paper/v3 对账仍限 v3，因为两者没有应收账本状态模型。

回归新增/修复包括 v7 paper risk documents 的整批 preflight 拒绝、v4 登记日/除息/支付/送股上市/T+1
流程、v4 benchmark/stress 事件面板输入、活动 trust-anchor digest 锁定，以及 evidence-index 中
`model_layer.py` 的摘要同步。`report.py` 的 IC binding 走规范化 generation 与日期、LightGBM 参数有严格类型
边界、stress retry 不依赖错误文案。当前该批测试为：普通全量 `.venv/bin/python -m pytest` →
`1563 passed, 33 skipped`；Qlib 全量 `.venv-qlib/bin/python -m pytest` → `1595 passed, 1 skipped`。
两环境按顺序运行，因为 test reviewer PEM 使用共享系统临时路径；并行执行曾发生测试文件竞态，最终顺序结果
已全部通过。环境存在既有 pandas `SettingWithCopyWarning` 与 Qlib/NumPy timedelta deprecation warnings。

最终验证绑定未提交工作树 HEAD `cd1befb4b5fcbe954e16aea148cc2332dbc1dc21`。真实公司行为源、全年历史 PIT
覆盖、多状态 OOS、真实成本校准、生产 reviewer key provisioning 和 broker 小资金实测仍未具备；当前仅完成
接口/机制/合成数据测试，不能将训练计划的交易收益验收标为完成。未运行 `scripts/run_gate.sh`、远程矩阵、
真实数据发布或实盘；当前无 release 声明。

## 第三十轮：真实历史 readiness 复核与停牌行语义

本机真实 Tushare readiness 复核读取 2024-01-01..2026-10-30 的 683 个交易日、5,430 个 PIT 月度近似
universe 成员和已发布 canonical bars。`suspend_d` 逐日查询 683 次，间隔 0.5 秒；全量 required bar
字段 null rate 均为 0。报告仍为 `blocked`：17 个成员缺少必要 bar sessions，且 `suspend_d` 无法解释；
`300096.XSHE`、`300205.XSHE` 仅有 47/46 个可交易行情 session，达不到 60 日最低要求。

只读复核 `stock_basic(list_status=D)` 显示 17 个缺口中有 14 个当前列为退市，另外 3 个不在当前退市集。
该接口未给出历史退市日期，因此不能把它作为中途缺口的有效解释，也没有静默排除这些标的。真实报告
在 gitignored 的 `data/trading-grade-history/reports/readiness-csiall-20240101-20261030.json`。

为确认是否只是全市场 `daily` 响应漏行，对 17 个缺口标的和 2 个短历史标的共发起 19 次按
`ts_code` 的历史 `daily` 只读查询，并逐日对照本地所需 session；19 个查询都没有返回任何 canonical
缺失日的可补数据。未向现有不可变分区写入修补行。TDX 日线适配器的一次抽查在 31 秒内未完成并被中断，
所以没有 TDX 可用性结论或数据写入。

修正 readiness 冲突规则：当 `suspend_d` 标记某标的停牌时，canonical 行 `status=suspended` 视为一致；
`trading`、`unknown` 等其他状态仍报冲突。新增回归覆盖合法的 suspended 零成交量行，并保留对
`trading` 行的冲突回归。修正后真实报告不再包含此前 `603056.XSHG` 的误报。

token 仅从本机 `.env` 读入当前启动包装进程，再通过 `TUSHARE_2K_TOKEN` 环境变量传给子进程；没有
出现在 argv、终端输出或报告。`.env` 与 `data/trading-grade-history/` 经 `git check-ignore` 确认忽略，
也未被 Git 跟踪。

定向 `.venv/bin/python -m pytest tests/test_training_readiness.py -q` 通过；全量
`.venv/bin/python -m pytest` 为 `1588 passed, 33 skipped`（86.57 秒），
`.venv-qlib/bin/python -m pytest` 为 `1620 passed, 1 skipped`（140.84 秒）。两套仍有既有
pandas `SettingWithCopyWarning`；Qlib 环境另有 Qlib/NumPy timedelta deprecation warnings。验证针对
未提交工作树 HEAD `cd1befb4b5fcbe954e16aea148cc2332dbc1dc21`。年度 OOS readiness、完整历史 PIT
退市/新股覆盖、因子与标签重发、生产评审凭证、策略收益验收及小资金验证仍未完成；未运行认证 gate、
远程矩阵或发布。

## 第三十一轮：raw Tushare 因子候选构造与验证集 shortlist

新增 `TushareFactorCandidateStore` 与 `build_tushare_factor_candidates.py`：从逐日 immutable
`daily_basic` / `moneyflow` 响应和 canonical `bars_daily/v2` 日历构造 8 个候选值/换手/量比/资金流特征，
以及基于 `total_mv`、`turnover_rate_f` 的 market-cap/liquidity 暴露。每个候选 bundle 都绑定逐日源
generation、checksum、抓取时间和日历 generation/checksum；读回会重新校验 owning stores 与 raw artifact。
因子公式、来源字段、单位、builder 源码 SHA 和 lock SHA 写入内容身份。暴露 Parquet 符合已有离线
`publish_factor_exposure_panel.py` 的输入形状；发布仍须 source approval reference 与外部 passed review。
经该 owning store 发布后，已有 `feature_preprocessing.v2` / Research Chain dataset adapter 可重读并复算 OLS。

候选数据默认用前一 governed session 的 provider rows，并将 `available_at` 设为决策 session 15:00 上海时间，
但清单强制 `historical_vintage_verified=false` 与 `strict_pit_eligible=false`。本地历史响应是在事后抓取，
没有 provider 原始 vintage；日滞后是假设，不证明供应商当时发布的确切值。Tushare `daily_basic` 文档显示
15:00–17:00 更新、`dv_ttm` 为百分比、`total_mv` 为万元；`moneyflow` 金额也以万元计。申万历史仍仅有
有效期，财报/预期历史覆盖也不足；本轮不把这些转换成严格 PIT，也没有把候选 factors 发布给研究训练链。

真实本地快照只读物化窗口 `2026-10-15..2026-10-30`，覆盖 12 个 session，`factors.parquet` 和
`exposures.parquet` 各 66,817 行，generation
`6b3b56d9c166f489fd1e190c7dce9e37c3c410dc069815fea3c4c01cf2a788dc`。数据与 receipt 留在
gitignored `data/trading-grade-history/derived/`。新增 candidate screen CLI 绑定候选 bundle、已发布 labels、
源/日历和环境；可选绑定 reviewed v3 成本配置。新增验证集 shortlist 协议：按显式方向、RankIC/HAC、正向
频率、覆盖、pairwise history 和相关性去重；配置的成本门槛缺 reviewed 成本报告时 fail-closed，最多保留三项。
Shortlist 不自动改 factor definition，不是跨研究程序的多重比较校正，也不能替代 withheld OOS 增量实验。
新的历史 labels 尚未就绪，readiness 仍 blocked，因此本轮没有生成或声称真实因子筛选/OOS 成果。

回归新增 Tushare 候选公式与 T+1 calendar alignment、缺失依赖拒绝、source/calendar lineage readback、
候选 screening report binding、方向反转、相关候选去重与 correlation history 缺失拒绝。最终工作树验证：
`.venv/bin/python -m pytest` → `1593 passed, 33 skipped`；
`.venv-qlib/bin/python -m pytest` → `1625 passed, 1 skipped`；相关 CLI `--help` 与 `git diff --check`
通过。两套仍有既有 `SettingWithCopyWarning`；Qlib/Pandas 有 NumPy timedelta deprecation warning。本轮未调用
vendor 网络、未读取 `.env`、未发布 exposure/factor owning artifacts、未运行认证 gate/远程矩阵/实盘。源码和
文档验证基于未提交工作树 HEAD `cd1befb4b5fcbe954e16aea148cc2332dbc1dc21`。
