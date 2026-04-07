"""竞价辨识度分析师 — 竞价期间辨识度个股筛选。"""

from __future__ import annotations

import logging
from typing import Optional

from .base import BaseAgent

logger = logging.getLogger(__name__)


class AuctionAgent(BaseAgent):
    """竞价辨识度分析师。

    擅长：竞价筛选、高开过顶、断板反包、超预期首板、竞价三信号分析。
    """

    name = "auction"
    prompt_file = "auction.md"
    tools_filter = [
        "get_market_data",
        "get_stock_detail",
        "get_history_data",
        "get_quant_rules",
        "get_prev_report",
    ]
