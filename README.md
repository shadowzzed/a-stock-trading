# A股短线交易分析工具集

A 股短线交易分析系统，覆盖数据采集、盘中实时分析、盘后多 Agent 复盘、新闻监控、策略回测全流程。

按产品线组织为三大模块 + 共享层，各产品可独立开发和部署。

## 架构

```
┌─────────────────────────────────────────────────────┐
│                   共享层 (shared)                     │
│   data/ ─ 数据采集    tools/ ─ 独立脚本    config.py  │
│   knowledge/ ─ 交易知识库                             │
└──────────┬──────────────┬──────────────┬─────────────┘
           │              │              │
     ┌─────▼─────┐  ┌────▼─────┐  ┌─────▼──────┐
     │ trading_  │  │ backtest/│  │ news_      │
     │ agent/    │  │          │  │ monitor/   │
     │           │  │ 经验系统  │  │            │
     │ chat/     │  │ 回测引擎  │  │ 新闻聚合   │
     │ intraday/ │  │          │  │ AI 解读    │
     │ review/   │  │          │  │ 事件催化   │
     └───────────┘  └──────────┘  └────────────┘
     Trading Agent   回测系统      News Monitor
```

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/shadowzzed/a-stock-trading.git
cd a-stock-trading

# 2. 安装依赖
pip install -e .

# 3. 配置数据目录
cp config.yaml.example config.yaml
# 编辑 config.yaml，修改 data_root 指向你的数据存储目录

# 4. 设置 AI API 密钥
export ARK_API_KEY="your_api_key"
export ARK_MODEL="your_model_endpoint"

