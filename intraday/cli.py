#!/usr/bin/env python3
"""盘中分析 Agent CLI

用法:
    python -m intraday opening_analysis
    python -m intraday early_session_analysis
    python -m intraday opening_analysis --dry-run
    python -m intraday opening_analysis --date 2026-04-01
"""

import argparse
import sys

from datetime import datetime


def main():
    parser = argparse.ArgumentParser(
        description="盘中分析 Agent（LangGraph + Grok/DeepSeek）",
    )
    parser.add_argument(
        "agent",
        choices=["opening_analysis", "early_session_analysis", "closing_review"],
        help="要运行的 Agent",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="分析日期（YYYY-MM-DD），默认今天",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅加载数据，不调 AI，不推送飞书",
    )

    args = parser.parse_args()

    from intraday.runner import run_agent

    report = run_agent(
        agent_name=args.agent,
        date=args.date,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print("\n" + "=" * 60)
        print("[dry-run] 数据加载完成，报告未生成")
        print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
