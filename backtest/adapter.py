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

    def load_market_snapshot(self, data_dir: str, date: str) -> dict:
        """加载 Layer 1 所需的完整市场快照（直接从 DB 读取）"""
        import sqlite3

        db_path = os.path.join(data_dir, "intraday", "intraday.db")
        snapshot = {"date": date}

        conn = sqlite3.connect(db_path, timeout=10)
        try:
            # 涨停统计
            row = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN blown_count > 0 THEN 1 ELSE 0 END), "
                "MAX(board_count) "
                "FROM limit_up WHERE date = ?", (date,)
            ).fetchone()
            total_lu = row[0] or 0
            blown = row[1] or 0
            snapshot["limit_up_count"] = total_lu
            snapshot["blown_rate"] = (blown / total_lu * 100) if total_lu > 0 else 0
            snapshot["max_board"] = row[2] or 0

            # 跌停统计
            row = conn.execute(
                "SELECT COUNT(*) FROM limit_down WHERE date = ?", (date,)
            ).fetchone()
            snapshot["limit_down_count"] = row[0] or 0

            # 板块分布
            rows = conn.execute(
                "SELECT industry, COUNT(*) as cnt FROM limit_up "
                "WHERE date = ? AND industry IS NOT NULL AND industry != '' "
                "GROUP BY industry ORDER BY cnt DESC",
                (date,),
            ).fetchall()
            snapshot["sector_distribution"] = {r[0]: r[1] for r in rows}

            # 连板梯队
            rows = conn.execute(
                "SELECT board_count, name FROM limit_up "
                "WHERE date = ? AND board_count > 1 ORDER BY board_count DESC",
                (date,),
            ).fetchall()
            ladder = {}
            for board, name in rows:
                key = str(board)
                if key not in ladder:
                    ladder[key] = []
                ladder[key].append(name)
            snapshot["board_ladder"] = ladder

            # 前日涨停数
            prev_row = conn.execute(
                "SELECT COUNT(*) FROM limit_up WHERE date = ("
                "  SELECT MAX(date) FROM limit_up WHERE date < ?"
                ")", (date,)
            ).fetchone()
            snapshot["prev_limit_up_count"] = prev_row[0] or 0

        finally:
            conn.close()

        return snapshot

    def discover_dates(
        self, data_dir: str, start: Optional[str] = None, end: Optional[str] = None
    ) -> list[str]:
        import sqlite3
        db_path = os.path.join(data_dir, "intraday", "intraday.db")
        if not os.path.exists(db_path):
            return []
        conn = sqlite3.connect(db_path)
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_bars ORDER BY date"
        ).fetchall()]
        conn.close()
        if start:
            dates = [d for d in dates if d >= start]
        if end:
            dates = [d for d in dates if d <= end]
        return dates


