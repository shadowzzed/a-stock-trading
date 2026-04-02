"""LangGraph 编排：短线复盘多 Agent 流水线"""

from __future__ import annotations

import os
from functools import partial
from typing import Optional

from langgraph.graph import StateGraph, END

from .state import AgentState
from .agents.analysts import sentiment_analyst, sector_analyst, leader_analyst
from .agents.debate import bull_researcher, bear_researcher, judge, final_review


import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_ai_providers


DEFAULT_CONFIG = {
    "max_debate_rounds": 1,            # 多空辩论轮数
}


def should_continue_debate(state: AgentState, max_rounds: int) -> str:
    """判断辩论是否继续"""
    debate = state.get("debate_state") or {}
    count = debate.get("count", 0)
    if count >= max_rounds * 2:
        return "judge"
    if count % 2 == 1:
        return "bear"
    return "bull"


def _create_llm(cfg: dict):
    """创建 LLM 实例（Grok 优先，DeepSeek fallback）"""
    from langchain_openai import ChatOpenAI

    providers = get_ai_providers()
    if not providers:
        raise ValueError("未配置任何 AI 提供商（XAI_API_KEY 或 ARK_API_KEY）")

    # 用第一个提供商作为主力
    primary = providers[0]
    llm = ChatOpenAI(
        model=primary["model"],
        base_url=primary["base"],
        api_key=primary["key"],
        temperature=cfg.get("temperature", 0.3),
    )

    # 如果有多个提供商，用 with_fallbacks 链接
    if len(providers) > 1:
        fallbacks = []
        for p in providers[1:]:
            fallbacks.append(ChatOpenAI(
                model=p["model"],
                base_url=p["base"],
                api_key=p["key"],
                temperature=cfg.get("temperature", 0.3),
            ))
        llm = llm.with_fallbacks(fallbacks)
        print("  [LLM] %s (fallback: %s)" % (
            primary["name"], ", ".join(p["name"] for p in providers[1:])), flush=True)
    else:
        print("  [LLM] %s" % primary["name"], flush=True)

    return llm


def build_graph(config: Optional[dict] = None) -> StateGraph:
    """构建 LangGraph 图（阶段1-3：分析→辩论→初步报告）"""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    llm = _create_llm(cfg)
    max_rounds = cfg["max_debate_rounds"]

    _sentiment = partial(sentiment_analyst, llm=llm)
    _sector = partial(sector_analyst, llm=llm)
    _leader = partial(leader_analyst, llm=llm)
    _bull = partial(bull_researcher, llm=llm)
    _bear = partial(bear_researcher, llm=llm)
    _judge = partial(judge, llm=llm)

    from langgraph.graph import START

    graph = StateGraph(AgentState)

    # 阶段1：三位分析师（并行执行）
    graph.add_node("sentiment_analyst", _sentiment)
    graph.add_node("sector_analyst", _sector)
    graph.add_node("leader_analyst", _leader)

    # 阶段2：辩论
    graph.add_node("bull", _bull)
    graph.add_node("bear", _bear)
    graph.add_node("judge", _judge)

    # 流程编排：START → 三位分析师并行 → bull
    graph.add_edge(START, "sentiment_analyst")
    graph.add_edge(START, "sector_analyst")
    graph.add_edge(START, "leader_analyst")
    graph.add_edge("sentiment_analyst", "bull")
    graph.add_edge("sector_analyst", "bull")
    graph.add_edge("leader_analyst", "bull")

    graph.add_conditional_edges(
        "bull",
        partial(should_continue_debate, max_rounds=max_rounds),
        {"bear": "bear", "judge": "judge"},
    )
    graph.add_conditional_edges(
        "bear",
        partial(should_continue_debate, max_rounds=max_rounds),
        {"bull": "bull", "judge": "judge"},
    )

    graph.add_edge("judge", END)

    return graph.compile()


def build_review_graph(config: Optional[dict] = None) -> StateGraph:
    """构建终审图（阶段4：人类反馈→修订报告）"""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    llm = _create_llm(cfg)

    _review = partial(final_review, llm=llm)

    graph = StateGraph(AgentState)
    graph.add_node("final_review", _review)
    graph.set_entry_point("final_review")
    graph.add_edge("final_review", END)

    return graph.compile()


