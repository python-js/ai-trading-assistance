# MRS-1.0 v1 验收报告（纸面线）

- 归档日：2026-10-19（UTC）
- 范围：OKX BTC-USDT-SWAP 永续，1m 驱动，纸面（PaperBroker）模拟撮合
- 硬约束：**不产生真实盘订单**；Runner 仅调用行情 REST + PaperBroker + 本地台账
- 静态核验：MRS 代码/Runner/Harness 中 `create_order|place_order|cancel_order|privatePost|apiKey` 命中 0（2026-10-19）
- 数据：2024-10-14 → 2026-10-14，1m 共 1,051,512 根；funding 历史 278 条（OKX 仅近 3 个月）
- 默认费率档：VIP0（taker 5bp / maker 2bp / slip 2bp）；maker 场景 = 费用敏感性上限（见 §9）

## 1. 结论

**工程发布门：通过。** T01–T14 全绿，§9.4 一致率 100%，§9.5/§9.6 报表归档完成。

**策略有效性门：不通过。**

1. §9.3 随机基准：策略期望 -0.068R（n=2），基准 -0.148R（n=2000）；Δ=+0.080R，95% CI [-1.236, +1.396] 混零 → **无边际信息**。
2. §9.6 WFA：唯一有样本的第 3 折内样本 -0.740R / 外推 +0.603R，**符号相反 → 不过门**；前两折 n=0 无法判定。
3. §9.6 灵敏度（12 变体全部完成）：`req_score`（-0.637R → -0.068R → +0.603R）与 `room_rr_min`（+0.023R vs -0.068R）符号翻转；`vol_pctile_min` 单调敏感；`tp_r_base` / `be_trigger_r` / `cooldown_min_bars` 为平台 → 参数邻域不稳（细节见 §6）。

**处置：不实盘化，保持纸面常驻继续采集。** 当前结论仅对 v1 参数组成立；任何后续版本须重新过 §9.3/§9.6。

## 2. 交付物

| 层 | 路径 |
|---|---|
| 配置/venue 核验 | `backend/app/mrs/config.py` |
| 指标 §4 | `backend/app/mrs/indicators.py` |
| 决策 §5 | `backend/app/mrs/decision.py` |
| 计划 §6.1–6.2 | `backend/app/mrs/plan.py` |
| 执行/管理/状态/台账 §6.3–§8 | `broker.py` / `manage.py` / `state.py` / `ledger.py` / `recover.py` |
| 引擎（同源） | `backend/app/mrs/engine.py` |
| 回测 Harness §9 | `backend/scripts/backtest_mrs.py` |
| 随机基准 §9.3 | `backend/scripts/run_mrs_baseline.py` |
| WFA §9.6 | `backend/scripts/run_mrs_wfa.py` |
| 灵敏度 §9.6 | `backend/scripts/run_mrs_sensitivity.py` |
| 真日志对照 §9.4 | `backend/scripts/compare_mrs_paper_log.py` |
| 纸面常驻 Runner | `backend/scripts/run_mrs_paper.py` + `start_mrs_paper.cmd` |
| 验证脚本 T01–T14 | `backend/scripts/verify_mrs_*.py` |

## 3. §9.5 回测结果（24 个月）

| 场景 | 信号/成交 | 期望R | 胜率 | PF | maxDD | 终值（初始 50,000） |
|---|---|---|---|---|---|---|
| VIP0 taker 5bp（默认） | 1 / 1 | -0.827 | 0% | 0.00 | 0.83R / 0.29% | 49,855.31（Δ-144.69） |
| maker 场景 taker 2bp | 2 / 2 | -0.068 ± 1.316 | 50% | 0.82 | 0.74R / 0.26% | 49,975.88（Δ-24.12） |

- 明细：`report_vip0_24m.md`、`report_maker_24m.md`（含分层、funnel、MFE/MAE、逐笔）。
- funnel 主要拒因：SESSION 83.31%、ZONE 5.54%、EVENT 4.24%、REGIME 3.57%、RESON 2.41%；PLAN 极少（VIP0 25 次、maker 24 次），其中 COST 占 84%/71%。
- funding 结算：两个场景均无跨 8h 结算持仓（结算 0 次）。
- 两个场景的差异交易：`room_rr_min` 闸门在 5bp 成本下额外拒绝了一笔 2026-09-04 的 SHORT（+0.603R），说明样本对成本档极敏感。

