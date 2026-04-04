"""Trade Agent 协调器 — 意图识别、任务分发、结果综合。"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from .agents.base import BaseAgent, SharedDataCache
from .agents.dragon import DragonAgent
from .agents.sentiment import SentimentAgent
from .agents.bullbear import BullBearAgent
from .agents.trend import TrendAgent

logger = logging.getLogger(__name__)

# 分发意图识别提示词
_DISPATCH_PROMPT = """根据用户消息，判断需要调用哪些分析师。

可选分析师：
- dragon: 龙头分析师（龙头辨识、连板梯队、破局龙、节点股）
- sentiment: 情绪分析师（情绪周期、涨跌停统计、赚钱效应）
- bullbear: 多空分析师（主线判断、板块轮动、资金流向、策略）
- trend: 趋势分析师（均线系统、量价背离、支撑压力位、技术面、趋势股扫描）

规则：
- 简单数据查询（"XX多少钱"、"涨停几家"）→ 空列表 []
- 龙头/连板/涨停梯队 → ["dragon"]
- 情绪/周期/赚钱效应 → ["sentiment"]
- 主线/轮动/策略 → ["bullbear"]
- 趋势/均线/技术面 → ["trend"]
- 找趋势股/均线上的股票/趋势扫描/沿均线运行 → ["trend"]
- 综合行情/整体分析 → ["dragon", "sentiment", "bullbear", "trend"]
- 个股分析（能不能买） → ["dragon", "bullbear", "trend"]
- 情绪+策略 → ["sentiment", "bullbear"]
- 板块分析 → ["bullbear", "trend"]

用户消息：{message}

请只输出一个 JSON 数组，不要其他内容。例如：["dragon", "sentiment"]
如果不需要分发，输出空数组：[]"""

# 带上下文的意图识别提示词
_DISPATCH_PROMPT_WITH_CTX = """根据用户消息和最近的对话历史，判断需要调用哪些分析师。

注意：用户的当前消息可能包含指代（如"它的"、"核心辨识度"等），需要结合历史上下文理解完整意图。

可选分析师：
- dragon: 龙头分析师（龙头辨识、连板梯队、破局龙、节点股）
- sentiment: 情绪分析师（情绪周期、涨跌停统计、赚钱效应）
- bullbear: 多空分析师（主线判断、板块轮动、资金流向、策略）
- trend: 趋势分析师（均线系统、量价背离、支撑压力位、技术面、趋势股扫描）

规则：
- 简单数据查询（"XX多少钱"、"涨停几家"）→ 空列表 []
- 龙头/连板/涨停梯队 → ["dragon"]
- 情绪/周期/赚钱效应 → ["sentiment"]
- 主线/轮动/策略 → ["bullbear"]
- 趋势/均线/技术面 → ["trend"]
- 找趋势股/均线上的股票/趋势扫描/沿均线运行 → ["trend"]
- 综合行情/整体分析 → ["dragon", "sentiment", "bullbear", "trend"]
- 个股分析（能不能买） → ["dragon", "bullbear", "trend"]
- 情绪+策略 → ["sentiment", "bullbear"]
- 板块分析 → ["bullbear", "trend"]

最近的对话历史：
{context}

用户当前消息：{message}

