"""阶段2：多空辩论 + 裁决官"""

from langchain_core.messages import SystemMessage, HumanMessage

from ..state import AgentState, DebateState
from ..prompts import BULL_RESEARCHER, BEAR_RESEARCHER, JUDGE, FINAL_REVIEW


def _get_prompt(state, agent_name, default_prompt):
    """获取 agent prompt，支持用户自定义追加"""
    overrides = state.get("prompt_overrides") or {}
    extra = overrides.get(agent_name, "")
    if extra:
        return default_prompt + "\n\n## 用户补充指令\n" + extra
    return default_prompt


def _build_analyst_context(state: AgentState) -> str:
    """合并三位分析师的报告 + 人类复盘"""
    reviews = state.get('reviews_text', '') or ''
    reviews_section = f"""# 人类交易员复盘文档
{reviews}""" if reviews else "# 人类交易员复盘文档\n（今日无人类复盘文档）"

    return f"""# 情绪周期分析报告
{state.get('sentiment_report', '（未生成）')}

# 板块轮动分析报告
{state.get('sector_report', '（未生成）')}

# 龙头辨识分析报告
{state.get('leader_report', '（未生成）')}

# 事件催化
{state.get('events_text', '（无）')}

{reviews_section}
"""


def bull_researcher(state: AgentState, llm) -> dict:
    """看多派研究员"""
    context = _build_analyst_context(state)
    debate = state.get("debate_state") or {}
    bear_history = debate.get("bear_history", [])

    if bear_history:
        prev = "\n\n".join(bear_history)
        user_msg = f"""{context}

看空派刚才说：
{prev[-1] if bear_history else ''}

请反驳看空派的观点，给出你的看多论据。"""
    else:
        user_msg = f"""{context}

请基于以上分析师报告，给出你的看多观点和做多机会。"""

    prompt = _get_prompt(state, "bull", BULL_RESEARCHER)
    response = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=user_msg),
    ])

    bull_history = list(debate.get("bull_history", []))
    bull_history.append(response.content)
    count = debate.get("count", 0) + 1

    new_debate: DebateState = {
        "bull_history": bull_history,
        "bear_history": list(debate.get("bear_history", [])),
        "history": [f"【看多派第{len(bull_history)}轮】\n{response.content}"],
        "current_response": response.content,
        "judge_decision": "",
        "count": count,
    }
    return {"debate_state": new_debate}


def bear_researcher(state: AgentState, llm) -> dict:
    """看空派研究员"""
    context = _build_analyst_context(state)
    debate = state.get("debate_state") or {}
    bull_history = debate.get("bull_history", [])

    user_msg = f"""{context}

看多派刚才说：
{bull_history[-1] if bull_history else '（无）'}

请反驳看多派的观点，指出做多的风险和隐患。"""

    prompt = _get_prompt(state, "bear", BEAR_RESEARCHER)
    response = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=user_msg),
    ])

    bear_history = list(debate.get("bear_history", []))
    bear_history.append(response.content)
    count = debate.get("count", 0) + 1

    new_debate: DebateState = {
        "bull_history": list(debate.get("bull_history", [])),
        "bear_history": bear_history,
        "history": [f"【看空派第{len(bear_history)}轮】\n{response.content}"],
        "current_response": response.content,
        "judge_decision": "",
        "count": count,
    }
    return {"debate_state": new_debate}


def judge(state: AgentState, llm) -> dict:
    """裁决官：综合所有信息输出最终报告"""
    context = _build_analyst_context(state)
    debate = state.get("debate_state") or {}
    debate_history = "\n\n---\n\n".join(debate.get("history", []))

    prev_report = state.get('prev_report', '') or ''
    prev_section = ""
    if prev_report:
        prev_section = f"""# 前日 Agent 预测报告（自我校准）
对比今天的实际数据，反思昨天的预测偏差，在今天的报告中体现校准。

{prev_report}

---

"""

    lessons_text = state.get('lessons_text', '') or ''
    lessons_section = f"\n{lessons_text}\n\n---\n\n" if lessons_text else ""

    # 补充指数和资金流数据供裁决官直接参考
    index_text = state.get('index_text', '') or ''
    capital_flow_text = state.get('capital_flow_text', '') or ''
    quant_rules_text = state.get('quant_rules_text', '') or ''
    extra_data = "\n\n".join(filter(None, [index_text, capital_flow_text, quant_rules_text]))
    extra_section = f"\n{extra_data}\n\n---\n\n" if extra_data else ""

    user_msg = f"""日期：{state['date']}
{lessons_section}{prev_section}{extra_section}{context}

# 多空辩论记录
{debate_history}

请综合以上所有信息，输出最终的每日复盘报告。"""

    prompt = _get_prompt(state, "judge", JUDGE)
    response = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=user_msg),
    ])

    return {"final_report": response.content}


def final_review(state: AgentState, llm) -> dict:
    """终审修订：根据人类反馈修订报告"""
    feedback = state.get("human_feedback", "")
    if not feedback:
        # 无反馈，直接用初版
        return {"reviewed_report": state.get("final_report", "")}

    user_msg = f"""# 初步复盘报告
{state.get('final_report', '')}

# 用户终审反馈
{feedback}

请根据用户反馈修订报告。"""

    response = llm.invoke([
        SystemMessage(content=FINAL_REVIEW),
        HumanMessage(content=user_msg),
    ])

    return {"reviewed_report": response.content}
