"""历史回测：用 Day D 数据跑 Agent，用 Day D+1 数据验证"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage
from .verify import _load_stock_pnl


VERIFIER_PROMPT = """你是一位短线交易回测验证官。

你需要对比 Agent 系统在 Day D 的预测与 Day D+1 的实际行情，评估预测准确性。

## 评分维度（每项 1-5 分，满分 20 分）

1. **情绪周期判断**（5分）：情绪阶段是否判断正确？情绪转换风险的识别是否有价值（列出的转换条件次日是否触发）？
2. **主线板块判断**（5分）：主线板块次日是否延续强势？支线/退潮的判断是否准确？
3. **龙头判断**（5分）：龙头次日表现如何？生命周期阶段判断是否准确？
4. **策略实效**（5分）：这是最重要的维度，以次日实际盈亏为准评估：
   - **关注标的次日涨跌幅**：推荐的股票次日实际涨了还是跌了？涨跌幅多少？
   - **操作逻辑是否可执行**：推荐"打板"的次日是否有打板机会？推荐"低吸"的是否有低吸位？
   - **仓位建议是否合理**：建议重仓时次日是否大涨？建议空仓时次日是否大跌？反过来就扣分
   - **风险提示是否有效**：提示的风险次日是否兑现？
   - 评分标准：推荐标的次日平均涨幅>3%=5分，1-3%=4分，0-1%=3分，-1~0%=2分，<-1%=1分（仅作参考，需结合操作逻辑综合判断）

注意：不再单独评估"方向判断"（涨/跌/震荡），因为方向本身不可预测。重点看情绪定位是否准确、策略是否产生实际盈利。

## 输出格式（严格 JSON）

```json
{
  "scores": {
    "sentiment": {"score": 3, "reason": "..."},
    "sector": {"score": 4, "reason": "..."},
    "leader": {"score": 3, "reason": "..."},
    "strategy": {"score": 3, "reason": "...（含推荐标的次日实际表现）"}
  },
  "total_score": 13,
  "key_lessons": [
    "教训1：...",
    "教训2：..."
  ],
  "what_was_right": [
    "正确判断1：...",
  ],
  "what_was_wrong": [
    "错误判断1：...",
  ]
}
```

只输出 JSON，不要其他内容。
"""


def run_backtest(
    data_dir: str,
    dates: list,
    config: Optional[dict] = None,
    output_dir: Optional[str] = None,
    on_progress=None,
):
    """对多个交易日运行回测

    Args:
        data_dir: trading 数据根目录
        dates: 交易日列表（已排序），如 ["2026-03-09", "2026-03-10", ...]
        config: Agent 配置
        output_dir: 输出目录（默认 data_dir/backtest/）
        on_progress: 进度回调 (current, total, date, status)

    Returns:
        回测结果汇总 dict
    """
    from .graph import DEFAULT_CONFIG, _create_llm
    from .data.loader import (
        load_daily_data,
        summarize_limit_up,
        summarize_limit_down,
    )

    cfg = {**DEFAULT_CONFIG, **(config or {})}
    llm = _create_llm(cfg)

    if not output_dir:
        output_dir = os.path.join(data_dir, "backtest")
    os.makedirs(output_dir, exist_ok=True)

    from .graph import _load_initial_state, build_graph

    results = []
    # 可以做的回测对：dates[i] 分析 → dates[i+1] 验证
    pairs = [(dates[i], dates[i + 1]) for i in range(len(dates) - 1)]
    prev_report = ""         # 前一天的报告，用于自我校准
    accumulated_lessons = []  # 累积的教训，注入给后续分析

    for idx, (day_d, day_d1) in enumerate(pairs):
        if on_progress:
            on_progress(idx + 1, len(pairs), day_d, "analyzing")

        print("=" * 60)
        print("回测 {}/{}: {} → {}".format(idx + 1, len(pairs), day_d, day_d1))
        print("=" * 60)

        # 构建累积教训文本，注入到 prompt_overrides
        run_config = dict(config or {})
        if accumulated_lessons:
            lessons_text = "\n".join(
                "- {}".format(l) for l in accumulated_lessons[-10:]  # 只保留最近10条
            )
            overrides = dict(run_config.get("prompt_overrides", {}))
            # 注入给情绪分析师和裁决官
            lessons_inject = "\n## 历史回测教训（从前几天验证中总结）\n以下是前几天预测中被验证为错误的教训，请在分析中注意避免：\n{}".format(lessons_text)
            for agent in ["sentiment_analyst", "sector_analyst", "judge"]:
                existing = overrides.get(agent, "")
                overrides[agent] = existing + lessons_inject
            run_config["prompt_overrides"] = overrides

        # Step 1: 用 Day D 跑 Agent（注入前日报告用于自我校准）
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
                    "day_d": day_d,
                    "day_d1": day_d1,
                    "status": "analysis_failed",
                    "error": str(e),
                })
                continue

        # 保存当日报告，作为下一天的 prev_report
        prev_report = report

        # Step 2: 加载 Day D+1 的实际数据
        try:
            data_d1 = load_daily_data(data_dir, day_d1)
            d1_summary = "## {} 实际行情\n\n".format(day_d1)
            d1_summary += summarize_limit_up(data_d1.limit_up) + "\n\n"
            d1_summary += summarize_limit_down(data_d1.limit_down)
            # 加载推荐标的实际盈亏
            stock_pnl = _load_stock_pnl(data_dir, day_d1, report)
            if stock_pnl:
                d1_summary += "\n\n" + stock_pnl
        except Exception as e:
            print("  [失败] {} 数据加载失败: {}".format(day_d1, e))
            results.append({
                "day_d": day_d,
                "day_d1": day_d1,
                "status": "d1_data_failed",
                "error": str(e),
            })
            continue

        # Step 3: 验证 Agent 打分
        if on_progress:
            on_progress(idx + 1, len(pairs), day_d, "verifying")

        verify_path = os.path.join(output_dir, "{}_verify.json".format(day_d))
        if os.path.exists(verify_path):
            with open(verify_path, "r", encoding="utf-8") as f:
                verify_result = json.load(f)
            print("  [跳过] {} 验证已存在".format(day_d))
        else:
            verify_msg = """## Day D ({day_d}) 的 Agent 预测报告