请只输出一个 JSON 数组，不要其他内容。例如：["dragon", "sentiment"]
如果不需要分发，输出空数组：[]"""


class CoordinatorAgent(BaseAgent):
    """协调器 Agent：意图识别 → 分发 → 综合。"""

    name = "coordinator"
    prompt_file = "coordinator.md"
    tools_filter = None  # 协调器可以使用所有工具

    def __init__(self, data_dir: str, memory_dir: str):
        # 共享数据缓存，并行分析时避免重复查询
        cache = SharedDataCache()

        super().__init__(data_dir, memory_dir, cache=cache)

        # 初始化 Sub-Agents（共享同一个 cache）
        self.dragon = DragonAgent(data_dir, memory_dir, cache=cache)
        self.sentiment = SentimentAgent(data_dir, memory_dir, cache=cache)
        self.bullbear = BullBearAgent(data_dir, memory_dir, cache=cache)
        self.trend = TrendAgent(data_dir, memory_dir, cache=cache)

        self._agent_map = {
            "dragon": self.dragon,
            "sentiment": self.sentiment,
            "bullbear": self.bullbear,
            "trend": self.trend,
        }

    def _dispatch(
        self, user_message: str, history: Optional[List[BaseMessage]] = None
    ) -> List[str]:
        """意图识别：判断需要调用哪些分析师。结合历史上下文理解指代。"""
        # 构建包含历史的 prompt，让意图识别能理解上下文指代
        context_lines = []
        if history:
            for msg in history[-6:]:  # 最近 3 轮（每轮 Human + AI）
                role = "用户" if isinstance(msg, HumanMessage) else "助手"
                context_lines.append(f"{role}: {msg.content}")
            context_block = "\n".join(context_lines)
            prompt = _DISPATCH_PROMPT_WITH_CTX.format(
                context=context_block, message=user_message
            )
        else:
            prompt = _DISPATCH_PROMPT.format(message=user_message)

        messages = [HumanMessage(content=prompt)]

        try:
            resp = self.llm.invoke(messages)
            text = resp.content.strip()
            # 尝试解析 JSON 数组
            if "[" in text:
                start = text.index("[")
                end = text.rindex("]") + 1
                agents = json.loads(text[start:end])
                if isinstance(agents, list):
                    # 过滤无效值
                    return [a for a in agents if a in self._agent_map]
        except Exception as e:
            logger.warning("意图识别失败，fallback 到全部分析师: %s", e)

        # fallback：如果无法识别意图，分发给全部分析师
        return list(self._agent_map.keys())

    def _collect_analyses(
        self,
        user_message: str,
        agent_names: List[str],
        history: Optional[List[BaseMessage]] = None,
    ) -> Dict[str, str]:
        """并行调用 Sub-Agents 并收集结果。"""
        results: Dict[str, str] = {}

        if not agent_names:
            return results

        # 构建带上下文的完整问题
        if history:
            context_lines = []
            for msg in history[-4:]:  # 最近 2 轮
                role = "用户" if isinstance(msg, HumanMessage) else "助手"
                context_lines.append(f"{role}: {msg.content}")
            context_block = "\n".join(context_lines)
            enriched_message = (
                f"[对话上下文]\n{context_block}\n\n"
                f"[用户当前问题]\n{user_message}"
            )
        else:
            enriched_message = user_message

        with ThreadPoolExecutor(max_workers=len(agent_names)) as executor:
            futures = {}
            for name in agent_names:
                agent = self._agent_map[name]
                future = executor.submit(agent.analyze, enriched_message)
                futures[future] = name

            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                    logger.info(
                        "[%s] 分析完成: %s",
                        name,
                        results[name][:100],
                    )
                except Exception as e:
                    logger.error("[%s] 分析异常: %s", name, e, exc_info=True)
                    results[name] = f"（{name} 分析失败: {e}）"

        return results

    def chat(
        self,
        user_message: str,
        history: Optional[List[BaseMessage]] = None,
    ) -> str:
        """主入口：意图识别 → 分发 → 综合。"""
        # 1. 意图识别（带历史上下文）
        agent_names = self._dispatch(user_message, history=history)
        logger.info("意图识别结果: %s → %s", user_message[:50], agent_names)

        # 2. 简单查询：不分发，直接用工具回复
        if not agent_names:
            return self._direct_reply(user_message, history)

        # 3. 复杂分析：分发到 Sub-Agents（带历史上下文）
        analyses = self._collect_analyses(user_message, agent_names, history=history)

        # 4. 综合回复
        return self._synthesize(user_message, analyses, history)

    def _direct_reply(
        self,
        user_message: str,
        history: Optional[List[BaseMessage]] = None,
    ) -> str:
        """简单查询：协调器直接用工具回复。"""
        llm_with_tools = self.llm.bind_tools(self.tools)

        messages: List[BaseMessage] = [SystemMessage(content=self.system_prompt)]
        if history:
            messages.extend(history)
        messages.append(HumanMessage(content=user_message))

        response = None
        for round_idx in range(3):
            response = llm_with_tools.invoke(messages)

            if not response.tool_calls:
                break

            logger.info(
                "[coordinator][round %d] 工具调用: %s",
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
                        logger.error("[coordinator] 工具 %s 异常: %s", tc["name"], e)
                        messages.append(
                            ToolMessage(
                                content=f"工具执行出错: {e}",
                                tool_call_id=tc["id"],
                            )
                        )

        if response and response.content:
            return response.content
        return "（无法获取有效回复）"

    def _synthesize(
        self,
        user_message: str,
        analyses: Dict[str, str],
        history: Optional[List[BaseMessage]] = None,
    ) -> str:
        """综合各分析师结果，生成最终回复。"""
        # 构建分析摘要
        analysis_text = ""
        agent_labels = {
            "dragon": "龙头分析师",
            "sentiment": "情绪分析师",
            "bullbear": "多空分析师",
            "trend": "趋势分析师",
        }
        for name, result in analyses.items():
            label = agent_labels.get(name, name)
            analysis_text += f"\n### {label}\n{result}\n"

        synthesis_prompt = f"""以下是各位分析师的分析结果：

{analysis_text}

请根据以上分析结果，综合回答用户的问题。要求：
1. 先给出核心结论
2. 引用各分析师的关键发现（标注来源）
3. 如果分析师之间有分歧，由你做最终判断并说明理由
4. 给出可操作的建议（如适用）
5. 附带风险提示

用户问题：{user_message}"""

        messages: List[BaseMessage] = [SystemMessage(content=self.system_prompt)]
        if history:
            messages.extend(history)
        messages.append(HumanMessage(content=synthesis_prompt))

        try:
            response = self.llm.invoke(messages)
            return response.content or "（综合分析未生成有效结果）"
        except Exception as e:
            logger.error("综合分析异常: %s", e, exc_info=True)
            # fallback：拼接各分析师结果
            parts = [f"**{agent_labels.get(n, n)}**:\n{r}" for n, r in analyses.items()]
            return "\n\n---\n\n".join(parts)