def _load_initial_state(
    data_dir: str,
    date: str,
    config: dict = None,
    prev_report: str = "",
) -> AgentState:
    """加载数据并构建初始状态

    Args:
        data_dir: trading 数据根目录
        date: 当日日期
        config: Agent 配置
        prev_report: 前一交易日的 Agent 报告（用于自我校准）
    """
    from .data.loader import (
        load_daily_data,
        summarize_limit_up,
        summarize_limit_down,
        summarize_stock_data,
        summarize_history,
        load_stock_pool,
        load_lessons,
        load_index_data,
        load_capital_flow,
        load_memory,
        load_quantitative_rules,
    )

    cfg = config or {}
    history_days = cfg.get("history_days", 7)
    data = load_daily_data(data_dir, date, history_days=history_days)

    reviews_text = ""
    if data.reviews:
        parts = []
        for name, content in data.reviews.items():
            parts.append("### {}\n{}".format(name, content))
        reviews_text = "\n\n".join(parts)

    # 加载跨周期记忆（严格截止到分析日期，不读未来数据）
    memory_dir = cfg.get("memory_dir", "")
    if not memory_dir:
        # 从 data_dir 向上推导到项目 data/ 目录，再拼接 memory/main/
        data_top = os.path.dirname(os.path.dirname(os.path.dirname(data_dir)))
        memory_dir = os.path.join(data_top, "memory", "main")
    memory_text = load_memory(memory_dir, date)

    return {
        "date": date,
        "limit_up_summary": summarize_limit_up(data.limit_up),
        "limit_down_summary": summarize_limit_down(data.limit_down),
        "stock_summary": summarize_stock_data(data.stock_data),
        "reviews_text": reviews_text,
        "events_text": data.events,
        "history_text": summarize_history(data.history),
        "stock_pool_text": load_stock_pool(data_dir),
        "prev_report": prev_report,
        "lessons_text": load_lessons(data_dir),
        "index_text": load_index_data(data_dir, date),
        "capital_flow_text": load_capital_flow(data_dir, date),
        "memory_text": memory_text,
        "quant_rules_text": load_quantitative_rules(data_dir),
        "sentiment_report": "",
        "sector_report": "",
        "leader_report": "",
        "sentiment_data": {},
        "sector_data": {},
        "leader_data": {},
        "debate_state": {
            "bull_history": [],
            "bear_history": [],
            "history": [],
            "current_response": "",
            "judge_decision": "",
            "count": 0,
        },
        "bull_claims": [],
        "bear_claims": [],
        "final_report": "",
        "final_decision": {},
        "human_feedback": "",
        "reviewed_report": "",
        "prompt_overrides": (config or {}).get("prompt_overrides", {}),
    }


