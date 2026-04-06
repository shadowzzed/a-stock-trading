"""数据适配层 — 桥接 backtest 引擎与 Trade Agent

将 Trade Agent (chat/) 的能力适配为 backtest/engine/protocols 定义的接口。
数据加载复用 review/data/ 和 review/tools/ 的基础设施。
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

        data_d1 = load_daily_data(data_dir, date, backtest_mode=True)

        summary = "## {} 实际行情\n\n".format(date)
        summary += summarize_limit_up(data_d1.limit_up) + "\n\n"
        summary += summarize_limit_down(data_d1.limit_down)

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


class ChatAgentRunner:
    """Agent 运行器 — 调用 Trade Agent (chat/) 执行分析

    与实盘使用完全相同的提示词和决策逻辑，
    回测验证的就是真正的 Trade Agent 能力。
    """

    def run(
        self,
        data_dir: str,
        date: str,
        config: Optional[dict] = None,
        prev_report: str = "",
        portfolio_state: Optional[dict] = None,
    ) -> str:
        from trading_agent.chat.agent import TradingChatAgent

        # 构造和实盘一样的自然语言消息
        message = self._build_backtest_message(date, prev_report, portfolio_state)
        agent = TradingChatAgent(backtest_max_date=date)
        result = agent.chat(message)

        # 审计：检查是否有越界数据访问
        audit = agent.get_audit_summary()
        if not audit["clean"]:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(
                "[BACKTEST AUDIT] %s 存在 %d 次越界数据访问（已拦截）: %s",
                date, audit["blocked_count"], audit["blocked_details"],
            )
        self._last_audit = audit

        return result

    @property
    def last_audit(self) -> Optional[dict]:
        """获取最近一次 run() 的审计结果。"""
        return getattr(self, '_last_audit', None)

    @staticmethod
    def _build_backtest_message(
        date: str,
        prev_report: str = "",
        portfolio_state: Optional[dict] = None,
    ) -> str:
        """构造回测模式的用户消息"""
        parts = [
            "请对 {} 的 A 股短线行情进行全面复盘分析。".format(date),
            "",
            "需要你完成：",
            "1. 情绪周期定位（当前阶段、关键数据、与前日对比）",
            "2. 主线与板块分析（主线、支线、退潮方向）",
            "3. 龙头生态（总龙头、板块龙头、梯队结构、补涨标的）",
            "4. **次日操盘计划**（必须包含具体的买入标的、买入条件、仓位建议）",
            "",
            "重点关注：",
            "- 涨停/跌停统计和炸板率",
            "- 连板梯队和龙头辨识",
            "- 板块资金流向和主线持续性",
            "- 事件催化对次日的影响",
        ]

        # 注入当前持仓状态
        if portfolio_state:
            parts.extend(["", "---", "## 当前持仓状态"])
            parts.append("- 总资产：{:.0f} 元".format(
                portfolio_state.get("total_value", 0)))
            parts.append("- 可用现金：{:.0f} 元（占比 {:.0f}%）".format(
                portfolio_state.get("cash", 0),
                portfolio_state.get("cash_pct", 100),
            ))
            positions = portfolio_state.get("positions", [])
            if positions:
                parts.append("- 当前持仓：")
                for p in positions:
                    parts.append(
                        "  - **{name}**（{code}）：{shares}股，"
                        "买入价 {buy_price:.2f}，"
                        "当前价 {current_price:.2f}，"
                        "浮盈 {pnl_pct:+.2f}%，"
                        "买入日期 {buy_date}".format(**p)
                    )
                parts.append("")
                parts.append(
                    "请根据当前持仓状态制定次日操盘计划：\n"
                    "- 对已持仓标的，给出持有/卖出判断及卖出条件\n"
                    "- 新买入标的不得与持仓重复\n"
                    "- 买入仓位不得超过可用现金"
                )
            else:
                parts.append("- 当前持仓：空仓")

        if prev_report:
            parts.extend([
                "",
                "---",
                "以下是前一交易日的 AI 分析报告，供参考校准：",
                prev_report[:3000],
            ])
        return "\n".join(parts)


class LangChainLLMCaller:
    """LLM 调用器 — 封装 langchain LLM（保留接口兼容）"""

    def __init__(self, llm=None):
        self._llm = llm

    def _ensure_llm(self):
        if self._llm is None:
            from config import get_ai_providers
            from langchain_openai import ChatOpenAI

            providers = get_ai_providers()
            if not providers:
                raise ValueError("未配置 AI 提供商")
            primary = providers[0]
            self._llm = ChatOpenAI(
                model=primary["model"],
                base_url=primary["base"],
                api_key=primary["key"],
                temperature=0.3,
            )
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
        """按股票代码加载日线数据（CSV 优先，mootdx fallback）"""
        import glob as glob_mod

        # 兼容代码格式：000788 或 sz.000788 都支持
        normalized = stock_code.strip()
        if "." in normalized:
            normalized = normalized.split(".", 1)[1]

        d_dir = os.path.join(data_dir, "daily", date)
        csv_files = glob_mod.glob(os.path.join(d_dir, "行情_*.csv"))

        for csv_file in csv_files:
            try:
                for row in self._read_csv_safe(csv_file):
                    code = row.get("代码", "").strip()
                    # CSV 中可能是 "sz.000788" 或 "000788"，都匹配
                    code_short = code.split(".", 1)[1] if "." in code else code
                    if code_short == normalized:
                        return self._row_to_dict(row, date)
            except Exception:
                continue

        # CSV 未命中 → mootdx fallback
        from trading_agent.review.data.loader import load_stock_daily_ohlcv_by_code
        result = load_stock_daily_ohlcv_by_code(data_dir, date, normalized)
        if result:
            result.pop("_source", None)
        return result

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
