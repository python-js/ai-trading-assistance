# ai-trading-assistance

个人向 AI 交易辅助系统：加密（OKX）+ A股 行情自动采集 → 规则评估 + 三票合议（技术/资金面/信息）→ LLM 研判 → 企业微信/飞书推送 → 模拟台账与 H5 看板闭环。

> **研究信号，非投资建议。** 不接实盘、不自动下单；纸面台账用于以数据而非感觉评估信号质量。

## 功能总览（P0~P3 全部完成）

| 阶段 | 内容 |
|---|---|
| P0 管道 | ccxt(OKX) 增量采集、EMA/RSI/ATR 指标、规则 DSL、vectorbt 回测 + walk-forward + 11 分区报告、APScheduler 收盘扫描、K 线截图、企微推送（重试 + push_log）、模拟台账 |
| P1 度量 | 信号确定性回放补登、纸面台账结算、周一 01:00 UTC 周报、事件驱动签发回测、FastAPI + H5 看板（标注回流）、标的搜索自助加币、A股（akshare 日线）全链路 |
| P2 智能 | 资金面算子（funding/OI，PIT 对齐）、LLM 网关（百炼 + 回退链）、新闻 RSS → LLM 结构化事件、三票合议引擎、AI 解释文案、多模态读图票、飞书适配器、信息票 IC 独立回测 |
| P3 知识 | 标注笔记 → 向量知识库（RAG 召回注入推送理由）、中文想法 → 规则草稿 → 沙箱回测、遗留修复（OKX 启动重试 / 回填仅已收盘） |
| 监控告警 | 扫描器内：心跳停更/恢复推送告警（状态迁移触发，防刷屏）；进程级：外部看门狗（心跳 >3 分钟即告警，计划任务每 5 分钟） |

## 架构

```
OKX / A股(新浪) 行情 ──► 扫描器 backend/scripts/run_scanner.py（APScheduler 常驻）
                          ├─ 指标 + 规则评估 → 信号 → 三票合议 → LLM 理由 → 企微/飞书卡片（含 K 线图）
                          ├─ 新闻 RSS → LLM 结构化 → 信息票 / 事件库
                          ├─ 心跳监控 → 停更 / 恢复告警卡片（on_alert）
                          └─ 周报（周一 01:00 UTC，台账命中率/盈亏比）
Postgres + pgvector ── ohlcv / signal / push_log / paper_position / news / knowledge_chunk …
FastAPI(backend) + Next.js(frontend) ── H5 看板：信号 / 台账 / 报表 / 知识库 / 策略页
看门狗 backend/scripts/watchdog.py（计划任务）── scanner.log 心跳停 >3min / 恢复 → 告警卡片
```

## 快速开始

1. **数据库**：`docker compose up -d`（Postgres + pgvector，`db/init/*.sql` 自动建表）
2. **配置**：`cp .env.example .env`，填 `POSTGRES_*`、`WECOM_WEBHOOK`、`LLM_API_KEY` 等（密钥不入 git）
3. **常驻扫描器**：双击 `start_scanner.cmd`（日志 `data/logs/scanner.log`；勿同时开两个实例）
4. **看板**：双击 `start_api.cmd`（FastAPI 127.0.0.1:8000）；前端 `cd frontend && npm run dev`（:3000）
5. **看门狗**：双击 `scripts/register_watchdog.cmd` 注册计划任务（每 5 分钟检查扫描器心跳；卸载命令见脚本内注释）

## 目录结构

- `backend/app/adapters/`：market（ccxt/akshare）、notify（企微/飞书）、chart（mplfinance）
- `backend/app/engine/`：scanner（扫描调度/心跳）、rules_dsl、backtest_event（事件回放）、ledger（台账）、votes（三票）、news、explain、rag（知识库）、rule_gen
- `backend/app/api/`：FastAPI 路由（信号/台账/报表/知识库/规则）
- `backend/scripts/`：`run_scanner.py`（入口）、`watchdog.py`（外部哨兵）、`backfill_signals.py`（停机补登）、`verify_*.py`（离线验证）
- `db/init/`：SQL schema；`frontend/`：Next.js 看板；`docs/`：各阶段功能文档

## 运维

- **日常**：保持扫描器窗口开启；在企微群观测推送；异常先看 `data/logs/scanner.log` 尾部
- **告警口径**：
  - 数据停更（>2×周期）→ 卡片告警，恢复自动通知（扫描器进程内，见 `_StaleAlerter`）
  - 扫描器进程死/僵死 → 看门狗告警（心跳停 >3 分钟；持续停摆每 30 分钟重报，恢复通知；日志 `data/logs/watchdog.log`）
- **停机补登**：`python backend/scripts/backfill_signals.py`（对全部启用规则确定性回放，幂等、不推送）
- **改代码后**：需重启扫描器生效；推送失败记录见 `push_log` 表
- **验证**：`python backend/scripts/verify_*.py` 全部离线可跑（推送类走本地 mock，不向真实 webhook 发消息）
- **口径约定**：存储/计算/job 触发 UTC，展示层北京时间（+8）；A股行情锁定新浪单源；LLM 统一百炼

## 文档

- [开发计划与方案.md](开发计划与方案.md)：总设计、路线图、决策日志
- [docs/P0-阶段1-设计.md](docs/P0-阶段1-设计.md)：管道设计
- [docs/P1-度量闭环-功能文档.md](docs/P1-度量闭环-功能文档.md)
- [docs/P2-智能研判-功能文档.md](docs/P2-智能研判-功能文档.md)
- [docs/P3-知识与生成-功能文档.md](docs/P3-知识与生成-功能文档.md)
