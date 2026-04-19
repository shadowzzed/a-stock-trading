#!/usr/bin/env python3
"""
策略版本对比 — 从 strategy_backtest_log 抓取历史回测结果，对比多个版本

用法:
  python3 trading/strategy_compare.py                 # 最近所有版本
  python3 trading/strategy_compare.py --window 20     # 仅 20 日窗口
  python3 trading/strategy_compare.py --top 5          # 只看 Top 5
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strategy_registry import get_conn, init_schema


def compare(window: int = None, top_n: int = 10):
    conn = get_conn()
    init_schema(conn)

    # 取每个策略的最新一条 backtest log
    if window:
        rows = conn.execute("""
            SELECT bl.strategy_id, sv.name, bl.window_days, bl.return_cost,
                   bl.return_market, bl.trade_count, bl.win_rate, bl.max_drawdown,
                   bl.sharpe, bl.run_at
            FROM strategy_backtest_log bl
            JOIN strategy_versions sv ON bl.strategy_id = sv.id
            WHERE bl.window_days = ?
            AND (bl.strategy_id, bl.run_at) IN (
                SELECT strategy_id, MAX(run_at)
                FROM strategy_backtest_log WHERE window_days = ?
                GROUP BY strategy_id
            )
            ORDER BY bl.return_market DESC NULLS LAST
            LIMIT ?
        """, (window, window, top_n)).fetchall()
    else:
        rows = conn.execute("""
            SELECT bl.strategy_id, sv.name, bl.window_days, bl.return_cost,
                   bl.return_market, bl.trade_count, bl.win_rate, bl.max_drawdown,
                   bl.sharpe, bl.run_at
            FROM strategy_backtest_log bl
            JOIN strategy_versions sv ON bl.strategy_id = sv.id
            ORDER BY bl.run_at DESC LIMIT ?
        """, (top_n,)).fetchall()

    if not rows:
        print("没有回测记录。先运行 strategy_health.py")
        return

    print(f"{'Name':<25}{'W':<5}{'Return%':<11}{'Trades':<8}{'Win%':<7}{'DD%':<8}{'Sharpe':<8}{'Run':<20}")
    print("-" * 95)
    for r in rows:
        sid, name, w, rc, rm, tc, wr, dd, sh, run_at = r
        rm_s = f"{rm:+.2f}" if rm is not None else "-"
        tc_s = str(tc) if tc is not None else "-"
        wr_s = f"{wr:.1f}" if wr is not None else "-"
        dd_s = f"{dd:.1f}" if dd is not None else "-"
        sh_s = f"{sh:.2f}" if sh is not None else "-"
        print(f"{name[:23]:<25}{w:<5}{rm_s:<11}{tc_s:<8}{wr_s:<7}{dd_s:<8}{sh_s:<8}{run_at[:19]}")

    conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, help="仅对比指定窗口 5/20/60")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    compare(args.window, args.top)


if __name__ == "__main__":
    main()
