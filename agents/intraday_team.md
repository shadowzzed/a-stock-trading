# 盘中分析 Agent Team

## 架构

```
定时调度
  ├── 09:25 数据拉取 Agent（intraday_data.py pull）
  ├── 09:26 开盘分析 Agent（opening_analysis.py）→ 飞书推送
  ├── 情绪分析 Agent（待建）
  ├── 板块轮动 Agent（待建）
  └── 策略建议 Agent（待建）
```

## 数据拉取 Agent

**工具**：`trading/intraday_data.py`
**定时**：工作日 09:25（cron: `25 9 * * 1-5`）

| 命令 | 输出 | 用途 |
|------|------|------|
| `pull` | 全量快照存入 SQLite | 定时拉取 |
| `snapshot` | 全量快照 JSON（行情+板块+异动+辨识度） | 盘中概览 |
| `query <date> <ts>` | 指定时间点数据 | 历史查询 |
| `compare <ts1> <ts2>` | 两个时间点对比 | 变化分析 |
| `bid <代码>` | 单只五档盘口 | 微观结构 |
| `minute <代码>` | 单只分时数据 | 分时走势 |
| `times [date]` | 已有时间点列表 | 数据索引 |

**数据库**：`trading/intraday/intraday.db`（单文件，含历史15个交易日 + 盘中实时快照）

## 开盘分析 Agent

**工具**：`trading/opening_analysis.py`
**定时**：工作日 09:26（cron: `26 9 * * 1-5`），依赖 09:25 数据拉取完成
**AI**：火山引擎 DeepSeek（${ARK_MODEL}）
**输出**：飞书私聊推送

### 分析内容

**1. 高开过顶**
- 过去 7 天内有涨停
- 昨天不是涨停
- 今日开盘价 ≥ 过去 7 天最高价
- 池内⭐股优先排序

**2. 板块总结**
- 按板块聚合开盘涨跌幅（⭐辨识度股票权重 2 倍）
- 分类：高开板块（加权均 >0.5%）、低开板块（<-0.5%）、中性
- 结合近 2 天新闻/事件催化分析高开原因

**3. 断板反包**
- 前天涨停
- 昨天不涨停
- 今天高开（>1%）

### 命令行

```bash
python3 trading/opening_analysis.py           # 完整流程（拉数据→分析→AI→飞书推送）
python3 trading/opening_analysis.py --dry-run  # 仅输出原始数据JSON，不调AI
```
