# 盘中分析 Agent Prompts

本目录包含 A 股盘中分析的 Agent 提示词，可被任何 AI Agent 框架驱动。

## Agent 列表

| Agent | 文件 | 触发时间 | 依赖数据 |
|-------|------|---------|---------|
| 开盘分析 | `opening_analysis.md` | 09:26（竞价数据就绪后） | 9:25 快照、股票池、7日K线、涨跌停CSV、新闻 |
| 早盘机会分析 | `early_session_analysis.md` | 09:41（早盘数据就绪后） | 9:40 快照、开盘分析报告、股票池、昨日复盘 |
| 收盘复盘准备 | `closing_review.md` | 18:00（收盘数据导出后） | 行情数据.md、涨跌停CSV |

## 路径变量

提示词中使用 `{变量名}` 格式的路径占位符，使用时需替换为实际路径：

| 变量 | 含义 | 示例 |
|------|------|------|
| `{DATA_DIR}` | 项目数据根目录（含 stocks.md、intraday/） | `/path/to/a-stock-trading` |
| `{SCRIPTS_DIR}` | 脚本目录 | `/path/to/a-stock-trading/scripts` |
| `{DAILY_DIR}` | 每日数据目录 | `/path/to/a-stock-trading/daily` |

## 执行流程

```
09:25  脚本: intraday_data.py snapshot     → 拉取竞价快照
09:26  Agent: opening_analysis.md          → 生成开盘分析报告
09:40  脚本: intraday_data.py snapshot     → 拉取早盘快照
09:41  Agent: early_session_analysis.md    → 生成早盘机会分析
10:00  脚本: intraday_data.py snapshot     → 拉取盘中快照
11:30  脚本: intraday_data.py snapshot     → 拉取午盘快照
14:40  脚本: intraday_data.py snapshot     → 拉取尾盘快照
15:05  脚本: export_daily_summary.py       → 导出收盘数据
18:00  Agent: closing_review.md            → 收盘复盘准备
```

## 在 HappyClaw 中使用

这些 prompt 文件可通过 HappyClaw 定时任务驱动：
- `schedule_type`: cron
- `execution_type`: agent
- `context_mode`: isolated

将 prompt 文件内容作为任务的 `prompt` 字段，替换路径变量后即可调度。
