# 盘中分析 Agent

可独立运行的盘中分析 Agent，支持任何 OpenAI-compatible API。

## 快速开始

```bash
# 设置环境变量
export ARK_API_KEY="your_api_key"
export ARK_MODEL="your_model_endpoint"

# 运行开盘分析
python -m agents.runner opening_analysis

# 运行早盘机会分析
python -m agents.runner early_session_analysis

# 运行收盘复盘准备
python -m agents.runner closing_review

# 指定数据目录
python -m agents.runner opening_analysis --data-dir /path/to/trading

# 只看 prompt 不调 AI（调试用）
python -m agents.runner opening_analysis --dry-run
```

## 文件结构

| 文件 | 用途 |
|------|------|
| `runner.py` | Agent 执行器：数据采集 → 构建上下文 → 调用 AI → 保存报告 |
| `config.py` | 配置管理：路径、API、prompt 加载 |
| `opening_analysis.md` | 开盘分析 prompt（09:26） |
| `early_session_analysis.md` | 早盘机会分析 prompt（09:41） |
| `closing_review.md` | 收盘复盘准备 prompt（18:00） |

## 环境变量

| 变量 | 必须 | 说明 |
|------|------|------|
| `ARK_API_KEY` | 是 | AI API Key |
| `ARK_MODEL` | 是 | 模型 endpoint |
| `ARK_API_BASE` | 否 | API 地址（默认火山引擎） |
| `TRADING_DATA_DIR` | 否 | 数据根目录（默认项目根） |
| `TRADING_SCRIPTS_DIR` | 否 | 脚本目录（默认 scripts/） |
| `TRADING_DAILY_DIR` | 否 | 每日数据目录（默认 daily/） |

## 执行流程

```
09:25  脚本: intraday_data.py snapshot     → 拉取竞价快照
09:26  Agent: opening_analysis             → 生成开盘分析报告
09:40  脚本: intraday_data.py snapshot     → 拉取早盘快照
09:41  Agent: early_session_analysis       → 生成早盘机会分析
10:00  脚本: intraday_data.py snapshot     → 拉取盘中快照
11:30  脚本: intraday_data.py snapshot     → 拉取午盘快照
14:40  脚本: intraday_data.py snapshot     → 拉取尾盘快照
15:05  脚本: export_daily_summary.py       → 导出收盘数据
18:00  Agent: closing_review               → 收盘复盘准备
```

## 在其他框架中集成

`config.load_prompt()` 可加载并渲染 prompt 文件：

```python
from agents.config import load_prompt

prompt = load_prompt("opening_analysis",
    data_dir="/path/to/data",
    scripts_dir="/path/to/scripts",
    daily_dir="/path/to/daily",
)
# prompt 已替换路径变量，可直接传给任何 LLM
```