## 4. §9.3 随机基准对照（maker 场景）

- 轨迹 n=2000（seed=20261014），入场池 = funnel 全闸门已过 bar，R=1.2×ATR15，同管理规则，真实 Engine 24h 预热。
- 基准期望 -0.148R ± 0.030，胜率 26.6%，PF 0.57，ΣR -296.04；平均持有 17.5 bar。
- 策略：-0.068R（n=2）；Δ=+0.080R，95% CI [-1.236, +1.396] → **无边际信息（停止实盘化）**。
- 明细：`baseline_maker.md` / `baseline_maker.json`。

## 5. §9.6 Walk-forward（3 折，内 6 月 + 外 2 月）

| 折 | 区间 | 内样本 | 外推 | 判定 |
|---|---|---|---|---|
| 1 | 2024-10-14→2025-06-14 | n=0 | n=0 | 样本不足 |
| 2 | 2025-06-14→2026-02-12 | n=0 | n=0 | 样本不足 |
| 3 | 2026-02-12→2026-10-14 | -0.740R（n=1） | +0.603R（n=1） | **符号相反 ❌** |

- 结论：不通过发布门。明细：`wfa_maker.md`。

## 6. §9.6 灵敏度（6 参数 ±20%，maker 场景，12 变体）

> 12/12 变体全部完成，明细：`sensitivity_maker.md` / `sensitivity_maker.json`。

| 参数 | 基线 | 低变体 | 高变体 | 判定 |
|---|---|---|---|---|
| req_score | -0.068R（n=2） | 2 → **-0.637R**（n=4，PF 0.19） | 4 → **+0.603R**（n=1） | **符号翻转**，极差 1.24R |
| room_rr_min | -0.068R（n=2） | 1.2 → **+0.023R**（n=3，PF 1.09） | 1.8 → -0.068R（n=2） | **符号翻转** |
| tp_r_base | -0.068R（n=2） | 1.76 → -0.068R（n=2） | 2.64 → -0.068R（n=2） | 平台 |
| be_trigger_r | -0.068R（n=2） | 1.2 → -0.056R（n=2） | 1.8 → -0.068R（n=2） | 平台 |
| cooldown_min_bars | -0.068R（n=2） | 20 → -0.068R（n=2） | 30 → -0.068R（n=2） | 平台 |
| vol_pctile_min | -0.068R（n=2） | 0.48 → **-0.314R**（n=3，PF 0.39） | 0.72 → -0.068R（n=2） | 单调敏感 |

- 判定口径：符号翻转 / 平台(<0.1R) / 单调 / 孤峰；每变体 = 生产 harness 独立运行。
- 6 个参数中 2 个符号翻转、1 个单调敏感，参数邻域不稳，不支持用调参“修复” v1。

## 7. §9.4 一致性

- dual-run（冻结窗口 2026-07-20→2026-10-18，两个 PYTHONHASHSEED）：Bundle 0 行一致，指纹 1,328,743 字符逐字节一致；窗口内 0 信号。
- 真日志对照（maker 24m 台账 ↔ 同源重放），两个窗口均 **100%**（各 10/10）：
  - 2026-06-01→06-16：含 LONG/TRAIL_MA 笔（`consistency_replay_2026-06.md`）。
  - 2026-09-01→09-10：含 SHORT/SEG1_THEN_TRAIL 笔；重放以窗口起点权益 49,870.6128 起跑（`consistency_replay_2026-09.md`）。
- 口径：台账已有历史成交的中期窗口，重放必须对齐窗口起点权益，否则风险预算→qty 不同（曾观察 0.6069 vs 0.6056），并非引擎非确定性。
- 工具：`compare_mrs_paper_log.py`。

## 8. T01–T14 验证索引

