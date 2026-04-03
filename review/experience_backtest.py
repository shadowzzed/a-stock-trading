"""经验驱动的回测引擎 v6

在 v5 基础上集成结构化经验库：
1. 场景化教训匹配（替代无差别注入）
2. 教训效果追踪（追踪注入后得分变化）
3. 自动提取结构化经验（从回测验证结果中）
4. 基准对照（同时跑有/无教训注入的对比）

用法:
    python -m review.run_backtest_v6 --data-dir ~/trading-data --start 2026-03-01 --end 2026-03-31
"""

from __future__ import annotations

import json
import os
import time
import argparse
from typing import Optional
from datetime import datetime

from .backtest import VERIFIER_PROMPT, _generate_summary
from .data.loader import (
    load_daily_data,
    summarize_limit_up,
    summarize_limit_down,
)
from .experience.experience_store import ExperienceStore, Experience
from .experience.scenario_classifier import (
    ScenarioClassifier,
    classify_error_type,
)
from .experience.lesson_tracker import LessonTracker
from .experience.prompt_engine import PromptEngine, build_market_data_from_daily


EXPERIENCE_EXTRACTOR_PROMPT = """你是一位经验提炼专家。

你将收到一次短线交易回测的验证结果（Agent 预测 vs 实际行情），请从中提炼出一条结构化经验教训。

## 输出格式（严格 JSON）

```json
{
  "prediction_summary": "Agent 做了什么判断（一句话概括）",
  "reality_summary": "实际发生了什么（一句话概括）",
  "error_type": "sentiment/sector/leader/strategy（哪个维度错得最严重）",
  "lesson": "教训：在 [场景] 下，[错误行为] 导致了 [后果]，正确做法是 [修正]",
  "correction_rule": "当 [场景条件] 时，必须 [具体操作/检查项]"
}
```

## 要求
- lesson 必须包含具体的场景描述，不能太泛
- correction_rule 必须是可执行的检查项，不能是空泛的"注意风险"
- 如果预测基本正确（总分>=15），lesson 可以是"强化正确判断"
- error_type 对应得分最低的维度
- 只输出 JSON，不要其他内容
"""