class MarketJudgmentRunner:
    """Layer 1: 市场研判运行器 — 精简 LLM 调用，只输出结构化 JSON

    LLM 只负责判断情绪阶段、识别最强板块、输出买入门控信号。
    不做选股、不做买卖决策。
    """

    SYSTEM_PROMPT = """你是 A 股短线市场研判专家。你的任务是分析当日市场数据，判断情绪阶段和最强板块方向。

## 情绪周期模型
冰点 → 修复 → 升温 → 高潮 → 分歧 → 退潮 → 冰点

判断依据：
- 涨停数趋势（增/减）、跌停数、炸板率
- 连板高度和梯队完整度
- 全市场成交额（≥2.5万亿容错率高，<2万亿环境差）

## 行情日类型
- 回暖日：量能放大、涨停集中、板块持续性强
- 变盘日：缩量、波动加剧、绿盘>4000家
- 震荡日：轮动快、持续性差

## 买入门控（action_gate）
- "可买入"：修复/升温/高潮期，市场环境健康
- "谨慎"：分歧期，可做最强品种低吸
- "空仓"：退潮/冰点/变盘日，不开新仓

## 输出要求
只输出一个 JSON，不要输出其他任何文字：

```json
{
  "sentiment_phase": "修复",
  "market_type": "回暖日",
  "top_sectors": ["电力", "AI算力"],
  "sector_logic": "电力板块连续3日领涨，龙头3板带动梯队",
  "action_gate": "可买入"
}
```

注意：
- top_sectors 最多2个板块，用行业名称（如"电力"、"通信设备"、"电网设备"、"通用设备"）
- sector_logic 一句话说明为什么选这些板块
- 如果没有明确主线，top_sectors 填当日涨停最集中的行业"""

    def run(self, market_snapshot: dict) -> dict:
        """执行市场研判

        Args:
            market_snapshot: 市场数据快照，包含涨跌停数据等

        Returns:
            dict with sentiment_phase, market_type, top_sectors, sector_logic, action_gate
        """
        import json as json_mod
        from config import get_ai_providers
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import SystemMessage, HumanMessage

        providers = get_ai_providers()
        if not providers:
            raise ValueError("未配置 AI 提供商")
        primary = providers[0]
        llm = ChatOpenAI(
            model=primary["model"],
            base_url=primary["base"],
            api_key=primary["key"],
            temperature=0,
        )

        user_msg = self._build_market_message(market_snapshot)
        response = llm.invoke([
            SystemMessage(content=self.SYSTEM_PROMPT),
            HumanMessage(content=user_msg),
        ])

        return self._parse_judgment(response.content)

    @staticmethod
    def _build_market_message(snapshot: dict) -> str:
        """构造市场数据消息"""
        date = snapshot.get("date", "未知")
        parts = [f"## {date} A股市场数据\n"]

        parts.append("| 指标 | 数值 |")
        parts.append("|------|------|")
        parts.append(f"| 涨停数 | {snapshot.get('limit_up_count', '?')} |")
        parts.append(f"| 跌停数 | {snapshot.get('limit_down_count', '?')} |")
        parts.append(f"| 炸板率 | {snapshot.get('blown_rate', '?'):.1f}% |")
        parts.append(f"| 最高连板 | {snapshot.get('max_board', '?')}板 |")

        if snapshot.get("prev_limit_up_count"):
            parts.append(f"| 前日涨停数 | {snapshot['prev_limit_up_count']} |")

        # 板块分布
        sector_dist = snapshot.get("sector_distribution", {})
        if sector_dist:
            parts.append("\n### 涨停板块分布（Top 10）\n")
            parts.append("| 行业 | 涨停数 |")
            parts.append("|------|--------|")
            for sector, count in sorted(sector_dist.items(), key=lambda x: -x[1])[:10]:
                parts.append(f"| {sector} | {count} |")

        # 连板梯队
        board_ladder = snapshot.get("board_ladder", {})
        if board_ladder:
            parts.append("\n### 连板梯队\n")
            for board, stocks in sorted(board_ladder.items(), key=lambda x: -int(x[0])):
                parts.append(f"- {board}板: {', '.join(stocks[:5])}")

        return "\n".join(parts)

    @staticmethod
    def _parse_judgment(text: str) -> dict:
        """从 LLM 输出中解析 JSON"""
        import json as json_mod
        import re

        # 尝试提取 JSON 块
        json_match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
        if json_match:
            text = json_match.group(1)
        else:
            # 尝试直接解析整个文本
            text = text.strip()
            # 去掉可能的 markdown 包装
            if text.startswith("```"):
                text = re.sub(r'^```\w*\n?', '', text)
                text = re.sub(r'\n?```$', '', text)

        try:
            result = json_mod.loads(text)
        except json_mod.JSONDecodeError:
            # 最后的 fallback：搜索第一个 { ... }
            brace_match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
            if brace_match:
                try:
                    result = json_mod.loads(brace_match.group())
                except json_mod.JSONDecodeError:
                    result = {}
            else:
                result = {}

        # 确保必要字段存在
        defaults = {
            "sentiment_phase": "未知",
            "market_type": "震荡日",
            "top_sectors": [],
            "sector_logic": "",
            "action_gate": "谨慎",
        }
        for key, default in defaults.items():
            if key not in result:
                result[key] = default

        return result


