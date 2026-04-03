"""龙头辨识分析师 — 识别龙头股、连板梯队、破局龙、节点股。"""

from __future__ import annotations

import logging
from typing import Optional

from .base import BaseAgent

logger = logging.getLogger(__name__)


class DragonAgent(BaseAgent):
    """龙头辨识分析师。

    擅长：龙头辨识、连板梯队分析、破局龙/节点股识别、补涨逻辑。
    """

    name = "dragon"
    prompt_file = "dragon.md"
    tools_filter = [
        "get_market_data",
        "get_stock_detail",
        "get_history_data",
        "get_quant_rules",
        "get_prev_report",
        "get_past_report",
    ]
