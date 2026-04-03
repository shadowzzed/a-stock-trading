#!/usr/bin/env python3
"""经验驱动回测 v6 — CLI 入口

用法:
    # 完整回测（分析+验证+经验提取）
    python -m backtest.run --data-dir ~/trading-data --start 2026-03-01 --end 2026-03-31

    # 回测 + 交易模拟
    python -m backtest.run --data-dir ~/trading-data --start 2026-03-01 --trade-sim

    # 仅运行交易模拟（复用已有报告，零 LLM 消耗）
    python -m backtest.run --data-dir ~/trading-data --start 2026-03-01 --trade-sim-only

    # 后台运行
    nohup python -m backtest.run --data-dir ~/trading-data --start 2026-03-01 > backtest.log 2>&1 &
"""

from __future__ import annotations

import argparse
import os

from .engine.core import BacktestEngine
from .adapter import ReviewDataProvider, ReviewAgentRunner, LangChainLLMCaller
from .trade.executor import TradeSimulator
from .trade.evaluator import evaluate, save_evaluation


def main():
    parser = argparse.ArgumentParser(description="经验驱动的回测 v6")
    parser.add_argument("--data-dir", required=True, help="trading 数据根目录")
    parser.add_argument("--start", help="开始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", help="结束日期 (YYYY-MM-DD)")
    parser.add_argument("--output", help="输出目录")
    parser.add_argument("--trade-sim", action="store_true",
                        help="启用交易模拟（在回测基础上模拟实际买卖）")
    parser.add_argument("--trade-sim-only", action="store_true",
                        help="仅运行交易模拟（复用已有报告，零 LLM 消耗）")
    parser.add_argument("--capital", type=float, default=1_000_000.0,
                        help="模拟初始资金（默认100万）")
    args = parser.parse_args()

    data_provider = ReviewDataProvider()

    # 发现日期范围
    dates = data_provider.discover_dates(args.data_dir, args.start, args.end)
    if not dates:
        print("未找到符合条件的交易日")
        return

    print("发现 {} 个交易日: {} ~ {}".format(len(dates), dates[0], dates[-1]))

    output_dir = args.output or os.path.join(args.data_dir, "backtest_v6")
    os.makedirs(output_dir, exist_ok=True)

    # ── 交易模拟（仅模拟模式） ──
    if args.trade_sim_only:
        _run_trade_sim_only(dates, args.data_dir, output_dir, args.capital)
        return

    # ── 完整回测 ──
    agent_runner = ReviewAgentRunner()
    llm_caller = LangChainLLMCaller()

    engine = BacktestEngine(
        data_provider=data_provider,
        agent_runner=agent_runner,
        llm_caller=llm_caller,
    )

    engine.run(
        data_dir=args.data_dir,
        dates=dates,
        output_dir=output_dir,
    )

    # ── 回测后追加交易模拟 ──
    if args.trade_sim:
        _run_trade_sim_only(dates, args.data_dir, output_dir, args.capital)


def _run_trade_sim_only(
    dates: list[str],
    data_dir: str,
    output_dir: str,
    capital: float,
):
    """仅运行交易模拟（复用已有的回测报告，不消耗 LLM）"""
    from .adapter import CSVStockDataProvider

    print("\n" + "=" * 60)
    print("交易模拟（零 LLM 消耗模式）")
    print("=" * 60)

    loader = CSVStockDataProvider()
    sim = TradeSimulator(initial_capital=capital)
    sim.set_data_loader(loader)

    # 需要三元组：(Day D, Day D+1, Day D+2)
    # D+1 用于买入执行，D+2 用于卖出
    pairs = [(dates[i], dates[i + 1]) for i in range(len(dates) - 1)]

    for idx, (day_d, day_d1) in enumerate(pairs):
        # D+2 用于卖出
        day_d2 = pairs[idx + 1][1] if idx + 1 < len(pairs) else None

        # 读取已有报告
        report = _load_report(data_dir, output_dir, day_d)
        if not report:
            print("  [跳过] {} 无报告".format(day_d))
            continue

        print("模拟 {}/{}: {} → {} (卖出日: {})".format(
            idx + 1, len(pairs), day_d, day_d1, day_d2 or "无"))

        sim.process_day(
            signal_date=day_d,
            target_date=day_d1,
            sell_date=day_d2,
            report=report,
            data_dir=data_dir,
        )

    # 评估并保存
    results = sim.get_results()
    snapshots = sim.get_snapshots()

    if results:
        ev = evaluate(results, snapshots, capital)
        save_evaluation(ev, output_dir, prefix="trade_sim")
    else:
        print("\n无交易记录，跳过评估")

    print("\n交易模拟完成。结果保存在 {}".format(output_dir))


def _load_report(data_dir: str, output_dir: str, date: str) -> str:
    """加载回测报告（优先从 output_dir，再从 daily 目录）"""
    # 优先从回测输出目录
    report_path = os.path.join(output_dir, "{}_report.md".format(date))
    if os.path.exists(report_path):
        with open(report_path, "r", encoding="utf-8") as f:
            return f.read()

    # 从 daily 目录找裁决报告
    daily_dir = os.path.join(data_dir, "daily", date)
    if os.path.isdir(daily_dir):
        for fname in os.listdir(daily_dir):
            if "裁决" in fname and fname.endswith(".md"):
                with open(os.path.join(daily_dir, fname), "r", encoding="utf-8") as f:
                    return f.read()

    return ""


if __name__ == "__main__":
    main()
