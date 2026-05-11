# Trade Agent Teams — 项目上下文

## 一句话概述

A 股短线交易 AI Agent 系统，覆盖数据采集 → 盘中分析 → 盘后复盘 → 新闻监控 → 历史影响分析全流程，通过飞书 Bot 提供交互式对话。

## 目标

建成一个**盘中可对话、盘后可复盘**的 AI 交易助手，核心价值：
- 盘中实时回答行情、情绪、龙头、趋势等问题（飞书群聊对话）
- 盘后自动生成多 Agent 复盘报告（情绪分析 → 板块分析 → 龙头分析 → 裁决报告）
- 全天新闻监控 + 事件催化提取 + 历史相似事件影响分析
- 跨周期记忆（每日认知积累、经验教训沉淀）
- 月化收益 ≥20%，最大回撤 ≤15%（见 [`docs/trade-agent-roadmap-2026-05-11.md`](docs/trade-agent-roadmap-2026-05-11.md)）

## 当前生产策略

**v8_tight + Layer1 deterministic**（自 2026-05-11 起切换上线）：
- 参数：`STOP_LOSS_PCT=-5, TAKE_PROFIT_PCT=10, MAX_HOLD_DAYS=5, MAX_POSITIONS=3`
- 文件：`trading_agent/intraday/monitor.py`、`trading_agent/intraday/layered_analysis.py`
- Layer 1 大盘门控：`backtest/layered_engine.py:_code_sentiment_fallback` 规则代码判断情绪，"退潮"/"冰点" → 强平 + 拒绝新买入
- 实测（两个真实窗口 37 个交易日）：
  - 熊市 3-12~4-10：**-1.49%**（v8_tight_naked 无 Layer1 跑 -7.96%，改善 6.47pp）
  - 震荡 4-13~5-08：**+18.26%**（Layer1 无副作用）
  - 合计 ~1.85 个月 +16.5%，月化 ~8.9%
- 详见 [`docs/trade-agent-roadmap-2026-05-11.md`](docs/trade-agent-roadmap-2026-05-11.md)
- 健康度建议：每周用 `python -m backtest.shadow_runner --weekly` 跑回测复检

## 架构

```
┌─────────────────────────────────────────────────────┐
│                    飞书群聊 / CLI                      │
│         (trading_agent/chat 入口)                     │
└──────────────────────┬──────────────────────────────┘
                       │ 用户消息
                       ▼
┌──────────────────────────────────────────────────────┐
│             CoordinatorAgent（协调器）                 │
│  trading_agent/chat/coordinator.py                   │
│  ┌─────────────────────────────────────────────┐     │
│  │  1. 意图识别 → 判断调用哪些分析师              │     │
│  │  2. 并行分发 → ThreadPoolExecutor             │     │
│  │  3. 结果综合 → 标注来源 + 分歧裁决            │     │
│  └─────────────────────────────────────────────┘     │
└────┬──────────┬──────────┬──────────┬───────────────┘
     │          │          │          │
     ▼          ▼          ▼          ▼
┌─────────┐┌─────────┐┌─────────┐┌─────────┐
│ Dragon  ││Sentiment││BullBear ││ Trend   │
│龙头分析师││情绪分析师││多空分析师││趋势分析师│
└────┬─────┘└────┬─────┘└────┬─────┘└────┬─────┘
     │           │           │           │
     └───────────┴───────────┴───────────┘
                       │
                       ▼ (SharedDataCache)
          ┌────────────────────────┐
          │  RetrievalToolFactory  │
          │  12 个数据检索工具       │
          └────────────┬───────────┘
                       │
          ┌────────────┴───────────┐
          │      数据层             │
          │  SQLite (intraday.db)  │
          │  SQLite (news_monitor) │
          │  CSV (daily/YYYY-MM-DD)│
          │  Embedding 向量索引     │
          │  记忆文件 (memory/)     │
          └────────────────────────┘
```

## 模块职责

项目按产品线拆分为独立模块：

