"""盘中连续监控系统 — 每分钟运行，跟踪标的+发现机会

两大功能：
1. 跟踪标的监控：对盘后推荐的标的实时跟踪价格变化，检测买入/卖出信号
2. 全市场扫描：在主线板块内实时发现新封板标的

状态持久化到文件，每分钟更新，支持断点续跑。

用法:
    python3 -m trading_agent.intraday.monitor              # 正常运行（每分钟执行一次）
    python3 -m trading_agent.intraday.monitor --backtest 2026-04-14  # 回测模式
"""

from __future__ import annotations

import json
import os
import sys
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

TRADING_DIR = os.path.expanduser("~/shared/trading")
INTRADAY_DB = os.path.join(TRADING_DIR, "intraday", "intraday.db")
CONCEPT_DB = os.path.join(TRADING_DIR, "stock_concept.db")
PORTFOLIO_FILE = os.path.join(TRADING_DIR, "portfolio_state.json")
MONITOR_STATE_FILE = os.path.join(TRADING_DIR, "monitor_state.json")

# 信号阈值
AUCTION_STRONG_PCT = 3.0     # 竞价高开超过此比例 = 超预期
AUCTION_WEAK_PCT = -2.0      # 竞价低开超过此比例 = 低于预期
STOP_LOSS_PCT = -7.0          # 盘中浮亏止损
TAKE_PROFIT_PCT = 15.0        # 盘中浮盈止盈
MIN_SCORE_FOR_OPPORTUNITY = 6  # 新机会最低评分（从 8 降到 6，让盘中扫描更敏感）
TREND_BREAKOUT_PCT = 5.0      # 趋势股盘中涨幅突破阈值 → 触发 trend_breakout 买入


@dataclass
class StockState:
    """单只股票的盘中状态"""
    code: str
    name: str
    is_watchlist: bool = False     # 是否盘后推荐标的
    kind: str = "limit_up"          # limit_up / trend（趋势股走不同买入信号）
    buy_price: float = 0.0         # 买入价（已持仓时）
    # 封板追踪
    is_sealed: bool = False        # 当前是否封板
    first_seal_time: str = ""      # 首次封板时间
    blown_count: int = 0           # 炸板次数
    last_blown_time: str = ""      # 最后一次炸板时间
    resealed: bool = False         # 是否回封过
    seal_volume: float = 0.0       # 封板时成交量
    trend_triggered: bool = False   # 趋势突破信号是否已触发（避免重复）
    # 价格追踪
    auction_price: float = 0.0     # 竞价价格（09:25）
    current_price: float = 0.0
    last_close: float = 0.0
    limit_up_price: float = 0.0
    high: float = 0.0
    low: float = 999999.0
    # 板块
    industry: str = ""


@dataclass
class MonitorState:
    """监控系统全局状态"""
    date: str = ""
    last_update_time: str = ""
    stocks: dict = field(default_factory=dict)           # code → StockState (as dict)
    sent_signals: list = field(default_factory=list)     # 已推送的信号列表
    sector_heat: dict = field(default_factory=dict)      # industry → limit_up_count
    total_limit_up: int = 0
    total_limit_down: int = 0


def load_monitor_state() -> MonitorState:
    if os.path.exists(MONITOR_STATE_FILE):
        with open(MONITOR_STATE_FILE) as f:
            data = json.load(f)
        state = MonitorState(**{k: v for k, v in data.items()
                               if k in MonitorState.__dataclass_fields__})
        return state
    return MonitorState()


def save_monitor_state(state: MonitorState):
    with open(MONITOR_STATE_FILE, "w") as f:
        json.dump(asdict(state), f, ensure_ascii=False, indent=2)


def load_portfolio() -> dict:
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE) as f:
            return json.load(f)
    return {"cash": 100_000, "positions": []}