| 编号 | 内容 | 脚本 |
|---|---|---|
| T01 | SMA/RMA/ATR/ADX 递推数值 | `verify_mrs_t01.py` |
| T02 | 分型确认延迟 + 指标自检 | `verify_mrs_t02.py` |
| T03/T04/T05 | rank 组合表 / 共振真值表 / zones fixture | `verify_mrs_t03_05.py` |
| T06 | 未来函数审计（截断不变性 + t+1 反例） | `verify_mrs_t06.py` |
| T07 | make_plan 数值算例/边界/对照组 | `verify_mrs_t07.py` |
| T08 | 触发器 T1/T2 + 决策序列闸门 | `verify_mrs_t08.py` |
| T09 | manage_step 顺序与时序数值 | `verify_mrs_t09.py` |
| T10 | 执行层幂等/重复事件 | `verify_mrs_t10.py` |
| T11 | 重启恢复矩阵（离线 + 真实落盘 E2E） | `verify_mrs_t11.py` |
| T12 | 保护单失败 → UNPROTECTED → 市价平 → HALTED_DAY | `verify_mrs_t12.py` |
| T13 | 数据缺口语义 + Runner 重启接线 | `verify_mrs_t13.py` |
| T14 | 模型不变量 + 端到端冒烟 | `verify_mrs_t14.py` |
| §9.2 | funding 正例/空头镜像/缺数据 | `verify_mrs_funding.py` |
| §9.4 | dual-run 一致性 | `verify_mrs_consistency.py` |

运行方式：`.venv/Scripts/python.exe backend/scripts/verify_mrs_tXX.py`（项目根，rc=0 为通过）。

## 9. v1 口径登记（重要）

1. **费率场景**：默认 VIP0（taker 5bp）。`maker` 场景仅把 `venue.taker_bps` 改为 2bp，仍按 taker 一次性计入（未建模 post-only 排队/免单），属**乐观上界**而非真实 maker 成交模拟。
2. **funding**：仅 2026-07 之后有真实历史（278 条）；更早按 0 并计 imputed（§9.2 允许）。两个回测场景均未跨结算点持仓，故对结果无影响。
3. **成本闸门**：`rt = 2*(fee+slip)/1e4`，`r < max(4*rt, 120*tick)` → Reject("COST")。5bp 档是该线 24 个月仅 1 笔成交的主因。
4. **WFA 低样本**：3 折中仅第 3 折内外各有 1 笔；“相反”结论按规格判定，但统计效力极低。
5. **随机基准**：策略侧 n=2，Δ 的 CI 很宽；判定的“无边际信息”结论在统计上是稳健方向，但不应解读为“策略显著更差/更好”。
6. **纸面 Runner**：REST 1m 收盘轮询；预热 7200 根（5 天）；缺口不跳 bar；每 bar 原子 persist；重启按 `mrs_state.json + mrs_orders.jsonl + runtime.groups` 重建；孤儿动作在首 bar 执行。
7. **运行状态**：`data/mrs_paper/`（不入库）；日志 `data/logs/mrs_paper.log`；常驻入口 `start_mrs_paper.cmd`（ASCII/CRLF）。
8. **推送**：企微 markdown（标题含“纸面”），webhook 缺失时仅日志；推送失败不阻断交易循环。
9. **配置指纹**：VIP0 `blake2:939e419ff4a6152a`；maker `blake2:098863d02252b319`。
10. **规格书原文**：实现期间所依据的本地规格书文件已不存在（`C:\Users\admin\Downloads\MRS-1.0-实现规格书_23f09072.md`），本报告以已实现代码与验证脚本为事实来源归档。

## 10. 后续建议（未执行）

- 策略层：在 v2 重新设计前，不建议调整 v1 参数“拟合”回测——灵敏度已显示邻域不稳。
- 数据层：补齐更长 funding 历史（或改为按交易所结算价推导）后再评估成本敏感度。
- 工程层：纸面 Runner 持续运行积累真日志；积累到足量信号后可直接用 `compare_mrs_paper_log.py` 做更大窗口的 §9.4 对照。
