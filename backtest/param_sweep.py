"""Phase 2: 零 LLM 成本参数网格搜索

先用 Layer 1 缓存（或生成新缓存），然后对 Layer 2/3 参数进行穷举，
每组参数组合只需要几秒钟（纯代码计算，不调 LLM）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import itertools
from dataclasses import dataclass, field
from typing import Optional

from .screener import screen_stocks, ScoredStock
from .adapter import ReviewDataProvider, MarketJudgmentRunner


@dataclass
class SweepResult:
    """单组参数的回测结果"""
    params: dict
    total_pnl_pct: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_loss_ratio: float = 0.0
    max_drawdown_pct: float = 0.0


def cache_layer1(
    data_dir: str,
    start_date: str,
    end_date: str,
    cache_dir: str,
) -> dict:
    """生成或加载 Layer 1 缓存

    Returns:
        dict: {date: {sentiment_phase, top_sectors, action_gate, snapshot}}
    """
    cache_file = os.path.join(cache_dir, "layer1_cache.json")

    if os.path.exists(cache_file):
        with open(cache_file, "r") as f:
            cache = json.load(f)
        print(f"[Layer 1 缓存] 已加载 {len(cache)} 天")
        return cache

    os.makedirs(cache_dir, exist_ok=True)
    provider = ReviewDataProvider()
    runner = MarketJudgmentRunner()
    dates = provider.discover_dates(data_dir, start_date, end_date)

    cache = {}
    for i, date in enumerate(dates):
        print(f"  [Layer 1] {i+1}/{len(dates)} {date}...", end=" ")
        try:
            snapshot = provider.load_market_snapshot(data_dir, date)
            judgment = runner.run(snapshot)

            # Fallback
            if not judgment.get("top_sectors"):
                sector_dist = snapshot.get("sector_distribution", {})
                if sector_dist:
                    top2 = sorted(sector_dist.items(), key=lambda x: -x[1])[:2]
                    judgment["top_sectors"] = [s[0] for s in top2]

            if judgment.get("sentiment_phase") == "未知":
                judgment["sentiment_phase"] = _code_sentiment(snapshot)
                phase = judgment["sentiment_phase"]
                if phase in ("退潮", "冰点"):
                    judgment["action_gate"] = "空仓"
                elif phase in ("修复", "升温", "高潮"):
                    judgment["action_gate"] = "可买入"
                else:
                    judgment["action_gate"] = "谨慎"

            cache[date] = {
                "judgment": judgment,
                "snapshot": snapshot,
            }
            print(f"{judgment['sentiment_phase']} | {judgment['action_gate']} | {judgment.get('top_sectors', [])}")
        except Exception as e:
            print(f"失败: {e}")
            snapshot = provider.load_market_snapshot(data_dir, date)
            sector_dist = snapshot.get("sector_distribution", {})
            top2 = sorted(sector_dist.items(), key=lambda x: -x[1])[:2] if sector_dist else []
            phase = _code_sentiment(snapshot)
            gate = "空仓" if phase in ("退潮", "冰点") else ("可买入" if phase in ("修复", "升温") else "谨慎")
            cache[date] = {
                "judgment": {"sentiment_phase": phase, "market_type": "震荡日",
                             "top_sectors": [s[0] for s in top2], "action_gate": gate},
                "snapshot": snapshot,
            }

    with open(cache_file, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"[Layer 1 缓存] 已保存 {len(cache)} 天到 {cache_file}")
    return cache


def _code_sentiment(snapshot: dict) -> str:
    lu = snapshot.get("limit_up_count", 0)
    ld = snapshot.get("limit_down_count", 0)
    blown = snapshot.get("blown_rate", 0)
    if lu < 20 and ld > 15:
        return "冰点"
    if lu < 30 and ld > 10:
        return "退潮"
    if lu > 70 and blown < 30:
        return "高潮"
    if lu >= 50:
        return "升温"
    if lu >= 30:
        return "修复"
    if blown > 50:
        return "退潮"
    return "分歧"


def simulate_with_params(
    layer1_cache: dict,
    data_dir: str,
    dates: list[str],
    stop_loss: float,
    take_profit: float,
    max_hold_days: int,
    max_positions: int,
    position_pct: float = 0.30,
    initial_capital: float = 100_000.0,
) -> SweepResult:
    """用缓存的 Layer 1 结果 + 指定参数跑一次模拟（零 LLM 成本）"""

    db_path = os.path.join(data_dir, "intraday", "intraday.db")
    concept_db = os.path.expanduser("~/shared/trading/stock_concept.db")

    params = {
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "max_hold_days": max_hold_days,
        "max_positions": max_positions,
    }
    result = SweepResult(params=params)

    cash = initial_capital
    positions = []  # [{code, name, buy_date, buy_price, shares, cost, hold_days}]
    trades = []     # [{pnl_pct}]
    peak_value = initial_capital

    pairs = list(zip(dates[:-1], dates[1:]))

    for day_d, day_d1 in pairs:
        cached = layer1_cache.get(day_d)
        if not cached:
            continue

        judgment = cached["judgment"]

        # ── Layer 3a: 卖出检查 ──
        remaining = []
        for pos in positions:
            pos["hold_days"] += 1
            open_price = _get_open(db_path, day_d, pos["code"])
            if open_price is None:
                remaining.append(pos)
                continue

            float_pnl = (open_price - pos["buy_price"]) / pos["buy_price"] * 100
            should_sell = False
            if float_pnl <= stop_loss:
                should_sell = True
            elif float_pnl >= take_profit:
                should_sell = True
            elif pos["hold_days"] >= max_hold_days:
                should_sell = True

            if should_sell:
                sell_amount = pos["shares"] * open_price
                pnl_pct = (open_price - pos["buy_price"]) / pos["buy_price"] * 100
                cash += sell_amount
                trades.append({"pnl_pct": pnl_pct})
            else:
                remaining.append(pos)

        positions = remaining

        # Layer 1 空仓门控
        if judgment["action_gate"] == "空仓" and positions:
            for pos in positions:
                price = _get_open(db_path, day_d, pos["code"])
                if price is None:
                    price = pos["buy_price"]
                sell_amount = pos["shares"] * price
                pnl_pct = (price - pos["buy_price"]) / pos["buy_price"] * 100
                cash += sell_amount
                trades.append({"pnl_pct": pnl_pct})
            positions = []

        # ── Layer 2: 量化选股 ──
        available = max_positions - len(positions)
        if available <= 0 or judgment["action_gate"] == "空仓":
            candidates = []
        else:
            try:
                candidates = screen_stocks(
                    date=day_d,
                    top_sectors=judgment.get("top_sectors", []),
                    action_gate=judgment["action_gate"],
                    intraday_db=db_path,
                    concept_db=concept_db,
                    max_picks=available,
                )
            except Exception:
                candidates = []

        held_codes = {p["code"] for p in positions}
        candidates = [c for c in candidates if c.code not in held_codes]

        # ── Layer 3b: 买入 ──
        for c in candidates:
            if len(positions) >= max_positions:
                break
            buy_price = _get_open(db_path, day_d1, c.code)
            if buy_price is None or buy_price <= 0:
                continue

            total_value = cash + sum(p["cost"] for p in positions)
            buy_amount = min(total_value * position_pct, cash)
            shares = int(buy_amount / buy_price / 100) * 100
            if shares <= 0:
                continue

            cost = shares * buy_price
            cash -= cost
            positions.append({
                "code": c.code, "name": c.name,
                "buy_date": day_d1, "buy_price": buy_price,
                "shares": shares, "cost": cost, "hold_days": 0,
            })

        # 跟踪最大回撤
        total_val = cash + sum(p["cost"] for p in positions)
        if total_val > peak_value:
            peak_value = total_val
        dd = (peak_value - total_val) / peak_value * 100
        if dd > result.max_drawdown_pct:
            result.max_drawdown_pct = dd

    # 强制平仓
    if positions:
        last_date = dates[-1]
        for pos in positions:
            price = _get_close(db_path, last_date, pos["code"])
            if price is None:
                price = pos["buy_price"]
            pnl_pct = (price - pos["buy_price"]) / pos["buy_price"] * 100
            cash += pos["shares"] * price
            trades.append({"pnl_pct": pnl_pct})

    # 汇总
    result.total_pnl_pct = (cash - initial_capital) / initial_capital * 100
    result.trade_count = len(trades)
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    result.win_count = len(wins)
    result.win_rate = len(wins) / len(trades) * 100 if trades else 0
    result.avg_win_pct = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    result.avg_loss_pct = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    result.profit_loss_ratio = abs(result.avg_win_pct / result.avg_loss_pct) if result.avg_loss_pct else 0

    return result


def _get_open(db_path, date, code):
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        r = conn.execute("SELECT open FROM daily_bars WHERE date=? AND code=?", (date, code)).fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def _get_close(db_path, date, code):
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        r = conn.execute("SELECT close FROM daily_bars WHERE date=? AND code=?", (date, code)).fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def run_grid_search(
    data_dir: str,
    start_date: str,
    end_date: str,
    cache_dir: str,
    output_file: str,
    param_grid: Optional[dict] = None,
) -> list[SweepResult]:
    """运行参数网格搜索

    Args:
        param_grid: 参数网格，默认值:
            {
                "stop_loss": [-3, -5, -7, -10],
                "take_profit": [10, 15, 20, 30],
                "max_hold_days": [3, 5, 7],
                "max_positions": [1, 2, 3],
            }
    """
    if param_grid is None:
        param_grid = {
            "stop_loss": [-3, -5, -7, -10],
            "take_profit": [10, 15, 20, 30],
            "max_hold_days": [3, 5, 7],
            "max_positions": [1, 2, 3],
        }

    # 1. 生成或加载 Layer 1 缓存
    print("=" * 60)
    print("Phase 2: 零 LLM 成本参数网格搜索")
    print("=" * 60)

    layer1_cache = cache_layer1(data_dir, start_date, end_date, cache_dir)

    provider = ReviewDataProvider()
    dates = provider.discover_dates(data_dir, start_date, end_date)

    # 2. 生成参数组合
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combos = list(itertools.product(*values))
    total = len(combos)

    print(f"\n参数组合: {total} 种")
    print(f"  止损: {param_grid['stop_loss']}")
    print(f"  止盈: {param_grid['take_profit']}")
    print(f"  持仓: {param_grid['max_hold_days']}")
    print(f"  持仓数: {param_grid['max_positions']}")
    print()

    # 3. 逐个模拟
    results = []
    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        r = simulate_with_params(
            layer1_cache=layer1_cache,
            data_dir=data_dir,
            dates=dates,
            stop_loss=params["stop_loss"],
            take_profit=params["take_profit"],
            max_hold_days=params["max_hold_days"],
            max_positions=params["max_positions"],
        )
        results.append(r)

        if (i + 1) % 20 == 0 or i + 1 == total:
            print(f"  [{i+1}/{total}] 最新: 止损{params['stop_loss']}% 止盈{params['take_profit']}% "
                  f"持仓{params['max_hold_days']}天 {params['max_positions']}只 → "
                  f"{r.total_pnl_pct:+.2f}% (胜率{r.win_rate:.0f}%)")

    # 4. 排序
    results.sort(key=lambda x: x.total_pnl_pct, reverse=True)

    # 5. 输出结果
    print(f"\n{'='*60}")
    print("Top 10 参数组合:")
    print(f"{'='*60}")
    print(f"{'排名':>4} | {'止损':>5} | {'止盈':>5} | {'持仓':>4} | {'数量':>4} | "
          f"{'收益':>8} | {'胜率':>5} | {'盈亏比':>6} | {'笔数':>4} | {'回撤':>6}")
    print("-" * 80)
    for i, r in enumerate(results[:10]):
        p = r.params
        print(f"{i+1:>4} | {p['stop_loss']:>4}% | {p['take_profit']:>4}% | "
              f"{p['max_hold_days']:>3}天 | {p['max_positions']:>3}只 | "
              f"{r.total_pnl_pct:>+7.2f}% | {r.win_rate:>4.0f}% | "
              f"{r.profit_loss_ratio:>5.2f} | {r.trade_count:>3} | "
              f"{r.max_drawdown_pct:>5.1f}%")

    print(f"\nWorst 5:")
    for i, r in enumerate(results[-5:]):
        p = r.params
        print(f"  止损{p['stop_loss']}% 止盈{p['take_profit']}% "
              f"持仓{p['max_hold_days']}天 {p['max_positions']}只 → "
              f"{r.total_pnl_pct:+.2f}%")

    # 保存完整结果
    output = {
        "param_grid": param_grid,
        "total_combos": total,
        "results": [
            {
                "rank": i + 1,
                "params": r.params,
                "pnl_pct": round(r.total_pnl_pct, 2),
                "trade_count": r.trade_count,
                "win_count": r.win_count,
                "win_rate": round(r.win_rate, 1),
                "avg_win_pct": round(r.avg_win_pct, 2),
                "avg_loss_pct": round(r.avg_loss_pct, 2),
                "profit_loss_ratio": round(r.profit_loss_ratio, 2),
                "max_drawdown_pct": round(r.max_drawdown_pct, 1),
            }
            for i, r in enumerate(results)
        ],
    }
    os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
    with open(output_file, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n完整结果已保存到: {output_file}")

    return results