| 目录 | 职责 | 入口 |
|------|------|------|
| `trading_agent/` | 交易 Agent（盘中对话 + 盘中分析） | — |
| `trading_agent/chat/` | 盘中对话 Agent Teams（Coordinator + 4 Sub-Agents，LangGraph + langchain-openai） | `python -m trading_agent.chat` |
| `trading_agent/intraday/` | 盘中定时分析（开盘/早盘/收盘） | `python -m trading_agent.intraday` |
| `trading_agent/review/` | **基础设施层**（仅保留 `data/loader.py`、`tools/retrieval.py`，被 chat 和 backtest 复用）。v6 重构后独立 CLI 入口已删除 | — |
| `news_monitor/` | 新闻监控 + 事件催化 + 盘前简报 + 影响分析 | `python -m news_monitor` |
| `news_monitor/impact/` | 新闻历史影响分析（embedding + 影响计算 + 实时检索） | `python -m news_monitor.impact.bootstrap` |
| `backtest/` | 回测引擎（经验回测 + 策略验证 + 影子运行） | `python -m backtest` |
| `data/` | 数据采集与存储（通达信接口 + SQLite + 数据治理） | 各脚本独立运行 |
| `data_tools/` | 数据缺陷检测与合成修复（minute_bars 污染、stock_meta 错误） | `data_tools/data_quality_check.py`、`synthesize_minute_bars.py`、`fix_stock_meta.py` |
| `tools/` | 独立工具（开盘分析、GLM 抢单、策略版本管理、每日维护脚本） | 各脚本独立运行 |
| `knowledge/` | 交易知识库（规则、框架、股票池模板） | 被其他模块引用 |
| `config.py` | 统一配置（YAML + 环境变量） | — |

## Agent Teams 详情

### Coordinator（协调器）
- **文件**: `trading_agent/chat/coordinator.py` + `trading_agent/chat/prompts/coordinator.md`
- **职责**: 意图识别 → 任务分发 → 结果综合
- **工具**: 全部工具（简单查询直接处理，不分发）

### 4 个 Sub-Agent

| Agent | 文件 | Prompt | 工具 | 触发场景 |
|-------|------|--------|------|---------|
| Dragon（龙头分析师） | `agents/dragon.py` | `prompts/dragon.md` | market_data, stock_detail, history_data, quant_rules, prev_report, past_report | 龙头、连板、涨停梯队 |
| Sentiment（情绪分析师） | `agents/sentiment.py` | `prompts/sentiment.md` | market_data, history_data, index_data, memory, lessons, quant_rules | 情绪、周期、赚钱效应 |
| BullBear（多空分析师） | `agents/bullbear.py` | `prompts/bullbear.md` | market_data, history_data, capital_flow, review_docs, quant_rules, prev_report | 主线、轮动、策略 |
| Trend（趋势分析师） | `agents/trend.py` | `prompts/trend.md` | market_data, stock_detail, history_data, index_data, capital_flow, **scan_trend_stocks** | 趋势、均线、技术面、趋势股扫描 |

### 关键机制
- **SharedDataCache**: 同一轮分析中多个 Agent 共享缓存，避免重复查库
- **Tool-calling loop**: 每个 Agent 最多 3 轮工具调用
- **意图识别**: Coordinator 用 LLM 判断用户意图，输出 JSON 数组决定调用哪些 Agent
- **并行分析**: ThreadPoolExecutor 并行调用多个 Agent

## 新闻监控模块

### 数据源（8 个）
TrendRadar DB、财联社电报、华尔街见闻、金十数据、BlockBeats、TechFlow、PANews、东方财富研报

### 处理流程
新闻采集 → 去重（精确 + 模糊语义） → AI 批量解读 → 打标（优先级/板块/个股） → 路由

- **交易时间 09:25-15:00**：高优先级实时推送 / 低优先级 20 分钟聚合
- **非交易时间**：全部入 `morning_brief_pool` 候选池，次日 9:00 由 morning_brief 精选 Top 12 发送
- **critical 兜底（任何时段）**：战争/熔断/紧急加息等关键词立即推送
- **打标格式**：`[财报] [商业航天] [神剑股份]`

### 早报机制（2026-05 引入）
LaunchAgent `com.luoxin.astocktrading.morning-brief` 每工作日 8:55 触发：
- `morning_brief.py`：池表粗筛 30 条（PRIORITY×事件×个股×板块×时效×解读长度）→ DeepSeek LLM 精排 12 条 + 一句话开盘启示
- `morning_brief_us.py`：yfinance 拉取 19 个 ETF（11 SPDR + 8 主题）+ 14 只明星股 → 板块异动 + 明星表现 + LLM A/美映射分析
- 合并发送到飞书，标记 `used_in_brief=1` 避免重复

### 高优先级关键词
| 类别 | 关键词 |
|------|--------|
| 供需 | 减产、扩产、限产、停产、产能、供需、涨价、降价、短缺、库存 |
| 财报 | 业绩预增/预减、净利润增长/下降、营收增长/下降、业绩快报、暴雷、扭亏、首亏 |
| 研报 | 研报、首次覆盖、目标价、评级上调/下调、买入/增持评级 |
| 地缘 | 制裁、冲突、战争、军事、袭击、威胁、封锁、禁令、关税 |

## 新闻历史影响分析（impact）