class ChatAgentRunner:
    """Agent 运行器 — 调用 Trade Agent (chat/) 执行分析（旧架构，保留兼容）

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

        # 将经验教训注入到用户消息前部，让所有 Agent 都能看到
        overrides = (config or {}).get("prompt_overrides", {})
        if overrides:
            lessons_parts = []
            for agent_name, text in overrides.items():
                lessons_parts.append(text)
            lessons_block = "\n\n".join(lessons_parts)
            message = (
                "---\n## ⚠️ 历史经验教训（在类似场景中验证过的失败/成功模式）\n\n"
                f"{lessons_block}\n\n"
                "请在分析和推荐时参考以上教训，避免重复已知的失败模式。\n---\n\n"
                + message
            )

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
            "- **趋势票扫描**：委托趋势分析师使用 scan_trend_stocks 扫描全市场趋势股，"
            "寻找沿5日线/10日线上方健康运行的标的（如德明利模式）",
            "",
            "**选股双轨制**：",
            "- **龙头股**（30%仓位/只）：连板龙头/空间板/板块总龙头，辨识度最高，高风险高收益",
            "- **趋势票**（30%仓位/只）：由趋势分析师 scan_trend_stocks 推荐，沿均线运行的中军/主线股",
            "- 最多同时持有3只（每只30%=90%仓位），不同板块分散风险",
            "- 龙头优先，趋势票补充；如果某类没有合适标的，空着即可",
            "",
            "**执行纪律**：",
            "- 每只标的的买入原因必须包含：板块逻辑、辨识度说明、预期管理（竞价低于预期怎么处理）",
            "- focus_stocks 中每只标的的 reason 字段不能只写几个字，必须写完整的参与逻辑（至少30字）",
            "- 严禁推荐后排跟风股、补涨股、无辨识度的边缘品种",
            "- 根据你的交易体系判断何时买入、何时空仓，不设硬性情绪阶段限制",
            "- 无论什么情绪阶段，都必须输出 focus_stocks（空仓时标注 action 为「观望」）",
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
                parts.extend([
                    "",
                    "请根据当前持仓和今日行情，制定次日操盘计划：",
                    "1. **对每只持仓标的做出决策**（在 JSON 的 position_actions 中输出）：",
                    "   - 超预期（如连板、主升浪中）→ 继续持有，给出新的卖出条件",
                    "   - 符合预期 → 可持有，但必须给出明确的止盈/止损条件",
                    "   - 低于预期（如竞价低开、主线退潮）→ 次日开盘卖出",
                    "2. **新买入标的**不得与持仓重复，仓位不超过可用现金",
                ])
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
                temperature=0,
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
        """按股票代码加载日线数据（DB 优先，mootdx fallback）"""
        import sqlite3

        # 兼容代码格式：000788 或 sz.000788 都支持
        normalized = stock_code.strip()
        if "." in normalized:
            normalized = normalized.split(".", 1)[1]

        db_path = os.path.join(data_dir, "intraday", "intraday.db")
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path, timeout=10)
            try:
                row = conn.execute(
                    "SELECT date, code, name, open, high, low, close, volume, amount "
                    "FROM daily_bars WHERE date = ? AND code = ?",
                    (date, normalized),
                ).fetchone()
                if row:
                    return {
                        "date": row[0], "code": row[1], "name": row[2] or "",
                        "open": row[3], "high": row[4], "low": row[5],
                        "close": row[6], "volume": row[7], "amount": row[8],
                    }
            except Exception:
                pass
            finally:
                conn.close()

        # DB 未命中 → mootdx fallback
        from trading_agent.review.data.loader import load_stock_daily_ohlcv_by_code
        result = load_stock_daily_ohlcv_by_code(data_dir, date, normalized)
        if result:
            result.pop("_source", None)
        return result

    def load_limit_up_info(
        self, data_dir: str, date: str, stock_name: str,
    ) -> Optional[dict]:
        """从 limit_up 表加载炸板次数等信息"""
        import sqlite3

        db_path = os.path.join(data_dir, "intraday", "intraday.db")
        if not os.path.exists(db_path):
            return None

        conn = sqlite3.connect(db_path, timeout=10)
        try:
            row = conn.execute(
                "SELECT code, name, blown_count, first_limit_time, board_count "
                "FROM limit_up WHERE date = ? AND name = ?",
                (date, stock_name),
            ).fetchone()
            if row:
                return {
                    "name": row[1],
                    "code": row[0],
                    "broken_count": row[2] or 0,
                    "first_seal_time": row[3] or "",
                    "board_count": row[4] or 1,
                }
        except Exception:
            pass
        finally:
            conn.close()
        return None

    def resolve_stock_code(
        self, data_dir: str, stock_name: str, date: str = "",
    ) -> Optional[str]:
        """股票名称 → 代码（从 stock_meta 表查询）"""
        if stock_name in self._name_code_cache:
            return self._name_code_cache[stock_name]

        import sqlite3

        db_path = os.path.join(data_dir, "intraday", "intraday.db")
        if not os.path.exists(db_path):
            return None

        conn = sqlite3.connect(db_path, timeout=10)
        try:
            if date:
                row = conn.execute(
                    "SELECT code FROM stock_meta WHERE date = ? AND name = ?",
                    (date, stock_name),
                ).fetchone()
            else:
                # 查最近的记录
                row = conn.execute(
                    "SELECT code FROM stock_meta WHERE name = ? ORDER BY date DESC LIMIT 1",
                    (stock_name,),
                ).fetchone()
            if row:
                code = row[0]
                self._name_code_cache[stock_name] = code
                return code
        except Exception:
            pass
        finally:
            conn.close()
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