def _save_intermediate_outputs(data_dir: str, date: str, state: dict):
    """保存各 agent 中间输出到 daily 目录"""
    import os
    import json as _json

    daily_dir = os.path.join(data_dir, "daily", date)
    os.makedirs(daily_dir, exist_ok=True)

    # 三位分析师 markdown 报告
    for key, filename in [
        ("sentiment_report", "agent_01_情绪分析.md"),
        ("sector_report", "agent_02_板块分析.md"),
        ("leader_report", "agent_03_龙头分析.md"),
    ]:
        content = state.get(key, "")
        if content:
            with open(os.path.join(daily_dir, filename), "w", encoding="utf-8") as f:
                f.write(content)

    # 三位分析师结构化 JSON
    for key, filename in [
        ("sentiment_data", "agent_state_01_sentiment.json"),
        ("sector_data", "agent_state_02_sector.json"),
        ("leader_data", "agent_state_03_leader.json"),
    ]:
        data = state.get(key)
        if data:
            with open(os.path.join(daily_dir, filename), "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)

    # 多空辩论
    debate = state.get("debate_state") or {}
    bull_history = debate.get("bull_history", [])
    bear_history = debate.get("bear_history", [])
    if bull_history or bear_history:
        parts = []
        max_rounds = max(len(bull_history), len(bear_history))
        for i in range(max_rounds):
            if i < len(bull_history):
                parts.append(f"## 看多派第{i+1}轮\n\n{bull_history[i]}")
            if i < len(bear_history):
                parts.append(f"## 看空派第{i+1}轮\n\n{bear_history[i]}")
        with open(os.path.join(daily_dir, "agent_04_多空辩论.md"), "w", encoding="utf-8") as f:
            f.write("\n\n---\n\n".join(parts))

    # 辩论结构化 JSON
    debate_data = {
        "bull_claims": state.get("bull_claims") or [],
        "bear_claims": state.get("bear_claims") or [],
        "rounds": len(state.get("bull_claims") or []),
    }
    with open(os.path.join(daily_dir, "agent_state_04_debate.json"), "w", encoding="utf-8") as f:
        _json.dump(debate_data, f, ensure_ascii=False, indent=2)

    # 最终报告
    final = state.get("final_report", "")
    if final:
        with open(os.path.join(daily_dir, "agent_05_裁决报告.md"), "w", encoding="utf-8") as f:
            f.write(final)

    # 最终决策结构化 JSON
    final_decision = state.get("final_decision")
    if final_decision:
        with open(os.path.join(daily_dir, "agent_state_05_decision.json"), "w", encoding="utf-8") as f:
            _json.dump(final_decision, f, ensure_ascii=False, indent=2)


def run(
    data_dir: str,
    date: str,
    config: Optional[dict] = None,
    debug: bool = False,
    prev_report: str = "",
) -> str:
    """运行短线复盘分析（完整流程，无终审）

    Args:
        data_dir: trading 数据根目录
        date: 当日日期
        config: Agent 配置
        debug: 是否启用调试输出
        prev_report: 前一交易日的 Agent 报告（用于自我校准）

    Returns:
        最终复盘报告文本
    """
    # 自动验证昨天的预测（如果条件满足），积累经验到经验库
    try:
        from .verify import verify_yesterday
        verify_yesterday(data_dir, date, config=config)
    except Exception as e:
        print(f"[自动验证] 跳过: {e}")

    init_state = _load_initial_state(data_dir, date, config, prev_report=prev_report)
    graph = build_graph(config)

    if debug:
        from rich.console import Console
        from rich.markdown import Markdown

        console = Console()
        for chunk in graph.stream(init_state):
            for node_name, node_output in chunk.items():
                console.rule("[bold cyan]{}".format(node_name))
                for key, val in node_output.items():
                    if isinstance(val, str) and val:
                        console.print(Markdown(val[:500] + ("..." if len(val) > 500 else "")))
                    elif isinstance(val, dict):
                        console.print("  {}: (debate state updated)".format(key))
        final = graph.invoke(init_state)
    else:
        final = graph.invoke(init_state)

    _save_intermediate_outputs(data_dir, date, final)
    return final.get("final_report", "（未生成报告）")


def run_with_review(
    data_dir: str,
    date: str,
    config: Optional[dict] = None,
    debug: bool = False,
    prev_report: str = "",
) -> dict:
    """运行短线复盘分析（支持终审的两阶段模式）

    Returns:
        dict with keys:
        - "report": AI 初步报告
        - "state": 完整状态（可用于终审）
    """
    # 自动验证昨天的预测（如果条件满足），积累经验到经验库
    try:
        from .verify import verify_yesterday
        verify_yesterday(data_dir, date, config=config)
    except Exception as e:
        print(f"[自动验证] 跳过: {e}")

    init_state = _load_initial_state(data_dir, date, config, prev_report=prev_report)
    graph = build_graph(config)

    if debug:
        from rich.console import Console
        from rich.markdown import Markdown

        console = Console()
        for chunk in graph.stream(init_state):
            for node_name, node_output in chunk.items():
                console.rule("[bold cyan]{}".format(node_name))
                for key, val in node_output.items():
                    if isinstance(val, str) and val:
                        console.print(Markdown(val[:500] + ("..." if len(val) > 500 else "")))

    final_state = graph.invoke(init_state)
    _save_intermediate_outputs(data_dir, date, final_state)
    return {
        "report": final_state.get("final_report", "（未生成报告）"),
        "state": final_state,
    }


def apply_review(
    state: dict,
    feedback: str,
    config: Optional[dict] = None,
) -> str:
    """应用人类终审反馈，生成修订报告

    Args:
        state: run_with_review() 返回的 state
        feedback: 人类反馈文本
        config: 配置覆盖

    Returns:
        修订后的报告
    """
    state["human_feedback"] = feedback
    review_graph = build_review_graph(config)
    result = review_graph.invoke(state)
    return result.get("reviewed_report", result.get("final_report", "（未生成报告）"))
