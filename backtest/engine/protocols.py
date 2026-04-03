"""回测引擎的接口协议 — 定义数据访问和 Agent 调用的抽象

回测引擎通过这些接口与外部系统交互，不直接 import review/ 的任何模块。
适配器（adapter.py）负责将这些接口桥接到具体实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Optional


@dataclass
class MarketData:
    """回测所需的标准化市场数据"""
    date: str
    limit_up_count: int = 0
    limit_down_count: int = 0
    blown_rate: float = 0.0
    max_board: int = 0
    sector_top1_count: int = 0
    sector_top1_total: int = 0
    prev_limit_up_count: Optional[int] = None
    sentiment_phase: str = ""
    volume_change_pct: Optional[float] = None
    # Day D+1 数据（验证用）
    next_date: str = ""
    next_summary: str = ""
    stock_pnl: str = ""


class DataProvider(Protocol):
    """数据访问接口 — 加载市场数据"""

    def load_market_data(self, data_dir: str, date: str) -> MarketData:
        """加载指定日期的市场数据"""
        ...

    def load_next_day_summary(
        self, data_dir: str, date: str, report: str
    ) -> tuple[str, str]:
        """加载 Day D+1 的实际行情摘要

        Returns:
            (next_date, summary_text)
        """
        ...

    def discover_dates(
        self, data_dir: str, start: Optional[str] = None, end: Optional[str] = None
    ) -> list[str]:
        """发现可用交易日列表"""
        ...


class AgentRunner(Protocol):
    """Agent 运行接口 — 跑分析生成报告"""

    def run(
        self,
        data_dir: str,
        date: str,
        config: Optional[dict] = None,
        prev_report: str = "",
    ) -> str:
        """运行 Agent 分析，返回报告文本"""
        ...


class LLMCaller(Protocol):
    """LLM 调用接口 — 用于验证和经验提取"""

    def invoke(self, system_prompt: str, user_message: str) -> str:
        """调用 LLM，返回原始响应文本"""
        ...