def _calc_limit_price(last_close: float, code: str) -> float:
    is_20cm = code.startswith(("300", "301", "688"))
    pct = 20 if is_20cm else 10
    return round(last_close * (1 + pct / 100), 2)


def init_day(state: MonitorState, date: str):
    """初始化当日监控状态"""
    state.date = date
    state.stocks = {}
    state.sent_signals = []
    state.sector_heat = {}
    state.total_limit_up = 0
    state.total_limit_down = 0

    # 加载盘后推荐的标的（watchlist）— 从 portfolio + 最新 layered_daily 推荐
    portfolio = load_portfolio()
    for pos in portfolio.get("positions", []):
        code = pos.get("code", "")
        if not code:
            continue
        state.stocks[code] = asdict(StockState(
            code=code,
            name=pos.get("name", ""),
            is_watchlist=True,
            buy_price=pos.get("buy_price", 0),
        ))

    # 也从最新的 layered_daily 分析中加载候选标的
    layered_dir = os.path.join(TRADING_DIR, "layered_daily")
    if os.path.isdir(layered_dir):
        files = sorted([f for f in os.listdir(layered_dir) if f.endswith(".json")])
        if files:
            with open(os.path.join(layered_dir, files[-1])) as f:
                prev = json.load(f)
            for c in prev.get("candidates", []):
                code = c.get("code", "")
                if code and code not in state.stocks:
                    state.stocks[code] = asdict(StockState(
                        code=code,
                        name=c.get("name", ""),
                        is_watchlist=True,
                    ))

    # 加载盘后推荐的关注板块
    layered_dir = os.path.join(TRADING_DIR, "layered_daily")
    prev_dates = sorted(
        [f.replace(".json", "") for f in os.listdir(layered_dir) if f.endswith(".json")]
    ) if os.path.isdir(layered_dir) else []
    if prev_dates:
        latest = prev_dates[-1]
        with open(os.path.join(layered_dir, f"{latest}.json")) as f:
            prev_analysis = json.load(f)
        state.sector_heat = {
            s: 0 for s in prev_analysis.get("judgment", {}).get("top_sectors", [])
        }


