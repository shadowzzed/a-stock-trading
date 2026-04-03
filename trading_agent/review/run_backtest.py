#!/usr/bin/env python3
"""独立回测脚本 - 可后台运行，不受会话中断影响"""

import os
import sys

# 强制无缓冲输出（nohup 模式下需要）
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)

# 设置 API Key
# OPENAI_API_KEY must be set in environment

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config import get_config

_cfg = get_config()
data_dir = _cfg["data_root"]

from short_term_agents.backtest import run_backtest

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

output_dir = os.path.join(data_dir, "backtest")

print("=" * 60)
print("开始回测: {} 个交易日, {} 对".format(len(dates), len(dates) - 1))
print("=" * 60)

summary = run_backtest(
    data_dir=data_dir,
    dates=dates,
    config=config,
    output_dir=output_dir,
)

print()
print("=" * 60)
print("=== 回测完成 ===")
print("=" * 60)
print("平均总分: {}/25".format(summary.get("avg_total_score", "?")))
print("完成天数: {}".format(summary.get("total_days", "?")))
if "avg_scores_by_dimension" in summary:
    print("\n各维度平均分:")
    for dim, score in summary["avg_scores_by_dimension"].items():
        print("  {}: {}/5".format(dim, score))
