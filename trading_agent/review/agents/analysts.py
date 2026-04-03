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
    """构建通用上下文段落（保留接口兼容，内部已清空——数据改为按需 tool 获取）"""
    sections = []
    return "\n\n".join(sections)


def sentiment_analyst(state: AgentState, llm, tools=None) -> dict:
    """情绪周期分析师"""
    user_msg = """以下是 {date} 的行情数据，请分析市场情绪周期。

## 当日数据

{limit_up}

{limit_down}

你可以使用以下工具获取额外数据（按需调用）：
- get_history_data: 获取近几日历史情绪数据对比
- get_review_docs: 获取人类复盘文档
- get_memory: 获取近期复盘记忆
- get_lessons: 获取历史经验教训
- get_prev_report: 获取昨日 AI 预测报告
- get_index_data: 获取指数行情
- get_capital_flow: 获取资金流向
- get_quant_rules: 获取量化规律

请先调用需要的工具获取数据，然后进行分析。
""".format(
        date=state['date'],
        limit_up=state['limit_up_summary'],
        limit_down=state['limit_down_summary'],
    )
    user_msg += _SENTIMENT_JSON_INSTRUCTION

    prompt = _get_prompt(state, "sentiment_analyst", SENTIMENT_ANALYST)

    if tools:
        from ..graph import _run_with_tools
        content = _run_with_tools(llm, tools, prompt, user_msg)
    else:
        response = llm.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content=user_msg),
        ])
        content = response.content

    sentiment_data, _ = _parse_structured_output(content)
    return {"sentiment_report": content, "sentiment_data": sentiment_data}


def sector_analyst(state: AgentState, llm, tools=None) -> dict:
    """板块轮动分析师"""
    pool_text = state.get('stock_pool_text', '') or ''
    pool_section = f"\n{pool_text}\n" if pool_text else ""

    user_msg = """以下是 {date} 的行情数据，请分析板块轮动情况。

{pool}
## 当日数据

{limit_up}

{limit_down}

{stock}

你可以使用以下工具获取额外数据（按需调用）：
- get_history_data: 获取近几日历史情绪数据对比
- get_review_docs: 获取人类复盘文档
- get_memory: 获取近期复盘记忆
- get_lessons: 获取历史经验教训
- get_prev_report: 获取昨日 AI 预测报告
- get_index_data: 获取指数行情
- get_capital_flow: 获取资金流向
- get_quant_rules: 获取量化规律

请先调用需要的工具获取数据，然后进行分析。
""".format(
        date=state['date'],
        pool=pool_section,
        limit_up=state['limit_up_summary'],
        limit_down=state['limit_down_summary'],
        stock=state['stock_summary'],
    )
    user_msg += _SECTOR_JSON_INSTRUCTION
    prompt = _get_prompt(state, "sector_analyst", SECTOR_ANALYST)

    if tools:
        from ..graph import _run_with_tools
        content = _run_with_tools(llm, tools, prompt, user_msg)
    else:
        response = llm.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content=user_msg),
        ])
        content = response.content

    sector_data, _ = _parse_structured_output(content)
    return {"sector_report": content, "sector_data": sector_data}


def leader_analyst(state: AgentState, llm, tools=None) -> dict:
    """龙头辨识分析师"""
    pool_text = state.get('stock_pool_text', '') or ''
    pool_section = f"\n{pool_text}\n" if pool_text else ""

    user_msg = """以下是 {date} 的涨停板数据，请辨识龙头股和梯队结构。

{pool}
## 当日涨停板

{limit_up}

你可以使用以下工具获取额外数据（按需调用）：
- get_history_data: 获取近几日历史情绪数据对比
- get_review_docs: 获取人类复盘文档
- get_memory: 获取近期复盘记忆
- get_lessons: 获取历史经验教训
- get_prev_report: 获取昨日 AI 预测报告
- get_stock_detail: 查询个股详细行情
- get_quant_rules: 获取量化规律

请先调用需要的工具获取数据，然后进行分析。
""".format(
        date=state['date'],
        pool=pool_section,
        limit_up=state['limit_up_summary'],
    )
    user_msg += _LEADER_JSON_INSTRUCTION
    prompt = _get_prompt(state, "leader_analyst", LEADER_ANALYST)

    if tools:
        from ..graph import _run_with_tools
        content = _run_with_tools(llm, tools, prompt, user_msg)
    else:
        response = llm.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content=user_msg),
        ])
        content = response.content

    leader_data, _ = _parse_structured_output(content)
    return {"leader_report": content, "leader_data": leader_data}