def update_minute(state: MonitorState, date: str, current_time: str,
                  db_path: str = INTRADAY_DB) -> list[dict]:
    """每分钟更新一次，返回新产生的信号列表

    Args:
        state: 当前监控状态
        date: 交易日
        current_time: 当前时间 HH:MM
        db_path: intraday.db 路径

    Returns:
        list of signal dicts: [{type, code, name, message, time}]
    """
    signals = []
    state.last_update_time = current_time

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        # 获取当前分钟全市场数据
        rows = conn.execute(
            "SELECT mb.code, mb.close, mb.volume, mb.high, mb.low, "
            "sm.name, sm.last_close, sm.limit_pct "
            "FROM minute_bars mb "
            "JOIN stock_meta sm ON mb.code = sm.code AND sm.date = ? "
            "WHERE mb.date = ? AND mb.time = ?",
            (date, date, current_time),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return signals

    # 全市场扫描
    limit_up_count = 0
    sector_counts = {}

    for code, close, volume, high, low, name, last_close, limit_pct in rows:
        if last_close <= 0 or close <= 0:
            continue

        limit_up_price = _calc_limit_price(last_close, code)
        is_at_limit = close >= limit_up_price
        pct_change = (close - last_close) / last_close * 100

        if is_at_limit:
            limit_up_count += 1

        # ── 更新已跟踪的标的状态 ──
        if code in state.stocks:
            s = state.stocks[code]
            s["current_price"] = close
            s["last_close"] = last_close
            s["limit_up_price"] = limit_up_price
            if close > s.get("high", 0):
                s["high"] = close
            if close < s.get("low", 999999):
                s["low"] = close

            # 竞价检测（09:25）
            if current_time == "09:25" and s["is_watchlist"]:
                s["auction_price"] = close
                auction_pct = pct_change
                sig_key = f"{code}:auction"
                if sig_key not in state.sent_signals:
                    if auction_pct >= AUCTION_STRONG_PCT:
                        signals.append({
                            "type": "auction_strong",
                            "code": code, "name": name,
                            "message": f"竞价超预期：高开{auction_pct:+.1f}%",
                            "time": current_time,
                        })
                        state.sent_signals.append(sig_key)
                    elif auction_pct <= AUCTION_WEAK_PCT:
                        signals.append({
                            "type": "auction_weak",
                            "code": code, "name": name,
                            "message": f"竞价低于预期：低开{auction_pct:+.1f}%",
                            "time": current_time,
                        })
                        state.sent_signals.append(sig_key)

            # 封板检测
            was_sealed = s.get("is_sealed", False)
            if is_at_limit and not was_sealed:
                # 新封板
                s["is_sealed"] = True
                if not s.get("first_seal_time"):
                    s["first_seal_time"] = current_time
                    s["seal_volume"] = volume
                else:
                    # 回封
                    s["resealed"] = True
                sig_key = f"{code}:seal:{current_time}"
                if sig_key not in state.sent_signals:
                    label = "回封" if s.get("resealed") else "封板"
                    signals.append({
                        "type": "sealed",
                        "code": code, "name": name,
                        "message": f"{label}（{current_time}）{'回封' if s.get('resealed') else '首封'}",
                        "time": current_time,
                    })
                    state.sent_signals.append(sig_key)
            elif not is_at_limit and was_sealed:
                # 炸板
                s["is_sealed"] = False
                s["blown_count"] = s.get("blown_count", 0) + 1
                s["last_blown_time"] = current_time
                sig_key = f"{code}:blown:{current_time}"
                if sig_key not in state.sent_signals:
                    signals.append({
                        "type": "blown",
                        "code": code, "name": name,
                        "message": f"炸板（第{s['blown_count']}次，{current_time}）",
                        "time": current_time,
                    })
                    state.sent_signals.append(sig_key)

            # 持仓止损/止盈检测
            buy_price = s.get("buy_price", 0)
            if buy_price > 0 and current_time >= "09:30":
                float_pnl = (close - buy_price) / buy_price * 100
                if float_pnl <= STOP_LOSS_PCT:
                    sig_key = f"{code}:stop_loss"
                    if sig_key not in state.sent_signals:
                        signals.append({
                            "type": "stop_loss",
                            "code": code, "name": name,
                            "message": f"止损触发：浮亏{float_pnl:.1f}%（阈值{STOP_LOSS_PCT}%）",
                            "time": current_time,
                        })
                        state.sent_signals.append(sig_key)
                elif float_pnl >= TAKE_PROFIT_PCT:
                    sig_key = f"{code}:take_profit"
                    if sig_key not in state.sent_signals:
                        signals.append({
                            "type": "take_profit",
                            "code": code, "name": name,
                            "message": f"止盈触发：浮盈{float_pnl:.1f}%（阈值+{TAKE_PROFIT_PCT}%）",
                            "time": current_time,
                        })
                        state.sent_signals.append(sig_key)

        # ── 全市场新封板检测（发现新机会） ──
        if is_at_limit and code not in state.stocks and current_time >= "09:30":
            # 查行业
            industry = _get_industry(code, db_path, date)
            if industry:
                sector_counts[industry] = sector_counts.get(industry, 0) + 1

            # 只关注主线板块内的新封板
            watched_sectors = set(state.sector_heat.keys())
            if industry and industry in watched_sectors:
                # 用 Layer 2 评分
                score = _quick_score(code, name, close, volume, last_close,
                                     current_time, industry, db_path, date)
                if score >= MIN_SCORE_FOR_OPPORTUNITY:
                    sig_key = f"{code}:opportunity"
                    if sig_key not in state.sent_signals:
                        # 加入跟踪
                        state.stocks[code] = asdict(StockState(
                            code=code, name=name or "",
                            is_sealed=True,
                            first_seal_time=current_time,
                            seal_volume=volume,
                            current_price=close,
                            last_close=last_close,
                            limit_up_price=limit_up_price,
                            industry=industry,
                        ))
                        signals.append({
                            "type": "opportunity",
                            "code": code, "name": name,
                            "message": f"新机会：{industry}板块 | {current_time}封板 | "
                                      f"评分{score}分 | 成交{volume*close/1e8:.1f}亿",
                            "time": current_time,
                        })
                        state.sent_signals.append(sig_key)

    # 更新全局统计
    state.total_limit_up = limit_up_count
    for sector, count in sector_counts.items():
        old = state.sector_heat.get(sector, 0)
        if count > old:
            state.sector_heat[sector] = count
            # 板块发酵信号
            if count >= old + 2 and sector in set(state.sector_heat.keys()):
                sig_key = f"{sector}:heat:{current_time}"
                if sig_key not in state.sent_signals:
                    signals.append({
                        "type": "sector_heat",
                        "code": "", "name": sector,
                        "message": f"板块发酵：{sector} 封板 {count} 只（+{count-old}）",
                        "time": current_time,
                    })
                    state.sent_signals.append(sig_key)

    return signals


def _get_industry(code: str, db_path: str, date: str) -> str:
    """获取个股行业"""
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        row = conn.execute(
            "SELECT industry FROM limit_up WHERE date = ? AND code = ?",
            (date, code),
        ).fetchone()
        if row:
            return row[0] or ""
        # fallback: 从 stock_concept.db 查
        if os.path.exists(CONCEPT_DB):
            conn2 = sqlite3.connect(CONCEPT_DB, timeout=5)
            row2 = conn2.execute(
                "SELECT industry FROM stock_concepts WHERE code = ?", (code,)
            ).fetchone()
            conn2.close()
            return (row2[0] or "") if row2 else ""
    finally:
        conn.close()
    return ""


def _quick_score(code, name, close, volume, last_close,
                 seal_time, industry, db_path, date) -> int:
    """快速评分（盘中简化版 Layer 2）"""
    score = 0

    # 首封时间
    try:
        t = int(seal_time.replace(":", "")) * 100  # "09:34" → 93400
    except (ValueError, TypeError):
        t = 150000
    if t <= 93500:
        score += 5  # S级
    elif t <= 100000:
        score += 4  # A级
    elif t <= 103000:
        score += 3  # B级
    elif t <= 130000:
        score += 2  # C级
    else:
        score += 1  # D/F级

    # 量能（用 volume * close 近似成交额）
    amount = volume * close
    if amount >= 5e8:
        score += 2
    elif amount < 3e8:
        score -= 3

    # 板块连续性
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        cont = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM limit_up "
            "WHERE industry = ? AND date <= ? AND date >= date(?, '-7 days')",
            (industry, date, date),
        ).fetchone()
        days = cont[0] if cont else 0
        if days >= 3:
            score += 2
        elif days >= 2:
            score += 1
    finally:
        conn.close()

    return score


