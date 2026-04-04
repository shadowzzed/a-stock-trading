"""LangGraph StateGraph — Trade Agent 对话图。

图拓扑：
  START → manage_context → dispatch → [fan_out] → run_analyst → synthesize → END

上下文管理策略（三层模型）：
  - 摘要层：超过 SUMMARY_THRESHOLD 条消息时，LLM 摘要旧消息，写入 state.summary
  - 近期层：最近 KEEP_RECENT 条原始消息，用 trim_messages token 级裁剪
  - System 层：Agent 专属 prompt，固定注入
"""

from __future__ import annotations

import json
import logging
import operator
import sqlite3
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, Sequence, TypedDict

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Send

from config import get_config

from .agents.base import BaseAgent, SharedDataCache
from .agents.bullbear import BullBearAgent
from .agents.dragon import DragonAgent
from .agents.sentiment import SentimentAgent
from .agents.trend import TrendAgent

logger = logging.getLogger(__name__)

# ── 上下文管理参数 ──────────────────────────────────────
SUMMARY_THRESHOLD = 20  # 超过此数量触发摘要
KEEP_RECENT = 10  # 摘要后保留最近 N 条原始消息
MAX_CONTEXT_TOKENS = 4000  # trim_messages token 上限

# Agent 名称 → 中文标签
AGENT_LABELS = {
    "dragon": "龙头分析师",
    "sentiment": "情绪分析师",
    "bullbear": "多空分析师",
    "trend": "趋势分析师",
}

# ── State 定义 ─────────────────────────────────────────


class TradingState(TypedDict, total=False):
    """Trade Agent 对话图状态。

    - messages: 由 add_messages reducer 管理（追加/去重/删除）
    - summary: 早期对话的压缩摘要
    - selected_agents: 意图识别选中的分析师
    - agent_results: 各分析师结果（operator.add 支持并行写入）
    """

    messages: Annotated[list[BaseMessage], add_messages]
    summary: str
    selected_agents: list[str]
    agent_results: Annotated[list[dict], operator.add]


# 用于 Send fan-out 的子状态
class AnalystTask(TypedDict, total=False):
    agent_name: str
    user_message: str
    context: str  # 早期摘要 + 近期对话
    agent_results: Annotated[list[dict], operator.add]


# ── 图节点 ─────────────────────────────────────────────


def manage_context(state: TradingState) -> dict:
    """上下文管理节点：摘要旧消息 + trim 近期消息。

    策略：
    1. 消息数 > SUMMARY_THRESHOLD → LLM 摘要前 N-KEEP_RECENT 条，然后用 RemoveMessage 删除
    2. 始终用 trim_messages 确保 token 不超标
    """
    messages = state.get("messages", [])
    summary = state.get("summary", "")
    updates: dict[str, Any] = {}

    # --- 摘要：消息过多时压缩旧消息 ---
    if len(messages) > SUMMARY_THRESHOLD:
        old_msgs = messages[:-KEEP_RECENT]
        recent_msgs = messages[-KEEP_RECENT:]

        # 构建摘要 prompt
        old_text = "\n".join(
            f"{'用户' if isinstance(m, HumanMessage) else '助手'}: {m.content[:300]}"
            for m in old_msgs
        )
        if summary:
            summary_instruction = (
                f"已有摘要：{summary}\n\n"
                "请扩展摘要，纳入以下新对话内容。保留关键信息："
                "具体股票名/代码、涨跌数据、用户明确表达的偏好或结论。"
            )
        else:
            summary_instruction = (
                "请为以下对话生成简洁摘要。保留关键信息："
                "具体股票名/代码、涨跌数据、用户明确表达的偏好或结论。"
            )

        try:
            from .agents.base import BaseAgent

            llm = _get_llm()
            resp = llm.invoke(
                [
                    SystemMessage(content="你是对话摘要助手。摘要用中文，200字以内。"),
                    HumanMessage(content=f"{summary_instruction}\n\n{old_text}"),
                ]
            )
            new_summary = resp.content
            logger.info("摘要生成完成（%d → %d 字符）", len(old_text), len(new_summary))
        except Exception as e:
            logger.warning("摘要生成失败，保留原始消息: %s", e)
            return updates

        # 用 RemoveMessage 删除旧消息（add_messages reducer 处理）
        removals = [RemoveMessage(id=m.id) for m in old_msgs if m.id]
        updates["summary"] = new_summary
        updates["messages"] = removals

    # --- Trim：确保 token 不超标 ---
    # 注意：摘要后 messages 还包含 recent，trim 只做安全兜底
    return updates