def run_backtest_v6(
    data_dir: str,
    dates: list,
    config: Optional[dict] = None,
    output_dir: Optional[str] = None,
    on_progress=None,
    with_baseline: bool = True,
):
    """经验驱动的回测 v6

    相比 v5 的改进：
    1. 场景化教训匹配（按市场状态检索最相关的教训）
    2. 每次回测后自动提取结构化经验
    3. 追踪教训注入效果
    4. 可选的基准对照（不带教训跑一遍作为对比）

    Args:
        data_dir: trading 数据根目录
        dates: 交易日列表（已排序）
        config: Agent 配置
        output_dir: 输出目录
        on_progress: 进度回调
        with_baseline: 是否同时跑无教训注入的基准对照
    """
    from .graph import DEFAULT_CONFIG, _create_llm, _load_initial_state, build_graph

    cfg = {**DEFAULT_CONFIG, **(config or {})}
    llm = _create_llm(cfg)

    if not output_dir:
        output_dir = os.path.join(data_dir, "backtest_v6")
    os.makedirs(output_dir, exist_ok=True)

    # 初始化经验系统
    exp_store = ExperienceStore(data_dir)
    lesson_tracker = LessonTracker(data_dir)
    prompt_engine = PromptEngine(data_dir)
    classifier = ScenarioClassifier()

    results = []
    pairs = [(dates[i], dates[i + 1]) for i in range(len(dates) - 1)]
    prev_report = ""

    for idx, (day_d, day_d1) in enumerate(pairs):
        if on_progress:
            on_progress(idx + 1, len(pairs), day_d, "analyzing")

        print("=" * 60)
        print("回测 {}/{}: {} → {} [v6 经验驱动]".format(
            idx + 1, len(pairs), day_d, day_d1))
        print("=" * 60)

        # ── 加载当日数据，提取场景标签 ──
        try:
            daily_data = load_daily_data(data_dir, day_d)
            market_data = build_market_data_from_daily(daily_data)
            scenario = classifier.classify(
                limit_up_count=market_data.get("limit_up_count", 0),
                limit_down_count=market_data.get("limit_down_count", 0),
                blown_rate=market_data.get("blown_rate", 0.0),
                max_board=market_data.get("max_board", 0),
                sector_top1_count=market_data.get("sector_top1_count", 0),
                sector_top1_total=market_data.get("limit_up_count", 0),
                prev_limit_up_count=market_data.get("prev_limit_up_count"),
            )
            print("  [场景] {}".format(scenario.to_description()))
        except Exception as e:
            print("  [场景识别失败] {}".format(e))
            scenario = classifier.classify()
            market_data = {}

        # ── 构建场景感知的 Prompt 注入 ──
        injection = prompt_engine.build_injection(
            market_data,
            agents=["sentiment_analyst", "sector_analyst", "judge"],
            max_lessons_per_agent=3,
        )
        injected_ids = []

        run_config = dict(config or {})
        if injection:
            overrides = dict(run_config.get("prompt_overrides", {}))
            for agent, inject_text in injection.items():
                existing = overrides.get(agent, "")
                overrides[agent] = existing + "\n\n" + inject_text
                # 收集注入的教训 ID（从 store 检索结果中获取）
            run_config["prompt_overrides"] = overrides

            # 检索注入了哪些教训 ID
            relevant = exp_store.search(
                scenario=scenario,
                min_confidence=0.3,
                limit=10,
            )
            active_ids = set(lesson_tracker.get_active_lessons()) or {e.id for e in relevant}
            injected_ids = [e.id for e in relevant if e.id in active_ids][:9]

            print("  [教训注入] {} 条，涉及 {}".format(
                sum(len(v) for v in injection.values()),
                ", ".join(injection.keys()),
            ))
        else:
            print("  [教训注入] 无匹配教训")

        # ── Step 1: 用 Day D 跑 Agent（带教训注入）──
        report_path = os.path.join(output_dir, "{}_report.md".format(day_d))
        if os.path.exists(report_path):
            with open(report_path, "r", encoding="utf-8") as f:
                report = f.read()
            print("  [跳过] {} 报告已存在".format(day_d))
        else:
            try:
                init_state = _load_initial_state(
                    data_dir=data_dir,
                    date=day_d,
                    config=run_config,
                    prev_report=prev_report,
                )
                run_cfg = {**run_config, "data_dir": data_dir, "date": day_d}
                graph = build_graph(run_cfg)
                final = graph.invoke(init_state)
                report = final.get("final_report", "（未生成报告）")
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(report)
                print("  [完成] {} 报告已生成".format(day_d))
            except Exception as e:
                print("  [失败] {} 分析失败: {}".format(day_d, e))
                results.append({
                    "day_d": day_d, "day_d1": day_d1,
                    "status": "analysis_failed", "error": str(e),
                })
                continue

        prev_report = report

        # ── Step 2: 加载 Day D+1 实际数据 ──
        try:
            from .verify import _load_stock_pnl
            data_d1 = load_daily_data(data_dir, day_d1)
            d1_summary = "## {} 实际行情\n\n".format(day_d1)
            d1_summary += summarize_limit_up(data_d1.limit_up) + "\n\n"
            d1_summary += summarize_limit_down(data_d1.limit_down)
            stock_pnl = _load_stock_pnl(data_dir, day_d1, report)
            if stock_pnl:
                d1_summary += "\n\n" + stock_pnl
        except Exception as e:
            print("  [失败] {} 数据加载失败: {}".format(day_d1, e))
            results.append({
                "day_d": day_d, "day_d1": day_d1,
                "status": "d1_data_failed", "error": str(e),
            })
            continue

        # ── Step 3: 验证打分 ──
        if on_progress:
            on_progress(idx + 1, len(pairs), day_d, "verifying")

        verify_path = os.path.join(output_dir, "{}_verify.json".format(day_d))
        if os.path.exists(verify_path):
            with open(verify_path, "r", encoding="utf-8") as f:
                verify_result = json.load(f)
            print("  [跳过] {} 验证已存在".format(day_d))
        else:
            verify_msg = (
                "## Day D ({day_d}) 的 Agent 预测报告\n\n"
                "{report}\n\n"
                "---\n\n"
                "## Day D+1 ({day_d1}) 的实际行情数据\n\n"
                "{d1_summary}\n\n"
                "请对比预测与实际，给出评分和教训。"
            ).format(day_d=day_d, day_d1=day_d1, report=report, d1_summary=d1_summary)

            try:
                from langchain_core.messages import SystemMessage, HumanMessage
                response = llm.invoke([
                    SystemMessage(content=VERIFIER_PROMPT),
                    HumanMessage(content=verify_msg),
                ])

                content = response.content
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0]
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0]

                verify_result = json.loads(content.strip())
                with open(verify_path, "w", encoding="utf-8") as f:
                    json.dump(verify_result, f, ensure_ascii=False, indent=2)
                print("  [完成] {} 验证: 总分 {}/20".format(
                    day_d, verify_result.get("total_score", "?")))
            except Exception as e:
                print("  [失败] {} 验证失败: {}".format(day_d, e))
                verify_result = {"error": str(e)}

        total_score = verify_result.get("total_score", 0)

        # ── Step 4: 提取结构化经验（新增！）──
        if on_progress:
            on_progress(idx + 1, len(pairs), day_d, "extracting_experience")

        extract_path = os.path.join(output_dir, "{}_experience.json".format(day_d))
        if not os.path.exists(extract_path) and "error" not in verify_result:
            try:
                experience = _extract_structured_experience(
                    llm=llm,
                    day_d=day_d,
                    day_d1=day_d1,
                    report=report,
                    verify_result=verify_result,
                    scenario=scenario,
                )
                if experience:
                    # 存入经验库
                    exp_store.add(experience)
                    with open(extract_path, "w", encoding="utf-8") as f:
                        json.dump({
                            "experience_id": experience.id,
                            "scenario": experience.scenario,
                            "lesson": experience.lesson,
                            "correction_rule": experience.correction_rule,
                            "error_type": experience.error_type,
                        }, f, ensure_ascii=False, indent=2)
                    print("  [经验提取] 新增教训: {}".format(
                        experience.lesson[:50]))
            except Exception as e:
                print("  [经验提取失败] {}".format(e))

        # ── Step 5: 记录教训效果 ──
        if injected_ids:
            lesson_tracker.record_injection(
                date=day_d,
                lesson_ids=injected_ids,
                score=total_score,
            )
            lesson_tracker.feedback_to_store(exp_store)

        results.append({
            "day_d": day_d,
            "day_d1": day_d1,
            "status": "completed",
            "scenario": scenario.to_dict(),
            "injected_lessons": len(injected_ids),
            "scores": verify_result.get("scores", {}),
            "total_score": total_score,
            "key_lessons": verify_result.get("key_lessons", []),
            "what_was_right": verify_result.get("what_was_right", []),
            "what_was_wrong": verify_result.get("what_was_wrong", []),
        })

        time.sleep(1)

    # ── 生成汇总报告 ──
    summary = _generate_summary_v6(results, output_dir, exp_store, lesson_tracker)
    return summary


