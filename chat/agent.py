"""盘中对话 Agent -- 基于 LangChain 工具调用的 ReAct 模式"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import List, Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI

from config import get_config, get_ai_providers
from review.tools.retrieval import RetrievalToolFactory

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是「短线助手」，一位专业的 A 股短线交易分析 AI。

你可以帮助用户进行：
- 实时行情分析（个股、板块、指数）
- 历史复盘查询（过去 7 天的涨跌停、连板梯队、龙头追踪）
- 交易策略讨论
- 情绪周期判断

你有以下工具可以调用：
- get_history_data: 查询近几日历史情绪数据
- get_review_docs: 获取博主复盘文档
- get_memory: 获取近期每日行情认知
- get_lessons: 获取历史经验教训
- get_prev_report: 获取昨日 Agent 报告
- get_index_data: 获取指数行情
- get_capital_flow: 获取资金流向
- get_quant_rules: 获取量化规律
- get_stock_detail: 查询个股详细行情（intraday.db）
- get_past_report: 获取任意历史日期的 Agent 报告

回答要求：
- 简洁直接，不要冗长的开场白
- 用数据说话，引用具体的涨跌停数、炸板率、连板高度等
- 给出明确可操作的建议，不要模棱两可
- 如果数据不够，主动调用工具获取
"""


class TradingChatAgent:
    """盘中对话 Agent，基于 LangChain 工具调用循环。"""

    def __init__(self):
        cfg = get_config()
        self.data_dir = cfg["data_root"]
        self.memory_dir = RetrievalToolFactory._infer_memory_dir(self.data_dir)

        # Create LLM with fallback chain
        providers = get_ai_providers()
        if not providers:
            raise ValueError("未配置 AI 提供商（Grok / DeepSeek）")

        primary = providers[0]
        self.llm = ChatOpenAI(
            model=primary["model"],
            base_url=primary["base"],
            api_key=primary["key"],
            temperature=0.3,
        )

        if len(providers) > 1:
            fallbacks = [
                ChatOpenAI(
                    model=p["model"],
                    base_url=p["base"],
                    api_key=p["key"],
                    temperature=0.3,
                )
                for p in providers[1:]
            ]
            self.llm = self.llm.with_fallbacks(fallbacks)

    def _get_tools(self) -> list:
        """创建绑定到当天日期的检索工具。"""
        today = datetime.now().strftime("%Y-%m-%d")
        factory = RetrievalToolFactory(self.data_dir, today, self.memory_dir)
        return factory.create_tools()

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
        tools = self._get_tools()
        llm_with_tools = self.llm.bind_tools(tools)
        tool_map = {t.name: t for t in tools}

        # Build message list
        messages: List[BaseMessage] = [SystemMessage(content=SYSTEM_PROMPT)]
        if history:
            messages.extend(history)
        messages.append(HumanMessage(content=user_message))

        # Tool-calling loop (max 5 rounds)
        response = None
        for _ in range(5):
            response = llm_with_tools.invoke(messages)

            if not response.tool_calls:
                break

            messages.append(response)
            for tc in response.tool_calls:
                tool_fn = tool_map.get(tc["name"])
                if tool_fn:
                    try:
                        result = tool_fn.invoke(tc["args"])
                        messages.append(
                            ToolMessage(
                                content=str(result),
                                tool_call_id=tc["id"],
                            )
                        )
                    except Exception as e:
                        logger.error("工具 %s 执行异常: %s", tc["name"], e)
                        messages.append(
                            ToolMessage(
                                content=f"工具执行出错: {e}",
                                tool_call_id=tc["id"],
                            )
                        )

        if response and response.content:
            return response.content
        return "（Agent 未生成有效回复）"