{report}

---

## Day D+1 ({day_d1}) 的实际行情数据

{d1_summary}

请对比预测与实际，给出评分和教训。""".format(
                day_d=day_d,
                day_d1=day_d1,
                report=report,
                d1_summary=d1_summary,
            )

            try:
                response = llm.invoke([
                    SystemMessage(content=VERIFIER_PROMPT),
                    HumanMessage(content=verify_msg),
                ])

                # 提取 JSON
                content = response.content
                # 尝试从 markdown code block 中提取
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0]
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0]

                verify_result = json.loads(content.strip())
                with open(verify_path, "w", encoding="utf-8") as f:
                    json.dump(verify_result, f, ensure_ascii=False, indent=2)
                print("  [完成] {} 验证: 总分 {}/25".format(
                    day_d, verify_result.get("total_score", "?")))
            except Exception as e:
                print("  [失败] {} 验证失败: {}".format(day_d, e))
                verify_result = {"error": str(e)}

        results.append({
            "day_d": day_d,
            "day_d1": day_d1,
            "status": "completed",
            "scores": verify_result.get("scores", {}),
            "total_score": verify_result.get("total_score", 0),
            "key_lessons": verify_result.get("key_lessons", []),
            "what_was_right": verify_result.get("what_was_right", []),
            "what_was_wrong": verify_result.get("what_was_wrong", []),
        })

        # 累积教训，注入给后续分析
        new_lessons = verify_result.get("key_lessons", [])
        if new_lessons:
            accumulated_lessons.extend(new_lessons)
            print("  [教训累积] +{} 条（共 {} 条）".format(
                len(new_lessons), len(accumulated_lessons)))

        time.sleep(1)  # 控制 API 速率

    # 生成汇总报告
    summary = _generate_summary(results, output_dir)
    return summary


def _generate_summary(results: list, output_dir: str) -> dict:
    """生成回测汇总"""
    completed = [r for r in results if r["status"] == "completed"]

    if not completed:
        summary = {"status": "no_completed_runs", "results": results}
    else:
        # 各维度平均分
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

        # 收集所有教训
        all_lessons = []
        all_right = []
        all_wrong = []
        for r in completed:
            all_lessons.extend(r.get("key_lessons", []))
            all_right.extend(r.get("what_was_right", []))
            all_wrong.extend(r.get("what_was_wrong", []))

        summary = {
            "status": "completed",
            "total_days": len(completed),
            "avg_total_score": total_avg,
            "max_possible_score": 20,
            "avg_scores_by_dimension": avg_scores,
            "score_by_day": [
                {"date": r["day_d"], "next": r["day_d1"], "score": r["total_score"]}
                for r in completed
            ],
            "all_lessons": all_lessons,
            "what_was_right": all_right,
            "what_was_wrong": all_wrong,
            "results": results,
        }

    # 保存汇总
    summary_path = os.path.join(output_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # 生成人可读的汇总报告
    report_path = os.path.join(output_dir, "回测报告.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(_format_summary_report(summary))

    print("\n汇总已保存到 {}".format(summary_path))
    print("报告已保存到 {}".format(report_path))

    return summary


def _format_summary_report(summary: dict) -> str:
    """格式化回测汇总报告"""
    if summary["status"] != "completed":
        return "# 回测未完成\n\n无有效结果。"

    lines = [
        "# 短线 Agent 历史回测报告",
        "",
        "## 总体表现",
        "",
        "- 回测交易日数：{}".format(summary["total_days"]),
        "- 平均总分：{} / {} ({:.0f}%)".format(
            summary["avg_total_score"],
            summary["max_possible_score"],
            summary["avg_total_score"] / summary["max_possible_score"] * 100,
        ),
        "",
        "## 各维度平均分",
        "",
        "| 维度 | 平均分 | 满分 | 评价 |",
        "|------|--------|------|------|",
    ]

    dim_names = {
        "sentiment": "情绪周期",
        "sector": "板块轮动",
        "leader": "龙头辨识",
        "strategy": "策略实效",
    }
    for dim, score in summary["avg_scores_by_dimension"].items():
        level = "优" if score >= 4 else "良" if score >= 3 else "中" if score >= 2 else "差"
        lines.append("| {} | {} | 5 | {} |".format(dim_names.get(dim, dim), score, level))

    lines.extend([
        "",
        "## 逐日得分",
        "",
        "| 分析日 | 验证日 | 总分 |",
        "|--------|--------|------|",
    ])
    for day in summary["score_by_day"]:
        lines.append("| {} | {} | {}/25 |".format(day["date"], day["next"], day["score"]))

    if summary["what_was_right"]:
        lines.extend(["", "## 正确判断（可强化）", ""])
        for item in summary["what_was_right"]:
            lines.append("- {}".format(item))

    if summary["what_was_wrong"]:
        lines.extend(["", "## 错误判断（需改进）", ""])
        for item in summary["what_was_wrong"]:
            lines.append("- {}".format(item))

    if summary["all_lessons"]:
        lines.extend(["", "## 核心教训", ""])
        for item in summary["all_lessons"]:
            lines.append("- {}".format(item))

    return "\n".join(lines)
