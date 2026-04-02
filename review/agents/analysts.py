"""阶段1：三位分析师 Agent"""

import json
import re

from langchain_core.messages import SystemMessage, HumanMessage

from ..state import AgentState
from ..prompts import SENTIMENT_ANALYST, SECTOR_ANALYST, LEADER_ANALYST


def _parse_structured_output(text: str) -> tuple:
    """从 LLM 输出中提取 JSON 块和文本报告。

    如果输出包含 ```json ... ``` 代码块，提取 JSON。
    否则尝试找最外层 { } 对象。

    Returns: (parsed_dict, full_text)
    """
    # Try to find JSON code block first
    json_match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            return data, text
        except json.JSONDecodeError:
            pass

    # Try to find standalone JSON object (greedy match for nested braces)
    brace_count = 0
    start = None
    for i, ch in enumerate(text):
        if ch == '{':
            if brace_count == 0:
                start = i
            brace_count += 1
        elif ch == '}':
            brace_count -= 1
            if brace_count == 0 and start is not None:
                try:
                    data = json.loads(text[start:i+1])
                    return data, text
                except json.JSONDecodeError:
                    start = None

    return {}, text


# 附加到各分析师 user message 末尾的 JSON 输出指令
_SENTIMENT_JSON_INSTRUCTION = """

重要：在你的分析报告的最开头，必须先输出一个 JSON 代码块，包含结构化结论，格式如下：
```json
{
  "summary": "一句话概括",
  "market_phase": "冰点/修复/升温/高潮/分歧/退潮",
  "confidence": 0.72,
  "risks": ["风险1"],
  "evidence": ["支撑证据1"],
  "turning_points": ["拐点信号1"],
  "sentiment_signals": {"limit_ups": 0, "limit_downs": 0, "bomb_rate": "0%"},
  "actionable_points": ["可操作要点1"]
}
```
然后再输出你的完整分析报告（markdown 格式）。"""

_SECTOR_JSON_INSTRUCTION = """

重要：在你的分析报告的最开头，必须先输出一个 JSON 代码块，包含结构化结论，格式如下：
```json
{
  "summary": "一句话概括",
  "main_sectors": [{"name": "板块名", "evidence": "判断依据"}],
  "secondary_sectors": [{"name": "板块名", "evidence": "判断依据"}],
  "fading_sectors": ["退潮板块1"],
  "sector_rotation_state": "集中/扩散/切换中",
  "confidence": 0.7,
  "risks": ["风险1"],
  "actionable_points": ["可操作要点1"]
}
```
然后再输出你的完整分析报告（markdown 格式）。"""

_LEADER_JSON_INSTRUCTION = """

重要：在你的分析报告的最开头，必须先输出一个 JSON 代码块，包含结构化结论，格式如下：
```json
{
  "summary": "一句话概括",
  "total_leader": {"name": "股票名", "board_height": 0, "lifecycle": "阶段", "is_breakthrough": false},
  "sector_leaders": [{"sector": "板块", "leader": "股票名", "mid": "中军", "elastic": "弹性"}],
  "tier_structure": "梯队描述",
  "supplement_targets": ["补涨标的1"],
  "confidence": 0.7,
  "risks": ["风险1"],
  "actionable_points": ["可操作要点1"]
}
```
然后再输出你的完整分析报告（markdown 格式）。"""


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
    user_msg += _SENTIMENT_JSON_INSTRUCTION
    prompt = _get_prompt(state, "sentiment_analyst", SENTIMENT_ANALYST)
    response = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=user_msg),
    ])
    sentiment_data, _ = _parse_structured_output(response.content)
    return {"sentiment_report": response.content, "sentiment_data": sentiment_data}


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
    user_msg += _SECTOR_JSON_INSTRUCTION
    prompt = _get_prompt(state, "sector_analyst", SECTOR_ANALYST)
    response = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=user_msg),
    ])
    sector_data, _ = _parse_structured_output(response.content)
    return {"sector_report": response.content, "sector_data": sector_data}


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
    user_msg += _LEADER_JSON_INSTRUCTION
    prompt = _get_prompt(state, "leader_analyst", LEADER_ANALYST)
    response = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=user_msg),
    ])
    leader_data, _ = _parse_structured_output(response.content)
    return {"leader_report": response.content, "leader_data": leader_data}
