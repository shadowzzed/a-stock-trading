"""数据适配层 — 唯一桥接 review/ 的文件

将 review/ 模块的具体实现适配为 backtest/engine/protocols 定义的接口。
如果未来数据源变更，只需修改此文件。
"""

from __future__ import annotations

import os
from typing import Optional

from .engine.protocols import DataProvider, AgentRunner, LLMCaller, MarketData, StockDataProvider


class ReviewDataProvider:
    """数据提供者 — 从 review.data.loader 加载行情数据"""

    def load_market_data(self, data_dir: str, date: str) -> MarketData:
        from trading_agent.review.data.loader import load_daily_data

        daily_data = load_daily_data(data_dir, date, backtest_mode=True)
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
        from trading_agent.review.data.loader import load_daily_data, summarize_limit_up, summarize_limit_down
        from trading_agent.review.verify import _load_stock_pnl, _enrich_from_db, _query_intraday_db

        data_d1 = load_daily_data(data_dir, date, backtest_mode=True)

        # 先生成 DB 补充数据（前置到 CSV 之前，避免 LLM 忽略）
        db_supplement = _enrich_from_db(data_dir, date, data_d1)

        summary = "## {} 实际行情\n\n".format(date)

        # DB 补充数据前置 + 醒目警告（CSV 数据不完整时）
        if db_supplement:
            summary += "> ⚠️ **以下为 intraday DB 验证的完整数据（比 CSV 更准确），请优先以此为准：**\n\n"
            summary += db_supplement + "\n\n---\n\n"

        summary += summarize_limit_up(data_d1.limit_up) + "\n\n"
        summary += summarize_limit_down(data_d1.limit_down)

        stock_pnl = _load_stock_pnl(data_dir, date, report)
        if stock_pnl:
            summary += "\n\n" + stock_pnl

        return date, summary

    def discover_dates(
        self, data_dir: str, start: Optional[str] = None, end: Optional[str] = None
    ) -> list[str]:
        import glob as glob_mod
        daily_root = os.path.join(data_dir, "daily")
        all_dates = sorted([
            d for d in os.listdir(daily_root)
            if os.path.isdir(os.path.join(daily_root, d))
        ])
        if start:
            all_dates = [d for d in all_dates if d >= start]
        if end:
            all_dates = [d for d in all_dates if d <= end]
        # 只返回有行情数据的交易日（过滤周末/非交易日）
        trading_dates = []
        for d in all_dates:
            csv_pattern = os.path.join(daily_root, d, "行情_*.csv")
            if glob_mod.glob(csv_pattern):
                trading_dates.append(d)
        return trading_dates


class ReviewAgentRunner:
    """Agent 运行器 — 调用 review.graph 执行分析"""

    def run(
        self,
        data_dir: str,
        date: str,
        config: Optional[dict] = None,
        prev_report: str = "",
    ) -> str:
        from trading_agent.review.graph import DEFAULT_CONFIG, _create_llm, _load_initial_state, build_graph

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
            from trading_agent.review.graph import DEFAULT_CONFIG, _create_llm
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


