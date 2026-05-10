"""方向二回测参数敏感性扫描

扫描止损/止盈组合，找最优参数。
"""
import json
import os
import sys
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from trading_agent.intraday import monitor
from backtest.monitor_backtest_v2 import run_monitor_backtest_v2


PARAMS = [
    # (stop_loss, take_profit)
    (-5.0, 10.0),
    (-5.0, 15.0),
    (-6.0, 12.0),
    (-7.0, 10.0),
    (-7.0, 15.0),
    (-7.0, 20.0),
    (-10.0, 15.0),
    (-10.0, 20.0),
    (-10.0, 25.0),
]


def run_single(stop_loss: float, take_profit: float) -> dict:
    # 动态修改 monitor.py 的全局常量
    monitor.STOP_LOSS_PCT = stop_loss
    monitor.TAKE_PROFIT_PCT = take_profit
    print(f"\n=== 参数: stop={stop_loss}  profit={take_profit} ===", flush=True)
    result = run_monitor_backtest_v2("2026-03-03", "2026-04-17")
    return {
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "final_capital": result["final_capital"],
        "return_pct": result["total_return_pct"],
        "trades": result["total_trades"],
        "wins": result["win_trades"],
        "losses": result["loss_trades"],
        "win_rate": result["win_rate"],
    }


def main():
    results = []
    for sl, tp in PARAMS:
        try:
            r = run_single(sl, tp)
            results.append(r)
        except Exception as e:
            import traceback
            traceback.print_exc()

    # 排序
    results.sort(key=lambda x: -x["return_pct"])

    out_path = os.path.expanduser("~/shared/backtest/param_sweep_v4.json")
    with open(out_path, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print("\n\n" + "=" * 60)
    print(f"参数扫描结果（{len(results)} 组）：")
    print(f"{'止损':>6}{'止盈':>6}{'收益':>10}{'笔数':>6}{'胜率':>6}")
    for r in results:
        print(f"{r['stop_loss']:>6.1f}{r['take_profit']:>6.1f}"
              f"{r['return_pct']:>9.2f}%{r['trades']:>6}{r['win_rate']:>5.1f}%")
    print(f"\n输出: {out_path}")


if __name__ == "__main__":
    main()
