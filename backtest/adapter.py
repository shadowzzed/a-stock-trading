"""数据适配层 — 唯一桥接 review/ 的文件

将 review/ 模块的具体实现适配为 backtest/engine/protocols 定义的接口。
如果未来数据源变更，只需修改此文件。
"""

from __future__ import annotations

import os
from typing import Optional

from .engine.protocols import DataProvider, AgentRunner, LLMCaller, MarketData


class ReviewDataProvider:
    """数据提供者 — 从 review.data.loader 加载行情数据"""

    def load_market_data(self, data_dir: str, date: str) -> MarketData:
        from review.data.loader import load_daily_data

        daily_data = load_daily_data(data_dir, date)
        limit_up = daily_data.limit_up
        limit_down = daily_data.limit_down

        result = MarketData(date=date)

        result.limit_up_count = len(limit_up) if not limit_up.empty else 0
        result.limit_down_count = len(limit_down) if not limit_down.empty else 0

        # 炸板率
        if not limit_up.empty and "炸板次数" in limit_up.columns:
            total = len(limit_up)
            blown = len(limit_up[limit_up["炸板次数"] > 0])
            result.blown_rate = blown / total * 100 if total > 0 else 0

        # 最高连板
        if not limit_up.empty and "连板数" in limit_up.columns:
            result.max_board = int(limit_up["连板数"].max())

        # 板块集中度
        if not limit_up.empty and "所属行业" in limit_up.columns:
            top1 = limit_up["所属行业"].value_counts()
            result.sector_top1_count = int(top1.iloc[0]) if len(top1) > 0 else 0
            result.sector_top1_total = result.limit_up_count

        # 前一日涨停数
        if daily_data.history:
            result.prev_limit_up_count = daily_data.history[-1].get("limit_up_count", 0)

        return result

    def load_next_day_summary(
        self, data_dir: str, date: str, report: str = ""
    ) -> tuple[str, str]:
        from review.data.loader import load_daily_data, summarize_limit_up, summarize_limit_down
        from review.verify import _load_stock_pnl

        data_d1 = load_daily_data(data_dir, date)
        summary = "## {} 实际行情\n\n".format(date)
        summary += summarize_limit_up(data_d1.limit_up) + "\n\n"
        summary += summarize_limit_down(data_d1.limit_down)

        stock_pnl = _load_stock_pnl(data_dir, date, report)
        if stock_pnl:
            summary += "\n\n" + stock_pnl

        return date, summary

    def discover_dates(
        self, data_dir: str, start: Optional[str] = None, end: Optional[str] = None
    ) -> list[str]:
        daily_root = os.path.join(data_dir, "daily")
        all_dates = sorted([
            d for d in os.listdir(daily_root)
            if os.path.isdir(os.path.join(daily_root, d))
        ])
        if start:
            all_dates = [d for d in all_dates if d >= start]
        if end:
            all_dates = [d for d in all_dates if d <= end]
        return all_dates


class ReviewAgentRunner:
    """Agent 运行器 — 调用 review.graph 执行分析"""

    def run(
        self,
        data_dir: str,
        date: str,
        config: Optional[dict] = None,
        prev_report: str = "",
    ) -> str:
        from review.graph import DEFAULT_CONFIG, _create_llm, _load_initial_state, build_graph

        cfg = {**DEFAULT_CONFIG, **(config or {})}
        init_state = _load_initial_state(
            data_dir=data_dir, date=date,
            config=config or {}, prev_report=prev_report,
        )
        run_cfg = {**(config or {}), "data_dir": data_dir, "date": date}
        graph = build_graph(run_cfg)
        final = graph.invoke(init_state)
        return final.get("final_report", "（未生成报告）")


class LangChainLLMCaller:
    """LLM 调用器 — 封装 langchain LLM"""

    def __init__(self, llm=None):
        self._llm = llm

    def _ensure_llm(self):
        if self._llm is None:
            from review.graph import DEFAULT_CONFIG, _create_llm
            self._llm = _create_llm(DEFAULT_CONFIG)
        return self._llm

    def invoke(self, system_prompt: str, user_message: str) -> str:
        from langchain_core.messages import SystemMessage, HumanMessage

        llm = self._ensure_llm()
        response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])
        return response.content
