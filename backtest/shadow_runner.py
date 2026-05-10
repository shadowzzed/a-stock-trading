"""策略池影子交易运行器

功能：
- 在指定日期区间对所有已注册策略运行回测
- 将结果写入 ~/shared/strategy_shadow/YYYY-MM-DD/
- 计算各策略健康度指标
- 生成飞书推送报告

用法：
  python3 -m backtest.shadow_runner                           # 日度（回测最近 1 个月）
  python3 -m backtest.shadow_runner --start 2026-04-01 --end 2026-04-17
  python3 -m backtest.shadow_runner --weekly                  # 周度体检模式
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backtest.strategies import STRATEGIES, get_strategy, list_strategies
from backtest.monitor_backtest_v2 import run_monitor_backtest_v2

SHADOW_DIR = os.path.expanduser("~/shared/strategy_shadow")


def compute_health(result: dict, lookback_trades: int = 20) -> dict:
    """计算策略健康度指标。"""
    trades = result.get("trades", [])
    recent = trades[-lookback_trades:] if len(trades) > lookback_trades else trades
    if not recent:
        return {"status": "no_data"}

    wins = [t for t in recent if t["pnl"] > 0]
    losses = [t for t in recent if t["pnl"] <= 0]
    avg_pnl_pct = sum(t["pnl_pct"] for t in recent) / len(recent)
    win_rate = len(wins) / len(recent) * 100

    # 连续亏损
    max_streak = 0
    cur_streak = 0
    for t in recent:
        if t["pnl"] < 0:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 0

    # 最大回撤（基于账户权益曲线：从 initial cash 累加 pnl）
    cum_pnl = 0
    peak_pnl = 0
    max_dd_amount = 0
    initial = 100_000
    for t in recent:
        cum_pnl += t["pnl"]
        peak_pnl = max(peak_pnl, cum_pnl)
        dd = peak_pnl - cum_pnl
        max_dd_amount = max(max_dd_amount, dd)
    max_dd = max_dd_amount / initial * 100  # 账户权益回撤百分比

    # 健康度等级
    status = "healthy"
    warnings = []
    if win_rate < 40:
        status = "stop"
        warnings.append(f"胜率 {win_rate:.0f}% < 40%")
    elif win_rate < 55:
        warnings.append(f"胜率 {win_rate:.0f}% 边缘")

    if avg_pnl_pct < -5:
        status = "stop"
        warnings.append(f"平均 {avg_pnl_pct:+.2f}% < -5%")
    elif avg_pnl_pct < 0:
        warnings.append(f"平均 {avg_pnl_pct:+.2f}% 负值")

    if max_streak > 5:
        status = "stop"
        warnings.append(f"连亏 {max_streak} 笔 > 5")
    elif max_streak > 3:
        warnings.append(f"连亏 {max_streak} 笔")

    if max_dd > 15:
        status = "stop"
        warnings.append(f"回撤 {max_dd:.1f}% > 15%")
    elif max_dd > 10:
        warnings.append(f"回撤 {max_dd:.1f}%")

    if len(warnings) >= 2 and status == "healthy":
        status = "warning"

    return {
        "status": status,            # healthy / warning / stop
        "sample_size": len(recent),
        "win_rate": round(win_rate, 1),
        "avg_pnl_pct": round(avg_pnl_pct, 2),
        "max_consecutive_loss": max_streak,
        "max_drawdown_pct": round(max_dd, 2),
        "warnings": warnings,
    }


def run_all_strategies(start_date: str, end_date: str) -> dict:
    """对所有策略跑影子交易。"""
    results = {}
    for name in list_strategies():
        strat = get_strategy(name)
        print(f"\n[{name}] {strat.description}")
        try:
            r = run_monitor_backtest_v2(start_date, end_date, strategy_params=strat.as_params_dict())
            health = compute_health(r)
            results[name] = {
                "description": strat.description,
                "params": strat.as_params_dict(),
                "return_pct": r["total_return_pct"],
                "trades": r["total_trades"],
                "win_rate": r["win_rate"],
                "wins": r["win_trades"],
                "losses": r["loss_trades"],
                "health": health,
                "final_capital": r["final_capital"],
            }
            print(f"  收益 {r['total_return_pct']:+.2f}% | {r['total_trades']} 笔 | 胜率 {r['win_rate']}% | 健康 {health['status']}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            results[name] = {"error": str(e)}
    return results


def format_report(start: str, end: str, results: dict) -> str:
    """生成飞书 Markdown 报告。"""
    lines = []
    lines.append(f"# 策略池体检报告 {start} ~ {end}\n")

    # 排序：按收益降序
    valid = [(n, r) for n, r in results.items() if "error" not in r]
    valid.sort(key=lambda x: -x[1]["return_pct"])

    lines.append("## 策略排名")
    lines.append("")
    lines.append("| 状态 | 策略 | 收益 | 笔数 | 胜率 | 平均/笔 | 最大连亏 |")
    lines.append("|------|------|------|------|------|--------|---------|")
    for name, r in valid:
        h = r["health"]
        status_icon = {"healthy": "✅", "warning": "⚠️", "stop": "🚫", "no_data": "⚪"}.get(h["status"], "⚪")
        lines.append(
            f"| {status_icon} | {name} | {r['return_pct']:+.2f}% | {r['trades']} | "
            f"{r['win_rate']}% | {h.get('avg_pnl_pct', '-')}% | {h.get('max_consecutive_loss', '-')} |"
        )

    lines.append("\n## 健康度详情\n")
    for name, r in valid:
        h = r["health"]
        lines.append(f"### {name}")
        lines.append(f"- {r['description']}")
        lines.append(f"- 收益 **{r['return_pct']:+.2f}%** ({r['trades']}笔 胜率 {r['win_rate']}%)")
        if h.get("warnings"):
            lines.append(f"- ⚠️ {' / '.join(h['warnings'])}")
        lines.append("")

    # 推荐行动
    healthy = [n for n, r in valid if r["health"]["status"] == "healthy"]
    warn = [n for n, r in valid if r["health"]["status"] == "warning"]
    stop = [n for n, r in valid if r["health"]["status"] == "stop"]

    lines.append("## 行动建议")
    if healthy:
        lines.append(f"- ✅ **继续运行**：{', '.join(healthy)}")
    if warn:
        lines.append(f"- ⚠️ **观察降仓**：{', '.join(warn)}")
    if stop:
        lines.append(f"- 🚫 **下线**：{', '.join(stop)}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--weekly", action="store_true")
    parser.add_argument("--send-feishu", action="store_true", help="通过 lark-cli 推送到飞书")
    parser.add_argument("--chat-id", default="oc_83cc5cd76a6bfc1940d6c07034197dfc")
    args = parser.parse_args()

    # 默认区间：最近 33 个交易日（约 1.5 月）
    today = datetime.now().strftime("%Y-%m-%d")
    if args.start and args.end:
        start, end = args.start, args.end
    elif args.weekly:
        # 周度体检：最近 1 个月
        d_end = datetime.now()
        d_start = d_end - timedelta(days=30)
        start, end = d_start.strftime("%Y-%m-%d"), d_end.strftime("%Y-%m-%d")
    else:
        start, end = "2026-03-03", today

    print(f"== 影子交易运行: {start} → {end} ==")
    results = run_all_strategies(start, end)

    # 写入磁盘
    out_dir = os.path.join(SHADOW_DIR, today)
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "shadow_results.json")
    with open(json_path, "w") as f:
        json.dump({"start": start, "end": end, "results": results}, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {json_path}")

    # 报告
    report = format_report(start, end, results)
    report_path = os.path.join(out_dir, "策略池报告.md")
    with open(report_path, "w") as f:
        f.write(report)
    print(f"报告已保存: {report_path}")

    # 飞书推送
    if args.send_feishu:
        import subprocess
        try:
            subprocess.run([
                "lark-cli", "im", "+messages-send",
                "--chat-id", args.chat_id,
                "--file", report_path,
            ], check=False)
            subprocess.run([
                "lark-cli", "im", "+messages-send",
                "--chat-id", args.chat_id,
                "--markdown", f"📊 策略池体检完成（{start}~{end}）\n已发送报告文件：`策略池报告.md`",
            ], check=False)
        except Exception as e:
            print(f"飞书推送失败: {e}")


if __name__ == "__main__":
    main()
