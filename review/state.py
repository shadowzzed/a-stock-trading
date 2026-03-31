"""Agent 状态定义"""

from typing import TypedDict, Annotated
from operator import add


class DebateState(TypedDict):
    """多空辩论子状态"""
    bull_history: list[str]
    bear_history: list[str]
    history: Annotated[list[str], add]
    current_response: str
    judge_decision: str
    count: int


class AgentState(TypedDict):
    """主状态"""
    # 输入
    date: str
    limit_up_summary: str       # 涨停板摘要
    limit_down_summary: str     # 跌停板摘要
    stock_summary: str          # 个股行情摘要
    reviews_text: str           # 复盘文档合并文本
    events_text: str            # 事件催化文本
    history_text: str           # 近几日历史情绪数据
    stock_pool_text: str        # 股票池辨识度核心标的
    prev_report: str            # 前一交易日的 Agent 报告（用于自我校准）
    lessons_text: str           # 历史经验教训（持久化累积）
    index_text: str             # 指数行情数据
    capital_flow_text: str      # 资金流数据（板块资金流+北向资金）
    memory_text: str            # 跨周期记忆（近期复盘总结）
    quant_rules_text: str       # 量化规律参考

    # 阶段1：分析师报告
    sentiment_report: str       # 情绪周期分析
    sector_report: str          # 板块轮动分析
    leader_report: str          # 龙头辨识分析

    # 阶段2：多空辩论
    debate_state: DebateState

    # 阶段3：最终输出
    final_report: str           # 最终复盘报告（AI 初步版）

    # 阶段4：人类终审
    human_feedback: str         # 人类终审反馈
    reviewed_report: str        # 终审后的最终报告

    # Agent 自定义 prompt 覆盖
    prompt_overrides: dict      # {agent_name: extra_prompt}
