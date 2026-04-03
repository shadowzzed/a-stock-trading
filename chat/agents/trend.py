"""趋势分析师 — 板块/个股趋势追踪、均线系统、量价背离、支撑/压力位。"""

from __future__ import annotations

import logging
from typing import Optional

from .base import BaseAgent

logger = logging.getLogger(__name__)


class TrendAgent(BaseAgent):
    """趋势分析师。

    擅长：K线形态、均线系统（5/10/20/60日线）、量价关系、
    支撑/压力位判断、板块趋势追踪、MACD/KDJ 等技术指标。
    """

    name = "trend"
    prompt_file = "trend.md"
    tools_filter = [
        "get_market_data",
        "get_stock_detail",
        "get_history_data",
        "get_index_data",
        "get_capital_flow",
    ]