def update_minute_fast(state: MonitorState, date: str, current_time: str,
                       minute_rows: list) -> list[dict]:
    """高速版 update_minute — 使用预加载到内存的数据

    Args:
        minute_rows: [(code, close, volume, high, low, name, last_close, limit_pct), ...]
    """
    signals = []
    state.last_update_time = current_time

    if not minute_rows:
        return signals

    limit_up_count = 0
    sector_counts = {}

    for code, close, volume, high, low, name, last_close, limit_pct in minute_rows:
        # 防御 NULL 值（03-30 附近 stock_meta 偶有空 last_close）
        if last_close is None or close is None:
            continue
        if last_close <= 0 or close <= 0:
            continue

        limit_up_price = _calc_limit_price(last_close, code)
        is_at_limit = close >= limit_up_price

        if is_at_limit:
            limit_up_count += 1

        # ── 已跟踪标的 ──
        if code in state.stocks:
            s = state.stocks[code]
            s["current_price"] = close
            s["last_close"] = last_close
            s["limit_up_price"] = limit_up_price
            if close > s.get("high", 0):
                s["high"] = close
            if close < s.get("low", 999999):
                s["low"] = close

            # 竞价
            if current_time == "09:25" and s["is_watchlist"]:
                s["auction_price"] = close
                pct = (close - last_close) / last_close * 100
                sig_key = f"{code}:auction"
                if sig_key not in state.sent_signals:
                    if pct >= AUCTION_STRONG_PCT:
                        signals.append({"type": "auction_strong", "code": code,
                                       "name": name, "message": f"竞价超预期：高开{pct:+.1f}%",
                                       "time": current_time})
                        state.sent_signals.append(sig_key)
                    elif pct <= AUCTION_WEAK_PCT:
                        signals.append({"type": "auction_weak", "code": code,
                                       "name": name, "message": f"竞价低于预期：低开{pct:+.1f}%",
                                       "time": current_time})
                        state.sent_signals.append(sig_key)

            # 封板/炸板
            was_sealed = s.get("is_sealed", False)
            if is_at_limit and not was_sealed:
                s["is_sealed"] = True
                if not s.get("first_seal_time"):
                    s["first_seal_time"] = current_time
                    s["seal_volume"] = volume
                else:
                    s["resealed"] = True
                sig_key = f"{code}:seal:{current_time}"
                if sig_key not in state.sent_signals:
                    label = "回封" if s.get("resealed") else "封板"
                    signals.append({"type": "sealed", "code": code, "name": name,
                                   "message": f"{label}（{current_time}）", "time": current_time})
                    state.sent_signals.append(sig_key)
            elif not is_at_limit and was_sealed:
                s["is_sealed"] = False
                s["blown_count"] = s.get("blown_count", 0) + 1
                sig_key = f"{code}:blown:{current_time}"
                if sig_key not in state.sent_signals:
                    signals.append({"type": "blown", "code": code, "name": name,
                                   "message": f"炸板（第{s['blown_count']}次，{current_time}）",
                                   "time": current_time})
                    state.sent_signals.append(sig_key)

            # 趋势股盘中突破信号（非涨停 watchlist）
            if (s.get("kind") == "trend" and s.get("is_watchlist")
                    and not s.get("trend_triggered") and current_time >= "09:30"
                    and not is_at_limit):
                today_pct = (close - last_close) / last_close * 100
                if today_pct >= TREND_BREAKOUT_PCT:
                    s["trend_triggered"] = True
                    sig_key = f"{code}:trend_breakout"
                    if sig_key not in state.sent_signals:
                        signals.append({
                            "type": "trend_breakout",
                            "code": code, "name": name,
                            "message": f"趋势突破：今日+{today_pct:.1f}%（未涨停）",
                            "time": current_time,
                        })
                        state.sent_signals.append(sig_key)

            # 止损/止盈
            buy_price = s.get("buy_price", 0)
            if buy_price > 0 and current_time >= "09:30":
                float_pnl = (close - buy_price) / buy_price * 100
                if float_pnl <= STOP_LOSS_PCT:
                    sig_key = f"{code}:stop_loss"
                    if sig_key not in state.sent_signals:
                        signals.append({"type": "stop_loss", "code": code, "name": name,
                                       "message": f"止损触发：浮亏{float_pnl:.1f}%", "time": current_time})
                        state.sent_signals.append(sig_key)
                elif float_pnl >= TAKE_PROFIT_PCT:
                    sig_key = f"{code}:take_profit"
                    if sig_key not in state.sent_signals:
                        signals.append({"type": "take_profit", "code": code, "name": name,
                                       "message": f"止盈触发：浮盈{float_pnl:.1f}%", "time": current_time})
                        state.sent_signals.append(sig_key)

        # ── 全市场新封板（简化版，不查行业） ──
        elif is_at_limit and current_time >= "09:30":
            sig_key = f"{code}:opportunity"
            if sig_key not in state.sent_signals:
                watched = set(state.sector_heat.keys())
                if watched:  # 只有在有关注板块时才检测新机会
                    # 简单评分（不查 DB）
                    try:
                        t_int = int(current_time.replace(":", "")) * 100
                    except (ValueError, TypeError):
                        t_int = 150000
                    score = 0
                    if t_int <= 93500: score += 5
                    elif t_int <= 100000: score += 4
                    elif t_int <= 103000: score += 3
                    else: score += 2
                    amount = volume * close
                    if amount >= 5e8: score += 2
                    if score >= MIN_SCORE_FOR_OPPORTUNITY:
                        state.stocks[code] = asdict(StockState(
                            code=code, name=name or "", is_sealed=True,
                            first_seal_time=current_time, seal_volume=volume,
                            current_price=close, last_close=last_close,
                            limit_up_price=limit_up_price,
                        ))
                        signals.append({"type": "opportunity", "code": code, "name": name or code,
                                       "message": f"新封板（{current_time}）评分{score}",
                                       "time": current_time})
                        state.sent_signals.append(sig_key)

    state.total_limit_up = limit_up_count
    return signals


