"""三层架构回测引擎

Layer 1: LLM 市场研判 → JSON (sentiment_phase, top_sectors, action_gate)
Layer 2: 量化选股 → 代码规则评分筛选
Layer 3: 风控执行 → 代码化买卖规则
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

from .adapter import ReviewDataProvider, MarketJudgmentRunner
from .screener import screen_stocks, ScoredStock, format_screening_result


# ── Layer 3 风控参数（可调优） ──────────────────────────

STOP_LOSS_PCT = -7.0       # 网格搜索最优（144种组合验证）
TAKE_PROFIT_PCT = 15.0     # 网格搜索验证：15%/20%/30%效果相同（5天超时先于止盈触发）
MAX_HOLD_DAYS = 5           # 网格搜索最优（5天≈7天，3天太短）
MAX_POSITIONS = 2           # 网格搜索最优（1只太少机会，3只分散过多）
POSITION_PCT = 0.30         # 固定30%仓位


@dataclass
class TradeRecord:
    """交易记录"""
    stock_name: str
    stock_code: str
    buy_date: str
    buy_price: float
    buy_amount: float    # 买入金额
    shares: int
    sell_date: str = ""
    sell_price: float = 0.0
    sell_amount: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    hold_days: int = 0
    buy_reason: str = ""
    sell_reason: str = ""


@dataclass
class Position:
    """持仓"""
    stock_name: str
    stock_code: str
    buy_date: str
    buy_price: float
    shares: int
    cost: float          # 买入总成本
    hold_days: int = 0


@dataclass
class LayeredBacktestResult:
    """分层回测结果"""
    initial_capital: float = 100_000.0
    final_capital: float = 100_000.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_loss_ratio: float = 0.0
    trades: list = field(default_factory=list)     # list[TradeRecord]
    positions: list = field(default_factory=list)   # 未平仓
    daily_log: list = field(default_factory=list)   # 每日操作日志
    judgments: list = field(default_factory=list)    # Layer 1 每日研判记录


def _code_sentiment_fallback(snapshot: dict) -> str:
    """纯代码的情绪阶段判断 fallback（当 LLM 返回"未知"时使用）

    基于涨跌停数据的简单规则：
    - 涨停 < 20 且 跌停 > 15 → 冰点
    - 涨停 < 30 且 跌停 > 10 → 退潮
    - 涨停 > 70 且 炸板率 < 30% → 高潮
    - 涨停 50-70 → 升温
    - 涨停 30-50 → 修复
    - 其他 → 分歧
    """
    lu = snapshot.get("limit_up_count", 0)
    ld = snapshot.get("limit_down_count", 0)
    blown = snapshot.get("blown_rate", 0)

    if lu < 20 and ld > 15:
        return "冰点"
    if lu < 30 and ld > 10:
        return "退潮"
    if lu > 70 and blown < 30:
        return "高潮"
    if lu >= 50:
        return "升温"
    if lu >= 30:
        return "修复"
    if blown > 50:
        return "退潮"
    return "分歧"


def _get_open_price(db_path: str, date: str, code: str) -> Optional[float]:
    """从 daily_bars 获取开盘价"""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        row = conn.execute(
            "SELECT open FROM daily_bars WHERE date = ? AND code = ?",
            (date, code),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _get_close_price(db_path: str, date: str, code: str) -> Optional[float]:
    """从 daily_bars 获取收盘价"""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        row = conn.execute(
            "SELECT close FROM daily_bars WHERE date = ? AND code = ?",
            (date, code),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def run_layered_backtest(
    data_dir: str,
    start_date: str,
    end_date: str,
    output_dir: str,
    initial_capital: float = 100_000.0,
    on_progress: Optional[Callable] = None,
) -> LayeredBacktestResult:
    """运行三层架构回测

    Args:
        data_dir: 交易数据根目录（含 intraday/intraday.db）
        start_date: 起始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD
        output_dir: 输出目录
        initial_capital: 初始资金
        on_progress: 进度回调 (current, total, date, status)
    """
    os.makedirs(output_dir, exist_ok=True)

    db_path = os.path.join(data_dir, "intraday", "intraday.db")
    concept_db = os.path.join(data_dir, "..", "stock_concept.db")
    # 尝试多个可能的路径
    if not os.path.exists(concept_db):
        concept_db = os.path.join(os.path.dirname(data_dir), "stock_concept.db")
    if not os.path.exists(concept_db):
        concept_db = os.path.expanduser("~/shared/trading/stock_concept.db")

    data_provider = ReviewDataProvider()
    judgment_runner = MarketJudgmentRunner()

    # 发现交易日
    dates = data_provider.discover_dates(data_dir, start_date, end_date)
    if len(dates) < 2:
        raise ValueError(f"交易日不足：{len(dates)} 天（至少需要2天）")

    # 构建 D → D+1 配对
    pairs = list(zip(dates[:-1], dates[1:]))
    print(f"\n{'='*60}")
    print(f"三层架构回测 | {start_date} → {end_date} | {len(pairs)} 个交易日")
    print(f"初始资金: {initial_capital:,.0f} | 仓位: {POSITION_PCT*100:.0f}%/只 | "
          f"止损: {STOP_LOSS_PCT}% | 止盈: +{TAKE_PROFIT_PCT}%")
    print(f"{'='*60}\n")

    # 初始化
    result = LayeredBacktestResult(initial_capital=initial_capital)
    cash = initial_capital
    positions: list[Position] = []
    all_trades: list[TradeRecord] = []

    for idx, (day_d, day_d1) in enumerate(pairs):
        print(f"\n[Day {idx+1}/{len(pairs)}] {day_d} → {day_d1}")

        if on_progress:
            on_progress(idx + 1, len(pairs), day_d, "processing")

        day_log = {"date": day_d, "next_date": day_d1, "actions": []}

        # ── Layer 3a: 先处理卖出（在 Day D 开盘执行） ──
        sells_today = []
        remaining_positions = []

        for pos in positions:
            pos.hold_days += 1

            # 获取当日开盘价计算浮盈亏
            current_price = _get_open_price(db_path, day_d, pos.stock_code)
            if current_price is None:
                # 无行情数据，保持持有
                remaining_positions.append(pos)
                continue

            float_pnl_pct = (current_price - pos.buy_price) / pos.buy_price * 100

            sell_reason = ""
            should_sell = False

            # 止损
            if float_pnl_pct <= STOP_LOSS_PCT:
                should_sell = True
                sell_reason = f"止损: 浮亏{float_pnl_pct:.1f}%超过{STOP_LOSS_PCT}%阈值"
            # 止盈
            elif float_pnl_pct >= TAKE_PROFIT_PCT:
                should_sell = True
                sell_reason = f"止盈: 浮盈{float_pnl_pct:.1f}%超过+{TAKE_PROFIT_PCT}%阈值"
            # 最大持仓天数
            elif pos.hold_days >= MAX_HOLD_DAYS:
                should_sell = True
                sell_reason = f"超时: 持仓{pos.hold_days}天超过{MAX_HOLD_DAYS}天上限"

            if should_sell:
                sell_amount = pos.shares * current_price
                pnl = sell_amount - pos.cost
                pnl_pct = (current_price - pos.buy_price) / pos.buy_price * 100

                trade = TradeRecord(
                    stock_name=pos.stock_name,
                    stock_code=pos.stock_code,
                    buy_date=pos.buy_date,
                    buy_price=pos.buy_price,
                    buy_amount=pos.cost,
                    shares=pos.shares,
                    sell_date=day_d,
                    sell_price=current_price,
                    sell_amount=sell_amount,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    hold_days=pos.hold_days,
                    sell_reason=sell_reason,
                )
                all_trades.append(trade)
                cash += sell_amount
                sells_today.append(trade)
                print(f"  [卖出] {pos.stock_name} @ {current_price:.2f} "
                      f"({pnl_pct:+.2f}%) — {sell_reason}")
                day_log["actions"].append({
                    "type": "sell", "name": pos.stock_name,
                    "price": current_price, "pnl_pct": pnl_pct,
                    "reason": sell_reason,
                })
            else:
                remaining_positions.append(pos)

        positions = remaining_positions

        # ── Layer 1: LLM 市场研判 ──
        try:
            snapshot = data_provider.load_market_snapshot(data_dir, day_d)
            t0 = time.time()
            judgment = judgment_runner.run(snapshot)
            elapsed = time.time() - t0

            # Fallback: LLM 返回空板块时，从市场数据直接提取 Top 2 行业
            if not judgment.get("top_sectors"):
                sector_dist = snapshot.get("sector_distribution", {})
                if sector_dist:
                    top2 = sorted(sector_dist.items(), key=lambda x: -x[1])[:2]
                    judgment["top_sectors"] = [s[0] for s in top2]
                    print(f"  [Layer 1] LLM 未返回板块，fallback 到涨停分布: {judgment['top_sectors']}")

            # Fallback: 情绪阶段为"未知"时，用简单规则判断
            if judgment.get("sentiment_phase") == "未知":
                judgment["sentiment_phase"] = _code_sentiment_fallback(snapshot)
                # 根据情绪阶段更新 action_gate
                phase = judgment["sentiment_phase"]
                if phase in ("退潮", "冰点"):
                    judgment["action_gate"] = "空仓"
                elif phase in ("修复", "升温", "高潮"):
                    judgment["action_gate"] = "可买入"
                else:
                    judgment["action_gate"] = "谨慎"

            print(f"  [Layer 1] {judgment['sentiment_phase']} | "
                  f"{judgment['market_type']} | "
                  f"板块: {judgment['top_sectors']} | "
                  f"门控: {judgment['action_gate']} ({elapsed:.1f}s)")
            result.judgments.append({"date": day_d, **judgment})
        except Exception as e:
            print(f"  [Layer 1 失败] {e}")
            # 完全 fallback 到代码规则
            snapshot = data_provider.load_market_snapshot(data_dir, day_d)
            sector_dist = snapshot.get("sector_distribution", {})
            top2 = sorted(sector_dist.items(), key=lambda x: -x[1])[:2] if sector_dist else []
            phase = _code_sentiment_fallback(snapshot)
            if phase in ("退潮", "冰点"):
                gate = "空仓"
            elif phase in ("修复", "升温"):
                gate = "可买入"
            else:
                gate = "谨慎"
            judgment = {
                "sentiment_phase": phase,
                "market_type": "震荡日",
                "top_sectors": [s[0] for s in top2],
                "action_gate": gate,
            }

        # Layer 1 情绪门控：空仓信号 → 卖出所有持仓
        if judgment["action_gate"] == "空仓" and positions:
            print(f"  [Layer 1 门控] 空仓信号，清仓 {len(positions)} 只持仓")
            for pos in positions:
                current_price = _get_open_price(db_path, day_d, pos.stock_code)
                if current_price is None:
                    current_price = pos.buy_price  # fallback

                sell_amount = pos.shares * current_price
                pnl = sell_amount - pos.cost
                pnl_pct = (current_price - pos.buy_price) / pos.buy_price * 100

                trade = TradeRecord(
                    stock_name=pos.stock_name, stock_code=pos.stock_code,
                    buy_date=pos.buy_date, buy_price=pos.buy_price,
                    buy_amount=pos.cost, shares=pos.shares,
                    sell_date=day_d, sell_price=current_price,
                    sell_amount=sell_amount, pnl=pnl, pnl_pct=pnl_pct,
                    hold_days=pos.hold_days,
                    sell_reason=f"Layer1情绪门控: {judgment['sentiment_phase']}→空仓",
                )
                all_trades.append(trade)
                cash += sell_amount
                print(f"  [清仓] {pos.stock_name} @ {current_price:.2f} ({pnl_pct:+.2f}%)")
            positions = []

        day_log["judgment"] = judgment

        # ── Layer 2: 量化选股 ──
        available_slots = MAX_POSITIONS - len(positions)
        if available_slots <= 0 or judgment["action_gate"] == "空仓":
            if judgment["action_gate"] != "空仓":
                print(f"  [Layer 2] 跳过（已满仓 {len(positions)}/{MAX_POSITIONS}）")
            else:
                print(f"  [Layer 2] 跳过（空仓信号）")
            candidates = []
        else:
            try:
                candidates = screen_stocks(
                    date=day_d,
                    top_sectors=judgment.get("top_sectors", []),
                    action_gate=judgment["action_gate"],
                    intraday_db=db_path,
                    concept_db=concept_db,
                    max_picks=available_slots,
                )
                if candidates:
                    print(f"  [Layer 2] {format_screening_result(candidates)}")
                else:
                    print(f"  [Layer 2] 无符合条件的候选标的")
            except Exception as e:
                print(f"  [Layer 2 失败] {e}")
                candidates = []

        # 排除已持仓的标的
        held_codes = {p.stock_code for p in positions}
        candidates = [c for c in candidates if c.code not in held_codes]

        day_log["candidates"] = [
            {"name": c.name, "code": c.code, "score": c.score}
            for c in candidates
        ]

        # ── Layer 3b: 执行买入（在 Day D+1 开盘执行） ──
        for candidate in candidates:
            if len(positions) >= MAX_POSITIONS:
                break

            buy_price = _get_open_price(db_path, day_d1, candidate.code)
            if buy_price is None or buy_price <= 0:
                print(f"  [买入跳过] {candidate.name} — 无 {day_d1} 开盘价数据")
                continue

            # 计算买入金额和股数
            total_value = cash + sum(p.cost for p in positions)
            buy_amount = total_value * POSITION_PCT
            if buy_amount > cash:
                buy_amount = cash  # 不超过可用现金

            shares = int(buy_amount / buy_price / 100) * 100  # 整手买入
            if shares <= 0:
                print(f"  [买入跳过] {candidate.name} — 资金不足")
                continue

            actual_cost = shares * buy_price
            cash -= actual_cost

            pos = Position(
                stock_name=candidate.name,
                stock_code=candidate.code,
                buy_date=day_d1,
                buy_price=buy_price,
                shares=shares,
                cost=actual_cost,
            )
            positions.append(pos)

            buy_reason = (
                f"L2评分{candidate.score:.0f}分 | "
                f"{candidate.board_count}连板 | "
                f"首封{candidate.first_limit_time} | "
                f"炸板{candidate.blown_count}次 | "
                f"成交{candidate.amount/1e8:.1f}亿 | "
                f"板块: {', '.join(judgment.get('top_sectors', []))}"
            )

            print(f"  [买入] {candidate.name}({candidate.code}) @ {buy_price:.2f} "
                  f"× {shares}股 = {actual_cost:.0f}元 — {buy_reason}")

            day_log["actions"].append({
                "type": "buy", "name": candidate.name, "code": candidate.code,
                "price": buy_price, "shares": shares, "amount": actual_cost,
                "reason": buy_reason,
            })

        # 记录日终状态
        total_value = cash + sum(p.cost for p in positions)
        day_log["end_of_day"] = {
            "cash": cash,
            "positions": len(positions),
            "total_value": total_value,
            "position_names": [p.stock_name for p in positions],
        }
        result.daily_log.append(day_log)
        print(f"  [日终] 现金 {cash:,.0f} | 持仓 {len(positions)} 只 | "
              f"总资产 {total_value:,.0f}")

        # 保存每日验证数据
        verify_path = os.path.join(output_dir, f"{day_d}_verify.json")
        with open(verify_path, "w", encoding="utf-8") as f:
            json.dump(day_log, f, ensure_ascii=False, indent=2)

    # ── 回测结束：处理未平仓 ──
    if positions:
        last_date = dates[-1]
        print(f"\n[回测结束] 强制平仓 {len(positions)} 只持仓 @ {last_date}")
        for pos in positions:
            close_price = _get_close_price(db_path, last_date, pos.stock_code)
            if close_price is None:
                close_price = pos.buy_price

            sell_amount = pos.shares * close_price
            pnl = sell_amount - pos.cost
            pnl_pct = (close_price - pos.buy_price) / pos.buy_price * 100

            trade = TradeRecord(
                stock_name=pos.stock_name, stock_code=pos.stock_code,
                buy_date=pos.buy_date, buy_price=pos.buy_price,
                buy_amount=pos.cost, shares=pos.shares,
                sell_date=last_date, sell_price=close_price,
                sell_amount=sell_amount, pnl=pnl, pnl_pct=pnl_pct,
                hold_days=pos.hold_days,
                sell_reason="回测结束强制平仓",
            )
            all_trades.append(trade)
            cash += sell_amount

    # ── 汇总结果 ──
    result.trades = all_trades
    result.final_capital = cash
    result.total_pnl = cash - initial_capital
    result.total_pnl_pct = (cash - initial_capital) / initial_capital * 100
    result.trade_count = len(all_trades)

    wins = [t for t in all_trades if t.pnl_pct > 0]
    losses = [t for t in all_trades if t.pnl_pct <= 0]
    result.win_count = len(wins)
    result.loss_count = len(losses)
    result.win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0
    result.avg_win_pct = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0
    result.avg_loss_pct = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0
    result.profit_loss_ratio = (
        abs(result.avg_win_pct / result.avg_loss_pct)
        if result.avg_loss_pct != 0 else 0
    )

    # ── 输出交割单 ──
    _write_settlement(result, output_dir)
    _write_summary_json(result, output_dir)

    print(f"\n{'='*60}")
    print(f"回测完成 | {result.trade_count} 笔交易 | "
          f"收益 {result.total_pnl:+,.0f} ({result.total_pnl_pct:+.2f}%)")
    print(f"胜率 {result.win_rate:.1f}% | 盈亏比 {result.profit_loss_ratio:.2f}")
    print(f"{'='*60}")

    return result


def _write_settlement(result: LayeredBacktestResult, output_dir: str):
    """输出交割单 Markdown"""
    lines = [
        "# 回测交割单（三层架构 | {:.0f}万起步，{:.0f}%仓位）\n".format(
            result.initial_capital / 10000, POSITION_PCT * 100),
        "## 一、收益概况\n",
        f"- 初始资金：{result.initial_capital:,.0f}",
        f"- 期末资金：{result.final_capital:,.0f}",
        f"- 总收益：{result.total_pnl:+,.0f}（{result.total_pnl_pct:+.2f}%）",
        f"- 交易笔数：{result.trade_count}（{result.win_count}胜{result.loss_count}负）",
        f"- 胜率：{result.win_rate:.1f}%",
        f"- 平均盈利：{result.avg_win_pct:+.2f}%",
        f"- 平均亏损：{result.avg_loss_pct:+.2f}%",
        f"- 盈亏比：{result.profit_loss_ratio:.2f}",
        f"- 止损阈值：{STOP_LOSS_PCT}% | 止盈阈值：+{TAKE_PROFIT_PCT}%",
        f"- 最大持仓天数：{MAX_HOLD_DAYS} | 最大同时持仓：{MAX_POSITIONS}",
        "\n## 二、逐笔交割单\n",
    ]

    for i, trade in enumerate(result.trades, 1):
        lines.append(f"### {i}. {trade.stock_name}（{trade.stock_code}）"
                     f"{'  ✅' if trade.pnl_pct > 0 else '  ❌'}\n")
        lines.append(f"- 买入：{trade.buy_date} @ {trade.buy_price:.2f}，"
                     f"{trade.shares}股 = {trade.buy_amount:,.0f}元")
        lines.append(f"- 卖出：{trade.sell_date} @ {trade.sell_price:.2f}，"
                     f"= {trade.sell_amount:,.0f}元")
        lines.append(f"- 盈亏：{trade.pnl:+,.0f}（{trade.pnl_pct:+.2f}%）| "
                     f"持仓 {trade.hold_days} 天")
        if trade.buy_reason:
            lines.append(f"- 买入原因：{trade.buy_reason}")
        lines.append(f"- 卖出原因：{trade.sell_reason}")
        lines.append("")

    path = os.path.join(output_dir, "交割单.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n交割单已写入: {path}")


def _write_summary_json(result: LayeredBacktestResult, output_dir: str):
    """输出汇总 JSON"""
    summary = {
        "mode": "layered_v1",
        "initial_capital": result.initial_capital,
        "final_capital": result.final_capital,
        "total_pnl": result.total_pnl,
        "total_pnl_pct": result.total_pnl_pct,
        "trade_count": result.trade_count,
        "win_count": result.win_count,
        "loss_count": result.loss_count,
        "win_rate": result.win_rate,
        "avg_win_pct": result.avg_win_pct,
        "avg_loss_pct": result.avg_loss_pct,
        "profit_loss_ratio": result.profit_loss_ratio,
        "params": {
            "stop_loss_pct": STOP_LOSS_PCT,
            "take_profit_pct": TAKE_PROFIT_PCT,
            "max_hold_days": MAX_HOLD_DAYS,
            "max_positions": MAX_POSITIONS,
            "position_pct": POSITION_PCT,
        },
        "trades": [
            {
                "name": t.stock_name, "code": t.stock_code,
                "buy_date": t.buy_date, "sell_date": t.sell_date,
                "buy_price": t.buy_price, "sell_price": t.sell_price,
                "pnl_pct": t.pnl_pct, "hold_days": t.hold_days,
                "buy_reason": t.buy_reason, "sell_reason": t.sell_reason,
            }
            for t in result.trades
        ],
        "judgments": result.judgments,
    }

    path = os.path.join(output_dir, "summary.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
