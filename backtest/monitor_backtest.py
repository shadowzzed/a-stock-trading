"""方向二回测：盘中监控系统多日模拟

模拟每个交易日的分钟级监控，统计信号质量和收益。

用法:
    python3 -m backtest.monitor_backtest --start 2026-04-07 --end 2026-04-17
"""

from __future__ import annotations

import json
import os
import sys
import sqlite3
from datetime import datetime

_project_root = os.path.dirname(os.path.dirname(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

TRADING_DIR = os.path.expanduser("~/shared/trading")
INTRADAY_DB = os.path.join(TRADING_DIR, "intraday", "intraday.db")


def run_monitor_backtest(start_date: str, end_date: str) -> dict:
    """多日盘中监控回测"""
    from trading_agent.intraday.monitor import (
        MonitorState, StockState, update_minute, update_minute_fast, _calc_limit_price,
    )
    from trading_agent.intraday.layered_analysis import run_analysis
    from dataclasses import asdict

    conn = sqlite3.connect(INTRADAY_DB, timeout=10)
    # 获取有 minute_bars 数据的交易日
    all_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM minute_bars WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date),
    ).fetchall()]
    conn.close()

    if len(all_dates) < 2:
        print(f"可用交易日不足: {len(all_dates)}")
        return {}

    print(f"盘中监控回测: {all_dates[0]} → {all_dates[-1]} ({len(all_dates)} 天)")
    print(f"{'='*70}\n")

    all_day_results = []
    total_signals = 0
    signal_stats = {}
    prev_candidates = []  # 前日推荐（在循环中传递）
    prev_sectors = []

    for i, date in enumerate(all_dates):
        # 初始化当日监控状态
        state = MonitorState()
        state.date = date
        state.stocks = {}
        state.sector_heat = {}

        # 用前日推荐作为 watchlist
        if prev_candidates:
            for c in prev_candidates:
                code = c.get("code", "")
                if code:
                    state.stocks[code] = asdict(StockState(
                        code=code, name=c.get("name", ""),
                        is_watchlist=True,
                    ))
            state.sector_heat = {s: 0 for s in prev_sectors}

        # 用当日数据生成"盘后推荐"给下一天用（模拟 15:10 分析）
        try:
            result = run_analysis(date=date, dry_run=True)
            prev_candidates = result.get("candidates", [])
            prev_sectors = result.get("judgment", {}).get("top_sectors", [])
        except Exception as e:
            print(f"  [{date}] 盘后分析失败: {e}")
            prev_candidates = []
            prev_sectors = []

        # 获取当日分钟时间序列
        conn = sqlite3.connect(INTRADAY_DB, timeout=10)
        times = [r[0] for r in conn.execute(
            "SELECT DISTINCT time FROM minute_bars WHERE date = ? ORDER BY time",
            (date,),
        ).fetchall()]
        conn.close()

        if not times:
            continue

        # 预加载全天数据到内存（优化：避免每分钟查 DB）
        conn = sqlite3.connect(INTRADAY_DB, timeout=10)
        all_minute_data = {}  # {time: [(code, close, volume, high, low, name, last_close, limit_pct)]}
        rows = conn.execute(
            "SELECT mb.time, mb.code, mb.close, mb.volume, mb.high, mb.low, "
            "sm.name, sm.last_close, sm.limit_pct "
            "FROM minute_bars mb "
            "JOIN stock_meta sm ON mb.code = sm.code AND sm.date = ? "
            "WHERE mb.date = ?",
            (date, date),
        ).fetchall()
        conn.close()
        for row in rows:
            t = row[0]
            if t not in all_minute_data:
                all_minute_data[t] = []
            all_minute_data[t].append(row[1:])  # (code, close, volume, high, low, name, last_close, limit_pct)

        # 逐分钟更新（使用内存数据）
        day_signals = []
        for t in times:
            minute_rows = all_minute_data.get(t, [])
            signals = update_minute_fast(state, date, t, minute_rows)
            day_signals.extend(signals)

        # 统计
        for s in day_signals:
            st = s["type"]
            signal_stats[st] = signal_stats.get(st, 0) + 1

        total_signals += len(day_signals)

        # 评估封板信号的次日收益
        seal_signals = [s for s in day_signals if s["type"] in ("sealed", "opportunity")]
        seal_pnls = []
        if seal_signals:
            conn = sqlite3.connect(INTRADAY_DB, timeout=10)
            next_date_row = conn.execute(
                "SELECT MIN(date) FROM daily_bars WHERE date > ?", (date,)
            ).fetchone()
            next_date = next_date_row[0] if next_date_row else None
            if next_date:
                for s in seal_signals:
                    code = s["code"]
                    # 当日收盘价
                    close_row = conn.execute(
                        "SELECT close FROM daily_bars WHERE date=? AND code=?",
                        (date, code),
                    ).fetchone()
                    # 次日收盘价
                    next_close_row = conn.execute(
                        "SELECT close, open FROM daily_bars WHERE date=? AND code=?",
                        (next_date, code),
                    ).fetchone()
                    if close_row and next_close_row:
                        # 次日开盘价买入 → 次日收盘价卖出
                        buy_price = next_close_row[1]  # 次日开盘
                        sell_price = next_close_row[0]  # 次日收盘
                        if buy_price > 0:
                            pnl = (sell_price - buy_price) / buy_price * 100
                            seal_pnls.append({
                                "code": code, "name": s["name"],
                                "seal_time": s["time"], "pnl": round(pnl, 2),
                            })
            conn.close()

        day_result = {
            "date": date,
            "watchlist_count": sum(1 for s in state.stocks.values() if s.get("is_watchlist")),
            "total_signals": len(day_signals),
            "signal_types": {s["type"]: sum(1 for x in day_signals if x["type"] == s["type"])
                           for s in day_signals},
            "seal_pnls": seal_pnls,
        }
        all_day_results.append(day_result)

        # 打印每日摘要
        seal_avg = (sum(p["pnl"] for p in seal_pnls) / len(seal_pnls)) if seal_pnls else 0
        print(f"[{date}] {len(day_signals)} 信号 | "
              f"watchlist {day_result['watchlist_count']} | "
              f"封板信号 {len(seal_signals)} 只 | "
              f"封板次日均收益 {seal_avg:+.2f}%")
        for s in day_signals[:5]:  # 只显示前5个
            print(f"  [{s['time']}] {s['type']:15s} | {s.get('name',''):10s} | {s['message']}")
        if len(day_signals) > 5:
            print(f"  ... 还有 {len(day_signals)-5} 个信号")

    # 汇总
    print(f"\n{'='*70}")
    print(f"回测汇总: {len(all_day_results)} 天 | 总信号 {total_signals}")
    print(f"\n信号类型分布:")
    for st, count in sorted(signal_stats.items(), key=lambda x: -x[1]):
        print(f"  {st}: {count}")

    # 封板信号收益统计
    all_seal_pnls = []
    for d in all_day_results:
        all_seal_pnls.extend(d.get("seal_pnls", []))

    if all_seal_pnls:
        wins = [p for p in all_seal_pnls if p["pnl"] > 0]
        losses = [p for p in all_seal_pnls if p["pnl"] <= 0]
        avg_pnl = sum(p["pnl"] for p in all_seal_pnls) / len(all_seal_pnls)
        print(f"\n封板信号次日收益:")
        print(f"  总数: {len(all_seal_pnls)} | 胜率: {len(wins)/len(all_seal_pnls)*100:.0f}%")
        print(f"  平均收益: {avg_pnl:+.2f}%")
        if wins:
            print(f"  均盈利: {sum(p['pnl'] for p in wins)/len(wins):+.2f}%")
        if losses:
            print(f"  均亏损: {sum(p['pnl'] for p in losses)/len(losses):+.2f}%")

    return {
        "days": len(all_day_results),
        "total_signals": total_signals,
        "signal_stats": signal_stats,
        "seal_pnls": all_seal_pnls,
        "daily_results": all_day_results,
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘中监控多日回测")
    parser.add_argument("--start", default="2026-04-07", help="开始日期")
    parser.add_argument("--end", default="2026-04-17", help="结束日期")
    parser.add_argument("--output", help="输出文件")
    args = parser.parse_args()

    result = run_monitor_backtest(args.start, args.end)

    if args.output and result:
        with open(args.output, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存到: {args.output}")


if __name__ == "__main__":
    main()
