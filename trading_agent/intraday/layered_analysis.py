"""盘后三层架构分析 — 每日 15:10 执行

读取当日市场数据，通过三层架构（Layer1:LLM研判 → Layer2:量化选股 → Layer3:风控）
输出明日操作计划，并推送到飞书。

用法:
    python3 -m trading_agent.intraday.layered_analysis
    python3 -m trading_agent.intraday.layered_analysis --date 2026-04-17
    python3 -m trading_agent.intraday.layered_analysis --dry-run
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# 确保项目根目录在 sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

TRADING_DIR = os.path.expanduser("~/shared/trading")
INTRADAY_DB = os.path.join(TRADING_DIR, "intraday", "intraday.db")
CONCEPT_DB = os.path.join(TRADING_DIR, "stock_concept.db")
PORTFOLIO_FILE = os.path.join(TRADING_DIR, "portfolio_state.json")

# Layer 3 参数（与回测一致）
STOP_LOSS_PCT = -7.0
TAKE_PROFIT_PCT = 15.0
MAX_HOLD_DAYS = 5
MAX_POSITIONS = 2  # watchlist 只看最核心的 4 只（= MAX_POSITIONS × 2）
POSITION_PCT = 0.30


def load_portfolio() -> dict:
    """加载持仓状态"""
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE) as f:
            return json.load(f)
    return {"cash": 100_000, "positions": [], "history": []}


def save_portfolio(portfolio: dict):
    """保存持仓状态"""
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)


def get_latest_trading_date() -> str:
    """获取最近一个有数据的交易日"""
    import sqlite3
    conn = sqlite3.connect(INTRADAY_DB, timeout=10)
    try:
        row = conn.execute("SELECT MAX(date) FROM daily_bars").fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def get_current_price(code: str, date: str) -> Optional[float]:
    """获取个股当日收盘价"""
    import sqlite3
    conn = sqlite3.connect(INTRADAY_DB, timeout=10)
    try:
        row = conn.execute(
            "SELECT close FROM daily_bars WHERE date = ? AND code = ?",
            (date, code),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def run_analysis(date: str = "", dry_run: bool = False) -> dict:
    """执行盘后三层分析

    Returns:
        dict with judgment, candidates, sell_actions, report_text
    """
    from backtest.adapter import ReviewDataProvider, MarketJudgmentRunner
    from backtest.screener import screen_stocks, format_screening_result
    from backtest.layered_engine import _code_sentiment_fallback

    if not date:
        date = get_latest_trading_date()
    if not date:
        return {"error": "无可用交易日数据"}

    print(f"[盘后分析] {date}")

    # ── Layer 1: 市场研判（代码确定性优先，LLM 仅做参考） ──
    provider = ReviewDataProvider()
    snapshot = provider.load_market_snapshot(TRADING_DIR, date)
    t0 = time.time()

    # 情绪判断：代码确定性逻辑为主（保证回测可重现）
    judgment = {"sentiment_phase": _code_sentiment_fallback(snapshot)}

    # 板块方向：始终用涨停分布（确定性）
    # 排除 ST板块 + 无意义的概念板块（噪音）
    SECTOR_BLACKLIST = {"ST板块", "退市整理", "2025年报预增", "2026年报预增",
                         "融资融券", "央企国企改革", "国企改革", "深股通", "沪股通"}
    sector_dist = snapshot.get("sector_distribution", {})
    if sector_dist:
        filtered = [(s, c) for s, c in sector_dist.items() if s not in SECTOR_BLACKLIST]
        top2 = sorted(filtered, key=lambda x: -x[1])[:2]
        judgment["top_sectors"] = [s[0] for s in top2]

    # action_gate：基于情绪阶段（确定性映射）
    phase = judgment["sentiment_phase"]
    if phase in ("退潮", "冰点"):
        judgment["action_gate"] = "空仓"
    elif phase in ("修复", "升温", "高潮"):
        judgment["action_gate"] = "可买入"
    else:
        judgment["action_gate"] = "谨慎"

    elapsed = time.time() - t0
    print(f"  [Layer 1] {judgment['sentiment_phase']} | {judgment.get('action_gate')} | "
          f"{judgment.get('top_sectors', [])} ({elapsed:.1f}s)")

    # ── Layer 3a: 持仓检查 → 卖出决策 ──
    portfolio = load_portfolio()
    sell_actions = []

    for pos in portfolio.get("positions", []):
        buy_price = pos.get("buy_price", 0)
        if not buy_price or buy_price <= 0:
            continue  # 跳过 pending_buy（还没有实际买入价）

        current_price = get_current_price(pos["code"], date)
        if current_price is None:
            continue

        float_pnl = (current_price - buy_price) / buy_price * 100
        pos["current_price"] = current_price
        pos["float_pnl"] = round(float_pnl, 2)

        # 持仓天数
        from datetime import datetime as dt
        buy_dt = dt.strptime(pos["buy_date"], "%Y-%m-%d")
        now_dt = dt.strptime(date, "%Y-%m-%d")
        hold_days = (now_dt - buy_dt).days

        should_sell = False
        reason = ""

        if judgment.get("action_gate") == "空仓":
            should_sell = True
            reason = f"Layer1情绪门控: {judgment['sentiment_phase']}→空仓"
        elif float_pnl <= STOP_LOSS_PCT:
            should_sell = True
            reason = f"止损: 浮亏{float_pnl:.1f}%超过{STOP_LOSS_PCT}%"
        elif float_pnl >= TAKE_PROFIT_PCT:
            should_sell = True
            reason = f"止盈: 浮盈{float_pnl:.1f}%超过+{TAKE_PROFIT_PCT}%"
        elif hold_days >= MAX_HOLD_DAYS:
            should_sell = True
            reason = f"超时: 持仓{hold_days}天超过{MAX_HOLD_DAYS}天"

        if should_sell:
            sell_actions.append({
                "name": pos["name"], "code": pos["code"],
                "buy_price": pos["buy_price"], "current_price": current_price,
                "float_pnl": float_pnl, "hold_days": hold_days,
                "reason": reason,
            })

    # ── Layer 2: 量化选股 ──
    # 只计算已实际买入的持仓（排除 pending_buy）
    actual_positions = [p for p in portfolio.get("positions", [])
                       if p.get("buy_price", 0) > 0 and p.get("status") != "pending_buy"]
    remaining_positions = [p for p in actual_positions
                          if not any(s["code"] == p["code"] for s in sell_actions)]
    available_slots = MAX_POSITIONS - len(remaining_positions)

    print(f"  [Layer 2 前置] slots={available_slots} gate={judgment.get('action_gate')} sectors={judgment.get('top_sectors')}")
    if available_slots <= 0 or judgment.get("action_gate") == "空仓":
        candidates = []
        print(f"  [Layer 2] 跳过（slots={available_slots} gate={judgment.get('action_gate')}）")
    else:
        try:
            candidates = screen_stocks(
                date=date,
                top_sectors=judgment.get("top_sectors", []),
                action_gate=judgment.get("action_gate", "谨慎"),
                intraday_db=INTRADAY_DB,
                concept_db=CONCEPT_DB,
                max_picks=max(available_slots * 2, 4),
            )
            print(f"  [Layer 2 原始] {len(candidates)} 只")
            # 排除已持仓
            held_codes = {p["code"] for p in remaining_positions}
            candidates = [c for c in candidates if c.code not in held_codes]
            print(f"  [Layer 2 去重后] {len(candidates)} 只")
        except Exception as e:
            import traceback
            print(f"  [Layer 2 失败] {e}")
            traceback.print_exc()
            candidates = []

    if candidates:
        print(f"  [Layer 2] {format_screening_result(candidates)}")

    # ── 生成报告 ──
    report = _format_report(date, judgment, snapshot, candidates, sell_actions,
                           remaining_positions, portfolio)

    result = {
        "date": date,
        "judgment": judgment,
        "candidates": [{"name": c.name, "code": c.code, "score": c.score,
                        "breakdown": c.score_breakdown} for c in candidates],
        "sell_actions": sell_actions,
        "report": report,
    }

    if not dry_run:
        # 更新持仓状态
        _update_portfolio(portfolio, sell_actions, candidates, date)
        save_portfolio(portfolio)

        # 保存分析记录
        record_dir = os.path.join(TRADING_DIR, "layered_daily")
        os.makedirs(record_dir, exist_ok=True)
        with open(os.path.join(record_dir, f"{date}.json"), "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    return result


def _format_report(date, judgment, snapshot, candidates, sell_actions,
                   remaining_positions, portfolio) -> str:
    """格式化飞书推送报告"""
    lines = [f"📊 **盘后研判（{date}）**\n"]

    # 情绪 + 板块
    phase = judgment.get("sentiment_phase", "?")
    gate = judgment.get("action_gate", "?")
    sectors = judgment.get("top_sectors", [])
    logic = judgment.get("sector_logic", "")
    lines.append(f"情绪：**{phase}** | 门控：**{gate}** | 主线：{', '.join(sectors) if sectors else '无'}")
    if logic:
        lines.append(f"板块逻辑：{logic}")

    # 市场数据
    lu = snapshot.get("limit_up_count", 0)
    ld = snapshot.get("limit_down_count", 0)
    blown = snapshot.get("blown_rate", 0)
    lines.append(f"涨停 {lu} | 跌停 {ld} | 炸板率 {blown:.0f}%")
    lines.append("")

    # 卖出计划
    if sell_actions:
        lines.append("📋 **明日卖出计划：**")
        for s in sell_actions:
            emoji = "🔴" if s["float_pnl"] < 0 else "🟢"
            lines.append(f"{emoji} {s['name']}({s['code']}) — 浮盈{s['float_pnl']:+.1f}% — {s['reason']}")
        lines.append("")

    # 买入候选
    if candidates:
        lines.append("🟢 **明日买入候选：**")
        for i, c in enumerate(candidates, 1):
            lines.append(f"{i}. **{c.name}**({c.code}) — 评分{c.score:.0f}分 | "
                        f"{c.board_count}连板 | 首封{c.first_limit_time} | "
                        f"炸板{c.blown_count}次 | 成交{c.amount/1e8:.1f}亿 | 30%仓位")
            # 评分明细
            bd = c.score_breakdown
            lines.append(f"   评分: {' | '.join(f'{k}={v}' for k, v in bd.items())}")
        lines.append("")
    elif gate != "空仓":
        lines.append("⚪ 无符合条件的买入候选\n")

    # 持仓状态
    if remaining_positions:
        lines.append("📦 **继续持有：**")
        for p in remaining_positions:
            pnl = p.get("float_pnl", 0)
            lines.append(f"- {p['name']}({p['code']}) 买入{p['buy_price']:.2f} "
                        f"当前{p.get('current_price', 0):.2f} {pnl:+.1f}%")
        lines.append("")

    # 风控
    total_positions = len(remaining_positions) + len(candidates)
    cash_pct = 100 - total_positions * 30
    lines.append(f"⚠️ 仓位：{total_positions}只×30% = {total_positions*30}%，现金{cash_pct}%")

    return "\n".join(lines)


def _update_portfolio(portfolio, sell_actions, candidates, date):
    """更新持仓状态（注意：实际买卖在次日开盘执行，这里只更新计划）"""
    # 标记待卖出的持仓
    sell_codes = {s["code"] for s in sell_actions}
    new_positions = []
    for p in portfolio.get("positions", []):
        if p["code"] in sell_codes:
            # 移入历史记录
            action = next((s for s in sell_actions if s["code"] == p["code"]), {})
            portfolio.setdefault("history", []).append({
                **p,
                "sell_date": date,
                "sell_reason": action.get("reason", ""),
                "pnl_pct": action.get("float_pnl", 0),
            })
        else:
            new_positions.append(p)

    # 添加待买入的标的（标记为 pending，次日竞价确认后才实际执行）
    for c in candidates:
        new_positions.append({
            "name": c.name,
            "code": c.code,
            "buy_date": date,  # 推荐日，实际买入在 D+1
            "buy_price": 0,    # 待 D+1 开盘价确认
            "status": "pending_buy",
            "score": c.score,
        })

    portfolio["positions"] = new_positions
    portfolio["last_update"] = date


def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘后三层架构分析")
    parser.add_argument("--date", help="分析日期（默认最新交易日）")
    parser.add_argument("--dry-run", action="store_true", help="只输出不保存")
    args = parser.parse_args()

    result = run_analysis(date=args.date or "", dry_run=args.dry_run)

    if "error" in result:
        print(f"错误: {result['error']}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print(result["report"])
    print("=" * 60)

    # 输出 JSON 供 send_message 使用
    print(json.dumps({
        "date": result["date"],
        "report": result["report"],
        "candidates": result["candidates"],
        "sell_actions": result["sell_actions"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
