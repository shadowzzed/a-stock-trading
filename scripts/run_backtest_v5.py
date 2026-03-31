#!/usr/bin/env python3
"""回测 v5 - 在v4基础上修复: 趋势惯性原则、概念陷阱警示、策略方向强制一致性"""

import os
import sys

sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)

# OPENAI_API_KEY must be set in environment
sys.path.insert(0, os.path.expanduser("~/src/short-term-agents"))

from short_term_agents.backtest import run_backtest

data_dir = os.path.expanduser("~/src/happyclaw/data/groups/main/trading")

dates = [
    "2026-03-09", "2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13",
    "2026-03-16", "2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20",
    "2026-03-23", "2026-03-24",
]

config = {
    "model": os.environ.get("ARK_MODEL", ""),
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "temperature": 0.3,
    "max_debate_rounds": 1,
}

output_dir = os.path.join(data_dir, "backtest_v5")

print("=" * 60)
print("V5 回测: {} 个交易日, {} 对 (从03-09开始)".format(len(dates), len(dates) - 1))
print("=" * 60)

summary = run_backtest(
    data_dir=data_dir,
    dates=dates,
    config=config,
    output_dir=output_dir,
)

print()
print("=" * 60)
print("=== V5 回测完成 ===")
print("=" * 60)
print("平均总分: {}/25".format(summary.get("avg_total_score", "?")))
print("完成天数: {}".format(summary.get("total_days", "?")))
if "avg_scores_by_dimension" in summary:
    print("\n各维度平均分:")
    for dim, score in summary["avg_scores_by_dimension"].items():
        print("  {}: {}/5".format(dim, score))

# 与 v2, v4 对比
import json
for ver, path in [("V2", "backtest_v2"), ("V4", "backtest_v4")]:
    p = os.path.join(data_dir, path, "summary.json")
    if os.path.exists(p):
        with open(p) as f:
            prev = json.load(f)
        prev_days = [d for d in prev.get("score_by_day", []) if d["date"] >= "2026-03-09"]
        v5_days = summary.get("score_by_day", [])
        if prev_days:
            prev_avg = sum(d["score"] for d in prev_days) / len(prev_days)
            v5_avg = sum(d["score"] for d in v5_days) / len(v5_days) if v5_days else 0
            print("\n=== {} vs V5 对比 (03-09起) ===".format(ver))
            print("{} 平均: {:.1f}/25 ({} 天)".format(ver, prev_avg, len(prev_days)))
            print("V5 平均: {:.1f}/25 ({} 天)".format(v5_avg, len(v5_days)))
            prev_map = {d["date"]: d["score"] for d in prev_days}
            print("\n  逐日对比:")
            for sd in v5_days:
                ps = prev_map.get(sd["date"])
                if ps is not None:
                    delta = sd["score"] - ps
                    sign = "+" if delta > 0 else ""
                    marker = " ★" if abs(delta) >= 3 else ""
                    print("    {} : {} → {} ({}{}){}".format(sd["date"], ps, sd["score"], sign, delta, marker))