def format_signals(signals: list[dict]) -> str:
    """格式化信号为飞书推送文本"""
    if not signals:
        return ""

    emoji_map = {
        "auction_strong": "🟢",
        "auction_weak": "🔴",
        "sealed": "🔒",
        "blown": "💥",
        "stop_loss": "🔴",
        "take_profit": "🟢",
        "opportunity": "⚡",
        "sector_heat": "🔥",
    }

    lines = []
    for s in signals:
        emoji = emoji_map.get(s["type"], "📌")
        name_part = f"**{s['name']}**({s['code']})" if s["code"] else f"**{s['name']}**"
        lines.append(f"{emoji} {name_part} — {s['message']}")

    return "\n".join(lines)


def run_backtest(date: str) -> list[dict]:
    """回测模式：模拟某日全天的分钟级监控"""
    state = MonitorState()
    init_day(state, date)

    conn = sqlite3.connect(INTRADAY_DB, timeout=10)
    try:
        times = [r[0] for r in conn.execute(
            "SELECT DISTINCT time FROM minute_bars WHERE date = ? ORDER BY time",
            (date,),
        ).fetchall()]
    finally:
        conn.close()

    all_signals = []
    for t in times:
        signals = update_minute(state, date, t)
        if signals:
            all_signals.extend(signals)

    return all_signals


def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘中连续监控系统")
    parser.add_argument("--backtest", help="回测模式：指定日期")
    parser.add_argument("--init", action="store_true", help="初始化当日状态")
    args = parser.parse_args()

    if args.backtest:
        print(f"=== 回测模式：{args.backtest} ===\n")
        signals = run_backtest(args.backtest)
        print(f"共产生 {len(signals)} 个信号：\n")
        for s in signals:
            print(f"  [{s['time']}] {s['type']:15s} | {s.get('name',''):10s} | {s['message']}")
        return

    # 正常模式：执行一次更新
    now = datetime.now()
    date = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")

    state = load_monitor_state()

    # 如果是新的一天或手动初始化
    if state.date != date or args.init:
        print(f"[初始化] {date}")
        init_day(state, date)

    signals = update_minute(state, date, current_time)
    save_monitor_state(state)

    if signals:
        report = format_signals(signals)
        print(report)
        # 输出 JSON 供 send_message 使用
        print(json.dumps({"signals": signals, "report": report}, ensure_ascii=False))
    else:
        print(f"[{current_time}] 无新信号")


if __name__ == "__main__":
    main()
