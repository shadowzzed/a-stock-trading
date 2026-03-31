# 电脑上所有 Agent / 脚本清单

> 更新时间：2026-03-30

## 一、HappyClaw 定时任务

### 盘中数据拉取（脚本，无AI消耗）

| 时间 | Task ID | 命令 | 说明 |
|------|---------|------|------|
| 09:25 | `task-...-u9900x` | `intraday_data.py pull` | 竞价数据 |
| 09:40 | `task-...-1yybhe` | `intraday_data.py pull` | 早盘10分钟 |
| 10:00 | `task-...-424xy5` | `intraday_data.py pull` | 早盘半小时 |
| 11:30 | `task-...-c3ues8` | `intraday_data.py pull` | 上午收盘 |
| 14:40 | `task-...-dx80sj` | `intraday_data.py pull` | 尾盘前20分钟 |
| 15:00 | `task-...-ugt4id` | `intraday_data.py pull` | 收盘定格 |

### 盘中 Agent 任务（Claude 驱动）

| 时间 | Agent | 模型 | 输出 |
|------|-------|------|------|
| 09:26 | 开盘分析 | Claude（HappyClaw） | 高开过顶 + 板块总结（⭐加权）+ 断板反包 → 飞书 + `开盘分析.md` |
| 09:41 | 早盘机会分析 | Claude（HappyClaw） | 强势股套利 + 超预期股 + 板块机会 → 飞书 + `早盘机会分析.md` |
| 18:00 | 收盘数据拉取 | Claude（HappyClaw） | AKShare 拉涨跌停 CSV + 创建当日目录 |

### 日常维护任务

| 时间 | Agent | 说明 |
|------|-------|------|
| 23:00 | 系统健康检查 | 磁盘/进程/Docker/网络全面巡检 |
| 23:30 | 分时量化提醒 | 提醒研究分时量量化思路 |

---

## 二、常驻后台进程

| 名称 | 位置 | 模型 | 运行方式 | 说明 |
|------|------|------|---------|------|
| **news_monitor** | `trading/news_monitor.py` | 火山引擎 DeepSeek | 后台常驻 Python 进程，30s 轮询 | 4 数据源（TrendRadar DB + 财联社 + 华尔街见闻 + 金十），AI 解读后逐条发飞书私聊 |
| **TrendRadar** | `~/src/TrendRadar/` | 火山引擎 DeepSeek | Docker 部署 | 10s 抓取 11 个热榜平台，AI 过滤，SQLite 存储，供 news_monitor 读取 |

---

## 三、Trading 工具脚本（按需调用）

| 脚本 | 位置 | AI模型 | 用途 |
|------|------|--------|------|
| `mootdx_tool.py` | `trading/` | 无 | 通达信接口（实时报价 / K线 / 分时 / 五档盘口），186 只股票全匹配 |
| `intraday_data.py` | `trading/` | 无 | 盘中快照管理（拉取 / 查询 / 对比 / 异动扫描），数据存 SQLite |
| `glm_sniper.py` | `trading/` | 无 | 智谱 GLM Coding 订阅抢单，支持多账号、0.3s 高频轮询 |
| `opening_analysis.py` | `trading/` | DeepSeek | 开盘分析独立脚本版（高开过顶 / 板块总结 / 断板反包） |

---

## 四、多 Agent 分析框架

| 项目 | 位置 | 模型 | 架构 | 运行方式 |
|------|------|------|------|---------|
| **short-term-agents** | `~/src/short-term-agents/` | 火山引擎 DeepSeek | LangGraph + 5 个 Agent 辩论（情绪/龙头/多/空/仲裁） | CLI `sta <日期>`，V5.1 Final（avg 14.2/20） |
| **TradingAgents** | `~/src/TradingAgents/` | OpenAI / Gemini / Claude | LangChain + Backtrader | CLI，多 LLM 回测框架 |

---

## 五、HappyClaw 内置 Skills

| Skill | 用途 |
|-------|------|
| akshare-skill | AKShare 金融数据 API |
| baostock / baostock-batch | A 股历史 K 线查询（单只 / 批量） |
| mx_data | 东方财富实时行情 / 资金流向 / 估值 |
| mx_search | 东方财富妙想搜索（新闻 / 研报 / 公告） |
| agent-browser | 网页浏览自动化（Playwright） |
| opencli | 复用 Chrome 登录态操作网站 |

---

## 六、一次性任务

| 时间 | 任务 | 说明 |
|------|------|------|
| 03-31 09:55 | GLM 抢单 | 双账号，Pro ¥149/月，`glm_sniper.py` |
| 03-31 09:58 | 2 分钟倒计时 | 飞书提醒 |
| 03-31 09:59 | 1 分钟倒计时 | 飞书提醒 |

---

## 模型使用汇总

| 模型 | 提供商 | 用途 |
|------|--------|------|
| **Claude** | Anthropic（HappyClaw 主引擎） | 所有 HappyClaw Agent 任务、交互对话 |
| **DeepSeek** | 火山引擎（endpoint: ${ARK_MODEL}） | 新闻解读、short-term-agents 辩论、TrendRadar 过滤、开盘分析脚本 |
| **OpenAI / Gemini** | 各官方 API | TradingAgents 回测框架 |
