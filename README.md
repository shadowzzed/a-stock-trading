# A股短线交易分析工具集

自用 A 股短线交易分析系统，覆盖盘前数据采集、盘中实时分析、盘后多 Agent 复盘、新闻监控全流程。

## 架构

```
┌─────────────┐    ┌──────────────┐    ┌─────────────┐
│   data/     │───▶│  intraday/   │───▶│   review/   │
│  数据采集    │    │  盘中分析     │    │  盘后复盘    │
└─────────────┘    └──────────────┘    └─────────────┘
                          │
┌─────────────┐           │
│  monitor/   │◀──────────┘
│  新闻监控    │  (新闻作为分析输入)
└─────────────┘
```

## 模块说明

### data/ — 数据采集与存储

通达信行情接口 + 盘中快照采集 + 收盘数据导出，数据存入 SQLite。

| 文件 | 功能 |
|------|------|
| `mootdx_tool.py` | 通达信接口封装，支持实时行情、日K线、分时、五档盘口 |
| `intraday_data.py` | 盘中快照采集，全市场 ~5000 只写入 `intraday.db` |
| `export_daily_summary.py` | 收盘后导出行情数据、涨跌停 CSV |
| `backfill_stock_data.py` | 历史 K 线数据回填 |
| `import_history.py` | 外部数据导入 |

### intraday/ — 盘中分析 Agent

AI 驱动的实时盘中分析，支持任何 OpenAI-compatible API。

| 时间 | Agent | 功能 |
|------|-------|------|
| 09:26 | `opening_analysis` | 高开过顶筛选、板块强弱排名、断板反包识别、风险提示 |
| 09:41 | `early_session_analysis` | 涨停股套利机会、超预期股分析、强势板块跟踪 |
| 18:00 | `closing_review` | 收盘数据确认、关键指标汇报、等待博主复盘 |

```bash
# 运行盘中分析
python -m intraday opening_analysis
python -m intraday early_session_analysis
python -m intraday closing_review

# 调试模式（只看 prompt 不调 AI）
python -m intraday opening_analysis --dry-run
```

### review/ — 盘后多 Agent 复盘

基于 LangGraph 的多 Agent 复盘框架：情绪分析师 + 板块分析师 + 龙头分析师 → 多空辩论 → 裁决报告。

```bash
# 运行复盘分析
python -m review 2026-03-31

# 交互模式（AI 出报告后等你终审）
python -m review 2026-03-31 -i

# 指定模型
python -m review 2026-03-31 --model your-model --base-url https://api.example.com/v1
```

### monitor/ — 新闻监控

实时财经新闻聚合 + AI 解读，4 个数据源，30 秒轮询。

- TrendRadar DB（11 个热榜平台）
- 财联社电报（重要级别）
- 华尔街见闻（A 股频道）
- 金十数据（重要标记）

```bash
python monitor/news_monitor.py
```

### tools/ — 独立工具

| 文件 | 功能 |
|------|------|
| `glm_sniper.py` | 智谱 GLM Coding Plan 自动抢单 |
| `glm_launcher.sh` | 双账号并行抢单启动器（配合 launchd） |

### knowledge/ — 交易知识库

| 文件 | 内容 |
|------|------|
| `框架.md` | 核心知识库：交易规则、认知框架、量化选股规则、情绪周期模型 |
| `A股短线交易规则整理.md` | A 股交易规则速查 |
| `quantitative_rules.json` | 量化筛选参数（JSON 格式，供程序读取） |

## 数据流

```
盘前
  09:25  data/intraday_data.py snapshot    → intraday.db (竞价快照)

盘中
  09:26  intraday/ opening_analysis        → daily/YYYY-MM-DD/开盘分析.md
  09:40  data/intraday_data.py snapshot    → intraday.db (早盘快照)
  09:41  intraday/ early_session_analysis  → daily/YYYY-MM-DD/早盘机会分析.md
  10:00  data/intraday_data.py snapshot    → intraday.db
  11:30  data/intraday_data.py snapshot    → intraday.db
  14:40  data/intraday_data.py snapshot    → intraday.db

盘后
  15:05  data/export_daily_summary.py      → daily/YYYY-MM-DD/{行情,涨停板,跌停板}.csv
  18:00  intraday/ closing_review          → 通知用户数据就绪
  晚间   review/ 多Agent复盘              → daily/YYYY-MM-DD/agent_*.md

全天
  30s    monitor/news_monitor.py           → daily/YYYY-MM-DD/新闻.md
```

## 配置

复制 `.env.example` 为 `.env`，填入实际值：

```bash
cp .env.example .env
```

| 变量 | 必须 | 说明 |
|------|------|------|
| `ARK_API_KEY` | 是 | AI API Key（火山引擎 DeepSeek 等） |
| `ARK_MODEL` | 是 | 模型 endpoint ID |
| `ARK_API_BASE` | 否 | API 地址（默认火山引擎） |
| `FEISHU_APP_ID` | 否 | 飞书推送（新闻监控用） |
| `FEISHU_APP_SECRET` | 否 | 飞书推送 |
| `FEISHU_WEBHOOK_URL` | 否 | 飞书 Webhook |

## 安装

```bash
pip install -e .

# 或仅安装依赖
pip install requests mootdx langgraph langchain-openai
```

## 股票池

`stocks.md` 维护 18 个板块约 174 只股票的跟踪池，⭐ 标记辨识度核心股。

## License

MIT License
