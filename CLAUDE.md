# Trade Agent Teams — 项目上下文

## 一句话概述

A 股短线交易 AI Agent 系统，覆盖数据采集 → 盘中分析 → 盘后复盘 → 新闻监控全流程，通过飞书 Bot 提供交互式对话。

## 目标

建成一个**盘中可对话、盘后可复盘**的 AI 交易助手，核心价值：
- 盘中实时回答行情、情绪、龙头、趋势等问题（飞书群聊对话）
- 盘后自动生成多 Agent 复盘报告（情绪分析 → 板块分析 → 龙头分析 → 裁决报告）
- 全天新闻监控 + 事件催化提取
- 跨周期记忆（每日认知积累、经验教训沉淀）

## 架构

```
┌─────────────────────────────────────────────────────┐
│                    飞书群聊 / CLI                      │
│              (chat/__main__.py 入口)                  │
└──────────────────────┬──────────────────────────────┘
                       │ 用户消息
                       ▼
┌──────────────────────────────────────────────────────┐
│             CoordinatorAgent（协调器）                 │
│  chat/coordinator.py                                 │
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
          │  review/tools/retrieval │
          │  12 个数据检索工具       │
          └────────────┬───────────┘
                       │
          ┌────────────┴───────────┐
          │      数据层             │
          │  SQLite (intraday.db)  │
          │  CSV (daily/YYYY-MM-DD)│
          │  记忆文件 (memory/)     │
          └────────────────────────┘
```

## 模块职责

| 目录 | 职责 | 入口 |
|------|------|------|
| `chat/` | 盘中对话 Agent Teams（Coordinator + 4 Sub-Agents） | `python -m chat` |
| `review/` | 盘后多 Agent 复盘（LangGraph 状态机） | `python -m review` |
| `data/` | 数据采集与存储（通达信接口 + SQLite） | 各脚本独立运行 |
| `intraday/` | 盘中定时分析（开盘/早盘/收盘） | `python -m intraday` |
| `monitor/` | 新闻监控 + 事件催化 + 盘前简报 | `python -m monitor` |
| `tools/` | 独立工具（开盘分析、GLM 抢单等） | 各脚本独立运行 |
| `knowledge/` | 交易知识库（规则、框架、股票池模板） | 被其他模块引用 |
| `config.py` | 统一配置（YAML + 环境变量） | — |

## Agent Teams 详情

### Coordinator（协调器）
- **文件**: `chat/coordinator.py` + `chat/prompts/coordinator.md`
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
- **密钥管理**: 全部通过环境变量（`ARK_API_KEY`, `XAI_API_KEY` 等），不写入代码

## 限制与约束

### 数据层
- `intraday.db` 依赖通达信接口（mootdx），盘中才能拉到最新数据
- 历史数据需要提前回填（`backfill_stock_data.py`），没有实时获取历史 K 线的能力
- 趋势股扫描基于 SQLite 中的日线数据，至少需要 6 个交易日的历史

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

## 测试

### 本地 CLI 模式
```bash
# 启动交互式对话（不需要飞书）
python -m chat --cli

# 测试特定问题
你> 今天涨停几家
你> 找趋势股
你> XX股票能不能买
```

### 盘后复盘测试
```bash
# 指定日期复盘
python -m review 2026-03-31

# 交互模式（AI 出报告后等你终审）
python -m review 2026-03-31 -i
```

### 数据工具测试
```bash
# 开盘分析 dry-run（不调 AI，仅输出原始数据）
python -m intraday opening_analysis --dry-run

# 导出日度数据
python data/export_daily_summary.py
```

### 单元测试
当前项目没有独立的测试套件。验证方式：
1. CLI 模式对话测试（`python -m chat --cli`）
2. 各模块 dry-run 模式
3. 飞书群实际对话验证

## 关键依赖

| 依赖 | 用途 |
|------|------|
| `langchain-openai` | LLM 调用（OpenAI-compatible API） |
| `langchain-core` | 消息类型、工具定义 |
| `mootdx` | 通达信行情接口 |
| `lark-oapi` | 飞书 SDK（WebSocket + REST） |
| `pandas` | 数据处理 |
| `requests` | HTTP 请求 |

## AI Provider 配置

支持多 provider fallback：
1. **Grok (xAI)** — 主力（`XAI_API_KEY`）
2. **DeepSeek (火山引擎 Ark)** — 备用（`ARK_API_KEY` + `ARK_MODEL`）

两个系统共用同一套 provider 配置（`config.py` 中的 `get_ai_providers()`）。
