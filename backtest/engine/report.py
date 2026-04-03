"""回测报告生成"""

from __future__ import annotations

import json
import os
from typing import Optional

from .core import BacktestResult
from ..experience.store import ExperienceStore
from ..experience.tracker import LessonTracker


def generate_summary(
    results: list[BacktestResult],
    output_dir: str,
    exp_store: ExperienceStore,
    tracker: LessonTracker,
) -> dict:
    """生成回测汇总报告（JSON + Markdown）"""
    completed = [r for r in results if r.status == "completed"]

    if not completed:
        summary = {
            "version": "v6",
            "status": "no_completed_runs",
            "results": [_result_to_dict(r) for r in results],
        }
    else:
        dims = ["sentiment", "sector", "leader", "strategy"]
        dim_scores = {d: [] for d in dims}
        for r in completed:
            for d in dims:
                s = r.scores.get(d, {})
                if isinstance(s, dict) and "score" in s:
                    dim_scores[d].append(s["score"])

        avg_scores = {}
        for d in dims:
            scores = dim_scores[d]
            avg_scores[d] = round(sum(scores) / len(scores), 1) if scores else 0

        total_avg = round(
            sum(r.total_score for r in completed) / len(completed), 1
        )

        # 统计注入效果
        with_injection = [r for r in completed if r.injected_lessons > 0]
        without_injection = [r for r in completed if r.injected_lessons == 0]

        avg_with = (
            round(sum(r.total_score for r in with_injection) / len(with_injection), 1)
            if with_injection else 0
        )
        avg_without = (
            round(sum(r.total_score for r in without_injection) / len(without_injection), 1)
            if without_injection else 0
        )

        all_lessons = []
        all_wrong = []
        for r in completed:
            all_lessons.extend(r.key_lessons)
            all_wrong.extend(r.what_was_wrong)

        summary = {
            "version": "v6",
            "status": "completed",
            "total_days": len(completed),
            "avg_total_score": total_avg,
            "max_possible_score": 20,
            "avg_scores_by_dimension": avg_scores,
            "injection_effect": {
                "avg_with_injection": avg_with,
                "avg_without_injection": avg_without,
                "days_with_injection": len(with_injection),
                "days_without_injection": len(without_injection),
            },
            "experience_store_stats": exp_store.stats,
            "lesson_tracker_stats": {
                "active": len(tracker.get_active_lessons()),
                "deprecated": len(tracker.get_deprecated_lessons()),
                "promotable": len(tracker.get_promotable_lessons()),
                "top_effective": tracker.get_effectiveness_ranking(5),
            },
            "score_by_day": [
                {
                    "date": r.day_d,
                    "next": r.day_d1,
                    "score": r.total_score,
                    "injected": r.injected_lessons,
                    "scenario": r.scenario,
                }
                for r in completed
            ],
            "all_lessons": all_lessons,
            "what_was_wrong": all_wrong,
            "results": [_result_to_dict(r) for r in results],
        }

    # 保存 JSON
    summary_path = os.path.join(output_dir, "summary_v6.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 生成人可读报告
    report_path = os.path.join(output_dir, "回测报告_v6.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(format_report(summary, exp_store, tracker))

    print("\n汇总已保存到 {}".format(summary_path))
    print("报告已保存到 {}".format(report_path))

    return summary


def format_report(
    summary: dict,
    exp_store: ExperienceStore,
    tracker: LessonTracker,
) -> str:
    """格式化 v6 报告"""
    if summary["status"] != "completed":
        return "# 回测 v6 未完成\n\n无有效结果。"

    dim_names = {
        "sentiment": "情绪周期",
        "sector": "板块轮动",
        "leader": "龙头辨识",
        "strategy": "策略实效",
    }

    lines = [
        "# 短线 Agent 历史回测报告 v6（经验驱动）",
        "",
        "## 总体表现",
        "",
        "- 回测交易日数：{}".format(summary["total_days"]),
        "- 平均总分：{} / {} ({:.0f}%)".format(
            summary["avg_total_score"],
            summary["max_possible_score"],
            summary["avg_total_score"] / summary["max_possible_score"] * 100,
        ),
    ]

    # 注入效果对比
    ie = summary.get("injection_effect", {})
    if ie.get("days_with_injection", 0) > 0:
        lines.extend([
            "",
            "## 教训注入效果",
            "",
            "| 状态 | 天数 | 平均分 |",
            "|------|------|--------|",
            "| 有教训注入 | {} | {} |".format(
                ie["days_with_injection"], ie["avg_with_injection"]),
            "| 无教训注入 | {} | {} |".format(
                ie["days_without_injection"], ie["avg_without_injection"]),
        ])
        if ie["avg_with_injection"] > 0 and ie["avg_without_injection"] > 0:
            diff = ie["avg_with_injection"] - ie["avg_without_injection"]
            lines.append("")
            if diff > 0:
                lines.append("**结论**：教训注入平均提升 {:.1f} 分".format(diff))
            else:
                lines.append("**结论**：教训注入效果不显著（差 {:.1f} 分）".format(diff))

    # 各维度得分
    lines.extend([
        "",
        "## 各维度平均分",
        "",
        "| 维度 | 平均分 | 满分 | 评价 |",
        "|------|--------|------|------|",
    ])
    for dim, score in summary["avg_scores_by_dimension"].items():
        level = "优" if score >= 4 else "良" if score >= 3 else "中" if score >= 2 else "差"
        lines.append("| {} | {} | 5 | {} |".format(dim_names.get(dim, dim), score, level))

    # 经验库统计
    ess = summary.get("experience_store_stats", {})
    lts = summary.get("lesson_tracker_stats", {})
    lines.extend([
        "",
        "## 经验库状态",
        "",
        "- 总经验数：{}".format(ess.get("total", 0)),
        "- 平均置信度：{}".format(ess.get("avg_confidence", 0)),
        "- 平均效果值：{}".format(ess.get("avg_effectiveness", 0)),
        "- 活跃教训：{}，废弃：{}，可升级：{}".format(
            lts.get("active", 0), lts.get("deprecated", 0), lts.get("promotable", 0)),
    ])

    # 错误类型分布
    by_type = ess.get("by_error_type", {})
    if by_type:
        lines.extend(["", "### 按错误类型分布", ""])
        for et, count in sorted(by_type.items(), key=lambda x: -x[1]):
            label = {"sentiment": "情绪", "sector": "板块", "leader": "龙头",
                     "strategy": "策略", "unknown": "未知"}.get(et, et)
            lines.append("- {}：{} 条".format(label, count))

    # 逐日得分
    lines.extend([
        "",
        "## 逐日得分",
        "",
        "| 分析日 | 验证日 | 总分 | 注入教训 | 场景 |",
        "|--------|--------|------|---------|------|",
    ])
    for day in summary["score_by_day"]:
        scenario_desc = ", ".join(
            v for v in day.get("scenario", {}).values() if v
        )[:30]
        lines.append("| {} | {} | {}/20 | {} | {} |".format(
            day["date"], day["next"], day["score"],
            day.get("injected", 0), scenario_desc,
        ))

    # Top 效果最好的教训
    top = lts.get("top_effective", [])
    if top:
        lines.extend(["", "## 效果最好的教训 (Top 5)", ""])
        for lid, imp in top:
            exp = exp_store.get(lid)
            if exp:
                lines.append("- [{:.1f}分改善] {}".format(imp, exp.lesson[:80]))

    return "\n".join(lines)


def _result_to_dict(r: BacktestResult) -> dict:
    return {
        "day_d": r.day_d,
        "day_d1": r.day_d1,
        "status": r.status,
        "scenario": r.scenario,
        "injected_lessons": r.injected_lessons,
        "scores": r.scores,
        "total_score": r.total_score,
        "key_lessons": r.key_lessons,
        "what_was_right": r.what_was_right,
        "what_was_wrong": r.what_was_wrong,
        "error": r.error,
    }
