"""盘中分析 Agent 配置 — 从全局 config 读取"""

import os
import sys

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config import get_config


def get_prompt_dir():
    """获取 prompt 目录"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def load_prompt(name: str, **kwargs) -> str:
    """加载并渲染 prompt 文件"""
    path = os.path.join(get_prompt_dir(), "%s.md" % name)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    cfg = get_config()
    variables = {
        "DATA_DIR": kwargs.get("data_dir", cfg["data_root"]),
        "SCRIPTS_DIR": kwargs.get("scripts_dir", os.path.join(cfg["project_root"], "data")),
        "DAILY_DIR": kwargs.get("daily_dir", cfg["daily_dir"]),
    }
    for key, value in variables.items():
        content = content.replace("{%s}" % key, value)

    return content
