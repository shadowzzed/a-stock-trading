"""Review Agent 数据检索工具

提供 10 个检索工具，供 LLM 按需查询历史复盘、行情、个股等数据。
每个工具通过 RetrievalToolFactory 创建，绑定到特定的 data_dir 和 date。
"""

from __future__ import annotations

import glob
import json
import logging
import os
from typing import Optional

from langchain_core.tools import tool

from config import get_config

logger = logging.getLogger(__name__)


def _str_result(result) -> str:
    """将 DataResult 或 str 统一转为 str（供 LLM 工具返回值）。"""
    from trading_agent.review.data.loader import DataResult
    if isinstance(result, DataResult):
        return str(result)
    return result if result else "无数据"


class RetrievalToolFactory:
    """创建绑定到特定 data_dir 和 date 的检索工具。

    Args:
        data_dir: trading 数据根目录
        date: 分析日期 (YYYY-MM-DD)
        memory_dir: 跨周期记忆目录 (默认从 config 获取)
        backtest_max_date: 回测模式下的日期上界，防止未来数据泄露
    """

    def __init__(self, data_dir: str, date: str, memory_dir: str = "",
                 backtest_max_date: Optional[str] = None):
        self.data_dir = data_dir
        self.date = date
        self.memory_dir = memory_dir or get_config()["memory_dir"]
        self.backtest_max_date = backtest_max_date
        self._cache: dict = {}
        self._audit_log: list[dict] = []

    def _check_date_boundary(self, target_date: str, tool_name: str) -> Optional[str]:
        """检查日期是否超出回测边界。超出则返回错误信息，否则返回 None。"""
        max_date = self.backtest_max_date or self.date
        if target_date > max_date:
            self._record_audit(tool_name, target_date, blocked=True)
            return f"日期 {target_date} 超出可查询范围（上限: {max_date}），{tool_name} 只能查询 {max_date} 及之前的数据"
        self._record_audit(tool_name, target_date, blocked=False)
        return None

    def _record_audit(self, tool_name: str, requested_date: str, blocked: bool):
        """记录工具调用审计条目。"""
        max_date = self.backtest_max_date or self.date
        entry = {
            "tool": tool_name,
            "requested_date": requested_date,
            "max_date": max_date,
            "blocked": blocked,
        }
        self._audit_log.append(entry)
        if blocked:
            logger.warning(
                "[BACKTEST AUDIT] 拦截越界请求: %s(date=%s), 上限=%s",
                tool_name, requested_date, max_date,
            )

    def get_audit_log(self) -> list[dict]:
        """返回本次运行的审计日志副本。"""
        return list(self._audit_log)

    def get_audit_summary(self) -> dict:
        """返回审计摘要：总调用数、拦截数、越界工具列表。"""
        total = len(self._audit_log)
        blocked = [e for e in self._audit_log if e["blocked"]]
        return {
            "total_date_checks": total,
            "blocked_count": len(blocked),
            "blocked_details": blocked,
            "clean": len(blocked) == 0,
        }

    def _cached(self, key: tuple, loader):
        """简单的字典缓存，避免同一次运行重复读文件"""
        if key not in self._cache:
            self._cache[key] = loader()
        return self._cache[key]

    def create_tools(self) -> list:
        """创建所有检索工具。"""
        return [
            self._make_get_market_data(),
            self._make_get_history_data(),
            self._make_get_review_docs(),
            self._make_get_memory(),
            self._make_get_lessons(),
            self._make_get_prev_report(),
            self._make_get_index_data(),
            self._make_get_capital_flow(),
            self._make_get_quant_rules(),
            self._make_get_stock_detail(),
            self._make_get_past_report(),
            self._make_scan_trend_stocks(),
        ]

    # ------------------------------------------------------------------
    # Tool 1: get_history_data
    # ------------------------------------------------------------------
    def _make_get_history_data(self):
        factory = self

        @tool
        def get_history_data(
            days_back: int = 7,
            metrics: Optional[list[str]] = None,
        ) -> str:
            """获取近几日的情绪数据对比（涨停数、跌停数、连板梯队、板块分布、龙头追踪）。

            Args:
                days_back: 回溯天数，默认 7，最大 14
                metrics: 可选指标过滤，如 ["limit_up_count", "max_board", "blown_rate"]
            """
            days_back = min(days_back, 14)
            cache_key = ("history_data", days_back, tuple(metrics or []))

            def _load():
                from trading_agent.review.data.loader import _load_history, summarize_history

                history = _load_history(factory.data_dir, factory.date, days_back,
                                    backtest_mode=bool(factory.backtest_max_date))

                if metrics:
                    filtered = []
                    for h in history:
                        item = {"date": h["date"]}
                        for m in metrics:
                            if m in h:
                                item[m] = h[m]
                        filtered.append(item)
                    if not filtered:
                        return "无数据"
                    lines = []
                    for item in filtered:
                        parts = [f"{k}={v}" for k, v in item.items()]
                        lines.append("- " + "，".join(parts))
                    return "## 近期情绪数据（筛选）\n" + "\n".join(lines)

                return summarize_history(history)

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_history_data

    # ------------------------------------------------------------------
    # Tool 2: get_review_docs
    # ------------------------------------------------------------------
    def _make_get_review_docs(self):
        factory = self

        @tool
        def get_review_docs(
            date: Optional[str] = None,
            reviewer: Optional[str] = None,
        ) -> str:
            """获取复盘文档（博主复盘、分析笔记等 markdown 文件）。

            Args:
                date: 日期，默认为分析日
                reviewer: 按文件名筛选（子串匹配），如 "北京炒家"
            """
            target_date = date or factory.date
            boundary_err = factory._check_date_boundary(target_date, "get_review_docs")
            if boundary_err:
                return boundary_err
            cache_key = ("review_docs", target_date, reviewer)

            def _load():
                daily_dir = os.path.join(factory.data_dir, "daily", target_date)
                if not os.path.isdir(daily_dir):
                    return f"无数据（目录不存在: {target_date}）"

                # 优先从 review_docs/ 子目录加载
                parts = []
                review_dir = os.path.join(daily_dir, "review_docs")
                search_paths = []

                if os.path.isdir(review_dir):
                    search_paths = sorted(
                        glob.glob(os.path.join(review_dir, "*.md"))
                    )
                else:
                    # 兼容旧目录结构
                    search_paths = sorted(
                        glob.glob(os.path.join(daily_dir, "*复盘*.md"))
                    )

                for md_path in search_paths:
                    name = os.path.basename(md_path)
                    if reviewer and reviewer not in name:
                        continue
                    try:
                        with open(md_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        parts.append(f"### {name}\n{content}")
                    except IOError:
                        continue

                if not parts:
                    return "无数据（未找到复盘文档）"
                return "\n\n---\n\n".join(parts)

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_review_docs

    # ------------------------------------------------------------------
    # Tool 3: get_memory
    # ------------------------------------------------------------------
    def _make_get_memory(self):
        factory = self

        @tool
        def get_memory(
            days_back: int = 5,
            date: Optional[str] = None,
        ) -> str:
            """获取跨周期记忆（近期每日复盘总结），严格只读取分析日之前的数据。

            Args:
                days_back: 回溯天数，默认 5，最大 10
                date: 指定读取某一天的记忆（优先级高于 days_back）
            """
            days_back = min(days_back, 10)

            if date:
                boundary_err = factory._check_date_boundary(date, "get_memory")
                if boundary_err:
                    return boundary_err
                cache_key = ("memory", date)
            else:
                cache_key = ("memory_days", days_back)

            def _load():
                from trading_agent.review.data.loader import load_memory

                if date:
                    # 读取指定日期的单条记忆
                    mem_file = os.path.join(factory.memory_dir, f"{date}.md")
                    if not os.path.exists(mem_file):
                        return f"无数据（记忆文件不存在: {date}）"
                    try:
                        with open(mem_file, "r", encoding="utf-8") as f:
                            return f.read()
                    except IOError:
                        return "无数据（读取失败）"

                return load_memory(factory.memory_dir, factory.date, max_days=days_back)

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_memory

    # ------------------------------------------------------------------
    # Tool 4: get_lessons
    # ------------------------------------------------------------------
    def _make_get_lessons(self):
        factory = self

        @tool
        def get_lessons(category: Optional[str] = None) -> str:
            """获取历史经验教训（从过往预测验证中积累）。

            Args:
                category: 按类别过滤，如 "情绪判断"、"板块分析"
            """
            cache_key = ("lessons", category)

            def _load():
                from trading_agent.review.data.loader import load_lessons

                text = load_lessons(factory.data_dir)
                if not text:
                    return "无数据"

                if category:
                    lines = text.split("\n")
                    filtered = [
                        line for line in lines
                        if category in line or line.startswith("#") or line.startswith("以下")
                    ]
                    if not any(category in l for l in filtered):
                        return f"无数据（未找到类别: {category}）"
                    return "\n".join(filtered)

                return text

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_lessons

    # ------------------------------------------------------------------
    # Tool 5: get_prev_report
    # ------------------------------------------------------------------
    def _make_get_prev_report(self):
        factory = self

        @tool
        def get_prev_report() -> str:
            """获取前一交易日的 Agent 裁决报告（用于自我校准和连贯性检查）。"""
            cache_key = ("prev_report",)

            def _load():
                daily_root = os.path.join(factory.data_dir, "daily")
                if not os.path.isdir(daily_root):
                    return "无数据"

                # 列出所有日期目录，找到分析日之前最近的一个
                all_dates = sorted([
                    d for d in os.listdir(daily_root)
                    if os.path.isdir(os.path.join(daily_root, d)) and d < factory.date
                ])

                if not all_dates:
                    return "无数据（无更早的交易日）"

                prev_date = all_dates[-1]
                report_path = os.path.join(
                    daily_root, prev_date, "agent_05_裁决报告.md"
                )

                if not os.path.exists(report_path):
                    return f"无数据（{prev_date} 无裁决报告）"

                try:
                    with open(report_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    return f"## 前一交易日报告（{prev_date}）\n\n{content}"
                except IOError:
                    return "无数据（读取失败）"

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_prev_report

    # ------------------------------------------------------------------
    # Tool 6: get_index_data
    # ------------------------------------------------------------------
    def _make_get_index_data(self):
        factory = self

        @tool
        def get_index_data(date: Optional[str] = None) -> str:
            """获取指数行情数据（上证、深证、创业板等的收盘价、涨跌幅、成交额）。

            Args:
                date: 日期，默认为分析日
            """
            target_date = date or factory.date
            boundary_err = factory._check_date_boundary(target_date, "get_index_data")
            if boundary_err:
                return boundary_err
            cache_key = ("index_data", target_date)

            def _load():
                from trading_agent.review.data.loader import load_index_data

                return load_index_data(factory.data_dir, target_date)

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_index_data

    # ------------------------------------------------------------------
    # Tool 7: get_capital_flow
    # ------------------------------------------------------------------
    def _make_get_capital_flow(self):
        factory = self

        @tool
        def get_capital_flow(date: Optional[str] = None) -> str:
            """获取资金流数据（板块资金流向、北向资金）。

            Args:
                date: 日期，默认为分析日
            """
            target_date = date or factory.date
            boundary_err = factory._check_date_boundary(target_date, "get_capital_flow")
            if boundary_err:
                return boundary_err
            cache_key = ("capital_flow", target_date)

            def _load():
                from trading_agent.review.data.loader import load_capital_flow

                return load_capital_flow(factory.data_dir, target_date)

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_capital_flow

    # ------------------------------------------------------------------
    # Tool 8: get_quant_rules
    # ------------------------------------------------------------------
    def _make_get_quant_rules(self):
        factory = self

        @tool
        def get_quant_rules(category: Optional[str] = None) -> str:
            """获取量化规律参考（从历史数据中总结的规律和操作指引）。

            Args:
                category: 按类别过滤，如 "涨停板"、"连板"、"情绪周期"
            """
            cache_key = ("quant_rules", category)

            def _load():
                from trading_agent.review.data.loader import load_quantitative_rules

                text = load_quantitative_rules(factory.data_dir)
                if not text:
                    return "无数据"

                if category:
                    lines = text.split("\n")
                    filtered = []
                    for line in lines:
                        if category in line or line.startswith("#") or line.startswith("以下"):
                            filtered.append(line)
                    if not any(category in l for l in filtered):
                        return f"无数据（未找到类别: {category}）"
                    return "\n".join(filtered)

                return text

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_quant_rules

    # ------------------------------------------------------------------
    # Tool 9: get_stock_detail (delegates to loader)
    # ------------------------------------------------------------------
    def _make_get_stock_detail(self):
        factory = self

        @tool
        def get_stock_detail(
            name: Optional[str] = None,
            code: Optional[str] = None,
            date: Optional[str] = None,
        ) -> str:
            """从 intraday 数据库查询个股详细行情（分时快照）。

            至少提供 name 或 code 之一。

            Args:
                name: 股票名称（模糊匹配），如 "贵州茅台"
                code: 股票代码（精确匹配），如 "600519"
                date: 日期，默认为分析日
            """
            if not name and not code:
                return "请提供 name 或 code 参数"

            target_date = date or factory.date
            boundary_err = factory._check_date_boundary(target_date, "get_stock_detail")
            if boundary_err:
                return boundary_err
            cache_key = ("stock_detail", name, code, target_date)

            def _load():
                from trading_agent.review.data.loader import load_stock_detail
                return load_stock_detail(factory.data_dir, name=name, code=code, date=target_date,
                                          max_date=factory.backtest_max_date)

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_stock_detail

    # ------------------------------------------------------------------
    # Tool 10: get_past_report (NEW - any past date)
    # ------------------------------------------------------------------
    def _make_get_past_report(self):
        factory = self

        @tool
        def get_past_report(date: str) -> str:
            """获取任意历史日期的 Agent 裁决报告。

            Args:
                date: 历史日期（YYYY-MM-DD），必须在分析日之前
            """
            if date >= factory.date:
                return "只能查询分析日之前的历史报告"
            boundary_err = factory._check_date_boundary(date, "get_past_report")
            if boundary_err:
                return boundary_err

            cache_key = ("past_report", date)

            def _load():
                report_path = os.path.join(
                    factory.data_dir, "daily", date, "agent_05_裁决报告.md"
                )

                if not os.path.exists(report_path):
                    return f"无数据（{date} 无裁决报告）"

                try:
                    with open(report_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    return f"## {date} 裁决报告\n\n{content}"
                except IOError:
                    return "无数据（读取失败）"

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_past_report

    # ------------------------------------------------------------------
    # Tool 11: get_market_data (delegates to loader)
    # ------------------------------------------------------------------
    def _make_get_market_data(self):
        factory = self

        @tool
        def get_market_data(
            date: Optional[str] = None,
            time: Optional[str] = None,
            name: Optional[str] = None,
            code: Optional[str] = None,
            mode: Optional[str] = "overview",
            sort_by: Optional[str] = "pctChg",
            top_n: Optional[int] = None,
        ) -> str:
            """获取行情快照数据（支持按日期+时间查询）。可查市场概览、股票池行情、个股详情。
            数据源优先从本地 SQLite，不存在时自动从通达信接口实时拉取。

            Args:
                date: 日期 YYYY-MM-DD，默认今天
                time: 时间点，如 "09:25"、"10:00"、"11:30"、"close"。默认返回该日最新可用快照
                name: 股票名称（模糊匹配，可选）
                code: 股票代码（精确匹配，可选）
                mode: "overview"=市场概览（涨幅TOP+跌幅TOP+涨停统计），"stock"=个股详情，"pool"=股票池行情。默认 overview
                sort_by: "pctChg"、"amount"、"volume"，默认 pctChg
                top_n: 返回数量（个股模式默认5，概览模式默认10）
            """
            if date:
                boundary_err = factory._check_date_boundary(date, "get_market_data")
                if boundary_err:
                    return boundary_err
            cache_key = ("market_data", date, time, name, code, mode, sort_by, top_n)

            def _load():
                from trading_agent.review.data.loader import load_market_snapshot
                return load_market_snapshot(
                    factory.data_dir,
                    date=date, time=time, name=name, code=code,
                    mode=mode, sort_by=sort_by, top_n=top_n,
                    max_date=factory.backtest_max_date,
                )

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_market_data

    # ------------------------------------------------------------------
    # Tool 12: scan_trend_stocks (delegates to loader)
    # ------------------------------------------------------------------
    def _make_scan_trend_stocks(self):
        factory = self

        @tool
        def scan_trend_stocks(
            min_pct: Optional[float] = 3.0,
            max_pct: Optional[float] = None,
            sector: Optional[str] = None,
            ma_type: Optional[str] = "both",
            top_n: Optional[int] = 30,
            hot_only: Optional[bool] = False,
        ) -> str:
            """全市场趋势股扫描 — 寻找沿5日线或10日线上方运行的趋势股。

            遍历全市场所有股票的历史日线，计算5日/10日均线，筛选出
            始终在均线上方运行且涨幅符合条件的趋势股。热门板块重点关注。

            Args:
                min_pct: 最低涨幅百分比，默认 3%
                max_pct: 最高涨幅百分比（可选，过滤短期爆炒）
                sector: 按板块名称筛选（模糊匹配，可选）
                ma_type: "5"=仅5日线, "10"=仅10日线, "both"=两者都看（默认）
                top_n: 返回数量，默认 30
                hot_only: 是否只看热门板块（涨幅>1%的板块），默认 False
            """
            cache_key = ("scan_trend", min_pct, max_pct, sector, ma_type, top_n, hot_only)

            def _load():
                from trading_agent.review.data.loader import scan_trend_stocks as loader_scan
                return loader_scan(
                    factory.data_dir,
                    date=factory.date,
                    min_pct=min_pct, max_pct=max_pct,
                    sector=sector, ma_type=ma_type,
                    top_n=top_n, hot_only=hot_only,
                    max_date=factory.backtest_max_date,
                )

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return scan_trend_stocks
