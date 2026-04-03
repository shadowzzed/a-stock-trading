"""评估报告 — 计算交易模拟的各项指标"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeEvaluation:
    """交易模拟评估结果"""
    # 基础统计
    total_signals: int = 0
    total_buy_attempts: int = 0      # 排除观望后的买入意图
    total_executed: int = 0          # 实际成交
    execution_rate: float = 0.0      # 成交率
    # 胜率
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    # 盈亏
    avg_pnl_pct: float = 0.0
    median_pnl_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0       # 盈亏比
    max_single_win: float = 0.0
    max_single_loss: float = 0.0
    total_pnl_amount: float = 0.0
    # 收益曲线
    initial_capital: float = 0.0
    final_value: float = 0.0
    total_return_pct: float = 0.0
    annualized_return: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_duration: int = 0
    # 分组统计
    by_action_type: dict = field(default_factory=dict)
    by_sentiment_phase: dict = field(default_factory=dict)
    # 净值曲线
    equity_curve: list = field(default_factory=list)


def evaluate(
    trades: list[dict],
    snapshots: list[dict],
    initial_capital: float = 1_000_000.0,
) -> TradeEvaluation:
    """评估交易模拟结果

    Args:
        trades: TradeSimulator.get_results() 的输出
        snapshots: TradeSimulator.get_snapshots() 的输出
        initial_capital: 初始资金
    """
    ev = TradeEvaluation(initial_capital=initial_capital)

    # 分类交易
    buy_records = [t for t in trades if t.get("buy_intended", False) or t.get("buy_executed", False)]
    sell_records = [t for t in trades if t.get("sell_price") is not None]

    # 统计信号
    ev.total_signals = len(trades)
    ev.total_buy_attempts = len([t for t in trades if t.get("action_type") and t["action_type"] != "观望"])
    ev.total_executed = len([t for t in buy_records if t.get("buy_executed")])
    ev.execution_rate = ev.total_executed / max(ev.total_buy_attempts, 1) * 100

    # 盈亏分析
    closed = [t for t in trades if t.get("pnl_pct") is not None]
    if closed:
        pnls = [t["pnl_pct"] for t in closed]
        ev.win_count = sum(1 for p in pnls if p > 0)
        ev.loss_count = sum(1 for p in pnls if p <= 0)
        ev.win_rate = ev.win_count / len(closed) * 100

        ev.avg_pnl_pct = sum(pnls) / len(pnls)
        sorted_pnls = sorted(pnls)
        n = len(sorted_pnls)
        ev.median_pnl_pct = sorted_pnls[n // 2]

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        ev.avg_win_pct = sum(wins) / len(wins) if wins else 0
        ev.avg_loss_pct = sum(losses) / len(losses) if losses else 0
        ev.profit_factor = abs(ev.avg_win_pct / ev.avg_loss_pct) if ev.avg_loss_pct != 0 else float("inf")

        ev.max_single_win = max(pnls)
        ev.max_single_loss = min(pnls)
        ev.total_pnl_amount = sum(t.get("pnl_amount", 0) or 0 for t in closed)

    # 收益曲线
    if snapshots:
        values = [s["total_value"] for s in snapshots]
        ev.final_value = values[-1]
        ev.total_return_pct = (ev.final_value - initial_capital) / initial_capital * 100

        # 年化收益（按交易日计算）
        trading_days = len(snapshots)
        if trading_days > 1:
            daily_return = (ev.final_value / initial_capital) ** (1 / trading_days) - 1
            ev.annualized_return = ((1 + daily_return) ** 250 - 1) * 100

        # 最大回撤
        ev.max_drawdown_pct, ev.max_drawdown_duration = _calc_max_drawdown(values)

        # 净值曲线
        ev.equity_curve = [
            {"date": s["date"], "value": s["total_value"], "daily_return": s["daily_return"]}
            for s in snapshots
        ]

    # 按操作类型分组
    ev.by_action_type = _group_by(closed, "action_type")

    return ev


def save_evaluation(
    evaluation: TradeEvaluation,
    output_dir: str,
    prefix: str = "trade_sim",
):
    """保存评估结果为 JSON + Markdown"""
    os.makedirs(output_dir, exist_ok=True)

    # JSON
    data = {
        "summary": {
            "total_signals": evaluation.total_signals,
            "total_buy_attempts": evaluation.total_buy_attempts,
            "total_executed": evaluation.total_executed,
            "execution_rate": round(evaluation.execution_rate, 1),
            "win_count": evaluation.win_count,
            "loss_count": evaluation.loss_count,
            "win_rate": round(evaluation.win_rate, 1),
            "avg_pnl_pct": round(evaluation.avg_pnl_pct, 2),
            "profit_factor": round(evaluation.profit_factor, 2),
            "total_return_pct": round(evaluation.total_return_pct, 2),
            "max_drawdown_pct": round(evaluation.max_drawdown_pct, 2),
            "initial_capital": evaluation.initial_capital,
            "final_value": round(evaluation.final_value, 2),
        },
        "by_action_type": evaluation.by_action_type,
        "equity_curve": evaluation.equity_curve,
    }
    json_path = os.path.join(output_dir, f"{prefix}_eval.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Markdown
    md_path = os.path.join(output_dir, f"{prefix}_报告.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_format_report(evaluation))

    print("交易模拟评估已保存到 {} 和 {}".format(json_path, md_path))


def _calc_max_drawdown(values: list[float]) -> tuple[float, int]:
    """计算最大回撤（%）和持续天数"""
    if not values:
        return 0.0, 0

    peak = values[0]
    max_dd = 0.0
    max_duration = 0
    dd_start = 0

    for i, v in enumerate(values):
        if v > peak:
            peak = v
            dd_start = i
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd
            max_duration = i - dd_start

    return round(max_dd, 2), max_duration


def _group_by(trades: list[dict], key: str) -> dict:
    """按字段分组统计"""
    groups: dict[str, list] = {}
    for t in trades:
        k = t.get(key, "unknown")
        groups.setdefault(k, []).append(t)

    result = {}
    for k, items in groups.items():
        pnls = [t["pnl_pct"] for t in items if t.get("pnl_pct") is not None]
        if not pnls:
            continue
        wins = [p for p in pnls if p > 0]
        result[k] = {
            "count": len(pnls),
            "win_rate": round(len(wins) / len(pnls) * 100, 1),
            "avg_pnl_pct": round(sum(pnls) / len(pnls), 2),
        }
    return result


def _format_report(ev: TradeEvaluation) -> str:
    """格式化 Markdown 评估报告"""
    lines = [
        "# 交易模拟评估报告",
        "",
        "## 总体表现",
        "",
        "- 初始资金：{:.0f}".format(ev.initial_capital),
        "- 期末净值：{:.2f}".format(ev.final_value),
        "- 累计收益率：{:+.2f}%".format(ev.total_return_pct),
        "- 年化收益率：{:+.1f}%".format(ev.annualized_return),
        "- 最大回撤：{:.2f}%（持续{}天）".format(ev.max_drawdown_pct, ev.max_drawdown_duration),
        "",
        "## 交易统计",
        "",
        "| 指标 | 值 |",
        "|------|-----|",
        "| 总信号数 | {} |".format(ev.total_signals),
        "| 买入意图 | {} |".format(ev.total_buy_attempts),
        "| 实际成交 | {} |".format(ev.total_executed),
        "| 成交率 | {:.1f}% |".format(ev.execution_rate),
        "| 胜率 | {:.1f}%（{}胜{}负）|".format(ev.win_rate, ev.win_count, ev.loss_count),
        "| 平均盈亏 | {:+.2f}% |".format(ev.avg_pnl_pct),
        "| 盈亏比 | {:.2f} |".format(ev.profit_factor),
        "| 单笔最大盈利 | {:+.2f}% |".format(ev.max_single_win),
        "| 单笔最大亏损 | {:+.2f}% |".format(ev.max_single_loss),
    ]

    # 按操作类型分组
    if ev.by_action_type:
        lines.extend([
            "",
            "## 按操作类型",
            "",
            "| 操作类型 | 次数 | 胜率 | 平均盈亏% |",
            "|---------|------|------|----------|",
        ])
        for action, stats in ev.by_action_type.items():
            lines.append("| {} | {} | {:.1f}% | {:+.2f}% |".format(
                action, stats["count"], stats["win_rate"], stats["avg_pnl_pct"]))

    # 净值曲线
    if ev.equity_curve:
        lines.extend([
            "",
            "## 逐日净值",
            "",
            "| 日期 | 净值 | 当日收益 |",
            "|------|------|---------|",
        ])
        for day in ev.equity_curve:
            lines.append("| {} | {:.2f} | {:+.2f}% |".format(
                day["date"], day["value"], day["daily_return"] * 100))

    return "\n".join(lines)
