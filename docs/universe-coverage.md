# 研究窗口的 universe 覆盖：4970 从哪来、能不能铺到全集

更新时间：2026-10-18
适用读者：要跑 v5 research-chain execute、或要判断"这只标的为什么没进训练集"的人。

本文回答两个问题：**当前 smoke 的 4970 只 universe 是怎么来的？能不能铺回 CSI 全指的 5068 只？**

## 1. 结论先行

| 问题 | 答案 |
|---|---|
| 原始 membership 被裁过吗？ | **没有**。`data/csi-all-store/.../2025-08-29/members.csv` 仍是 **5068** 只 |
| 4970 是契约要求吗？ | **不是**。它是 `/tmp/keep-instruments.json` 手工 workaround |
| 能铺回 5068 的 membership 吗？ | **能**，不需要任何契约变更 |
| 能用 5068 跑 `fail_closed` 吗？ | **当前数据不能**。合法 16 会话窗口的 complete-case 上限是 **4993**（9/9–9/30）或 **4988**（再扣非尾段 null label） |

**所以"铺到全集"要分成两件事**：membership 制品可以铺回 5068；但能进入训练的
complete-case universe 受**数据本身**限制，当前上限约 98.5%。把两者混为一谈会得到错误结论。

## 2. 4970 的真实来源

21 会话窗口 `2025-09-09..2025-10-15` 的逐层收缩：

| 层级 | 数量 | 说明 |
|---|---:|---|
| csi-all 快照（2025-08-29） | 5068 | 权威成员表 |
| 有任何 factor 行 | 5067 | `601989.XSHG` 完全没有 bars（退市/停发） |
| 21 个 factor 分区全有历史 | 5035 | 32 只标的窗口内停牌/停更 |
| 全历史且无任何空因子 | **4970** | 65 只至少一个因子单元格为空 |

生成链（全部在 `/tmp`，**不可复现、不可审计**）：

- `/tmp/iterate.py:14-19` 反复删"任意一行有 null"的标的；
- `/tmp/make_subset.py:15-38` 要求标的在每个 factor 分区都出现，再与 keep 列表求交；
- `/tmp/rebuild_final.py:17,26-36` 把这个子集发布成 `universe_id=csi-all-decision`；
- `src/uq/research_chain/membership_input.py:142-184` 按日求交，最终 frame 就是 4970 只。

## 3. 65 只空因子的构成（这是关键）

| 组 | 数量 | 原因 | 性质 |
|---|---:|---|---|
| 只有 `close_location_1d` 为空 | **47** | `high == low`（一字板），`raw_price.py:34-36` 返回 NaN | **合法市场状态**，不是数据缺陷 |
| `amount_20d` + `volume_ratio_20d` 为空 | **18** | `rolling(20, min_periods=20)` warm-up，`raw_price.py:38-40` | lead-in 期间真实缺 bar |

47 只逐名核对结果：**37 只一字涨停、10 只一字跌停、0 只"真平盘但不在涨跌停价"**。
即每一个 null 都能被交易所涨跌停解释，没有发现数据错误。

> 这一条很重要：`close_location_1d` 在涨跌停日**本来就没有定义**（分母 `high-low = 0`）。
> 把它当成"数据缺失"然后剔除整只标的，等于**用市场状态惩罚标的**，方向是错的。

18 只 warmup 标的逐只核查：`2025-08-11..2025-09-04` 这 19 个交易日里，它们各自只有
**9–18 个 bar**。因此**把因子窗口提前不会让它们变干净**——缺口是 lead-in 期间真实缺 bar，
不是因子起点造成的。

## 4. 32 只 partial-history 的构成

| 组 | 数量 | 说明 |
|---|---:|---|
| 窗口内停止、之后不返回 | 7 | 001206/001314/002169/002185/002571/605178/688381；**均非退市**，10-16 之后恢复行情 |
| 有缺口但之后返回 | 25 | 缺失日与 `suspend_d` 对得上，无"分区缺日但 bar 存在"的管线不一致 |

## 5. 为什么 5068 过不了 `fail_closed`

`src/uq/research_chain/adapters.py:355-359` 对 `ordered_features + label` 做全 null 检查，
任何 null 即 `ContractError`。当前数据下五个独立来源：

1. `601989.XSHG` 无 bars（1 只）；
2. 32 只 partial history（缺分区 → 缺行）；
3. 47 只涨跌停日的 `close_location_1d`（合法 null）；
4. 18 只 20 日 warm-up（真实缺 bar）；
5. label 尾段（`shift(-5)`，若 label 只建到研究窗末日则末 5 日全 null）。

**`declared_fill` 不是出口**：schema 允许它（`dataset_policy_template.v1.json`），
但 `adapters.py:355` 只对 `fail_closed` 检查，`declared_fill` 仅"跳过检查"、**没有任何填值实现**；
下游 Qlib 仍硬拒 null（`adapters.py:1019-1022`、`qlib_export.py:113-117`）。
用它只会得到"声明了 fill、实际仍是 null"的 dataset，然后在 export 阶段失败。

## 6. 各窗口的 complete-case 上限

| 窗口 | 会话 | factor-clean | 再扣非尾段 null label | 说明 |
|---|---:|---:|---:|---|
| 9/9–10/15 | 21 | 4970 | 4970 | 当前 smoke 的来源 |
| 9/9–9/30 | 16 | **4993** | **4988** | 合法两段 split 的最小窗 |
| 9/9–9/16 | 6 | 5036 | — | **不合法**：purge 5 + embargo 2 要求两段间隔 ≥ 7 会话 |

## 7. 建议路线（推荐 A）

**A. 用受治理的 complete-case universe，不要再用 `/tmp` 列表**（推荐）

- 发布一个 `universe_id=csi-all-complete-2025-09-09-to-2025-09-30` 的 membership 制品，
  成员 **4988**，并把 80 只排除标的**及原因**写进证据文件；
- 报告里写"complete-case CSI All, 4988/5068 = 98.4%"，**不要**称其为"全 A 5068 全量"；
- 优点：可审计、可复现、不需要任何契约变更、不需要补数据。

**B. 补齐数据后追求 5068**

需要同时做到：补 `601989` 与 32 只 partial 的 bars；对 47 只涨跌停 null 定义 reviewed 语义
（删除 `close_location_1d`、或定义 fill、或把涨跌停日排除出可交易 universe）；
给 18 只 warmup 补 lead-in。**这些都会改变因子/数据语义，按仓库规则必须走新版本**，
不是本轮能顺手完成的。

**C. 缩小窗口到 9/9–9/16** —— 不合法（见 §6），不要走。

## 8. 一个必须先修的独立错误

`/tmp/request-final.json` 的 `window_start_date..window_end_date` 是 **9/9–9/16**，
但 `dataset_policy_template.split_policy.splits[1]` 写到 **9/24–10/15**。
`SplitValidator` 要求 split 边界在 frame 的实际交易日集合内
（`src/uq/models/dataset.py:46-57`，调用点 `dataset_writer.py:58-63`），
所以 stage 02 **必然失败**（已实测 `final_status=failed`）。铺多少 universe 都不影响这一条。

## 9. 证据等级

- 全部数字由**本轮实测**得出，可复现命令见 `cr.md` §8.51。
- 47/18/32 三组分类由独立 subagent 逐名核对，本人复核了 47 组的涨停/跌停拆分（37/10/0）。
- `declared_fill` 无实现、Qlib 硬拒 null 两条，本人独立读码确认（附行号）。