# 5. 首次运行（自动创建数据目录结构）
python -m trading_agent.intraday opening_analysis --dry-run
```

## 代码与数据分离

代码在 Git 仓库中，运行时数据存储在 `config.yaml` 指定的 `data_root` 目录下：

```
~/trading-data/              ← data_root（不在仓库中）
├── intraday/intraday.db     # 盘中快照 SQLite（全市场 ~5000 只）
├── daily/YYYY-MM-DD/        # 每日数据（CSV、分析报告、新闻）
├── news_monitor.db          # 新闻去重数据库
├── stocks.md                # 股票池（首次运行从模板自动复制）
└── logs/                    # 运行日志
```

配置优先级：环境变量 `TRADING_DATA_ROOT` > `config.yaml` > 默认值 `./runtime_data/`

### 运行时数据位置

所有运行时数据都在 `data_root` 下（默认 `~/shared/trading/`），仓库内**不应**有任何业务数据：

| 路径 | 内容 |
|------|------|
| `{data_root}/intraday/intraday.db` | 盘中快照 SQLite（全市场 ~5000 只） |
| `{data_root}/daily/YYYY-MM-DD/` | 每日数据（CSV、分析报告、新闻） |
| `{data_root}/chat_checkpoints/chat.db` | LangGraph chat agent 对话状态持久化（2026-05-11 从仓库内 `trading/checkpoints/` 迁出） |
| `{data_root}/news_monitor.db` | 新闻去重数据库 |
| `{data_root}/logs/` | 运行日志 |

### 已废弃/移除的入口

| 路径 | 状态 |
|------|------|
| `trading_agent/chat/*.mjs` + `node_modules/` + `package.json` | **已移除**（2026-05-11）。此前是 2026-04-03 的 Node.js 版重写尝试（OpenAI SDK + lark-oapi/node-sdk），4-15 之后未再迭代；当前生产路径是 Python 版（`python -m trading_agent.chat` → LangGraph + langchain-openai）。如需查看历史代码：`git show 653201c` |
| `trading_agent/review/` 的 CLI 入口 | **已移除**。v6 重构后 review 模块仅保留 `data/loader.py`、`tools/retrieval.py` 作为基础设施 |

## 产品线说明

### trading_agent/ — Trading Agent（盘中交易代理）

AI 驱动的盘中交易分析系统，包含三个子系统：

#### chat/ — 飞书对话 Bot

Agent Teams 架构的盘中实时交易助手：

```
Coordinator（意图识别+综合）──▶ 4 Sub-Agents
  ├── dragon     龙头分析师
  ├── sentiment  情绪分析师
  ├── bullbear   多空分析师
  └── trend      趋势分析师
```

```bash
# 飞书 Bot 模式
python -m trading_agent.chat

# 本地 CLI 测试
python -m trading_agent.chat --cli
```

#### intraday/ — 盘中定时分析

LangGraph 编排的定时分析流水线：

| 时间 | Agent | 功能 |
|------|-------|------|
| 09:26 | `opening_analysis` | 高开过顶筛选、板块强弱排名、断板反包识别 |
| 09:41 | `early_session_analysis` | 涨停股套利机会、超预期股分析、强势板块跟踪 |
| 18:00 | `closing_review` | 收盘数据确认、关键指标汇报 |

```bash
python -m trading_agent.intraday opening_analysis
python -m trading_agent.intraday early_session_analysis
python -m trading_agent.intraday closing_review
python -m trading_agent.intraday opening_analysis --dry-run   # 调试模式
```

#### review/ — 复用基础设施（已不再独立运行）

> **注意**：盘后多 Agent 复盘流程已在 v6 重构中拆解，独立 CLI 入口（`python -m trading_agent.review`）不再保留。本目录现仅保留 `data/loader.py`、`tools/retrieval.py` 作为基础设施，被 `trading_agent/chat/` 和 `backtest/` 复用。
>
> 盘后复盘的能力现整合在 `trading_agent/chat/`（飞书对话）+ `trading_agent/intraday/closing_review` 中。

### backtest/ — 回测系统

策略回测引擎 + 结构化经验库 + 策略池影子运行，支持 Trading Agent 自我迭代：

| 模块 | 功能 |
|------|------|
| `engine/` | 回测引擎核心（数据加载、策略执行、报告生成） |
| `experience/` | 结构化经验库（教训分类、场景匹配、prompt 注入） |
| `screener.py` | Layer 2 量化选股（涨停股评分 + 反包加分 + 趋势股路径） |
| `adapter.py` | 数据适配层，桥接 trading_agent/review/ 的数据 |
| `monitor_backtest_v2.py` | 方向二盘中监控回测（买卖信号配对+T+1+超时强平+入场过滤） |
| `strategies/__init__.py` | 策略池（8 个预配置变体：base/tight/wide/sealed_only/heat_gate/conservative…） |
| `shadow_runner.py` | 多策略并行影子运行 + 健康度评估 + 报告生成 |
| `param_sweep_v4.py` | 止损/止盈参数敏感性扫描 |
| `run.py` | 经验系统回测入口 |

```bash
# 经验系统回测
python -m backtest

# 方向二盘中回测（单策略）
python -m backtest.monitor_backtest_v2 --start 2026-03-03 --end 2026-04-17 \
    --output ~/shared/backtest/monitor.json \
    --report ~/shared/backtest/交割单.md

# 策略池影子运行 + 健康度体检
python -m backtest.shadow_runner --start 2026-03-03 --end 2026-04-17 --send-feishu
python -m backtest.shadow_runner --weekly                 # 周度体检（最近 30 天）

# 参数敏感性
python -m backtest.param_sweep_v4
```

### news_monitor/ — News Monitor（新闻监控）

实时财经新闻聚合 + AI 解读 + 事件催化提取。

**数据源**（30 秒轮询，共 8 个）：
- TrendRadar DB（11 个热榜平台聚合）
- 财联社电报（重要级别）
- 华尔街见闻（A 股频道）
- 金十数据（重要标记）
- BlockBeats（区块链/加密相关 A 股联动）
- TechFlow（深科技/AI 行业资讯）
- PANews（PA 财经/科技）
- 东方财富研报（首次覆盖、目标价、评级变更）

```bash
# 新闻监控（全天运行）
python news_monitor/news_monitor.py

# 事件催化提取
python -m news_monitor catalyst

# 盘前简报
python -m news_monitor briefing
```

### data_tools/ — 数据质量工具

用于检测和修复 `intraday.db` 中的数据缺陷（mootdx 缓存错位、stock_meta 字段错误等）。

| 文件 | 功能 |
|------|------|
| `data_quality_check.py` | 检测 minute_bars 污染（唯一价比例 < 30%）并自动用 daily OHLC 合成修复 |
| `synthesize_minute_bars.py` | 用 daily OHLC 合成 minute_bars（full 整天 / morning 早盘两种模式） |
| `fix_stock_meta.py` | 修复 stock_meta.last_close（从 daily_bars 前日 close 补正） |

```bash
# 扫描修复（建议每日盘后跑）
python data_tools/data_quality_check.py --scan 2026-04-01 2026-04-30

# 手动合成某天
python data_tools/synthesize_minute_bars.py 2026-04-16 full
python data_tools/synthesize_minute_bars.py 2026-04-13 morning

# 修复 stock_meta
python data_tools/fix_stock_meta.py 2026-03-01 2026-04-30
```

### data/ — 共享数据层

通达信行情接口 + 盘中快照采集 + 收盘数据导出，数据存入 SQLite。

| 文件 | 功能 |
|------|------|
| `mootdx_tool.py` | 通达信接口封装，支持实时行情、日K线、分时、五档盘口 |
| `intraday_data.py` | 盘中快照采集，全市场 ~5000 只写入 `intraday.db` |
| `export_daily_summary.py` | 收盘后导出行情数据、涨跌停 CSV |
| `backfill_stock_data.py` | 历史 K 线数据回填 |
| `backfill_minute_bars_sina.py` | 新浪 1min API 回填（~9 天窗口，备用） |
| `import_history.py` | 外部数据导入 |
| `rebuild_limit_up.py` | 重建 limit_up 表（按板块规则从 daily_bars 精确识别） |
| `data_quality_fix.py` | 批量修补数据缺陷（NULL pct_chg、\x00 字符、空 industry 等） |
| `data_quality_audit.py` | 每日数据体检，发现异常告警（退出码分级） |

### tools/ — 独立脚本

| 文件 | 功能 |
|------|------|
| `glm_launcher.sh` | 双账号并行抢单启动器 |
| `glm_sniper.py` | 智谱 GLM Coding Plan 自动抢单 |
| `opening_analysis.py` | 开盘分析独立脚本 |
| `backtest_gap_up.py` | 涨停回测工具 |
| `doctor.py` | 环境诊断工具 |
| `strategy_registry.py` | 策略版本库（SQLite 持久化，管理策略元数据+回测历史） |
| `strategy_health.py` | 每日滚动回测（5/20/60 日窗口）+ 阈值告警 |
| `strategy_compare.py` | 策略版本间对比分析 |
| `daily_maintenance.sh` | 每日盘后自动维护脚本（LaunchAgent/cron 调度） |

#### 策略迭代机制使用

```bash
# 1. 初始化策略库 + 登记新版本
python3 tools/strategy_registry.py init
python3 tools/strategy_registry.py register \
    --name "方向二_v16" --params '{"STALE_HOLD_DAYS": 2}' \
    --note "自主迭代版 2026-04-19"

# 2. 运行健康度监控（写入滚动回测结果）
python3 tools/strategy_health.py --windows 5 20 60

# 3. 查看对比
python3 tools/strategy_compare.py --window 20

# 4. 淘汰失效版本
python3 tools/strategy_registry.py status <id> --status retired --note "被 v17 替代"
```

#### 每日自动维护

推荐用 macOS LaunchAgent 或 Linux crontab：

```bash
# macOS LaunchAgent 位置：~/Library/LaunchAgents/com.luoxin.astocktrading.daily.plist
# Linux crontab：
# 0 17 * * 1-5 /path/to/a-stock-trading/tools/daily_maintenance.sh
```

每日 17:00 自动执行：
1. 数据修复（`data/data_quality_fix.py`）
2. 7 天数据体检（`data/data_quality_audit.py`）
3. 策略健康度监控（`tools/strategy_health.py`）
4. 周一额外跑 limit_up 重建（`data/rebuild_limit_up.py`）

日志输出到 `~/shared/trading/logs/daily_maintenance_YYYY-MM-DD.log`。

### knowledge/ — 交易知识库

| 文件 | 内容 |
|------|------|
| `框架.md` | 核心知识库：交易规则、认知框架、量化选股规则、情绪周期模型 |
| `A股短线交易规则整理.md` | A 股交易规则速查 |
| `quantitative_rules.json` | 量化筛选参数（JSON 格式，供程序读取） |
| `stocks_template.md` | 股票池模板（18 板块 ~174 只，⭐ 标记辨识度核心股） |

## 数据流

```
盘前
  09:00  news_monitor/ briefing                         → daily/YYYY-MM-DD/盘前简报.md
  09:25  data/intraday_data.py snapshot                 → intraday.db (竞价快照)

盘中
  09:26  trading_agent/intraday/ opening_analysis       → daily/YYYY-MM-DD/开盘分析.md
  09:40  data/intraday_data.py snapshot                 → intraday.db (早盘快照)
  09:41  trading_agent/intraday/ early_session_analysis → daily/YYYY-MM-DD/早盘机会分析.md
  10:00  data/intraday_data.py snapshot                 → intraday.db
  11:30  data/intraday_data.py snapshot                 → intraday.db
  14:40  data/intraday_data.py snapshot                 → intraday.db

盘后
  15:05  data/export_daily_summary.py                   → daily/YYYY-MM-DD/{行情,涨停板,跌停板}.csv
  18:00  trading_agent/intraday/ closing_review         → 通知用户数据就绪
  晚间    trading_agent/review/ 多Agent复盘              → daily/YYYY-MM-DD/agent_*.md
  晚间    news_monitor/ catalyst                        → daily/YYYY-MM-DD/事件催化.md

全天
  30s    news_monitor/news_monitor.py                   → daily/YYYY-MM-DD/新闻.md
```

## 环境变量

| 变量 | 必须 | 说明 |
|------|------|------|
| `ARK_API_KEY` | 是 | AI API Key（火山引擎 DeepSeek / OpenAI 等） |
| `ARK_MODEL` | 是 | 模型 endpoint ID |
| `ARK_API_BASE` | 否 | API 地址（默认火山引擎） |
| `TRADING_DATA_ROOT` | 否 | 数据根目录（也可在 config.yaml 中配置） |
| `FEISHU_APP_ID` | 否 | 飞书推送（新闻监控用） |
| `FEISHU_APP_SECRET` | 否 | 飞书推送 |
| `FEISHU_WEBHOOK_URL` | 否 | 飞书 Webhook |

## License

MIT License
