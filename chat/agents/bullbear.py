"""多空分析师 — 主线判断、板块轮动、资金流向、交易策略。"""

from __future__ import annotations

import logging
from typing import Optional

from .base import BaseAgent

logger = logging.getLogger(__name__)


class BullBearAgent(BaseAgent):
    """多空分析师。

    擅长：主线/支线判定、板块轮动节奏、资金流向、量价关系、交易策略。
    """

    name = "bullbear"
    prompt_file = "bullbear.md"
    tools_filter = [
        "get_market_data",
        "get_history_data",
        "get_capital_flow",
        "get_review_docs",
        "get_quant_rules",
        "get_prev_report",
    ]
