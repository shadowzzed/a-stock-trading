"""Review Agent 数据检索工具

提供检索工具，供 LLM 按需查询历史复盘、行情、个股、新闻等数据。
每个工具通过 RetrievalToolFactory 创建，绑定到特定的 data_dir 和 date。
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sqlite3
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
        target_date = self._norm_date(target_date)
        if not target_date:
            return None
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

    def refresh_date(self, new_date: str):
        """更新分析日期并清空缓存（用于跨天运行时刷新日期边界）。"""
        if self.date == new_date:
            return
        logger.info("工具日期刷新: %s → %s", self.date, new_date)
        self.date = new_date
        self._cache.clear()

    @staticmethod
    def _norm_date(date):
        """将 None-like 的日期值（None、空串、"null"、"none"）统一为 None。"""
        if date is None or not isinstance(date, str):
            return None
        if date.lower() in ("null", "none", ""):
            return None
        return date

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
            self._make_get_news(),
            self._make_search_similar_news(),
            self._make_get_news_impact(),
            self._make_get_sentiment_index(),
        ]

    # ------------------------------------------------------------------
    # Tool 1: get_history_data
    # ------------------------------------------------------------------
    def _make_get_history_data(self):
        factory = self

        @tool
        def get_history_data(
            days_back: Optional[int] = 7,
            metrics: Optional[list[str]] = None,
        ) -> str:
            """获取近几日的情绪数据对比（涨停数、跌停数、连板梯队、板块分布、龙头追踪）。

            Args:
                days_back: 回溯天数，默认 7，最大 14
                metrics: 可选指标过滤，如 ["limit_up_count", "max_board", "blown_rate"]
            """
            # LLM 偶尔传入 None，做防御式处理
            if days_back is None:
                days_back = 7
            days_back = min(int(days_back), 14)
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
            target_date = factory._norm_date(date) or factory.date
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
            days_back: Optional[int] = 5,
            date: Optional[str] = None,
        ) -> str:
            """获取跨周期记忆（近期每日复盘总结），严格只读取分析日之前的数据。

            Args:
                days_back: 回溯天数，默认 5，最大 10
                date: 指定读取某一天的记忆（优先级高于 days_back）
            """
            if days_back is None:
                days_back = 5
            days_back = min(int(days_back), 10)
            date = factory._norm_date(date)

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
            target_date = factory._norm_date(date) or factory.date
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
            target_date = factory._norm_date(date) or factory.date
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

            target_date = factory._norm_date(date) or factory.date
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
        def get_past_report(date: Optional[str] = None) -> str:
            """获取任意历史日期的 Agent 裁决报告。

            Args:
                date: 历史日期（YYYY-MM-DD），必须在分析日之前
            """
            date = factory._norm_date(date) or ""
            if not date:
                return "请提供有效日期（YYYY-MM-DD）"
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
            date = factory._norm_date(date)
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

    # ------------------------------------------------------------------
    # Tool 13: get_news (新闻查询)
    # ------------------------------------------------------------------
    def _make_get_news(self):
        factory = self

        @tool
        def get_news(
            date: Optional[str] = None,
            stock: Optional[str] = None,
            plate: Optional[str] = None,
            source: Optional[str] = None,
            sentiment: Optional[str] = None,
            event_type: Optional[str] = None,
            limit: Optional[int] = 30,
        ) -> str:
            """查询新闻数据（标题、AI解读、关联个股/板块、利好/利空、事件类型）。

            Args:
                date: 日期 YYYY-MM-DD，默认为分析日
                stock: 按个股过滤（代码或名称，模糊匹配）
                plate: 按板块过滤（模糊匹配），如 "光伏"、"半导体"
                source: 按来源过滤，如 "财联社"、"金十数据"
                sentiment: 按情绪过滤，"利好" 或 "利空"
                event_type: 按事件类型过滤，如 "产能变动"、"政策利好"、"财报超预期"、"地缘冲突"、"研报首覆"
                limit: 返回条数，默认 30
            """
            target_date = factory._norm_date(date) or factory.date
            if limit is None:
                limit = 30
            boundary_err = factory._check_date_boundary(target_date, "get_news")
            if boundary_err:
                return boundary_err
            cache_key = ("news", target_date, stock, plate, source, sentiment, event_type, limit)

            def _load():
                cfg = get_config()
                db_path = cfg["news_db"]
                if not os.path.exists(db_path):
                    return "无数据（新闻数据库不存在）"

                conn = sqlite3.connect(db_path, timeout=10)
                try:
                    query = """
                        SELECT title, source, news_time, stocks, plates, interpretation, created_date, event_type
                        FROM news
                        WHERE created_date = ?
                    """
                    params: list = [target_date]

                    if source:
                        query += " AND source = ?"
                        params.append(source)
                    if event_type:
                        query += " AND event_type LIKE ?"
                        params.append(f"%{event_type}%")

                    query += " ORDER BY sent_at DESC LIMIT ?"
                    params.append(limit)

                    rows = conn.execute(query, params).fetchall()

                    if not rows:
                        return f"无数据（{target_date} 无新闻）"

                    lines = []
                    for title, src, news_time, stocks_json, plates_json, interp, cdate, evt_type in rows:
                        stocks_list = json.loads(stocks_json) if stocks_json else []
                        plates_list = json.loads(plates_json) if plates_json else []

                        # 过滤：个股
                        if stock and not any(stock in s for s in stocks_list):
                            if stock not in title and stock not in (interp or ""):
                                continue
                        # 过滤：板块
                        if plate and not any(plate in p for p in plates_list):
                            if plate not in (interp or ""):
                                continue
                        # 过滤：情绪
                        if sentiment and sentiment not in (interp or ""):
                            continue

                        line = f"### {title}\n`{src} {news_time}`"
                        if evt_type:
                            line += f"  `{evt_type}`"
                        line += "\n"
                        if interp:
                            line += f"{interp}\n"
                        if plates_list:
                            line += f"板块：{'、'.join(plates_list)}\n"
                        if stocks_list:
                            line += f"个股：{'、'.join(stocks_list)}\n"
                        lines.append(line)

                    if not lines:
                        return f"无数据（{target_date} 无匹配的新闻）"

                    header = f"## 新闻（{target_date}）共 {len(lines)} 条\n\n"
                    return header + "\n".join(lines)
                finally:
                    conn.close()

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_news

    # ------------------------------------------------------------------
    # Tool 14: search_similar_news (新闻相似检索 + 历史影响)
    # ------------------------------------------------------------------
    def _make_search_similar_news(self):
        factory = self

        @tool
        def search_similar_news(
            query: str,
            top_k: int = 8,
        ) -> str:
            """向量检索历史相似新闻，并返回这些新闻发布后的股价影响统计。

            输入一段新闻描述或事件关键词，从历史新闻库中找到最相似的事件，
            统计它们对关联个股的价格影响（5分钟到5天的涨跌幅）。

            Args:
                query: 新闻描述或事件关键词，如 "光伏减产"、"央行降准"
                top_k: 返回相似新闻数量，默认 8
            """
            cache_key = ("similar_news", query, top_k)

            def _load():
                try:
                    from news_monitor.impact.search import analyze_news_impact
                    report = analyze_news_impact(query, "", top_k=top_k, timeout_sec=15.0)
                    return report if report else "无数据（未找到相似新闻或影响分析模块不可用）"
                except ImportError:
                    return "无数据（新闻影响分析模块未安装）"
                except Exception as e:
                    logger.warning("search_similar_news 失败: %s", e)
                    return f"查询失败: {e}"

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return search_similar_news

    # ------------------------------------------------------------------
    # Tool 15: get_news_impact (新闻影响统计)
    # ------------------------------------------------------------------
    def _make_get_news_impact(self):
        factory = self

        @tool
        def get_news_impact(
            stock_code: Optional[str] = None,
            date: Optional[str] = None,
            days_back: Optional[int] = 30,
        ) -> str:
            """查询新闻对个股的历史价格影响统计。

            返回指定个股在新闻发布后各时间窗口（5min/15min/30min/1h/当日/次日~5日）
            的平均涨跌幅、最大涨跌幅、样本数等统计。

            Args:
                stock_code: 股票代码，如 "600519"。不填则返回全市场统计
                date: 限定新闻日期，默认不限
                days_back: 回溯天数，默认 30
            """
            if days_back is None:
                days_back = 30
            days_back = int(days_back)
            target_date = factory._norm_date(date) or factory.date
            cache_key = ("news_impact", stock_code, target_date, days_back)

            def _load():
                cfg = get_config()
                db_path = cfg["news_db"]
                if not os.path.exists(db_path):
                    return "无数据（新闻数据库不存在）"

                conn = sqlite3.connect(db_path, timeout=10)
                try:
                    # 检查 news_impacts 表是否存在
                    tables = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='news_impacts'"
                    ).fetchall()
                    if not tables:
                        return "无数据（影响分析表不存在，需先运行 bootstrap）"

                    query = """
                        SELECT
                            COUNT(*) as cnt,
                            AVG(pct_5min) as avg_5min,
                            AVG(pct_15min) as avg_15min,
                            AVG(pct_30min) as avg_30min,
                            AVG(pct_1h) as avg_1h,
                            AVG(pct_eod) as avg_eod,
                            AVG(pct_next1d) as avg_next1d,
                            AVG(pct_next3d) as avg_next3d,
                            AVG(pct_next5d) as avg_next5d,
                            AVG(max_gain_pct) as avg_max_gain,
                            AVG(max_loss_pct) as avg_max_loss,
                            AVG(vol_ratio_1h) as avg_vol_ratio
                        FROM news_impacts
                        WHERE news_date <= ?
                          AND news_date >= date(?, '-' || ? || ' days')
                    """
                    params: list = [target_date, target_date, days_back]

                    if stock_code:
                        query += " AND stock_code = ?"
                        params.append(stock_code)

                    row = conn.execute(query, params).fetchone()

                    if not row or row[0] == 0:
                        return "无数据（无匹配的影响记录）"

                    cnt = row[0]
                    labels = ["5min", "15min", "30min", "1h", "当日", "次日", "3日", "5日"]
                    values = row[1:9]
                    max_gain = row[9]
                    max_loss = row[10]
                    vol_ratio = row[11]

                    title = f"## 新闻影响统计（{'个股 ' + stock_code if stock_code else '全市场'}，{days_back}天内，{cnt} 个样本）\n\n"
                    lines = []
                    for label, val in zip(labels, values):
                        if val is not None:
                            direction = "📈" if val > 0 else "📉" if val < 0 else "➡️"
                            lines.append(f"- {label}：{direction} **{val:+.2f}%**")
                    lines.append("")
                    if max_gain is not None:
                        lines.append(f"- 平均最大涨幅：**{max_gain:+.2f}%**")
                    if max_loss is not None:
                        lines.append(f"- 平均最大跌幅：**{max_loss:+.2f}%**")
                    if vol_ratio is not None:
                        lines.append(f"- 平均量能比（新闻后1h/前1h）：**{vol_ratio:.1f}x**")

                    return title + "\n".join(lines)
                finally:
                    conn.close()

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_news_impact

    # ------------------------------------------------------------------
    # Tool 16: get_sentiment_index (新闻情绪指数)
    # ------------------------------------------------------------------
    def _make_get_sentiment_index(self):
        factory = self

        @tool
        def get_sentiment_index(
            hours_back: Optional[int] = 8,
            date: Optional[str] = None,
        ) -> str:
            """获取新闻情绪指数走势。

            情绪值范围 -1（全利空）到 +1（全利好），基于新闻面的利好/利空比例计算。
            可用于辅助判断市场情绪周期阶段（冰点/修复/升温/高潮/分歧/退潮）。

            Args:
                hours_back: 回溯小时数，默认 8（一个交易日）
                date: 指定日期，默认为分析日。指定日期时返回当天全天数据
            """
            if hours_back is None:
                hours_back = 8
            hours_back = int(hours_back)
            target_date = factory._norm_date(date) or factory.date
            date = factory._norm_date(date)
            cache_key = ("sentiment_index", hours_back, target_date)

            def _load():
                cfg = get_config()
                db_path = cfg["news_db"]
                if not os.path.exists(db_path):
                    return "无数据（新闻数据库不存在）"

                conn = sqlite3.connect(db_path, timeout=10)
                try:
                    tables = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='news_sentiment_index'"
                    ).fetchall()
                    if not tables:
                        return "无数据（情绪指数表不存在）"

                    if date:
                        # 指定日期：返回当天全天数据
                        rows = conn.execute("""
                            SELECT timestamp, sentiment_score, bullish_count, bearish_count, neutral_count, total_count
                            FROM news_sentiment_index
                            WHERE created_date = ?
                            ORDER BY timestamp
                        """, (target_date,)).fetchall()
                    else:
                        # 默认：最近 N 小时
                        rows = conn.execute("""
                            SELECT timestamp, sentiment_score, bullish_count, bearish_count, neutral_count, total_count
                            FROM news_sentiment_index
                            WHERE timestamp >= datetime('now', '-' || ? || ' hours', 'localtime')
                            ORDER BY timestamp
                        """, (hours_back,)).fetchall()

                    if not rows:
                        return "无数据（指定时间范围内无情绪数据）"

                    # 计算汇总
                    scores = [r[1] for r in rows]
                    total_bullish = sum(r[2] for r in rows)
                    total_bearish = sum(r[3] for r in rows)
                    total_neutral = sum(r[4] for r in rows)
                    total_news = sum(r[5] for r in rows)
                    avg_score = sum(scores) / len(scores)
                    latest_score = scores[-1]

                    # 情绪阶段判断
                    if avg_score > 0.5:
                        phase_hint = "偏高潮（利好密集）"
                    elif avg_score > 0.2:
                        phase_hint = "偏升温（利好占优）"
                    elif avg_score > -0.2:
                        phase_hint = "中性（多空均衡）"
                    elif avg_score > -0.5:
                        phase_hint = "偏退潮（利空占优）"
                    else:
                        phase_hint = "偏冰点（利空密集）"

                    lines = [
                        f"## 新闻情绪指数",
                        f"- 最新情绪值：**{latest_score:+.2f}**",
                        f"- 平均情绪值：**{avg_score:+.2f}**（{phase_hint}）",
                        f"- 统计：利好 {total_bullish} | 利空 {total_bearish} | 中性 {total_neutral}（共 {total_news} 条新闻）",
                        f"- 采样点数：{len(rows)}",
                        "",
                        "### 走势",
                    ]
                    for ts, score, bull, bear, neut, cnt in rows:
                        bar = "+" * int(abs(score) * 10)
                        direction = "📈" if score > 0 else "📉" if score < 0 else "➡️"
                        lines.append(f"- `{ts[11:16]}` {direction} {score:+.2f} ({bull}好/{bear}空/{neut}中)")

                    return "\n".join(lines)
                finally:
                    conn.close()

            result = factory._cached(cache_key, _load)
            return _str_result(result)

        return get_sentiment_index
