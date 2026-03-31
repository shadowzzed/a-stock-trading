"""阶段1：三位分析师 Agent"""

from langchain_core.messages import SystemMessage, HumanMessage

from ..state import AgentState
from ..prompts import SENTIMENT_ANALYST, SECTOR_ANALYST, LEADER_ANALYST


def _get_prompt(state, agent_name, default_prompt):
    """获取 agent prompt，支持用户自定义追加"""
    overrides = state.get("prompt_overrides") or {}
    extra = overrides.get(agent_name, "")
    if extra:
        return default_prompt + "\n\n## 用户补充指令\n" + extra
    return default_prompt


def _build_context_sections(state):
    """构建通用上下文段落（指数、资金流、记忆、量化规律）"""
    sections = []

    index_text = state.get('index_text', '') or ''
    if index_text:
        sections.append(index_text)

    capital_flow_text = state.get('capital_flow_text', '') or ''
    if capital_flow_text:
        sections.append(capital_flow_text)

    memory_text = state.get('memory_text', '') or ''
    if memory_text:
        sections.append(memory_text)

    quant_rules_text = state.get('quant_rules_text', '') or ''
    if quant_rules_text:
        sections.append(quant_rules_text)

    return "\n\n".join(sections)


def sentiment_analyst(state: AgentState, llm) -> dict:
    """情绪周期分析师"""
    history_text = state.get('history_text', '') or '（无历史数据）'
    prev_report = state.get('prev_report', '') or ''

    prev_section = ""
    if prev_report:
        prev_section = """## 前日 Agent 预测报告（自我校准用）
以下是你们昨天输出的预测报告。对比今天的实际数据，思考昨天哪些判断正确、哪些偏差了，在今天的分析中避免重复犯错。

{prev}

---
""".format(prev=prev_report)

    lessons_text = state.get('lessons_text', '') or ''
    lessons_section = f"\n{lessons_text}\n\n---\n" if lessons_text else ""

    extra_context = _build_context_sections(state)
    extra_section = f"\n{extra_context}\n\n---\n" if extra_context else ""

    user_msg = """以下是 {date} 的行情数据，请分析市场情绪周期。
{lessons}{prev_section}{extra}{history}

## 当日数据

{limit_up}

{limit_down}

{reviews}
""".format(
        date=state['date'],
        lessons=lessons_section,
        prev_section=prev_section,
        extra=extra_section,
        history=history_text,
        limit_up=state['limit_up_summary'],
        limit_down=state['limit_down_summary'],
        reviews=state.get('reviews_text', '') or '（无复盘文档）',
    )
    prompt = _get_prompt(state, "sentiment_analyst", SENTIMENT_ANALYST)
    response = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=user_msg),
    ])
    return {"sentiment_report": response.content}


def sector_analyst(state: AgentState, llm) -> dict:
    """板块轮动分析师"""
    history_text = state.get('history_text', '') or '（无历史数据）'
    pool_text = state.get('stock_pool_text', '') or ''
    pool_section = f"\n{pool_text}\n" if pool_text else ""

    extra_context = _build_context_sections(state)
    extra_section = f"\n{extra_context}\n\n---\n" if extra_context else ""

    user_msg = """以下是 {date} 的行情数据，请分析板块轮动情况。

{history}
{pool}{extra}
## 当日数据

{limit_up}

{limit_down}

{stock}

{reviews}
""".format(
        date=state['date'],
        history=history_text,
        pool=pool_section,
        extra=extra_section,
        limit_up=state['limit_up_summary'],
        limit_down=state['limit_down_summary'],
        stock=state['stock_summary'],
        reviews=state.get('reviews_text', '') or '（无复盘文档）',
    )
    prompt = _get_prompt(state, "sector_analyst", SECTOR_ANALYST)
    response = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=user_msg),
    ])
    return {"sector_report": response.content}


def leader_analyst(state: AgentState, llm) -> dict:
    """龙头辨识分析师"""
    history_text = state.get('history_text', '') or '（无历史数据）'
    pool_text = state.get('stock_pool_text', '') or ''
    pool_section = f"\n{pool_text}\n" if pool_text else ""

    extra_context = _build_context_sections(state)
    extra_section = f"\n{extra_context}\n\n---\n" if extra_context else ""

    user_msg = """以下是 {date} 的涨停板数据，请辨识龙头股和梯队结构。

{history}
{pool}{extra}
## 当日涨停板

{limit_up}

{reviews}
""".format(
        date=state['date'],
        history=history_text,
        pool=pool_section,
        extra=extra_section,
        limit_up=state['limit_up_summary'],
        reviews=state.get('reviews_text', '') or '（无复盘文档）',
    )
    prompt = _get_prompt(state, "leader_analyst", LEADER_ANALYST)
    response = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=user_msg),
    ])
    return {"leader_report": response.content}
