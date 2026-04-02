"""盘中分析 Agent 状态定义"""

from typing import TypedDict, Optional


class IntradayState(TypedDict):
    # 输入
    agent_name: str              # "opening_analysis" | "early_session_analysis"
    date: str                    # YYYY-MM-DD
    dry_run: bool                # 仅输出数据，不调 AI

    # Stage 1: 数据
    context_text: str            # 组装好的数据上下文（user prompt 部分）
    data_raw: dict               # 原始查询结果

    # Stage 2: AI
    report: str                  # AI 生成的分析报告
    ai_provider: str             # 使用的 AI 提供商

    # Stage 3: 输出
    output_path: str             # 报告保存路径
    feishu_sent: bool            # 飞书是否推送成功

    # 错误
    error: str