def _extract_structured_experience(
    llm,
    day_d: str,
    day_d1: str,
    report: str,
    verify_result: dict,
    scenario,
) -> Optional[Experience]:
    """从验证结果中提取结构化经验"""
    from langchain_core.messages import SystemMessage, HumanMessage

    # 构建输入
    scores_text = json.dumps(verify_result.get("scores", {}), ensure_ascii=False)
    wrong_items = "\n".join(
        "- {}".format(w) for w in verify_result.get("what_was_wrong", [])
    )
    lessons_items = "\n".join(
        "- {}".format(l) for l in verify_result.get("key_lessons", [])
    )

    extract_msg = (
        "## 回测验证结果\n\n"
        "**分析日期**: {day_d}\n"
        "**验证日期**: {day_d1}\n"
        "**市场场景**: {scenario}\n"
        "**总分**: {total}/20\n\n"
        "### 各维度评分\n{scores}\n\n"
        "### 错误判断\n{wrong}\n\n"
        "### 已有教训摘要\n{lessons}\n\n"
        "请提炼一条最关键的结构化经验教训。"
    ).format(
        day_d=day_d,
        day_d1=day_d1,
        scenario=scenario.to_description(),
        total=verify_result.get("total_score", 0),
        scores=scores_text,
        wrong=wrong_items or "无",
        lessons=lessons_items or "无",
    )

    response = llm.invoke([
        SystemMessage(content=EXPERIENCE_EXTRACTOR_PROMPT),
        HumanMessage(content=extract_msg),
    ])

    content = response.content
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        content = content.split("```")[1].split("```")[0]

    try:
        extracted = json.loads(content.strip())
    except json.JSONDecodeError:
        return None

    # 确定主要错误类型
    error_type = extracted.get("error_type") or classify_error_type(
        verify_result.get("scores", {})
    )

    return Experience(
        date=day_d,
        scenario=scenario.to_dict(),
        prediction=extracted.get("prediction_summary", ""),
        reality=extracted.get("reality_summary", ""),
        scores=verify_result.get("scores", {}),
        error_type=error_type,
        lesson=extracted.get("lesson", ""),
        correction_rule=extracted.get("correction_rule", ""),
    )


