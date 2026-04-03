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
                from data.loader import _load_history, summarize_history

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
                from data.loader import load_memory

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
                from data.loader import load_lessons

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
                from data.loader import load_index_data

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
                from data.loader import load_capital_flow

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
                from data.loader import load_quantitative_rules

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

    # ------------------------------------------------------------------
    # Tool 11: get_market_data (NEW - flexible market snapshot)
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
            from datetime import datetime as _dt

            ds = date or _dt.now().strftime("%Y-%m-%d")
            db_path = os.path.join(factory.data_dir, "intraday", "intraday.db")

            rows = []
            actual_ts = None

            if os.path.exists(db_path):
                conn = None
                try:
                    conn = sqlite3.connect(db_path)
                    conn.row_factory = sqlite3.Row

                    # Check if data exists for this date
                    has_data = conn.execute(
                        "SELECT 1 FROM snapshots WHERE date = ? LIMIT 1", (ds,)
                    ).fetchone()

                    if has_data:
                        # Resolve target timestamp
                        if time and time not in ("close", "latest"):
                            ts_row = conn.execute(
                                "SELECT ts FROM snapshots WHERE date = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                                (ds, time + ":59"),
                            ).fetchone()
                        else:
                            ts_row = conn.execute(
                                "SELECT ts FROM snapshots WHERE date = ? ORDER BY ts DESC LIMIT 1",
                                (ds,),
                            ).fetchone()

                        if ts_row:
                            actual_ts = ts_row[0]
                            conditions = ["date = ?", "ts = ?"]
                            params: list = [ds, actual_ts]

                            if code:
                                conditions.append("code LIKE ?")
                                params.append(f"%{code}%")
                            if name:
                                conditions.append("name LIKE ?")
                                params.append(f"%{name}%")
                            if mode == "pool":
                                conditions.append("in_pool = 1")

                            where = " AND ".join(conditions)
                            sort_col = (
                                "amount_yi" if sort_by == "amount"
                                else "volume" if sort_by == "volume"
                                else "pctChg"
                            )
                            query = f"SELECT * FROM snapshots WHERE {where} ORDER BY {sort_col} DESC"
                            rows = [dict(r) for r in conn.execute(query, params).fetchall()]
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error("[get_market_data] DB error: %s", e)
                finally:
                    if conn:
                        conn.close()

            # Fallback: mootdx real-time (only for today)
            today_str = _dt.now().strftime("%Y-%m-%d")
            if not rows and ds == today_str:
                try:
                    import subprocess
                    mootdx_path = os.path.join(factory.data_dir, "mootdx_tool.py")
                    output = subprocess.check_output(
                        ["python3", mootdx_path, "quotes"] + ([code] if code else []),
                        timeout=15, encoding="utf-8",
                    )
                    for line in output.strip().split("\n"):
                        parts = line.strip().split()
                        if len(parts) >= 8 and parts[0].isdigit() and len(parts[0]) == 6:
                            row = {
                                "code": parts[0], "name": parts[1],
                                "price": float(parts[2]), "pctChg": float(parts[3]),
                                "amount_yi": float(parts[-1]) if parts[-1].replace(".", "").isdigit() else 0,
                            }
                            if name and name not in row["name"]:
                                continue
                            if code and code not in row["code"]:
                                continue
                            rows.append(row)
                    actual_ts = "实时"
                except Exception:
                    pass

            if not rows:
                return f"无行情数据（{ds} {time or ''}），本地数据库和通达信接口均无数据"

            # ─── Format output ───
            def _fmt_pct(v):
                if v is None: return "-"
                n = float(v)
                return f"{n:+.2f}%"

            def _fmt_price(v):
                if v is None: return "-"
                return f"{float(v):.2f}"

            def _fmt_amt(v):
                if v is None: return "-"
                return f"{float(v):.2f}"

            if mode == "stock":
                n = top_n or 5
                filtered = rows[:n]
                r = filtered[0]
                lines = [
                    f"## {r.get('name','')}（{r.get('code','')}）",
                    f"日期: {ds}  时间: {actual_ts}", "",
                    "| 代码 | 名称 | 现价 | 涨跌幅 | 开盘 | 最高 | 最低 | 成交额(亿) |",
                    "|------|------|------|--------|------|------|------|-----------|",
                ]
                for r in filtered:
                    lines.append(
                        f"| {r['code']} | {r['name']} | {_fmt_price(r.get('price'))} "
                        f"| {_fmt_pct(r.get('pctChg'))} | {_fmt_price(r.get('open'))} "
                        f"| {_fmt_price(r.get('high'))} | {_fmt_price(r.get('low'))} "
                        f"| {_fmt_amt(r.get('amount_yi'))} |"
                    )
                return "\n".join(lines)

            # overview / pool
            n = top_n or 10
            limit_ups = [r for r in rows if r.get("is_limit_up")]
            limit_downs = [r for r in rows if r.get("is_limit_down")]
            up_count = sum(1 for r in rows if (r.get("pctChg") or 0) > 0)
            down_count = sum(1 for r in rows if (r.get("pctChg") or 0) < 0)
            total_amount = sum(r.get("amount_yi", 0) or 0 for r in rows)

            sorted_rows = sorted(rows, key=lambda r: r.get("pctChg", 0) or 0, reverse=True)
            top_gainers = sorted_rows[:n]
            top_losers = sorted_rows[-n:][::-1]

            label = "股票池" if mode == "pool" else "全市场"
            lines = [
                f"## 行情概览（{label}）",
                f"日期: {ds}  时间: {actual_ts}  总数: {len(rows)}",
                f"涨: {up_count}  跌: {down_count}  涨停: {len(limit_ups)}  跌停: {len(limit_downs)}  总成交: {total_amount:.1f}亿",
                "", f"### 涨幅 TOP{n}",
                "| 代码 | 名称 | 现价 | 涨跌幅 | 成交额(亿) |",
                "|------|------|------|--------|-----------|",
            ]
            for r in top_gainers:
                lines.append(f"| {r['code']} | {r['name']} | {_fmt_price(r.get('price'))} | {_fmt_pct(r.get('pctChg'))} | {_fmt_amt(r.get('amount_yi'))} |")

            lines += ["", f"### 跌幅 TOP{n}"]
            for r in top_losers:
                lines.append(f"| {r['code']} | {r['name']} | {_fmt_price(r.get('price'))} | {_fmt_pct(r.get('pctChg'))} | {_fmt_amt(r.get('amount_yi'))} |")

            if limit_ups and mode != "pool":
                lines += ["", f"### 涨停（{len(limit_ups)}只）"]
                for r in limit_ups:
                    amt = r.get("amount_yi", 0) or 0
                    lines.append(f"- {r['name']}（{r['code']}）{amt:.1f}亿")

            return "\n".join(lines)

        return get_market_data

    # ------------------------------------------------------------------
    # Tool 12: scan_trend_stocks (全市场趋势股扫描)
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
            from datetime import datetime as _dt

            ds = factory.date or _dt.now().strftime("%Y-%m-%d")
            db_path = os.path.join(factory.data_dir, "intraday", "intraday.db")
            if not os.path.exists(db_path):
                return "无数据（intraday.db 不存在）"

            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
            except Exception:
                return "无数据（数据库连接失败）"

            try:
                # 1. 获取最近交易日列表（需要至少11个交易日来算10日均线）
                trading_days = [
                    r[0] for r in conn.execute(
                        "SELECT DISTINCT date FROM snapshots "
                        "WHERE ts = '15:00:00' ORDER BY date DESC LIMIT 12"
                    ).fetchall()
                ]
                if len(trading_days) < 6:
                    return f"数据不足：仅 {len(trading_days)} 个交易日，至少需要6个"

                today = trading_days[0]
                # 用最近11个交易日（含今天）计算均线
                calc_days = trading_days[:11]

                # 2. 获取这些日期的所有收盘数据
                placeholders = ",".join(["?"] * len(calc_days))
                rows = conn.execute(f"""
                    SELECT date, code, name, price, pctChg, open, high, low,
                           amount_yi, volume, sector, star, in_pool
                    FROM snapshots
                    WHERE date IN ({placeholders}) AND ts = '15:00:00'
                    ORDER BY code, date DESC
                """, calc_days).fetchall()

                if not rows:
                    return "无收盘数据"

                # 3. 按股票聚合，计算均线
                stock_data = {}  # code -> {dates: {date: price, ...}, info: {...}}
                for r in rows:
                    code = r["code"]
                    if code not in stock_data:
                        stock_data[code] = {
                            "prices": {},  # date -> price (收盘价)
                            "name": r["name"],
                            "sector": r["sector"] or "",
                            "star": r["star"],
                            "in_pool": r["in_pool"],
                        }
                    stock_data[code]["prices"][r["date"]] = r["price"]

                # 4. 计算均线并筛选趋势股
                results = []
                need_ma5 = ma_type in ("5", "both")
                need_ma10 = ma_type in ("10", "both")

                for code, sd in stock_data.items():
                    prices = sd["prices"]
                    if today not in prices:
                        continue

                    today_price = prices[today]
                    if not today_price or today_price <= 0:
                        continue

                    # 按日期降序排列（最近的在前）
                    sorted_dates = sorted(prices.keys(), reverse=True)

                    # 今天涨跌幅
                    # 从数据库中拿今天的 pctChg
                    today_row = next(
                        (r for r in rows if r["code"] == code and r["date"] == today),
                        None
                    )
                    if not today_row:
                        continue
                    today_pct = today_row["pctChg"] or 0
                    today_amount = today_row["amount_yi"] or 0

                    # 涨幅过滤
                    if today_pct < min_pct:
                        continue
                    if max_pct is not None and today_pct > max_pct:
                        continue

                    # 计算5日均线
                    ma5 = None
                    if need_ma5 and len(sorted_dates) >= 5:
                        ma5_dates = sorted_dates[:5]
                        ma5_prices = [prices[d] for d in ma5_dates if prices.get(d)]
                        if len(ma5_prices) >= 5:
                            ma5 = sum(ma5_prices) / len(ma5_prices)

                    # 计算10日均线
                    ma10 = None
                    if need_ma10 and len(sorted_dates) >= 10:
                        ma10_dates = sorted_dates[:10]
                        ma10_prices = [prices[d] for d in ma10_dates if prices.get(d)]
                        if len(ma10_prices) >= 10:
                            ma10 = sum(ma10_prices) / len(ma10_prices)

                    # 判断是否沿均线运行：价格在均线上方 + 近5日大部分时间在均线上方
                    above_ma5 = False
                    above_ma10 = False

                    if need_ma5 and ma5:
                        # 今天在5日线上方
                        if today_price >= ma5:
                            # 检查近5日中至少3日在5日线上方
                            days_above = sum(
                                1 for d in ma5_dates
                                if prices.get(d, 0) >= ma5 * 0.99  # 允许1%误差
                            )
                            if days_above >= 3:
                                above_ma5 = True

                    if need_ma10 and ma10:
                        if today_price >= ma10:
                            ma10_check_dates = sorted_dates[:min(5, len(sorted_dates))]
                            days_above = sum(
                                1 for d in ma10_check_dates
                                if prices.get(d, 0) >= ma10 * 0.99
                            )
                            if days_above >= 3:
                                above_ma10 = True

                    # 至少满足一个均线条件
                    if not above_ma5 and not above_ma10:
                        continue

                    results.append({
                        "code": code,
                        "name": sd["name"],
                        "price": today_price,
                        "pctChg": today_pct,
                        "amount_yi": today_amount,
                        "sector": sd["sector"],
                        "star": sd["star"],
                        "in_pool": sd["in_pool"],
                        "ma5": round(ma5, 2) if ma5 else None,
                        "ma10": round(ma10, 2) if ma10 else None,
                        "above_ma5": above_ma5,
                        "above_ma10": above_ma10,
                        "dist_ma5": round((today_price / ma5 - 1) * 100, 2) if ma5 else None,
                        "dist_ma10": round((today_price / ma10 - 1) * 100, 2) if ma10 else None,
                    })

                # 5. 热门板块筛选
                if hot_only:
                    sector_avg = {}
                    sector_counts = {}
                    for r in results:
                        s = r["sector"]
                        if not s:
                            continue
                        sector_avg.setdefault(s, []).append(r["pctChg"])
                        sector_counts[s] = sector_counts.get(s, 0) + 1
                    hot_sectors = {
                        s for s, pcts in sector_avg.items()
                        if sum(pcts) / len(pcts) > 1.0 and sector_counts.get(s, 0) >= 2
                    }
                    results = [r for r in results if r["sector"] in hot_sectors]

                # 6. 板块过滤
                if sector:
                    results = [
                        r for r in results
                        if sector in r["sector"]
                    ]

                # 7. 排序：池内优先 > 星标 > 涨幅
                results.sort(
                    key=lambda x: (
                        -int(x["in_pool"] or 0),
                        -int(x["star"] or 0),
                        -(x["pctChg"] or 0),
                    )
                )

                # 8. 截取 top_n
                results = results[:top_n]

                if not results:
                    return "未找到符合条件的趋势股"

                # 9. 格式化输出
                lines = [
                    f"## 趋势股扫描结果（{ds}）",
                    f"筛选条件：涨幅≥{min_pct}%"
                    + (f"≤{max_pct}%" if max_pct else "")
                    + f" | 均线类型={ma_type}"
                    + (f" | 板块含「{sector}」" if sector else "")
                    + (f" | 仅热门板块" if hot_only else ""),
                    f"共找到 {len(results)} 只趋势股\n",
                    "| 代码 | 名称 | 现价 | 涨幅 | 5日线 | 10日线 | 距5日线 | 距10日线 | 成交额(亿) | 板块 |",
                    "|------|------|------|------|-------|--------|---------|----------|-----------|------|",
                ]

                for r in results:
                    star_mark = "⭐" if r["star"] else ""
                    pool_mark = "🏊" if r["in_pool"] else ""
                    name_display = f"{star_mark}{pool_mark}{r['name']}"

                    ma5_str = f"{r['ma5']:.2f}" if r["ma5"] else "-"
                    ma10_str = f"{r['ma10']:.2f}" if r["ma10"] else "-"
                    dist5 = f"{r['dist_ma5']:+.1f}%" if r["dist_ma5"] is not None else "-"
                    dist10 = f"{r['dist_ma10']:+.1f}%" if r["dist_ma10"] is not None else "-"

                    lines.append(
                        f"| {r['code']} | {name_display} | {r['price']:.2f} "
                        f"| {r['pctChg']:+.2f}% | {ma5_str} | {ma10_str} "
                        f"| {dist5} | {dist10} | {r['amount_yi']:.1f} | {r['sector']} |"
                    )

                # 板块汇总
                sector_summary = {}
                for r in results:
                    s = r["sector"] or "未知"
                    sector_summary.setdefault(s, {"count": 0, "pcts": []})
                    sector_summary[s]["count"] += 1
                    sector_summary[s]["pcts"].append(r["pctChg"])

                lines.append("\n### 板块分布")
                for s, info in sorted(
                    sector_summary.items(),
                    key=lambda x: -x[1]["count"]
                ):
                    avg_pct = sum(info["pcts"]) / len(info["pcts"])
                    lines.append(
                        f"- **{s}**：{info['count']}只，平均涨幅 {avg_pct:+.2f}%"
                    )

                return "\n".join(lines)

            except Exception as e:
                import logging
                logging.getLogger(__name__).error("[scan_trend_stocks] error: %s", e)
                return f"扫描失败: {e}"
            finally:
                conn.close()

        return scan_trend_stocks