def dispatch(state: TradingState) -> dict:
    """意图识别节点：判断需要调用哪些分析师。

    结合摘要 + 近期对话理解上下文指代。
    """
    messages = state.get("messages", [])
    summary = state.get("summary", "")
    user_msg = ""
    if messages:
        last = messages[-1]
        user_msg = last.content if hasattr(last, "content") else str(last)

    # 构建上下文
    context_parts = []
    if summary:
        context_parts.append(f"[早期对话摘要] {summary}")

    recent = messages[-6:] if len(messages) > 6 else messages
    for m in recent[:-1]:  # 排除当前消息
        role = "用户" if isinstance(m, HumanMessage) else "助手"
        content = m.content[:200] if hasattr(m, "content") else str(m)[:200]
        context_parts.append(f"{role}: {content}")

    context_block = "\n".join(context_parts) if context_parts else "（无历史上下文）"

    prompt = _DISPATCH_PROMPT_WITH_CTX.format(context=context_block, message=user_msg)

    llm = _get_llm()
    try:
        resp = llm.invoke([HumanMessage(content=prompt)])
        text = resp.content.strip()
        if "[" in text:
            start = text.index("[")
            end = text.rindex("]") + 1
            agents = json.loads(text[start:end])
            if isinstance(agents, list):
                valid = {a for a in agents if a in AGENT_LABELS}
                return {"selected_agents": sorted(valid)}
    except Exception as e:
        logger.warning("意图识别失败，fallback 到全部分析师: %s", e)

    return {"selected_agents": sorted(AGENT_LABELS.keys())}


def run_analyst(state: AnalystTask) -> dict:
    """分析师执行节点：调用对应 Agent 的 analyze 方法。

    每个 Send 创建独立的 run_analyst 调用，并行执行。
    """
    agent_name = state["agent_name"]
    user_msg = state["user_message"]
    context = state.get("context", "")

    agent = _get_agent(agent_name)
    if not agent:
        return {"agent_results": [{"agent": agent_name, "result": f"未知分析师: {agent_name}"}]}

    enriched = user_msg
    if context:
        enriched = f"[对话上下文]\n{context}\n\n[用户当前问题]\n{user_msg}"

    try:
        result = agent.analyze(enriched)
        logger.info("[%s] 分析完成: %s", agent_name, result[:100])
        return {"agent_results": [{"agent": agent_name, "result": result}]}
    except Exception as e:
        logger.error("[%s] 分析异常: %s", agent_name, e, exc_info=True)
        return {"agent_results": [{"agent": agent_name, "result": f"（{agent_name} 分析失败: {e}）"}]}


def direct_reply(state: TradingState) -> dict:
    """简单查询：协调器直接用工具回复（无 Sub-Agent）。"""
    messages = state.get("messages", [])
    user_msg = ""
    if messages:
        last = messages[-1]
        user_msg = last.content if hasattr(last, "content") else str(last)

    coordinator = _get_coordinator()
    reply = coordinator._direct_reply(user_msg)
    return {"messages": [AIMessage(content=reply)]}


def synthesize(state: TradingState) -> dict:
    """综合节点：汇总各分析师结果，生成最终回复。"""
    results = state.get("agent_results", [])
    messages = state.get("messages", [])
    user_msg = ""
    if messages:
        last = messages[-1]
        user_msg = last.content if hasattr(last, "content") else str(last)

    # 构建分析摘要
    analysis_parts = []
    for r in results:
        label = AGENT_LABELS.get(r["agent"], r["agent"])
        analysis_parts.append(f"### {label}\n{r['result']}")
    analysis_text = "\n".join(analysis_parts)

    synthesis_prompt = f"""以下是各位分析师的分析结果：

{analysis_text}

请根据以上分析结果，综合回答用户的问题。要求：
1. 先给出核心结论
2. 引用各分析师的关键发现（标注来源）
3. 如果分析师之间有分歧，由你做最终判断并说明理由
4. 给出可操作的建议（如适用）
5. 附带风险提示

用户问题：{user_msg}"""

    coordinator = _get_coordinator()
    messages_for_llm: list[BaseMessage] = [SystemMessage(content=coordinator.system_prompt)]
    messages_for_llm.append(HumanMessage(content=synthesis_prompt))

    try:
        llm = _get_llm()
        response = llm.invoke(messages_for_llm)
        reply = response.content or "（综合分析未生成有效结果）"
    except Exception as e:
        logger.error("综合分析异常: %s", e, exc_info=True)
        # fallback：拼接各分析师结果
        parts = [f"**{AGENT_LABELS.get(r['agent'], r['agent'])}**:\n{r['result']}" for r in results]
        reply = "\n\n---\n\n".join(parts)

    return {"messages": [AIMessage(content=reply)]}


# ── 路由函数 ───────────────────────────────────────────


def route_or_fan_out(state: TradingState):
    """dispatch 后统一路由：
    - 有分析师 → 返回 Send 列表（并行 fan-out 到 run_analyst）
    - 无分析师 → 返回 "direct_reply" 字符串
    """
    agents = state.get("selected_agents", [])
    if not agents:
        return "direct_reply"

    messages = state.get("messages", [])
    summary = state.get("summary", "")
    user_msg = ""
    if messages:
        last = messages[-1]
        user_msg = last.content if hasattr(last, "content") else str(last)

    # 构建上下文
    context_parts = []
    if summary:
        context_parts.append(f"[早期对话摘要] {summary}")
    recent = messages[-6:] if len(messages) > 6 else messages[:-1]
    for m in recent:
        role = "用户" if isinstance(m, HumanMessage) else "助手"
        content = m.content[:200] if hasattr(m, "content") else str(m)[:200]
        context_parts.append(f"{role}: {content}")
    context = "\n".join(context_parts)

    logger.info("并行分发: %s", agents)
    return [
        Send(
            "run_analyst",
            AnalystTask(
                agent_name=name,
                user_message=user_msg,
                context=context,
                agent_results=[],
            ),
        )
        for name in agents
    ]


