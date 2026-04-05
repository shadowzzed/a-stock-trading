"""盘中对话 Agent -- 委托给 Coordinator 的 Agent Teams 架构。"""

from __future__ import annotations

import logging
from typing import List, Optional

from langchain_core.messages import BaseMessage

from config import get_config
from trading_agent.review.tools.retrieval import RetrievalToolFactory

from .coordinator import CoordinatorAgent

logger = logging.getLogger(__name__)


class TradingChatAgent:
    """盘中对话 Agent，基于 Agent Teams 架构。

    对外接口不变（chat 方法），内部委托给 CoordinatorAgent 进行
    意图识别、任务分发和结果综合。
    """

    def __init__(self, backtest_max_date: Optional[str] = None):
        cfg = get_config()
        data_dir = cfg["data_root"]
        memory_dir = cfg["memory_dir"]

        self.coordinator = CoordinatorAgent(data_dir, memory_dir,
                                            backtest_max_date=backtest_max_date)
        logger.info("Trade Agent Teams 已初始化（4 位分析师）")

    def chat(
        self,
        user_message: str,
        history: Optional[List[BaseMessage]] = None,
    ) -> str:
        """处理一条用户消息，返回回复文本。

        Args:
            user_message: 用户消息文本
            history: 对话历史消息列表（可选，用于多轮对话）

        Returns:
            Agent 回复文本
        """
        return self.coordinator.chat(user_message, history=history)
