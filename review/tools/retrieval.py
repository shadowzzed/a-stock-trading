"""Review Agent 数据检索工具

提供 10 个检索工具，供 LLM 按需查询历史复盘、行情、个股等数据。
每个工具通过 RetrievalToolFactory 创建，绑定到特定的 data_dir 和 date。
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
from typing import Optional

from langchain_core.tools import tool


class RetrievalToolFactory:
    """创建绑定到特定 data_dir 和 date 的检索工具。

    Args:
        data_dir: trading 数据根目录
        date: 分析日期 (YYYY-MM-DD)
        memory_dir: 跨周期记忆目录 (默认自动推导)
    """

    def __init__(self, data_dir: str, date: str, memory_dir: str = ""):
        self.data_dir = data_dir
        self.date = date
        self.memory_dir = memory_dir or self._infer_memory_dir(data_dir)
        self._cache: dict = {}

    @staticmethod
    def _infer_memory_dir(data_dir: str) -> str:
        """从 data_dir 推导 memory 目录路径"""
        data_top = os.path.dirname(os.path.dirname(os.path.dirname(data_dir)))
        return os.path.join(data_top, "memory", "main")

    def _cached(self, key: tuple, loader):
        """简单的字典缓存，避免同一次运行重复读文件"""
        if key not in self._cache:
            self._cache[key] = loader()
        return self._cache[key]

    def create_tools(self) -> list:
        """创建所有检索工具。"""
        return [
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
                from ..data.loader import _load_history, summarize_history

                history = _load_history(factory.data_dir, factory.date, days_back)

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
            return result if result else "无数据"

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
            return result if result else "无数据"

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
                cache_key = ("memory", date)
            else:
                cache_key = ("memory_days", days_back)

            def _load():
                from ..data.loader import load_memory

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
            return result if result else "无数据"

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
                from ..data.loader import load_lessons

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
            return result if result else "无数据"

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
            return result if result else "无数据"

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
            cache_key = ("index_data", target_date)

            def _load():
                from ..data.loader import load_index_data

                return load_index_data(factory.data_dir, target_date)

            result = factory._cached(cache_key, _load)
            return result if result else "无数据"

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
            cache_key = ("capital_flow", target_date)

            def _load():
                from ..data.loader import load_capital_flow

                return load_capital_flow(factory.data_dir, target_date)

            result = factory._cached(cache_key, _load)
            return result if result else "无数据"

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
                from ..data.loader import load_quantitative_rules

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
            return result if result else "无数据"

        return get_quant_rules

    # ------------------------------------------------------------------
    # Tool 9: get_stock_detail (NEW - SQLite)
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
            cache_key = ("stock_detail", name, code, target_date)

            def _load():
                db_path = os.path.join(
                    factory.data_dir, "intraday", "intraday.db"
                )
                if not os.path.exists(db_path):
                    return "无数据（intraday.db 不存在）"

                try:
                    conn = sqlite3.connect(db_path)
                    conn.row_factory = sqlite3.Row
                except Exception:
                    return "无数据（数据库连接失败）"

                try:
                    # 构建查询条件
                    conditions = ["date = ?"]
                    params: list = [target_date]

                    if code:
                        conditions.append("code LIKE ?")
                        params.append(f"%{code}%")
                    if name:
                        conditions.append("name LIKE ?")
                        params.append(f"%{name}%")

                    where = " AND ".join(conditions)
                    query = f"""
                        SELECT date, ts, code, name, price, pctChg,
                               open, high, low, last_close,
                               volume, amount, amount_yi,
                               is_limit_up, is_limit_down, sector
                        FROM snapshots
                        WHERE {where}
                        ORDER BY ts
                    """
                    rows = conn.execute(query, params).fetchall()
                finally:
                    conn.close()

                if not rows:
                    return "无数据（未找到匹配的股票快照）"

                # 格式化输出
                lines = []
                first = rows[0]
                stock_name = first["name"]
                stock_code = first["code"]
                sector = first["sector"] or ""

                header = f"## {stock_name}（{stock_code}）{f' - {sector}' if sector else ''}"
                header += f"\n日期: {target_date}，共 {len(rows)} 条快照\n"
                lines.append(header)

                lines.append("| 时间 | 价格 | 涨跌幅 | 成交额(亿) | 涨停 |")
                lines.append("|------|------|--------|-----------|------|")

                for row in rows:
                    ts = row["ts"]
                    price = row["price"] or 0
                    pct = row["pctChg"] or 0
                    amt = row["amount_yi"] or 0
                    limit = "是" if row["is_limit_up"] else ""
                    lines.append(
                        f"| {ts} | {price:.2f} | {pct:+.2f}% | {amt:.2f} | {limit} |"
                    )

                # 汇总统计
                prices = [r["price"] for r in rows if r["price"]]
                if prices:
                    lines.append("")
                    lines.append(
                        f"开盘 {prices[0]:.2f}，最高 {max(prices):.2f}，"
                        f"最低 {min(prices):.2f}，收盘 {prices[-1]:.2f}"
                    )

                return "\n".join(lines)

            result = factory._cached(cache_key, _load)
            return result if result else "无数据"

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
            return result if result else "无数据"

        return get_past_report
