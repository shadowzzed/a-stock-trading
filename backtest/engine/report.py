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
