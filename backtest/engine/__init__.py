"""回测引擎 — 接口驱动，依赖注入

引擎本身不依赖任何具体的数据源或 Agent 实现，
通过 Protocol 定义接口，由外部 adapter 注入具体实现。
"""

from .core import BacktestEngine, BacktestResult
from .protocols import DataProvider, AgentRunner, LLMCaller
from .report import generate_summary, format_report

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "DataProvider",
    "AgentRunner",
    "LLMCaller",
    "generate_summary",
    "format_report",
]