# ── 意图识别 Prompt ─────────────────────────────────────

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


# ── 单例管理（延迟初始化）──────────────────────────────

_agents: Dict[str, BaseAgent] = {}
_coordinator: Optional[Any] = None
_llm = None
_cache: Optional[SharedDataCache] = None


def _ensure_initialized():
    """确保 Agent 和 LLM 已初始化（延迟到首次调用）。"""
    global _agents, _coordinator, _llm, _cache

    if _coordinator is not None:
        return

    cfg = get_config()
    data_dir = cfg["data_root"]
    memory_dir = cfg["memory_dir"]

    _cache = SharedDataCache()
    _coordinator = CoordinatorAgent.__new__(CoordinatorAgent)
    # 手动初始化 coordinator（跳过 __init__ 中的 sub-agent 创建）
    _coordinator.data_dir = data_dir
    _coordinator.memory_dir = memory_dir
    _coordinator.cache = _cache

    # 初始化 LLM（复用 BaseAgent 的 provider 逻辑）
    from config import get_ai_providers
    from langchain_openai import ChatOpenAI

    providers = get_ai_providers()
    if not providers:
        raise ValueError("未配置 AI 提供商")

    primary = providers[0]
    _llm = ChatOpenAI(
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
        _llm = _llm.with_fallbacks(fallbacks)

    # 初始化各 Agent（共享 cache）
    _coordinator.llm = _llm
    _coordinator.name = "coordinator"
    _coordinator.prompt_file = "coordinator.md"
    _coordinator.tools_filter = None

    # 初始化工具（coordinator 需要 _direct_reply）
    from datetime import datetime
    from trading_agent.review.tools.retrieval import RetrievalToolFactory

    today = datetime.now().strftime("%Y-%m-%d")
    factory = RetrievalToolFactory(data_dir, today, memory_dir)
    _coordinator.tools = factory.create_tools()
    _coordinator.tool_map = {t.name: t for t in _coordinator.tools}
    _coordinator.system_prompt = _coordinator._load_prompt()

    # 初始化 Sub-Agents
    _agents["dragon"] = DragonAgent(data_dir, memory_dir, cache=_cache)
    _agents["sentiment"] = SentimentAgent(data_dir, memory_dir, cache=_cache)
    _agents["bullbear"] = BullBearAgent(data_dir, memory_dir, cache=_cache)
    _agents["trend"] = TrendAgent(data_dir, memory_dir, cache=_cache)

    logger.info("LangGraph 图节点已初始化（4 位分析师 + coordinator）")


def _get_llm():
    _ensure_initialized()
    return _llm


def _get_agent(name: str) -> Optional[BaseAgent]:
    _ensure_initialized()
    return _agents.get(name)


def _get_coordinator():
    _ensure_initialized()
    return _coordinator


def reset_initialization():
    """重置初始化状态（测试用）。"""
    global _agents, _coordinator, _llm, _cache
    _agents = {}
    _coordinator = None
    _llm = None
    _cache = None


# ── 构建 + 编译图 ─────────────────────────────────────


def build_graph():
    """构建 Trade Agent 对话图。"""
    builder = StateGraph(TradingState)

    # 添加节点
    builder.add_node("manage_context", manage_context)
    builder.add_node("dispatch", dispatch)
    builder.add_node("run_analyst", run_analyst)
    builder.add_node("synthesize", synthesize)
    builder.add_node("direct_reply", direct_reply)

    # 边
    builder.add_edge(START, "manage_context")
    builder.add_edge("manage_context", "dispatch")

    # dispatch → 统一路由：Send fan-out（有分析师）或 direct_reply（无分析师）
    builder.add_conditional_edges(
        "dispatch",
        route_or_fan_out,
        ["run_analyst", "direct_reply"],
    )

    # run_analyst 完成 → synthesize
    builder.add_edge("run_analyst", "synthesize")

    # synthesize 和 direct_reply → END
    builder.add_edge("synthesize", END)
    builder.add_edge("direct_reply", END)

    return builder


def create_graph(checkpointer=None):
    """创建并编译对话图。

    Args:
        checkpointer: 持久化后端。默认 InMemorySaver。
            生产可用 SqliteSaver 做磁盘持久化。

    Returns:
        编译后的 CompiledStateGraph
    """
    if checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        checkpointer = InMemorySaver()

    graph = build_graph().compile(checkpointer=checkpointer)
    logger.info("Trade Agent LangGraph 已编译")
    return graph


def create_graph_with_sqlite(db_path: str = "trading_checkpoints.db"):
    """创建使用 SQLite 持久化的对话图。"""
    import sqlite3 as _sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = _sqlite3.connect(db_path, check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    return create_graph(checkpointer=checkpointer)
