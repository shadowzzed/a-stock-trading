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
    events_text: str            # 事件催化文本
    stock_pool_text: str        # 股票池辨识度核心标的

    # 阶段1：分析师报告
    sentiment_report: str       # 情绪周期分析
    sector_report: str          # 板块轮动分析
    leader_report: str          # 龙头辨识分析

    # 阶段1：结构化分析数据（新增）
    sentiment_data: dict        # 情绪分析结构化结论
    sector_data: dict           # 板块分析结构化结论
    leader_data: dict           # 龙头分析结构化结论

    # 阶段2：多空辩论
    debate_state: DebateState

    # 阶段2：辩论结构化数据（新增）
    bull_claims: list           # 看多派结构化论点列表
    bear_claims: list           # 看空派结构化论点列表

    # 阶段3：最终输出
    final_report: str           # 最终复盘报告（AI 初步版）

    # 阶段3：最终决策（新增）
    final_decision: dict        # 结构化决策对象

    # 阶段4：人类终审
    human_feedback: str         # 人类终审反馈
    reviewed_report: str        # 终审后的最终报告

    # Agent 自定义 prompt 覆盖
    prompt_overrides: dict      # {agent_name: extra_prompt}

    # 数据质量
    data_quality_warnings: list  # 数据缺失/质量警告汇总