class CSVStockDataProvider:
    """个股日线数据提供者 — 从 daily CSV 文件加载"""

    def __init__(self):
        self._name_code_cache: dict[str, str] = {}  # {name: code}

    def _read_csv_safe(self, csv_path: str) -> list[dict]:
        """安全读取 CSV（处理 NUL 字节等异常）"""
        import csv as csv_mod
        import io as io_mod

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            raw = f.read().replace("\x00", "")  # 清除 NUL 字节
        return list(csv_mod.DictReader(io_mod.StringIO(raw)))

    def load_stock_daily(
        self, data_dir: str, date: str, stock_name: str,
    ) -> Optional[dict]:
        """从行情 CSV 加载指定股票的日线数据（委托给 trading_agent 数据层，含 mootdx fallback）"""
        from trading_agent.review.data.loader import load_stock_daily_ohlcv

        result = load_stock_daily_ohlcv(data_dir, date, stock_name)
        if result:
            # 去掉内部字段
            result.pop("_source", None)
        return result

    def load_stock_daily_by_code(
        self, data_dir: str, date: str, stock_code: str,
    ) -> Optional[dict]:
        """按股票代码加载日线数据"""
        import glob as glob_mod

        d_dir = os.path.join(data_dir, "daily", date)
        csv_files = glob_mod.glob(os.path.join(d_dir, "行情_*.csv"))
        if not csv_files:
            return None

        for csv_file in csv_files:
            try:
                for row in self._read_csv_safe(csv_file):
                    code = row.get("代码", "").strip()
                    if code == stock_code:
                        return self._row_to_dict(row, date)
            except Exception:
                continue

        return None

    def load_limit_up_info(
        self, data_dir: str, date: str, stock_name: str,
    ) -> Optional[dict]:
        """从涨停板 CSV 加载炸板次数等信息"""
        import csv as csv_mod
        import glob as glob_mod

        d_dir = os.path.join(data_dir, "daily", date)
        csv_files = glob_mod.glob(os.path.join(d_dir, "涨停板_*.csv"))
        if not csv_files:
            return None

        for csv_file in csv_files:
            try:
                with open(csv_file, "r", encoding="utf-8-sig") as f:
                    reader = csv_mod.DictReader(f)
                    for row in reader:
                        name = row.get("名称", "").strip()
                        if name == stock_name:
                            return {
                                "name": name,
                                "code": row.get("代码", ""),
                                "broken_count": int(row.get("炸板次数", 0)),
                                "first_seal_time": row.get("首次封板时间", ""),
                                "board_count": int(row.get("连板数", 1)),
                            }
            except Exception:
                continue

        return None

    def resolve_stock_code(
        self, data_dir: str, stock_name: str, date: str = "",
    ) -> Optional[str]:
        """股票名称 → 代码"""
        if stock_name in self._name_code_cache:
            return self._name_code_cache[stock_name]

        # 从最近的行情 CSV 查找
        import csv as csv_mod
        import glob as glob_mod

        daily_root = os.path.join(data_dir, "daily")
        if not os.path.isdir(daily_root):
            return None

        # 优先查指定日期，再查最近的
        search_dates = [date] if date else []
        if not search_dates:
            dirs = sorted(os.listdir(daily_root), reverse=True)
            search_dates = dirs[:5]

        for d in search_dates:
            csv_files = glob_mod.glob(
                os.path.join(daily_root, d, "行情_*.csv")
            )
            for csv_file in csv_files:
                try:
                    with open(csv_file, "r", encoding="utf-8-sig") as f:
                        reader = csv_mod.DictReader(f)
                        for row in reader:
                            if row.get("名称", "").strip() == stock_name:
                                code = row.get("代码", "").strip()
                                self._name_code_cache[stock_name] = code
                                return code
                except Exception:
                    continue

        return None

    @staticmethod
    def _row_to_dict(row: dict, date: str) -> dict:
        """CSV 行 → 标准化字典"""
        def _float(val, default=0.0):
            try:
                return float(str(val).replace(",", ""))
            except (ValueError, TypeError):
                return default

        return {
            "date": date,
            "code": row.get("代码", "").strip() or row.get("code", "").strip(),
            "name": row.get("名称", "").strip(),
            "open": _float(row.get("开盘价") or row.get("open")),
            "high": _float(row.get("最高价") or row.get("high")),
            "low": _float(row.get("最低价") or row.get("low")),
            "close": _float(row.get("收盘价") or row.get("close")),
            "pct_chg": _float(row.get("涨跌幅") or row.get("pctChg")),
            "volume": _float(row.get("成交量") or row.get("volume")),
            "amount": _float(row.get("成交额") or row.get("amount")),
            "last_close": _float(row.get("昨收", row.get("前收盘"))),
        }
