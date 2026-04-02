#!/usr/bin/env python3
"""回测 v3 - 八项优化: 均值回归分级、退潮判定、独狼区分、普反日、一字板、补涨、三板组排除、龙头第一+唯一"""

import os
import sys

sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)

# OPENAI_API_KEY must be set in environment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_config

_cfg = get_config()
data_dir = _cfg["data_root"]

from short_term_agents.backtest import run_backtest

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

output_dir = os.path.join(data_dir, "backtest_v3")

print("=" * 60)
print("V3 回测: {} 个交易日, {} 对 (从03-09开始)".format(len(dates), len(dates) - 1))
print("=" * 60)

summary = run_backtest(
    data_dir=data_dir,
    dates=dates,
    config=config,
    output_dir=output_dir,
)

print()
print("=" * 60)
print("=== V3 回测完成 ===")
print("=" * 60)
print("平均总分: {}/25".format(summary.get("avg_total_score", "?")))
print("完成天数: {}".format(summary.get("total_days", "?")))
if "avg_scores_by_dimension" in summary:
    print("\n各维度平均分:")
    for dim, score in summary["avg_scores_by_dimension"].items():
        print("  {}: {}/5".format(dim, score))

# 与 v1, v2 对比 (只对比相同日期范围)
import json
for ver, path in [("V1", "backtest"), ("V2", "backtest_v2")]:
    p = os.path.join(data_dir, path, "summary.json")
    if os.path.exists(p):
        with open(p) as f:
            prev = json.load(f)
        # 筛选出03-09及之后的日期
        prev_days = [d for d in prev.get("score_by_day", []) if d["date"] >= "2026-03-09"]
        v3_days = summary.get("score_by_day", [])
        if prev_days:
            prev_avg = sum(d["score"] for d in prev_days) / len(prev_days)
            v3_avg = sum(d["score"] for d in v3_days) / len(v3_days) if v3_days else 0
            print("\n=== {} vs V3 对比 (03-09起) ===".format(ver))
            print("{} 平均: {:.1f}/25 ({} 天)".format(ver, prev_avg, len(prev_days)))
            print("V3 平均: {:.1f}/25 ({} 天)".format(v3_avg, len(v3_days)))
            # 逐日对比
            prev_map = {d["date"]: d["score"] for d in prev_days}
            print("\n  逐日对比:")
            for sd in v3_days:
                ps = prev_map.get(sd["date"])
                if ps is not None:
                    delta = sd["score"] - ps
                    sign = "+" if delta > 0 else ""
                    marker = " ★" if abs(delta) >= 3 else ""
                    print("    {} : {} → {} ({}{}){}".format(sd["date"], ps, sd["score"], sign, delta, marker))
