"""阶段2：多空辩论 + 裁决官"""

from langchain_core.messages import SystemMessage, HumanMessage

from ..state import AgentState, DebateState
from ..prompts import BULL_RESEARCHER, BEAR_RESEARCHER, JUDGE, FINAL_REVIEW
from .analysts import _parse_structured_output


# 附加到辩论 user message 末尾的 JSON 输出指令
_DEBATE_JSON_INSTRUCTION = """

在你的辩论输出的最开头，必须先输出一个 JSON 代码块：
```json
{
  "claim": "本轮核心观点",
  "core_evidence": ["证据1", "证据2"],
  "attack_previous_point": "针对对方观点的反驳",
  "uncertainty": "本观点的不确定性",
  "trading_implication": "对交易动作的含义"
}
```
然后再输出你的完整论述。"""

# 附加到裁决官 user message 末尾的 JSON 输出指令
_JUDGE_JSON_INSTRUCTION = """

在你的报告最开头，必须先输出一个 JSON 代码块：
```json
{
  "market_bias": "偏多/偏空/震荡",
  "position_advice": "轻仓/半仓/重仓/空仓",
  "main_sectors": ["板块1", "板块2"],
  "focus_stocks": ["个股1", "个股2"],
  "do_actions": ["可做动作1"],
  "avoid_actions": ["避免动作1"],
  "risk_conditions": ["风险触发条件1"],
  "confidence": 0.68
}
```
然后再输出完整的复盘报告。"""


def _get_prompt(state, agent_name, default_prompt):
    """获取 agent prompt，支持用户自定义追加"""
    overrides = state.get("prompt_overrides") or {}
    extra = overrides.get(agent_name, "")
    if extra:
        return default_prompt + "\n\n## 用户补充指令\n" + extra
    return default_prompt


def _build_analyst_context(state: AgentState) -> str:
    """合并三位分析师的报告 + 事件催化"""
    return f"""# 情绪周期分析报告
{state.get('sentiment_report', '（未生成）')}

# 板块轮动分析报告
{state.get('sector_report', '（未生成）')}

# 龙头辨识分析报告
{state.get('leader_report', '（未生成）')}

# 事件催化
{state.get('events_text', '（无）')}
"""


def bull_researcher(state: AgentState, llm, tools=None) -> dict:
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

    user_msg += """

你可以使用以下工具获取额外数据：
- get_review_docs: 获取人类复盘文档
- get_stock_detail: 查询个股详细行情
- get_history_data: 获取历史情绪数据
"""

    user_msg += _DEBATE_JSON_INSTRUCTION

    prompt = _get_prompt(state, "bull", BULL_RESEARCHER)

    if tools:
        from ..graph import _run_with_tools
        content = _run_with_tools(llm, tools, prompt, user_msg)
    else:
        response = llm.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content=user_msg),
        ])
        content = response.content

    # 解析结构化论点
    claim_data, _ = _parse_structured_output(content)
    bull_claims = list(state.get("bull_claims") or [])
    if claim_data:
        bull_claims.append(claim_data)

    bull_history = list(debate.get("bull_history", []))
    bull_history.append(content)
    count = debate.get("count", 0) + 1

    new_debate: DebateState = {
        "bull_history": bull_history,
        "bear_history": list(debate.get("bear_history", [])),
        "history": [f"【看多派第{len(bull_history)}轮】\n{content}"],
        "current_response": content,
        "judge_decision": "",
        "count": count,
    }
    return {"debate_state": new_debate, "bull_claims": bull_claims}


def bear_researcher(state: AgentState, llm, tools=None) -> dict:
    """看空派研究员"""
    context = _build_analyst_context(state)
    debate = state.get("debate_state") or {}
    bull_history = debate.get("bull_history", [])

    user_msg = f"""{context}

看多派刚才说：
{bull_history[-1] if bull_history else '（无）'}

请反驳看多派的观点，指出做多的风险和隐患。"""

    user_msg += """

你可以使用以下工具获取额外数据：
- get_review_docs: 获取人类复盘文档
- get_stock_detail: 查询个股详细行情
- get_history_data: 获取历史情绪数据
"""

    user_msg += _DEBATE_JSON_INSTRUCTION

    prompt = _get_prompt(state, "bear", BEAR_RESEARCHER)

    if tools:
        from ..graph import _run_with_tools
        content = _run_with_tools(llm, tools, prompt, user_msg)
    else:
        response = llm.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content=user_msg),
        ])
        content = response.content

    # 解析结构化论点
    claim_data, _ = _parse_structured_output(content)
    bear_claims = list(state.get("bear_claims") or [])
    if claim_data:
        bear_claims.append(claim_data)

    bear_history = list(debate.get("bear_history", []))
    bear_history.append(content)
    count = debate.get("count", 0) + 1

    new_debate: DebateState = {
        "bull_history": list(debate.get("bull_history", [])),
        "bear_history": bear_history,
        "history": [f"【看空派第{len(bear_history)}轮】\n{content}"],
        "current_response": content,
        "judge_decision": "",
        "count": count,
    }
    return {"debate_state": new_debate, "bear_claims": bear_claims}


def judge(state: AgentState, llm, tools=None) -> dict:
    """裁决官：综合所有信息输出最终报告"""
    context = _build_analyst_context(state)
    debate = state.get("debate_state") or {}
    debate_history = "\n\n---\n\n".join(debate.get("history", []))

    user_msg = f"""日期：{state['date']}

{context}

# 多空辩论记录
{debate_history}

请综合以上所有信息，输出最终的每日复盘报告。

你可以使用以下工具获取额外数据：
- get_lessons: 获取历史经验教训
- get_prev_report: 获取昨日报告
- get_index_data: 获取指数行情
- get_capital_flow: 获取资金流向
- get_quant_rules: 获取量化规律
- get_memory: 获取近期记忆
- get_stock_detail: 查询个股详细行情
- get_review_docs: 获取人类复盘文档
"""

    user_msg += _JUDGE_JSON_INSTRUCTION

    prompt = _get_prompt(state, "judge", JUDGE)

    if tools:
        from ..graph import _run_with_tools
        content = _run_with_tools(llm, tools, prompt, user_msg)
    else:
        response = llm.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content=user_msg),
        ])
        content = response.content

    final_decision, _ = _parse_structured_output(content)

    # 累积传递辩论结构化数据
    result = {
        "final_report": content,
        "final_decision": final_decision,
        "bull_claims": state.get("bull_claims") or [],
        "bear_claims": state.get("bear_claims") or [],
    }
    return result


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
