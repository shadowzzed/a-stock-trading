"""盘中分析 Agent 配置"""

import os

# 项目根目录（a-stock-trading/）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 路径配置（可通过环境变量覆盖）
DATA_DIR = os.environ.get("TRADING_DATA_DIR", PROJECT_ROOT)
SCRIPTS_DIR = os.environ.get("TRADING_SCRIPTS_DIR", os.path.join(PROJECT_ROOT, "scripts"))
DAILY_DIR = os.environ.get("TRADING_DAILY_DIR", os.path.join(DATA_DIR, "daily"))
INTRADAY_DB = os.environ.get("TRADING_INTRADAY_DB", os.path.join(DATA_DIR, "intraday", "intraday.db"))
STOCKS_FILE = os.environ.get("TRADING_STOCKS_FILE", os.path.join(DATA_DIR, "stocks.md"))

# AI 配置
AI_API_KEY = os.environ.get("ARK_API_KEY", "")
AI_API_BASE = os.environ.get("ARK_API_BASE", "https://ark.cn-beijing.volces.com/api/v3")
AI_MODEL = os.environ.get("ARK_MODEL", "")


def get_prompt_dir():
    """获取 prompt 目录"""
    return os.path.dirname(os.path.abspath(__file__))


def load_prompt(name: str, **kwargs) -> str:
    """加载并渲染 prompt 文件

    Args:
        name: prompt 文件名（不含 .md）
        **kwargs: 路径变量覆盖

    Returns:
        渲染后的 prompt 文本
    """
    path = os.path.join(get_prompt_dir(), f"{name}.md")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # 替换路径变量
    variables = {
        "DATA_DIR": kwargs.get("data_dir", DATA_DIR),
        "SCRIPTS_DIR": kwargs.get("scripts_dir", SCRIPTS_DIR),
        "DAILY_DIR": kwargs.get("daily_dir", DAILY_DIR),
    }
    for key, value in variables.items():
        content = content.replace("{%s}" % key, value)

    return content