### 模块文件
| 文件 | 职责 |
|------|------|
| `impact/db.py` | 数据库操作 — `news_embeddings` + `news_impacts` 表 |
| `impact/embed.py` | Embedding 编码 — BAAI/bge-m3 本地推理（1024维） |
| `impact/calc.py` | 影响计算引擎 — 各时间窗口涨跌幅、极值、量能比 |
| `impact/search.py` | 向量检索 + 影响聚合 + 报告格式化 |
| `impact/hooks.py` | 管线集成钩子（3s 超时保护） |
| `impact/bootstrap.py` | 冷启动脚本（批量 embedding + 影响回填） |
| `impact/prompts.py` | AI 润色提示词模板 |

### 数据库表
**news_embeddings**：news_id + 1024维 float32 BLOB + 模型版本
**news_impacts**：22 个字段 — 盘中窗口（5min/15min/30min/1h/2h/eod）、盘后窗口（次日~第5日）、极值（max_gain/max_loss）、恢复时间、量能比

### 集成流程
高优先级新闻推送前自动触发：embedding 编码 → 向量检索 Top-10 相似历史新闻 → 影响统计聚合 → 附加在推送消息末尾

### 冷启动
```bash
python -m news_monitor.impact.bootstrap           # 全量回填
python -m news_monitor.impact.bootstrap --embed    # 仅 embedding
python -m news_monitor.impact.bootstrap --impact   # 仅影响计算
```

### 新增依赖
`sentence-transformers>=2.2.0`（pip install，模型 ~1.2GB 自动下载）

## 数据检索工具（12 个）

