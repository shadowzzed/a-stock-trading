"""回测报告生成 — 收益率驱动"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import BacktestResult
    from ..experience.store import ExperienceStore


def generate_summary(
    results: list,
    output_dir: str,
    exp_store=None,
    tracker=None,
) -> dict:
    """生成回测汇总报告（JSON + Markdown）"""
    completed = [r for r in results if r.status == "completed"]

    if not completed:
        summary = {
            "version": "v6",
            "mode": "pnl_driven",
            "status": "no_completed_runs",
            "results": [_result_to_dict(r) for r in results],
        }
    else:
        # 收益率统计
        pnl_list = [r.avg_pnl_pct for r in completed if r.avg_pnl_pct != 0]
        hit_rates = [r.hit_rate for r in completed if r.hit_rate > 0]
        total_recs = sum(len(r.recommendations) for r in completed)

        avg_pnl = round(sum(pnl_list) / len(pnl_list), 2) if pnl_list else 0
        avg_hit = round(sum(hit_rates) / len(hit_rates), 1) if hit_rates else 0

        # 按日统计
        daily_stats = []
        for r in completed:
            buys = [rec for rec in r.recommendations if rec.action == "买入"]
            daily_stats.append({
                "date": r.day_d,
                "next_date": r.day_d1,
                "avg_pnl_pct": r.avg_pnl_pct,
                "hit_rate": r.hit_rate,
                "buy_count": len(buys),
                "injected": r.injected_lessons,
                "scenario": r.scenario,
            })

        summary = {
            "version": "v6",
            "mode": "pnl_driven",
            "status": "completed",
            "total_days": len(completed),
            "avg_pnl_pct": avg_pnl,
            "avg_hit_rate": avg_hit,
            "total_recommendations": total_recs,
            "experience_store_stats": exp_store.stats if exp_store else {},
            "daily_stats": daily_stats,
            "results": [_result_to_dict(r) for r in results],
        }

    # 汇总数据泄露审计
    leak_audit = _aggregate_leak_audit(output_dir)
    summary["data_leak_audit"] = leak_audit

    # 保存 JSON
    summary_path = os.path.join(output_dir, "summary_v6.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 生成人可读报告
    report_path = os.path.join(output_dir, "回测报告_v6.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(format_report(summary, exp_store))

    print("\n汇总已保存到 {}".format(summary_path))
    print("报告已保存到 {}".format(report_path))

    return summary


def format_report(summary: dict, exp_store=None) -> str:
    """格式化收益率驱动报告"""
    if summary["status"] != "completed":
        return "# 回测 v6 未完成\n\n无有效结果。"

    lines = [
        "# 短线 Agent 历史回测报告 v6（收益率驱动）",
        "",
        "## 总体表现",
        "",
        "- 回测交易日数：{}".format(summary["total_days"]),
        "- 日均推荐收益率：{:+.2f}%".format(summary["avg_pnl_pct"]),
        "- 平均命中率：{:.1f}%".format(summary["avg_hit_rate"]),
        "- 推荐标的总数：{}".format(summary["total_recommendations"]),
    ]

    # 经验库统计
    ess = summary.get("experience_store_stats", {})
    if ess:
        lines.extend([
            "",
            "## 经验库状态",
            "",
            "- 总经验数：{}".format(ess.get("total", 0)),
            "- 平均置信度：{}".format(ess.get("avg_confidence", 0)),
        ])

    # 数据泄露审计
    leak = summary.get("data_leak_audit", {})
    if leak.get("clean", True):
        lines.extend(["", "## 数据泄露审计", "", "✅ 回测期间无越界数据访问，结果可信。"])
    else:
        lines.extend([
            "", "## 数据泄露审计",
            "", "⚠️ 回测期间检测到 {} 次越界数据访问（已被拦截）：".format(leak["total_blocked"]),
        ])
        for v in leak.get("violated_days", []):
            lines.append("- {} — {} 次拦截".format(v["date"], v["blocked_count"]))

    # 逐日统计
    lines.extend([
        "",
        "## 逐日收益",
        "",
        "| 分析日 | 验证日 | 推荐收益 | 命中率 | 买入数 | 注入教训 | 场景 |",
        "|--------|--------|---------|--------|--------|---------|------|",
    ])
    for day in summary["daily_stats"]:
        scenario_desc = ", ".join(
            str(v) for v in day.get("scenario", {}).values() if v
        )[:30]
        lines.append("| {} | {} | {:+.2f}% | {:.0f}% | {} | {} | {} |".format(
            day["date"], day["next_date"],
            day["avg_pnl_pct"], day["hit_rate"],
            day["buy_count"], day.get("injected", 0),
            scenario_desc,
        ))

    return "\n".join(lines)


def _aggregate_leak_audit(output_dir: str) -> dict:
    """从各日 verify.json 汇总数据泄露审计结果。"""
    import glob
    total_blocked = 0
    violated_days = []

    for vf in sorted(glob.glob(os.path.join(output_dir, "*_verify.json"))):
        try:
            with open(vf, "r", encoding="utf-8") as f:
                data = json.load(f)
            audit = data.get("data_leak_audit", {})
            blocked = audit.get("blocked_count", 0)
            if blocked > 0:
                total_blocked += blocked
                violated_days.append({
                    "date": data.get("day_d", ""),
                    "blocked_count": blocked,
                    "details": audit.get("blocked_details", []),
                })
        except (json.JSONDecodeError, IOError):
            continue

    return {
        "clean": total_blocked == 0,
        "total_blocked": total_blocked,
        "violated_days": violated_days,
    }


def _result_to_dict(r) -> dict:
    return {
        "day_d": r.day_d,
        "day_d1": r.day_d1,
        "status": r.status,
        "scenario": r.scenario,
        "injected_lessons": r.injected_lessons,
        "avg_pnl_pct": r.avg_pnl_pct,
        "hit_rate": r.hit_rate,
        "recommendations": [
            {
                "stock": rec.stock,
                "action": rec.action,
                "buy_condition": rec.buy_condition,
                "position": rec.position,
                "next_pct_chg": rec.next_pct_chg,
                "pnl_pct": rec.pnl_pct,
                "is_limit_up": rec.is_limit_up,
                "is_limit_down": rec.is_limit_down,
            }
            for rec in r.recommendations
        ],
        "error": r.error,
    }


def generate_settlement_report(
    tracker,
    output_dir: str,
    initial_capital: float = 1_000_000.0,
    display_capital: float = 100_000.0,
) -> str:
    """生成逐笔交割单 + 收益报告。

    Args:
        tracker: BacktestPortfolioTracker 实例（含 closed_trades）
        output_dir: 输出目录
        initial_capital: 实际回测初始资金
        display_capital: 展示用初始资金（如10万）

    Returns:
        生成的报告文件路径
    """
    scale = display_capital / initial_capital
    closed = tracker.closed_trades
    open_positions = tracker.positions

    # ── 用展示资金计算复利交割单 ──
    equity = display_capital
    trade_records = []

    for t in closed:
        position = equity * 0.3
        pnl_pct = t["pnl_pct"]
        pnl_amount = position * pnl_pct / 100
        equity += pnl_amount
        trade_records.append({
            "buy_date": t["buy_date"],
            "sell_date": t["sell_date"],
            "name": t["name"],
            "position": round(position),
            "pnl_pct": pnl_pct,
            "pnl_amount": round(pnl_amount),
            "equity": round(equity),
            "buy_reason": t.get("buy_reason", ""),
            "sell_reason": t.get("reason", ""),
            "hold_days": t.get("hold_days", 0),
        })

    # ── 生成 Markdown ──
    lines = [
        "# 回测交割单（{}万起步，30%仓位，复利模式）".format(int(display_capital / 10000)),
        "",
        "## 一、收益概况",
        "",
        "- 初始资金：{:,.0f}".format(display_capital),
    ]

    # 计算已平仓后资金
    closed_equity = equity
    closed_pnl = closed_equity - display_capital
    closed_pnl_pct = closed_pnl / display_capital * 100

    # 统计已平仓
    wins = [t for t in trade_records if t["pnl_pct"] > 0]
    losses = [t for t in trade_records if t["pnl_pct"] < 0]
    win_rate = len(wins) / len(trade_records) * 100 if trade_records else 0
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0

    lines.extend([
        "- 已平仓资金：{:,.0f}".format(closed_equity),
        "- 已平仓收益：{:>+,.0f}（{:+.2f}%）".format(closed_pnl, closed_pnl_pct),
        "- 已平仓笔数：{}笔（{}胜{}负）".format(len(trade_records), len(wins), len(losses)),
        "- 胜率：{:.1f}%".format(win_rate),
        "- 平均盈利：{:+.2f}%".format(avg_win),
        "- 平均亏损：{:+.2f}%".format(avg_loss),
    ])

    if open_positions:
        open_value = sum(p.get("cost", 0) * scale for p in open_positions)
        lines.extend([
            "- 剩余持仓：{}只，市值约{:,.0f}".format(len(open_positions), open_value),
            "- 持仓标的：{}".format("、".join(p["name"] for p in open_positions)),
        ])

    # ── 逐笔交割单 ──
    lines.extend([
        "",
        "## 二、逐笔交割单",
        "",
    ])

    for i, t in enumerate(trade_records, 1):
        buy_reason = t.get("buy_reason", "")
        sell_reason = t.get("sell_reason", "")
        lines.extend([
            "### {}. {}（{}→{}）".format(i, t["name"], t["buy_date"], t["sell_date"]),
            "",
            "- 仓位金额：{:,}（本金×30%）".format(t["position"]),
            "- 盈亏：{:+,}（{:+.2f}%）".format(t["pnl_amount"], t["pnl_pct"]),
            "- 持仓天数：{}天".format(t.get("hold_days", 0)),
            "- **买入原因**：{}".format(buy_reason or "未记录"),
            "- **卖出原因**：{}".format(sell_reason or "未记录"),
            "",
        ])

    # 未平仓
    if open_positions:
        lines.extend([
            "",
            "## 三、未平仓持仓",
            "",
        ])
        for p in open_positions:
            pos_amount = equity * 0.3
            lines.extend([
                "### {}（买入日:{}）".format(p["name"], p["buy_date"]),
                "",
                "- 仓位金额：{:,}".format(round(pos_amount)),
                "- **买入原因**：{}".format(p.get("buy_reason", "") or "未记录"),
                "",
            ])

    # 保存文件
    report_path = os.path.join(output_dir, "交割单.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # 同时保存 JSON
    json_path = os.path.join(output_dir, "交割单.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "initial_capital": display_capital,
            "closed_equity": round(closed_equity),
            "closed_pnl_pct": round(closed_pnl_pct, 2),
            "closed_pnl_amount": round(closed_pnl),
            "win_rate": round(win_rate, 1),
            "total_trades": len(trade_records),
            "wins": len(wins),
            "losses": len(losses),
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "open_positions": len(open_positions),
            "trades": trade_records,
        }, f, ensure_ascii=False, indent=2)

    print("交割单已保存到 {}（{}笔已平仓，{}笔持有中）".format(
        report_path, len(trade_records), len(open_positions)))

    return report_path
