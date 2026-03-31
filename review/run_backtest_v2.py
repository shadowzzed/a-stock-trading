#!/usr/bin/env python3
"""回测 v2 - 优化后的 prompts"""

import os
import sys

sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)

# OPENAI_API_KEY must be set in environment
sys.path.insert(0, os.path.expanduser("~/src/short-term-agents"))

from short_term_agents.backtest import run_backtest

data_dir = os.path.expanduser("~/src/happyclaw/data/groups/main/trading")

dates = [
    "2026-03-04", "2026-03-05", "2026-03-06",
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

output_dir = os.path.join(data_dir, "backtest_v2")

print("=" * 60)
print("V2 回测: {} 个交易日, {} 对".format(len(dates), len(dates) - 1))
print("=" * 60)

summary = run_backtest(
    data_dir=data_dir,
    dates=dates,
    config=config,
    output_dir=output_dir,
)

print()
print("=" * 60)
print("=== V2 回测完成 ===")
print("=" * 60)
print("平均总分: {}/25".format(summary.get("avg_total_score", "?")))
print("完成天数: {}".format(summary.get("total_days", "?")))
if "avg_scores_by_dimension" in summary:
    print("\n各维度平均分:")
    for dim, score in summary["avg_scores_by_dimension"].items():
        print("  {}: {}/5".format(dim, score))

# 与 v1 对比
import json
v1_path = os.path.join(data_dir, "backtest", "summary.json")
if os.path.exists(v1_path):
    with open(v1_path) as f:
        v1 = json.load(f)
    print("\n=== V1 vs V2 对比 ===")
    print("V1 平均: {}/25".format(v1.get("avg_total_score", "?")))
    print("V2 平均: {}/25".format(summary.get("avg_total_score", "?")))
    if "avg_scores_by_dimension" in v1 and "avg_scores_by_dimension" in summary:
        for dim in v1["avg_scores_by_dimension"]:
            v1s = v1["avg_scores_by_dimension"].get(dim, 0)
            v2s = summary.get("avg_scores_by_dimension", {}).get(dim, 0)
            delta = v2s - v1s
            sign = "+" if delta > 0 else ""
            print("  {}: {} → {} ({}{})".format(dim, v1s, v2s, sign, round(delta, 1)))