| 工具名 | 功能 | 数据源 |
|--------|------|--------|
| `get_market_data` | 行情快照（概览/个股/股票池） | SQLite / mootdx 实时 |
| `get_stock_detail` | 个股分时快照 | SQLite |
| `get_history_data` | 近期情绪数据对比 | CSV |
| `get_index_data` | 指数行情 | CSV |
| `get_capital_flow` | 资金流向/北向资金 | CSV |
| `get_review_docs` | 复盘文档 | Markdown 文件 |
| `get_memory` | 跨周期记忆 | memory/*.md |
| `get_lessons` | 历史经验教训 | 文件 |
| `get_prev_report` | 前日裁决报告 | Markdown 文件 |
| `get_past_report` | 历史任意日期报告 | Markdown 文件 |
| `get_quant_rules` | 量化规律 | JSON 文件 |
| `scan_trend_stocks` | 全市场趋势股扫描（MA5/MA10） | SQLite |

## 代码与数据分离

- **代码**: Git 仓库（当前目录）
- **数据**: `config.yaml` 中 `data_root` 指定的目录（默认 `~/trading-data`）
- **配置优先级**: 环境变量 > config.yaml > 默认值
- **密钥管理**: `config.py` 优先读环境变量（`ARK_API_KEY`、`XAI_API_KEY` 等），缺失时回落到 `config.yaml` 中的明文字段（`ai_api_key`、`glm_api_key`、`minimax_api_key`、`feishu_app_secret` 等）。**`config.yaml` 已 .gitignore，不进版本控制**，但属于本地明文存储；生产部署应统一使用环境变量并清空 yaml 中的对应字段

## 限制与约束

### 数据层
- `intraday.db` 依赖通达信接口（mootdx），盘中才能拉到最新数据
- 历史数据需要提前回填（`data/backfill_stock_data.py`），没有实时获取历史 K 线的能力
- 趋势股扫描基于 SQLite 中的日线数据，至少需要 6 个交易日的历史
- 影响分析依赖 `snapshots` 表中的盘中快照数据，数据覆盖范围决定影响计算精度

### AI 层
- 每个 Agent 最多 3 轮工具调用（防止无限循环）
- 对话历史每群最多保留 20 条（内存限制）
- 意图识别依赖 LLM 判断，可能误判（fallback 到全部分析师）
- 4 个 Agent 并行分析时，每个独立消耗 API token

### 业务层
- 仅支持 A 股市场
- 专注短线交易（不涉及中长线价值分析）
- 情绪周期模型是经验性的，不是精确科学
- 板块判定依赖 `stocks.md` 中的股票池映射

### 运行时
- 飞书 Bot 需要独立的 App ID/Secret（不同于新闻监控的飞书配置）
- 端口锁（19876）防止重复启动，需要手动 kill 旧进程
- 无数据库迁移机制，schema 变更需要手动处理
- bge-m3 模型首次运行自动下载（~1.2GB），缓存于 `~/.cache/huggingface/`

## 测试

### 本地 CLI 模式
```bash
# 启动交互式对话（不需要飞书）
python -m trading_agent.chat --cli

# 测试特定问题
你> 今天涨停几家
你> 找趋势股
你> XX股票能不能买
```

### 盘后复盘
盘后复盘的能力已在 v6 重构中拆分：
- 实时复盘问答 → `trading_agent/chat/`（飞书对话）
- 收盘后定时收评 → `python -m trading_agent.intraday closing_review`
- 离线策略回顾 → `python -m backtest`

`trading_agent/review/` 已无独立 CLI 入口，仅留 `data/loader.py`、`tools/retrieval.py` 作基础设施。

### 新闻监控测试
```bash
# 单次运行
python -m news_monitor --once

# 影响分析冷启动
python -m news_monitor.impact.bootstrap
```

### 数据工具测试
```bash
# 开盘分析 dry-run（不调 AI，仅输出原始数据）
python -m trading_agent.intraday opening_analysis --dry-run

# 导出日度数据
python data/export_daily_summary.py
```

### 单元测试
当前项目没有独立的测试套件。验证方式：
1. CLI 模式对话测试（`python -m trading_agent.chat --cli`）
2. 各模块 dry-run 模式
3. 飞书群实际对话验证

## 关键依赖

| 依赖 | 用途 |
|------|------|
| `langchain-openai` | LLM 调用（OpenAI-compatible API） |
| `langchain-core` | 消息类型、工具定义 |
| `langgraph` | 复盘状态机 |
| `mootdx` | 通达信行情接口 |
| `lark-oapi` | 飞书 SDK（WebSocket + REST） |
| `sentence-transformers` | 本地 Embedding（bge-m3，影响分析用） |
| `pandas` | 数据处理 |
| `requests` | HTTP 请求 |

## AI Provider 配置

支持多 provider fallback：
1. **Grok (xAI)** — 主力（`XAI_API_KEY`）
2. **DeepSeek (火山引擎 Ark)** — 备用（`ARK_API_KEY` + `ARK_MODEL`）

两个系统共用同一套 provider 配置（`config.py` 中的 `get_ai_providers()`）。

## 数据治理与策略迭代（2026-04-19 引入）

### 数据质量治理（`data/`）

| 脚本 | 职责 |
|------|------|
| `data_quality_fix.py` | 批量修补 NULL pct_chg、\x00 字符、空 industry、错误 last_close（幂等）|
| `data_quality_audit.py` | 每日体检；退出码 0=健康 / 1=警告 / 2=严重 |
| `rebuild_limit_up.py` | 全量重建 limit_up：按板块规则（10%/20%/30%/5% ST）从 daily_bars 识别 |
| `backfill_minute_bars_sina.py` | 新浪 1min API 回填（~9 天窗口），minute_bars 损坏备用 |

**为什么**：东方财富涨停 API 实测漏抓率高达 46%（如 04-08 228 只 vs 表内 123 只）。必须基于日K精确重建。

### 策略版本生命周期（`tools/`）

```
开发 → registry.register → health 监控 → compare 对比 → retired 淘汰
```

| 脚本 | 职责 |
|------|------|
| `strategy_registry.py` | 版本库（`~/shared/trading/strategy_registry.db`）：策略元数据 + 回测历史 |
| `strategy_health.py` | 每日滚动回测（5/20/60 日）+ 阈值告警 |
| `strategy_compare.py` | 多版本对比 |

**告警阈值**（`strategy_health.py:THRESHOLDS`）：
- 胜率 < 40%
- 最大回撤 > 15%
- 近 5 日收益 < -5%
- 近 20 日收益 < 0%

触发 → `/tmp/strategy_health_alert.txt` 写入 → Agent pickup 可发飞书。

### 每日自动化（LaunchAgent）

`tools/daily_maintenance.sh` 整合全部维护逻辑，调度：

```
文件：~/Library/LaunchAgents/com.luoxin.astocktrading.daily.plist
调度：周一至周五 17:00
日志：~/shared/trading/logs/
禁用：launchctl unload ~/Library/LaunchAgents/com.luoxin.astocktrading.daily.plist
```

**为什么不 crontab**：Claude 受限 shell 里 macOS 会挂起要求 Full Disk Access 授权。LaunchAgent 在用户空间无此问题。Linux 服务器部署直接用 crontab。

### Layer 1 为什么不用 LLM

实测 temperature=0 仍无法消除随机性（同一输入多次调用给出不同情绪判断），导致回测不可重现。改用 `backtest/layered_engine._code_sentiment_fallback` 纯代码规则（涨停数/跌停数/炸板率/边际变化）。板块方向用 `sector_distribution` 排序 + 排除 ST板块/年报预增等噪音概念。
