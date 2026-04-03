"""情绪周期分析师 — 判断市场情绪阶段、赚钱效应、退潮/修复信号。"""

from __future__ import annotations

import logging
from typing import Optional

from .base import BaseAgent

logger = logging.getLogger(__name__)


class SentimentAgent(BaseAgent):
    """情绪周期分析师。

    擅长：情绪周期定位、涨跌停统计、炸板率分析、冰点/高潮识别。
    """

    name = "sentiment"
    prompt_file = "sentiment.md"
    tools_filter = [
        "get_market_data",
        "get_history_data",
        "get_index_data",
        "get_memory",
        "get_lessons",
        "get_quant_rules",
    ]