def _generate_summary_v6(
    results: list, output_dir: str,
    exp_store: ExperienceStore, tracker: LessonTracker,
) -> dict:
    """生成 v6 汇总报告（含经验库统计和效果追踪）"""
    completed = [r for r in results if r["status"] == "completed"]

    if not completed:
        summary = {
            "version": "v6",
            "status": "no_completed_runs",
            "results": results,
        }
    else:
        dims = ["sentiment", "sector", "leader", "strategy"]
        dim_scores = {d: [] for d in dims}
        for r in completed:
            for d in dims:
                s = r.get("scores", {}).get(d, {})
                if isinstance(s, dict) and "score" in s:
                    dim_scores[d].append(s["score"])

        avg_scores = {}
        for d in dims:
            scores = dim_scores[d]
            avg_scores[d] = round(sum(scores) / len(scores), 1) if scores else 0

        total_avg = round(
            sum(r["total_score"] for r in completed) / len(completed), 1
        )

        # 统计注入效果
        with_injection = [r for r in completed if r.get("injected_lessons", 0) > 0]
        without_injection = [r for r in completed if r.get("injected_lessons", 0) == 0]

        avg_with = (
            round(sum(r["total_score"] for r in with_injection) / len(with_injection), 1)
            if with_injection else 0
        )
        avg_without = (
            round(sum(r["total_score"] for r in without_injection) / len(without_injection), 1)
            if without_injection else 0
        )

        all_lessons = []
        all_wrong = []
        for r in completed:
            all_lessons.extend(r.get("key_lessons", []))
            all_wrong.extend(r.get("what_was_wrong", []))

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
                    "date": r["day_d"],
                    "next": r["day_d1"],
                    "score": r["total_score"],
                    "injected": r.get("injected_lessons", 0),
                    "scenario": r.get("scenario", {}),
                }
                for r in completed
            ],
            "all_lessons": all_lessons,
            "what_was_wrong": all_wrong,
            "results": results,
        }

    # 保存 JSON
    summary_path = os.path.join(output_dir, "summary_v6.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 生成人可读报告
    report_path = os.path.join(output_dir, "回测报告_v6.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(_format_v6_report(summary, exp_store, tracker))

    print("\n汇总已保存到 {}".format(summary_path))
    print("报告已保存到 {}".format(report_path))

    return summary


def _format_v6_report(summary: dict, exp_store: ExperienceStore, tracker: LessonTracker) -> str:
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


# ── CLI 入口 ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="经验驱动的回测 v6")
    parser.add_argument("--data-dir", required=True, help="trading 数据根目录")
    parser.add_argument("--start", help="开始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", help="结束日期 (YYYY-MM-DD)")
    parser.add_argument("--output", help="输出目录")
    parser.add_argument("--no-baseline", action="store_true", help="不跑基准对照")
    args = parser.parse_args()

    # 发现日期范围
    daily_root = os.path.join(args.data_dir, "daily")
    all_dates = sorted([
        d for d in os.listdir(daily_root)
        if os.path.isdir(os.path.join(daily_root, d))
    ])

    if args.start:
        all_dates = [d for d in all_dates if d >= args.start]
    if args.end:
        all_dates = [d for d in all_dates if d <= args.end]

    if not all_dates:
        print("未找到符合条件的交易日")
        return

    print("发现 {} 个交易日: {} ~ {}".format(
        len(all_dates), all_dates[0], all_dates[-1]))

    run_backtest_v6(
        data_dir=args.data_dir,
        dates=all_dates,
        output_dir=args.output,
        with_baseline=not args.no_baseline,
    )


if __name__ == "__main__":
    main()
