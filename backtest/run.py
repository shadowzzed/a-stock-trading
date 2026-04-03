#!/usr/bin/env python3
"""经验驱动回测 v6 — CLI 入口

用法:
    # 回测指定日期范围
    python -m backtest.run --data-dir ~/trading-data --start 2026-03-01 --end 2026-03-31

    # 回测所有可用日期
    python -m backtest.run --data-dir ~/trading-data

    # 后台运行
    nohup python -m backtest.run --data-dir ~/trading-data --start 2026-03-01 > backtest_v6.log 2>&1 &
"""

from __future__ import annotations

import argparse

from .engine.core import BacktestEngine
from .adapter import ReviewDataProvider, ReviewAgentRunner, LangChainLLMCaller


def main():
    parser = argparse.ArgumentParser(description="经验驱动的回测 v6（独立版）")
    parser.add_argument("--data-dir", required=True, help="trading 数据根目录")
    parser.add_argument("--start", help="开始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", help="结束日期 (YYYY-MM-DD)")
    parser.add_argument("--output", help="输出目录")
    args = parser.parse_args()

    # 通过适配器注入具体实现
    data_provider = ReviewDataProvider()
    agent_runner = ReviewAgentRunner()
    llm_caller = LangChainLLMCaller()

    engine = BacktestEngine(
        data_provider=data_provider,
        agent_runner=agent_runner,
        llm_caller=llm_caller,
    )

    # 发现日期范围
    dates = data_provider.discover_dates(args.data_dir, args.start, args.end)

    if not dates:
        print("未找到符合条件的交易日")
        return

    print("发现 {} 个交易日: {} ~ {}".format(
        len(dates), dates[0], dates[-1]))

    engine.run(
        data_dir=args.data_dir,
        dates=dates,
        output_dir=args.output,
    )


if __name__ == "__main__":
    main()
