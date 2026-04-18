"""方向二回测：盘中监控系统多日模拟（带真实买卖交易，遵循 T+1）

买卖规则：
- 买入信号：sealed（封板）/ opportunity（板块内新封板）→ 当分钟 close 买入
- 卖出信号：stop_loss（止损）/ take_profit（止盈）/ blown（炸板）→ 当分钟 close 卖出
- T+1 规则：当日买入的股票，当日不可卖出（必须持至下一个交易日才能卖）
- 无 EOD 强平：遵循 T+1，当日买入一律隔夜持仓
- 资金模型：10万起步，30% 仓位复利，每只股票最多一个仓位

用法:
    python3 -m backtest.monitor_backtest --start 2026-04-07 --end 2026-04-17 --output result.json
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

INITIAL_CAPITAL = 100_000.0
POSITION_PCT = 0.30          # 每笔 30% 仓位
MAX_CONCURRENT_POSITIONS = 20  # 几乎不限，靠资金自然约束（每笔 30% 仓位）
STALE_HOLD_DAYS = 2           # 持仓 N 天且浮盈 < STALE_MIN_RETURN → 开盘卖出
STALE_MIN_RETURN = 3.0        # 持仓期最大浮盈不足此值 → stale 卖出

BUY_SIGNALS = {"sealed", "opportunity", "trend_breakout"}
SELL_SIGNALS = {"stop_loss", "take_profit", "blown"}


def run_monitor_backtest(start_date: str, end_date: str) -> dict:
    """多日盘中监控回测（带真实买卖）"""
    from trading_agent.intraday.monitor import (
        MonitorState, StockState, update_minute_fast, _calc_limit_price,
    )
    from trading_agent.intraday.layered_analysis import run_analysis
    from dataclasses import asdict

    conn = sqlite3.connect(INTRADAY_DB, timeout=10)
    all_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM minute_bars WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date),
    ).fetchall()]
    conn.close()

    if len(all_dates) < 2:
        print(f"可用交易日不足: {len(all_dates)}")
        return {}

    print(f"盘中监控回测（带交易）: {all_dates[0]} → {all_dates[-1]} ({len(all_dates)} 天)")
    print(f"起始资金: ¥{INITIAL_CAPITAL:,.0f} | 仓位: {POSITION_PCT*100:.0f}% 复利\n")
    print(f"{'='*80}\n")

    capital = INITIAL_CAPITAL
    all_trades = []             # 已平仓的完整交易
    open_positions = {}          # code -> {buy_date, buy_time, buy_price, shares, buy_reason, cost}
    daily_snapshots = []
    prev_candidates = []
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
                if not code:
                    continue
                # 判断是否是趋势股（breakdown 里含 kind='趋势' 或 trend_3d）
                bd = c.get("breakdown") or {}
                is_trend = bd.get("kind") == "趋势" or "trend_3d" in bd
                kind = "trend" if is_trend else "limit_up"
                state.stocks[code] = asdict(StockState(
                    code=code, name=c.get("name", ""),
                    is_watchlist=True, kind=kind,
                ))
            state.sector_heat = {s: 0 for s in prev_sectors}

        # 若有隔夜持仓，把持仓标记到 state（以便触发 stop_loss/take_profit）
        for code, pos in open_positions.items():
            if code not in state.stocks:
                state.stocks[code] = asdict(StockState(
                    code=code, name=pos["name"], is_watchlist=False,
                ))
            state.stocks[code]["buy_price"] = pos["buy_price"]

        # 用当日数据生成次日推荐
        try:
            result = run_analysis(date=date, dry_run=True)
            prev_candidates = result.get("candidates", [])
            prev_sectors = result.get("judgment", {}).get("top_sectors", [])
        except Exception as e:
            print(f"  [{date}] 盘后分析失败: {e}")
            prev_candidates = []
            prev_sectors = []

        # 预加载全天分钟数据
        conn = sqlite3.connect(INTRADAY_DB, timeout=10)
        times = [r[0] for r in conn.execute(
            "SELECT DISTINCT time FROM minute_bars WHERE date = ? ORDER BY time",
            (date,),
        ).fetchall()]
        all_minute_data = {}
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
            all_minute_data.setdefault(t, []).append(row[1:])

        if not times:
            continue

        # 构建 code→(close, name) 的快速查询表（用于取卖出价）
        minute_price_map = {}  # {(time, code): (close, name)}
        for t, trows in all_minute_data.items():
            for code, close, _vol, _hi, _lo, name, _lc, _lp in trows:
                minute_price_map[(t, code)] = (close, name)

        day_signals = []
        day_buys = []
        day_sells = []

        # ── 开盘前 stale 检查：持仓 >= STALE_HOLD_DAYS 且浮盈不足 → 开盘卖出 ──
        stale_codes = []
        for code, pos in list(open_positions.items()):
            hold_days = all_dates.index(date) - all_dates.index(pos["buy_date"])
            if hold_days < STALE_HOLD_DAYS:
                continue
            # 检查昨日是否涨停（若涨停说明仍强势，不卖）
            prev_date = all_dates[all_dates.index(date) - 1]
            try:
                _c = sqlite3.connect(INTRADAY_DB, timeout=10)
                row = _c.execute(
                    "SELECT pct_chg, close FROM daily_bars WHERE code=? AND date=?",
                    (code, prev_date),
                ).fetchone()
                _c.close()
                prev_pct = (row[0] or 0) if row else 0
                prev_close = (row[1] or 0) if row else 0
            except Exception:
                prev_pct = 0
                prev_close = 0
            if prev_pct >= 9.5:
                continue  # 昨日涨停，继续持有
            # 浮盈不足 STALE_MIN_RETURN 且持仓超时 → 卖出
            if prev_close > 0:
                floating_pnl = (prev_close - pos["buy_price"]) / pos["buy_price"] * 100
                if floating_pnl >= STALE_MIN_RETURN:
                    continue  # 浮盈足够，继续持有
            # 以开盘价（times[0] 分钟的 close）卖出
            open_t = times[0] if times else None
            if not open_t:
                continue
            price_entry = minute_price_map.get((open_t, code))
            if not price_entry:
                continue
            open_price = price_entry[0]
            sell_value = pos["shares"] * open_price
            pnl = sell_value - pos["cost"]
            pnl_pct = (open_price - pos["buy_price"]) / pos["buy_price"] * 100
            capital += sell_value
            trade = {
                **pos,
                "sell_date": date,
                "sell_time": open_t,
                "sell_price": open_price,
                "sell_value": sell_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "sell_reason": f"stale: 持仓{hold_days}天无涨停，开盘清仓",
                "capital_after_sell": capital,
                "hold_days": hold_days,
            }
            all_trades.append(trade)
            day_sells.append({
                "time": open_t, "code": code, "name": pos["name"],
                "price": open_price, "shares": pos["shares"],
                "pnl": pnl, "pnl_pct": pnl_pct, "reason": "stale",
            })
            stale_codes.append(code)
        for c in stale_codes:
            open_positions.pop(c, None)

        # 逐分钟更新
        for t in times:
            minute_rows = all_minute_data.get(t, [])
            signals = update_minute_fast(state, date, t, minute_rows)
            day_signals.extend(signals)

            # 处理信号 → 交易
            for s in signals:
                code = s["code"]
                sig_type = s["type"]
                if not code:
                    continue

                # 取当分钟的 close 作为成交价
                price_entry = minute_price_map.get((t, code))
                if not price_entry:
                    continue
                price, name = price_entry

                # ── 买入 ──
                if sig_type in BUY_SIGNALS and code not in open_positions:
                    if capital < 1000 or price <= 0:
                        continue
                    if len(open_positions) >= MAX_CONCURRENT_POSITIONS:
                        continue  # 仓位已满
                    target_cost = capital * POSITION_PCT
                    shares = int(target_cost / price / 100) * 100  # 按手(100股)取整
                    if shares < 100:
                        continue
                    cost = shares * price
                    capital -= cost
                    open_positions[code] = {
                        "code": code,
                        "name": name or s.get("name", code),
                        "buy_date": date,
                        "buy_time": t,
                        "buy_price": price,
                        "shares": shares,
                        "cost": cost,
                        "buy_reason": f"{sig_type}: {s.get('message', '')}",
                        "capital_before_buy": capital + cost,
                    }
                    # 同步到 monitor state，使止损/止盈能触发
                    if code in state.stocks:
                        state.stocks[code]["buy_price"] = price
                    day_buys.append({
                        "time": t, "code": code, "name": name,
                        "price": price, "shares": shares, "cost": cost,
                        "reason": sig_type,
                    })

                # ── 卖出 ── （T+1：当日买入不可当日卖出）
                elif sig_type in SELL_SIGNALS and code in open_positions:
                    pos_check = open_positions[code]
                    if pos_check["buy_date"] == date:
                        continue  # T+1 禁止当日卖出
                    pos = open_positions.pop(code)
                    sell_value = pos["shares"] * price
                    pnl = sell_value - pos["cost"]
                    pnl_pct = (price - pos["buy_price"]) / pos["buy_price"] * 100
                    capital += sell_value
                    trade = {
                        **pos,
                        "sell_date": date,
                        "sell_time": t,
                        "sell_price": price,
                        "sell_value": sell_value,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "sell_reason": f"{sig_type}: {s.get('message', '')}",
                        "capital_after_sell": capital,
                        "hold_days": all_dates.index(date) - all_dates.index(pos["buy_date"]),
                    }
                    all_trades.append(trade)
                    day_sells.append({
                        "time": t, "code": code, "name": name,
                        "price": price, "shares": pos["shares"],
                        "pnl": pnl, "pnl_pct": pnl_pct, "reason": sig_type,
                    })

        # 当日快照（T+1：当日买入一律隔夜持仓，无强平）
        total_value = capital + sum(
            p["shares"] * minute_price_map.get((times[-1], c), (p["buy_price"],))[0]
            for c, p in open_positions.items()
        )
        daily_snapshots.append({
            "date": date,
            "capital": capital,
            "positions": len(open_positions),
            "total_value": total_value,
            "buys": len(day_buys),
            "sells": len(day_sells),
        })

        print(f"[{date}] 信号 {len(day_signals):3d} | "
              f"买入 {len(day_buys)} 卖出 {len(day_sells)} | "
              f"持仓 {len(open_positions)} | "
              f"现金 ¥{capital:,.0f} | 总值 ¥{total_value:,.0f} "
              f"({(total_value-INITIAL_CAPITAL)/INITIAL_CAPITAL*100:+.2f}%)")
        for b in day_buys[:3]:
            print(f"  买 [{b['time']}] {b['name']:10s}({b['code']}) "
                  f"@{b['price']:.2f} × {b['shares']} = ¥{b['cost']:,.0f} [{b['reason']}]")
        for sv in day_sells[:3]:
            print(f"  卖 [{sv['time']}] {sv['name']:10s}({sv['code']}) "
                  f"@{sv['price']:.2f} × {sv['shares']} = 盈亏 ¥{sv['pnl']:+,.0f} "
                  f"({sv['pnl_pct']:+.2f}%) [{sv['reason']}]")

    # ── 汇总 ──
    total_value_final = capital + sum(p["cost"] for p in open_positions.values())
    total_pnl = total_value_final - INITIAL_CAPITAL
    total_return = total_pnl / INITIAL_CAPITAL * 100
    wins = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] <= 0]

    print(f"\n{'='*80}")
    print(f"回测汇总: {len(all_dates)} 天")
    print(f"{'='*80}")
    print(f"起始资金: ¥{INITIAL_CAPITAL:,.0f}")
    print(f"最终现金: ¥{capital:,.0f}")
    print(f"未平仓数: {len(open_positions)} (按成本估值 ¥{sum(p['cost'] for p in open_positions.values()):,.0f})")
    print(f"总权益:   ¥{total_value_final:,.0f}")
    print(f"总盈亏:   ¥{total_pnl:+,.0f} ({total_return:+.2f}%)")
    print(f"\n交易统计:")
    print(f"  总交易数: {len(all_trades)}")
    if all_trades:
        print(f"  胜率: {len(wins)}/{len(all_trades)} = {len(wins)/len(all_trades)*100:.1f}%")
        print(f"  平均收益: {sum(t['pnl_pct'] for t in all_trades)/len(all_trades):+.2f}%")
        if wins:
            print(f"  均盈利: {sum(t['pnl_pct'] for t in wins)/len(wins):+.2f}%")
        if losses:
            print(f"  均亏损: {sum(t['pnl_pct'] for t in losses)/len(losses):+.2f}%")

    return {
        "days": len(all_dates),
        "initial_capital": INITIAL_CAPITAL,
        "final_capital": capital,
        "final_total_value": total_value_final,
        "total_pnl": total_pnl,
        "total_return_pct": total_return,
        "trades": all_trades,
        "open_positions": list(open_positions.values()),
        "daily_snapshots": daily_snapshots,
        "stats": {
            "total_trades": len(all_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins)/len(all_trades)*100 if all_trades else 0,
        },
    }


def format_trade_statement(result: dict) -> str:
    """生成交割单 Markdown"""
    lines = []
    lines.append(f"# 方向二回测交割单（带真实买卖）\n")
    lines.append(f"- **回测天数**: {result['days']} 天")
    lines.append(f"- **起始资金**: ¥{result['initial_capital']:,.0f}")
    lines.append(f"- **最终权益**: ¥{result['final_total_value']:,.0f}")
    lines.append(f"- **总盈亏**: ¥{result['total_pnl']:+,.0f} ({result['total_return_pct']:+.2f}%)")
    stats = result["stats"]
    lines.append(f"- **总交易数**: {stats['total_trades']} | "
                 f"胜 {stats['wins']} 负 {stats['losses']} | 胜率 {stats['win_rate']:.1f}%\n")

    lines.append("## 逐笔交割单\n")
    if not result["trades"]:
        lines.append("_本期无已平仓交易_\n")
    else:
        lines.append("| # | 买入日 | 买入时间 | 卖出日 | 卖出时间 | 标的 | 买价 | 卖价 | 股数 | 成本 | 盈亏 | 收益率 | 买入原因 | 卖出原因 |")
        lines.append("|---|--------|---------|--------|---------|------|------|------|------|------|------|--------|---------|---------|")
        for i, t in enumerate(result["trades"], 1):
            lines.append(
                f"| {i} | {t['buy_date']} | {t['buy_time']} | "
                f"{t['sell_date']} | {t['sell_time']} | "
                f"{t['name']}({t['code']}) | "
                f"{t['buy_price']:.2f} | {t['sell_price']:.2f} | {t['shares']} | "
                f"¥{t['cost']:,.0f} | ¥{t['pnl']:+,.0f} | {t['pnl_pct']:+.2f}% | "
                f"{t['buy_reason']} | {t['sell_reason']} |"
            )

    if result["open_positions"]:
        lines.append("\n## 未平仓持仓\n")
        lines.append("| 标的 | 买入日 | 买入时间 | 买价 | 股数 | 成本 | 买入原因 |")
        lines.append("|------|--------|---------|------|------|------|---------|")
        for p in result["open_positions"]:
            lines.append(
                f"| {p['name']}({p['code']}) | {p['buy_date']} | {p['buy_time']} | "
                f"{p['buy_price']:.2f} | {p['shares']} | ¥{p['cost']:,.0f} | {p['buy_reason']} |"
            )

    lines.append("\n## 每日权益曲线\n")
    lines.append("| 日期 | 现金 | 持仓数 | 总权益 | 累计收益率 |")
    lines.append("|------|------|--------|--------|------------|")
    for d in result["daily_snapshots"]:
        ret = (d["total_value"] - result["initial_capital"]) / result["initial_capital"] * 100
        lines.append(
            f"| {d['date']} | ¥{d['capital']:,.0f} | {d['positions']} | "
            f"¥{d['total_value']:,.0f} | {ret:+.2f}% |"
        )

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘中监控多日回测（带真实买卖）")
    parser.add_argument("--start", default="2026-04-07", help="开始日期")
    parser.add_argument("--end", default="2026-04-17", help="结束日期")
    parser.add_argument("--output", help="JSON 输出文件")
    parser.add_argument("--statement", help="交割单 Markdown 输出文件")
    args = parser.parse_args()

    result = run_monitor_backtest(args.start, args.end)

    if args.output and result:
        with open(args.output, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 结果: {args.output}")

    if args.statement and result:
        statement = format_trade_statement(result)
        with open(args.statement, "w") as f:
            f.write(statement)
        print(f"交割单: {args.statement}")


if __name__ == "__main__":
    main()
