"""BaseAgent — 所有 Sub-Agent 的基类。

提供 LLM 创建、工具绑定、对话循环、数据缓存的通用逻辑。
"""

from __future__ import annotations

import json
import logging
import os
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI

from config import get_config, get_ai_providers
from trading_agent.review.tools.retrieval import RetrievalToolFactory

logger = logging.getLogger(__name__)

# prompts 目录
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class SharedDataCache:
    """同一轮分析中，多个 Agent 共享的数据缓存。

    避免并行调用时重复查库/读文件。
    """

    def __init__(self):
        self._cache: Dict[tuple, str] = {}

    def get_or_fetch(self, tool_name: str, args: dict, fetch_fn) -> str:
        key = (tool_name, json.dumps(args, sort_keys=True))
        if key not in self._cache:
            self._cache[key] = fetch_fn()
        return self._cache[key]


class BaseAgent:
    """Sub-Agent 基类。

    子类需要定义：
    - name: Agent 名称
    - prompt_file: 提示词文件名（在 prompts/ 目录下）
    - tools_filter: 该 Agent 可用的工具名列表（None = 全部）
    """

    name: str = "base"
    prompt_file: str = ""
    tools_filter: Optional[list[str]] = None

    def __init__(
        self,
        data_dir: str,
        memory_dir: str,
        cache: Optional[SharedDataCache] = None,
        backtest_max_date: Optional[str] = None,
    ):
        self.data_dir = data_dir
        self.memory_dir = memory_dir
        self.cache = cache
        self.backtest_max_date = backtest_max_date

        # LLM（复用同一套 provider 配置）
        providers = get_ai_providers()
        if not providers:
            raise ValueError("未配置 AI 提供商")

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

        # 工具
        today = datetime.now().strftime("%Y-%m-%d")
        factory = RetrievalToolFactory(
            data_dir, today, memory_dir,
            backtest_max_date=self.backtest_max_date,
        )
        all_tools = factory.create_tools()

        if self.tools_filter:
            self.tools = [t for t in all_tools if t.name in self.tools_filter]
        else:
            self.tools = all_tools

        self.tool_map = {t.name: t for t in self.tools}

        # 系统提示词
        self.system_prompt = self._load_prompt()

    def _load_prompt(self) -> str:
        if not self.prompt_file:
            return ""
        path = PROMPTS_DIR / self.prompt_file
        if path.exists():
            return path.read_text(encoding="utf-8")
        logger.warning("Prompt 文件不存在: %s", path)
        return ""

    def analyze(self, question: str, context: str = "") -> str:
        """执行分析，返回结果文本。

        Args:
            question: 用户问题或协调器分发的问题
            context: 额外上下文（如其他 Agent 的中间结果）
        """
        llm_with_tools = self.llm.bind_tools(self.tools)

        messages: List[BaseMessage] = [SystemMessage(content=self.system_prompt)]

        user_content = question
        if context:
            user_content = f"## 额外上下文\n{context}\n\n## 问题\n{question}"

        messages.append(HumanMessage(content=user_content))

        # Tool-calling loop (max 3 rounds)
        response = None
        for round_idx in range(3):
            response = llm_with_tools.invoke(messages)

            if not response.tool_calls:
                logger.info("[%s][round %d] 无工具调用", self.name, round_idx)
                break

            logger.info(
                "[%s][round %d] 工具调用: %s",
                self.name,
                round_idx,
                [tc["name"] for tc in response.tool_calls],
            )
            messages.append(response)

            for tc in response.tool_calls:
                tool_fn = self.tool_map.get(tc["name"])
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
                        logger.error("[%s] 工具 %s 异常: %s", self.name, tc["name"], e)
                        messages.append(
                            ToolMessage(
                                content=f"工具执行出错: {e}",
                                tool_call_id=tc["id"],
                            )
                        )

        if response and response.content:
            return response.content
        return "（分析未生成有效结果）"
