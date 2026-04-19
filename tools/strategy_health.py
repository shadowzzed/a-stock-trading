#!/usr/bin/env python3
"""
策略健康度每日监控 — 滚动回测 + 阈值告警

每晚盘后跑，对每个 active 策略执行：
  1. 短期（近 5 日）、中期（近 20 日）、长期（近 60 日）滚动回测
  2. 计算收益/胜率/最大回撤/夏普
  3. 写入 strategy_backtest_log
  4. 对比阈值，超出则打印告警（ERR 退出码 2）

用法:
  python3 trading/strategy_health.py                       # 所有 active 策略
  python3 trading/strategy_health.py --strategy <id>       # 指定策略
  python3 trading/strategy_health.py --windows 5 20        # 仅跑指定窗口
  python3 trading/strategy_health.py --alert               # 触发阈值时打印飞书告警
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strategy_registry import (
    get_conn as registry_conn,
    init_schema as init_registry,
    log_backtest,
)

INTRADAY_DB = os.path.expanduser("~/shared/trading/intraday/intraday.db")
TRADING_ROOT = os.path.expanduser("~/src/a-stock-trading")

# 告警阈值（可调）
THRESHOLDS = {
    "min_win_rate": 40.0,       # 胜率 < 40% 告警
    "max_drawdown": -15.0,      # 回撤 > 15% 告警
    "min_rolling_5d_return": -5.0,  # 近 5 日 -5% 告警
    "min_rolling_20d_return": 0.0,  # 近 20 日 < 0% 告警
}


def _get_trading_dates(conn: sqlite3.Connection, end: str, window: int) -> tuple[str, str]:
    """取最近 window 个交易日的起止日期"""
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_bars WHERE date <= ? "
        "ORDER BY date DESC LIMIT ?",
        (end, window),
    ).fetchall()
    if not rows:
        return None, None
    dates = sorted(r[0] for r in rows)
    return dates[0], dates[-1]


def _run_backtest(start: str, end: str) -> dict:
    """调方向二回测，返回关键指标"""
    tmp_result = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_result.close()
    try:
        subprocess.run(
            ["python3", "-m", "backtest.monitor_backtest",
             "--start", start, "--end", end,
             "--output", tmp_result.name],
            cwd=TRADING_ROOT,
            check=True,
            capture_output=True,
            timeout=1800,
        )
        with open(tmp_result.name) as f:
            result = json.load(f)
    finally:
        os.unlink(tmp_result.name)

    snaps = result.get("daily_snapshots", [])
    if not snaps:
        return {}

    initial = result["initial_capital"]
    final_cost = result["final_total_value"]
    final_mkt = snaps[-1]["total_value"]

    # 最大回撤（按市值权益曲线）
    peak = initial
    max_dd = 0
    for s in snaps:
        peak = max(peak, s["total_value"])
        dd = (s["total_value"] - peak) / peak * 100
        max_dd = min(max_dd, dd)

    # 简单夏普（对日收益率）
    daily_returns = []
    for i in range(1, len(snaps)):
        prev = snaps[i-1]["total_value"]
        cur = snaps[i]["total_value"]
        if prev > 0:
            daily_returns.append((cur - prev) / prev)
    sharpe = 0.0
    if len(daily_returns) >= 2:
        import statistics
        mean_r = statistics.mean(daily_returns)
        std_r = statistics.stdev(daily_returns)
        if std_r > 0:
            sharpe = (mean_r / std_r) * (252 ** 0.5)  # 年化

    return {
        "return_cost": result["total_return_pct"],
        "return_market": (final_mkt - initial) / initial * 100,
        "trade_count": result["stats"]["total_trades"],
        "win_count": result["stats"]["wins"],
        "win_rate": result["stats"]["win_rate"],
        "max_drawdown": max_dd,
        "sharpe": round(sharpe, 2),
        "metadata": {"open_positions": len(result.get("open_positions", []))},
    }


def _check_thresholds(window: int, metrics: dict) -> list[str]:
    """对比阈值，返回告警文本列表"""
    alerts = []
    if metrics.get("win_rate") is not None and metrics["win_rate"] < THRESHOLDS["min_win_rate"]:
        alerts.append(f"胜率 {metrics['win_rate']:.1f}% < {THRESHOLDS['min_win_rate']:.0f}%")
    if metrics.get("max_drawdown") is not None and metrics["max_drawdown"] < THRESHOLDS["max_drawdown"]:
        alerts.append(f"回撤 {metrics['max_drawdown']:.1f}% < {THRESHOLDS['max_drawdown']:.0f}%")
    if window == 5 and metrics.get("return_market") is not None \
            and metrics["return_market"] < THRESHOLDS["min_rolling_5d_return"]:
        alerts.append(f"近5日收益 {metrics['return_market']:+.2f}% < {THRESHOLDS['min_rolling_5d_return']:.0f}%")
    if window == 20 and metrics.get("return_market") is not None \
            and metrics["return_market"] < THRESHOLDS["min_rolling_20d_return"]:
        alerts.append(f"近20日收益 {metrics['return_market']:+.2f}% < {THRESHOLDS['min_rolling_20d_return']:.0f}%")
    return alerts


def run_health_check(strategy_id: str = None, windows: list = None,
                     alert_mode: bool = False) -> int:
    windows = windows or [5, 20, 60]

    conn = registry_conn()
    init_registry(conn)

    # 选择 active 策略
    if strategy_id:
        strategies = conn.execute(
            "SELECT id, name FROM strategy_versions WHERE id=?", (strategy_id,)
        ).fetchall()
    else:
        strategies = conn.execute(
            "SELECT id, name FROM strategy_versions WHERE status='active'"
        ).fetchall()

    if not strategies:
        print("没有找到 active 策略。先运行 strategy_registry.py register")
        return 0

    intraday = sqlite3.connect(INTRADAY_DB, timeout=10)
    end_date = intraday.execute(
        "SELECT MAX(date) FROM daily_bars"
    ).fetchone()[0]

    max_alert_level = 0
    all_alerts = []

    for sid, sname in strategies:
        print(f"\n=== {sname} [{sid}] ===")
        for window in windows:
            start, end = _get_trading_dates(intraday, end_date, window)
            if not start:
                continue
            # 需要至少 2 天才能回测
            if start == end:
                continue
            print(f"  [{window}d] {start} → {end}")
            try:
                metrics = _run_backtest(start, end)
            except Exception as e:
                print(f"    回测失败: {e}")
                continue
            if not metrics:
                continue

            # 写入
            log_backtest(sid, window, start, end, metrics)

            # 打印
            rc = metrics["return_cost"]
            rm = metrics["return_market"]
            wr = metrics["win_rate"]
            dd = metrics["max_drawdown"]
            sh = metrics["sharpe"]
            tc = metrics["trade_count"]
            print(f"    收益: 成本 {rc:+.2f}% / 市值 {rm:+.2f}% | "
                  f"{tc} 笔 胜率 {wr:.1f}% | 回撤 {dd:.1f}% | 夏普 {sh:.2f}")

            # 阈值检查
            alerts = _check_thresholds(window, metrics)
            if alerts:
                max_alert_level = max(max_alert_level, 1)
                print(f"    ⚠️  告警: {'; '.join(alerts)}")
                all_alerts.append(f"{sname}[{window}d]: {'; '.join(alerts)}")

    intraday.close()
    conn.close()

    if all_alerts:
        print("\n" + "="*60)
        print(f"⚠️ 健康度监控发现 {len(all_alerts)} 条告警")
        for a in all_alerts:
            print(f"  - {a}")
        if alert_mode:
            _send_alert("\n".join(all_alerts))
    else:
        print("\n✓ 所有策略健康度正常")

    return max_alert_level


def _send_alert(text: str):
    """通过 MCP 发送飞书告警（需要在 Agent 环境下）"""
    # 留空：Agent 运行时可 import mcp 工具发送
    print(f"[ALERT] 飞书告警文本准备完毕（{len(text)} 字符）")
    with open("/tmp/strategy_health_alert.txt", "w") as f:
        f.write(text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy")
    parser.add_argument("--windows", nargs="+", type=int, default=[5, 20, 60])
    parser.add_argument("--alert", action="store_true")
    args = parser.parse_args()

    level = run_health_check(args.strategy, args.windows, args.alert)
    sys.exit(level)


if __name__ == "__main__":
    main()
