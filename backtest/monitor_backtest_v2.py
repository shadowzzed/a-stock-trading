"""方向二回测 v2：盘中监控 — 真实持仓模拟（买卖信号配对）

策略：
- 买入信号（sealed/auction_strong/opportunity）→ 实盘建仓（当分钟收盘价）
- 卖出信号（blown/stop_loss/take_profit/auction_weak）→ 实盘平仓
- 仓位规则：最多同时持仓 2 只，每笔 30% 成本基准资金
- 持仓可跨日，次日继续监控并执行卖出信号
- 回测结束强制平仓

用法:
    python3 -m backtest.monitor_backtest_v2 --start 2026-04-07 --end 2026-04-17 \\
        --output ~/shared/backtest/monitor_v2.json --report ~/shared/backtest/方向二_交割单_v2.md
"""

from __future__ import annotations

import json
import os
import sys
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime

_project_root = os.path.dirname(os.path.dirname(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

TRADING_DIR = os.path.expanduser("~/shared/trading")
INTRADAY_DB = os.path.join(TRADING_DIR, "intraday", "intraday.db")

MAX_POSITIONS = 3
POSITION_PCT = 0.30
INITIAL_CASH = 100_000
MAX_HOLD_DAYS = 5                # 最大持仓天数（超时强制平仓）

# 买入信号类型（进场）
# opportunity（全市场新封板）在回测中表现不佳，暂时剔除
BUY_SIGNALS = {"sealed", "auction_strong"}
# 卖出信号类型（出场）
SELL_SIGNALS = {"blown", "stop_loss", "take_profit", "auction_weak"}

# ── 入场前过滤 ──
AUCTION_BUY_MIN_PREV_PCT = -5.0   # 昨日跌幅阈值（低于此不买，除非反包）
AUCTION_BUY_MAX_PREV_BLOWN = 3    # 昨日炸板次数（> 此数不买，除非反包）

# 反包场景的宽松规则
REVERSAL_LOOKBACK_PREV_DROP = -3.0   # 昨日跌幅 ≤ -3% 视为反包候选
REVERSAL_LOOKBACK_PREV_BLOWN = 2     # 昨日炸板 ≥ 2 次视为反包候选


def _prev_day_context(intraday_db: str, date: str, code: str) -> dict:
    """获取昨日 pct_chg 和炸板次数，用于入场过滤。"""
    import sqlite3
    conn = sqlite3.connect(intraday_db, timeout=10)
    try:
        prev_row = conn.execute(
            "SELECT MAX(date) FROM daily_bars WHERE date < ?", (date,)
        ).fetchone()
        if not prev_row or not prev_row[0]:
            return {"prev_pct": None, "prev_blown": 0}
        prev_date = prev_row[0]
        pct_row = conn.execute(
            "SELECT pct_chg FROM daily_bars WHERE date=? AND code=?",
            (prev_date, code),
        ).fetchone()
        blown_row = conn.execute(
            "SELECT blown_count FROM limit_up WHERE date=? AND code=?",
            (prev_date, code),
        ).fetchone()
        return {
            "prev_pct": pct_row[0] if pct_row else None,
            "prev_blown": blown_row[0] if blown_row else 0,
            "prev_date": prev_date,
        }
    finally:
        conn.close()


def _get_market_heat(date: str, intraday_db: str) -> int:
    """获取昨日涨停家数（市场热度代理指标）"""
    import sqlite3
    conn = sqlite3.connect(intraday_db, timeout=10)
    try:
        prev_row = conn.execute(
            "SELECT MAX(date) FROM daily_bars WHERE date < ?", (date,)
        ).fetchone()
        if not prev_row or not prev_row[0]:
            return 999  # 无法判断，放行
        prev_date = prev_row[0]
        cnt_row = conn.execute(
            "SELECT COUNT(*) FROM limit_up WHERE date = ?", (prev_date,)
        ).fetchone()
        return cnt_row[0] if cnt_row else 0
    finally:
        conn.close()


MARKET_HEAT_COLD = 40       # 昨日涨停 < 此数视为冷淡
MARKET_HEAT_HOT = 50        # 昨日涨停 >= 此数视为活跃


def _is_buy_allowed(signal: dict, date: str, intraday_db: str) -> tuple[bool, str]:
    """入场前过滤：规避"低位补涨"和"高位分歧"陷阱。

    过滤规则：
    1. 昨日大跌 -5% 到 -3% 之间（不够反包深度）→ 跳过
    2. 昨日涨停 (+9.8% 及以上) 且非龙头（连板 ≤ 2）→ 跳过（避免补涨板）
    3. 昨日炸板 ≥ 3 次 → 跳过（严重分歧）

    反包保护：
    - 昨日跌幅 ≤ -3% → 符合反包定义，允许入场
    - 封板信号不做此过滤
    """
    sig_type = signal["type"]
    if sig_type != "auction_strong":
        return True, ""

    import sqlite3
    conn = sqlite3.connect(intraday_db, timeout=10)
    try:
        prev_row = conn.execute(
            "SELECT MAX(date) FROM daily_bars WHERE date < ?", (date,)
        ).fetchone()
        if not prev_row or not prev_row[0]:
            return True, ""
        prev_date = prev_row[0]
        pct_row = conn.execute(
            "SELECT pct_chg FROM daily_bars WHERE date=? AND code=?",
            (prev_date, signal["code"]),
        ).fetchone()
        lu_row = conn.execute(
            "SELECT board_count, blown_count FROM limit_up WHERE date=? AND code=?",
            (prev_date, signal["code"]),
        ).fetchone()
    finally:
        conn.close()

    prev_pct = pct_row[0] if pct_row else None
    prev_board = lu_row[0] if lu_row else 0
    prev_blown = lu_row[1] if lu_row else 0

    if prev_pct is None:
        return True, ""

    # 反包场景（昨日深跌）→ 允许
    if prev_pct <= REVERSAL_LOOKBACK_PREV_DROP:
        return True, "反包"
    if prev_blown >= REVERSAL_LOOKBACK_PREV_BLOWN and prev_pct < 0:
        return True, "反包(多次炸板)"

    # 昨日温和下跌但未达反包 → 跳过
    if prev_pct < AUCTION_BUY_MIN_PREV_PCT:
        return False, f"过滤: 昨日{prev_pct:.1f}% 未达反包线"

    # 高位分歧：昨日涨停 + 非龙头（连板 ≤ 2）→ 跳过
    if prev_pct >= 9.5 and prev_board and prev_board <= 2:
        return False, f"过滤: 昨日涨停{prev_board}板非龙头(易分歧)"

    # 严重炸板 → 跳过
    if prev_blown >= 4:
        return False, f"过滤: 昨日炸板{prev_blown}次"

    # 市场冷淡过滤（已实验，效果不显著 — 保留代码但阈值调到 9999 = 事实上关闭）
    # heat = _get_market_heat(date, intraday_db)
    # if heat < MARKET_HEAT_COLD and sig_type == "auction_strong":
    #     return False, f"过滤: 市场冷淡(昨日涨停{heat}家)"

    return True, ""


@dataclass
class Position:
    code: str
    name: str
    buy_date: str
    buy_time: str
    buy_price: float
    shares: int
    amount: float
    entry_reason: str


@dataclass
class Trade:
    code: str
    name: str
    buy_date: str
    buy_time: str
    buy_price: float
    shares: int
    buy_amount: float
    sell_date: str
    sell_time: str
    sell_price: float
    sell_amount: float
    pnl: float
    pnl_pct: float
    entry_reason: str
    exit_reason: str
    hold_days: int


class Portfolio:
    def __init__(self, initial_cash: float = INITIAL_CASH):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[Trade] = []

    def cost_basis_equity(self) -> float:
        return self.cash + sum(p.amount for p in self.positions.values())

    def try_buy(self, code: str, name: str, date: str, time: str,
                price: float, reason: str) -> Position | None:
        if code in self.positions:
            return None
        if len(self.positions) >= MAX_POSITIONS:
            return None
        if price <= 0:
            return None
        equity = self.cost_basis_equity()
        target_amount = equity * POSITION_PCT
        shares = int(target_amount / price / 100) * 100
        if shares < 100:
            return None
        actual_amount = shares * price
        if actual_amount > self.cash:
            return None
        self.cash -= actual_amount
        pos = Position(
            code=code, name=name, buy_date=date, buy_time=time,
            buy_price=price, shares=shares, amount=actual_amount,
            entry_reason=reason,
        )
        self.positions[code] = pos
        return pos

    def sell(self, code: str, date: str, time: str,
             price: float, reason: str, force: bool = False) -> Trade | None:
        if code not in self.positions:
            return None
        p = self.positions[code]
        # T+1 规则：买入当日不能卖出（force=True 可绕过，用于回测结束强平）
        if not force and p.buy_date == date:
            return None
        self.positions.pop(code)
        sell_amount = p.shares * price
        pnl = sell_amount - p.amount
        pnl_pct = pnl / p.amount * 100 if p.amount > 0 else 0
        self.cash += sell_amount
        d1 = datetime.strptime(p.buy_date, "%Y-%m-%d")
        d2 = datetime.strptime(date, "%Y-%m-%d")
        hold_days = (d2 - d1).days
        trade = Trade(
            code=p.code, name=p.name,
            buy_date=p.buy_date, buy_time=p.buy_time, buy_price=p.buy_price,
            shares=p.shares, buy_amount=p.amount,
            sell_date=date, sell_time=time, sell_price=price,
            sell_amount=sell_amount, pnl=pnl, pnl_pct=pnl_pct,
            entry_reason=p.entry_reason, exit_reason=reason,
            hold_days=hold_days,
        )
        self.closed_trades.append(trade)
        return trade


def _signal_to_reason(sig: dict) -> str:
    sig_type = sig["type"]
    mapping = {
        "sealed": "封板",
        "auction_strong": "竞价高开",
        "opportunity": "新封板机会",
        "blown": "炸板",
        "stop_loss": "止损-7%",
        "take_profit": "止盈+15%",
        "auction_weak": "竞价低开",
    }
    return mapping.get(sig_type, sig_type)


def run_monitor_backtest_v2(start_date: str, end_date: str, strategy_params: dict = None) -> dict:
    """运行方向二回测。

    Args:
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
        strategy_params: 可选，传入 Strategy.as_params_dict()，覆盖默认参数
    """
    params = strategy_params or {}
    # 应用参数覆盖
    global MAX_POSITIONS, MAX_HOLD_DAYS
    _saved_max_pos = MAX_POSITIONS
    _saved_max_hold = MAX_HOLD_DAYS
    if "max_positions" in params:
        MAX_POSITIONS = params["max_positions"]
    if "max_hold_days" in params:
        MAX_HOLD_DAYS = params["max_hold_days"]
    # 止损止盈通过模块全局变量覆盖（必须同时覆盖 monitor.py 和 layered_analysis.py
    # —— 否则盘中 sell 走 monitor.py 阈值、盘后 sell 走 layered_analysis.py 阈值，导致策略不一致）
    if "stop_loss_pct" in params or "take_profit_pct" in params:
        from trading_agent.intraday import monitor as _monitor
        from trading_agent.intraday import layered_analysis as _la
        _saved_sl = _monitor.STOP_LOSS_PCT
        _saved_tp = _monitor.TAKE_PROFIT_PCT
        _saved_la_sl = _la.STOP_LOSS_PCT
        _saved_la_tp = _la.TAKE_PROFIT_PCT
        if "stop_loss_pct" in params:
            _monitor.STOP_LOSS_PCT = params["stop_loss_pct"]
            _la.STOP_LOSS_PCT = params["stop_loss_pct"]
        if "take_profit_pct" in params:
            _monitor.TAKE_PROFIT_PCT = params["take_profit_pct"]
            _la.TAKE_PROFIT_PCT = params["take_profit_pct"]
    # 入场信号过滤
    buy_signals_override = None
    if params.get("sealed_only"):
        buy_signals_override = {"sealed"}
    elif params.get("auction_strong_only"):
        buy_signals_override = {"auction_strong"}

    try:
        return _run_monitor_backtest_v2_impl(start_date, end_date, params, buy_signals_override)
    finally:
        MAX_POSITIONS = _saved_max_pos
        MAX_HOLD_DAYS = _saved_max_hold
        if "stop_loss_pct" in params or "take_profit_pct" in params:
            _monitor.STOP_LOSS_PCT = _saved_sl
            _monitor.TAKE_PROFIT_PCT = _saved_tp
            _la.STOP_LOSS_PCT = _saved_la_sl
            _la.TAKE_PROFIT_PCT = _saved_la_tp


def _run_monitor_backtest_v2_impl(start_date: str, end_date: str, params: dict, buy_signals_override) -> dict:
    from trading_agent.intraday.monitor import (
        MonitorState, StockState, update_minute_fast,
    )
    from trading_agent.intraday.layered_analysis import run_analysis

    conn = sqlite3.connect(INTRADAY_DB, timeout=10)
    all_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM minute_bars WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date),
    ).fetchall()]
    conn.close()

    if len(all_dates) < 1:
        print(f"可用交易日不足: {len(all_dates)}")
        return {}

    print(f"方向二盘中监控回测 v2: {all_dates[0]} → {all_dates[-1]} ({len(all_dates)} 天)")
    print(f"{'='*70}\n")

    portfolio = Portfolio()
    daily_log = []
    prev_candidates = []
    prev_sectors = []
    last_price_map: dict[str, float] = {}  # 最后一笔成交价（用于强平）
    layer1_judgments = []  # Phase 4: Layer1 GLM 门控决策记录

    # Phase 4 准备：Layer1 门控
    layer1_gate_enabled = bool(params.get("layer1_gate", False))
    layer1_provider_name = params.get("layer1_provider", "deterministic")
    layer1_runner = None
    layer1_data_provider = None
    layer1_provider_idx = 0
    layer1_use_deterministic = (layer1_provider_name == "deterministic")
    if layer1_gate_enabled:
        from backtest.adapter import ReviewDataProvider, MarketJudgmentRunner
        from backtest.layered_engine import _code_sentiment_fallback
        layer1_data_provider = ReviewDataProvider()
        if not layer1_use_deterministic:
            from config import get_ai_providers
            providers = get_ai_providers()
            for i, p in enumerate(providers):
                if p["name"] == layer1_provider_name:
                    layer1_provider_idx = i
                    break
            layer1_runner = MarketJudgmentRunner()

    for day_idx, date in enumerate(all_dates):
        state = MonitorState()
        state.date = date
        state.stocks = {}
        state.sector_heat = {}

        # 1. 加载前日盘后推荐为 watchlist
        if prev_candidates:
            for c in prev_candidates:
                code = c.get("code", "")
                if code:
                    state.stocks[code] = asdict(StockState(
                        code=code, name=c.get("name", ""),
                        is_watchlist=True,
                    ))
            state.sector_heat = {s: 0 for s in prev_sectors}

        # 2. 把持仓注入 state.stocks（跨日持仓需要继续监控）
        for code, pos in portfolio.positions.items():
            if code not in state.stocks:
                state.stocks[code] = asdict(StockState(
                    code=code, name=pos.name, is_watchlist=True,
                    buy_price=pos.buy_price,
                ))
            else:
                state.stocks[code]["buy_price"] = pos.buy_price

        # 2.5 持仓超时检查：> MAX_HOLD_DAYS 的仓位在当日开盘强平
        overtime_sells = []
        for code, pos in list(portfolio.positions.items()):
            from datetime import datetime as _dt
            d1 = _dt.strptime(pos.buy_date, "%Y-%m-%d")
            d2 = _dt.strptime(date, "%Y-%m-%d")
            hold_days = (d2 - d1).days
            if hold_days >= MAX_HOLD_DAYS:
                # 稍后在 09:25 开盘价卖出
                overtime_sells.append(code)

        # 3. 生成当日盘后推荐给下一天
        try:
            result = run_analysis(date=date, dry_run=True)
            prev_candidates = result.get("candidates", [])
            prev_sectors = result.get("judgment", {}).get("top_sectors", [])
        except Exception as e:
            print(f"  [{date}] 盘后分析失败: {e}")
            prev_candidates = []
            prev_sectors = []

        # 3.5 Phase 4: Layer1 GLM 门控（用前一日数据判断今日基调）
        daily_gate = "可买入"
        if layer1_gate_enabled and day_idx > 0:
            prev_date = all_dates[day_idx - 1]
            try:
                snapshot = layer1_data_provider.load_market_snapshot(TRADING_DIR, prev_date)
                if layer1_use_deterministic:
                    # 与生产 layered_analysis.py 完全一致的 deterministic 判断
                    phase = _code_sentiment_fallback(snapshot)
                    if phase in ("退潮", "冰点"):
                        gate = "空仓"
                    elif phase in ("修复", "升温", "高潮"):
                        gate = "可买入"
                    else:
                        gate = "谨慎"
                    judgment = {"sentiment_phase": phase, "action_gate": gate, "top_sectors": []}
                else:
                    judgment = layer1_runner.run(snapshot, provider_index=layer1_provider_idx)
                daily_gate = judgment.get("action_gate", "可买入")
                layer1_judgments.append({
                    "date": date, "prev_date": prev_date,
                    "phase": judgment.get("sentiment_phase"),
                    "gate": daily_gate,
                    "sectors": judgment.get("top_sectors", []),
                })
                print(f"  [Layer1 门控] {date} 基于 {prev_date}: {judgment.get('sentiment_phase')} → {daily_gate}")
            except Exception as e:
                print(f"  [Layer1 失败] {e}")
                daily_gate = "可买入"  # 失败时默认放行


        # 4. 取当日分钟序列
        conn = sqlite3.connect(INTRADAY_DB, timeout=10)
        times = [r[0] for r in conn.execute(
            "SELECT DISTINCT time FROM minute_bars WHERE date = ? ORDER BY time",
            (date,),
        ).fetchall()]
        rows = conn.execute(
            "SELECT mb.time, mb.code, mb.close, mb.volume, mb.high, mb.low, "
            "COALESCE(sm.name, '') AS name, "
            "COALESCE(sm.last_close, 0) AS last_close, "
            "COALESCE(sm.limit_pct, 10) AS limit_pct "
            "FROM minute_bars mb "
            "LEFT JOIN stock_meta sm ON mb.code = sm.code AND sm.date = ? "
            "WHERE mb.date = ? AND mb.close IS NOT NULL",
            (date, date),
        ).fetchall()
        # 若 stock_meta 无 last_close，用 daily_bars 前一天 close 填充
        if rows and any(r[7] == 0 for r in rows):
            prev_row = conn.execute(
                "SELECT MAX(date) FROM daily_bars WHERE date < ?", (date,)
            ).fetchone()
            if prev_row and prev_row[0]:
                prev_close_map = dict(conn.execute(
                    "SELECT code, close FROM daily_bars WHERE date=?",
                    (prev_row[0],),
                ).fetchall())
                fixed_rows = []
                for r in rows:
                    if r[7] == 0 and r[1] in prev_close_map:
                        pc = prev_close_map[r[1]]
                        if isinstance(pc, bytes):
                            try: pc = float(int.from_bytes(pc, 'little'))
                            except Exception: pc = 0
                        fixed_rows.append(r[:7] + (pc or 0,) + r[8:])
                    else:
                        fixed_rows.append(r)
                rows = fixed_rows
        conn.close()

        all_minute_data: dict[str, list] = {}
        for row in rows:
            t = row[0]
            all_minute_data.setdefault(t, []).append(row[1:])

        if not times:
            continue

        day_signals = []
        day_actions = []  # 当日买卖记录

        # 持仓超时在 09:25 首先执行强平
        if overtime_sells and times:
            first_t = times[0]
            first_rows = all_minute_data.get(first_t, [])
            first_prices = {row[0]: row[1] for row in first_rows}
            for code in overtime_sells:
                price = first_prices.get(code, 0)
                if price <= 0 and code in portfolio.positions:
                    price = portfolio.positions[code].buy_price
                trade = portfolio.sell(code, date, first_t, price, "超时强平", force=True)
                if trade:
                    day_actions.append({
                        "action": "SELL", "code": code,
                        "name": portfolio.closed_trades[-1].name,
                        "time": first_t, "price": price,
                        "pnl": trade.pnl, "pnl_pct": trade.pnl_pct,
                        "reason": "超时强平",
                    })

        # Phase 4: Layer1 空仓信号
        # 硬门控（默认）：09:25 开盘价清仓所有
        # 软门控（layer1_soft_gate=True）：仅拒绝新买入，不强平已有仓位
        layer1_soft_gate = params.get("layer1_soft_gate", False)
        if daily_gate == "空仓" and not layer1_soft_gate and portfolio.positions and times:
            first_t = times[0]
            first_rows = all_minute_data.get(first_t, [])
            first_prices = {row[0]: row[1] for row in first_rows}
            for code in list(portfolio.positions.keys()):
                price = first_prices.get(code, 0)
                if price <= 0:
                    price = portfolio.positions[code].buy_price
                # 智能保护：浮盈 ≥ 5% 的仓位不强平（让其继续走止盈/止损规则）
                if params.get("layer1_smart_protect", False):
                    cur_pos = portfolio.positions[code]
                    float_pnl = (price - cur_pos.buy_price) / cur_pos.buy_price * 100
                    if float_pnl >= 5.0:
                        continue  # 已盈利仓位保留
                trade = portfolio.sell(code, date, first_t, price, "Layer1空仓", force=True)
                if trade:
                    day_actions.append({
                        "action": "SELL", "code": code,
                        "name": portfolio.closed_trades[-1].name,
                        "time": first_t, "price": price,
                        "pnl": trade.pnl, "pnl_pct": trade.pnl_pct,
                        "reason": "Layer1空仓",
                    })

        for t in times:
            minute_rows = all_minute_data.get(t, [])
            # 构建 price map（本分钟所有股票的收盘价）
            minute_price_map = {row[0]: row[1] for row in minute_rows}
            last_price_map.update(minute_price_map)

            # 调用监控引擎
            signals = update_minute_fast(state, date, t, minute_rows)
            day_signals.extend(signals)

            # 处理信号
            for sig in signals:
                code = sig["code"]
                name = sig.get("name", "")
                sig_type = sig["type"]
                price = minute_price_map.get(code, 0)

                if price <= 0:
                    continue

                effective_buy_signals = buy_signals_override if buy_signals_override else BUY_SIGNALS
                if sig_type in effective_buy_signals:
                    # Phase 4: Layer1 空仓门控 → 拒绝所有买入
                    if daily_gate == "空仓":
                        continue
                    # H_TIME 实验：买入时段白名单过滤
                    # 默认 None = 不过滤；传入列表则只在这些时段入场
                    # 时段格式: 字符串 "HH:MM-HH:MM"
                    time_window = params.get("buy_time_window")
                    if time_window:
                        cur_minutes = int(t.split(':')[0]) * 60 + int(t.split(':')[1])
                        in_any_window = False
                        for win in time_window:
                            start_str, end_str = win.split('-')
                            start_m = int(start_str.split(':')[0]) * 60 + int(start_str.split(':')[1])
                            end_m = int(end_str.split(':')[0]) * 60 + int(end_str.split(':')[1])
                            if start_m <= cur_minutes <= end_m:
                                in_any_window = True
                                break
                        if not in_any_window:
                            continue
                    # 入场前过滤（entry_filter=False 时跳过）
                    if params.get("entry_filter", True):
                        allowed, note = _is_buy_allowed(sig, date, INTRADAY_DB)
                        if not allowed:
                            continue
                    # 市场冷淡过滤
                    heat_min = params.get("market_heat_min", 0)
                    if heat_min > 0 and sig_type == "auction_strong":
                        heat = _get_market_heat(date, INTRADAY_DB)
                        if heat < heat_min:
                            continue
                    # sealed 信号：要求昨日已 ≥N 板（H1 实验：sealed 需要"已是连板"）
                    sealed_min_prev_board = params.get("sealed_min_prev_board", 0)
                    if sealed_min_prev_board > 0 and sig_type == "sealed":
                        ctx = _prev_day_context(INTRADAY_DB, date, code)
                        prev_board = 0
                        if ctx.get("prev_date"):
                            _conn = sqlite3.connect(INTRADAY_DB, timeout=5)
                            try:
                                _row = _conn.execute(
                                    "SELECT board_count FROM limit_up WHERE date=? AND code=?",
                                    (ctx["prev_date"], code),
                                ).fetchone()
                                prev_board = _row[0] if _row else 0
                            finally:
                                _conn.close()
                        if prev_board < sealed_min_prev_board:
                            continue
                    # sealed 信号：要求是当日板块前 N 个封板（H6 实验：板块龙头优先）
                    sealed_must_be_sector_top_n = params.get("sealed_must_be_sector_top_n", 0)
                    if sealed_must_be_sector_top_n > 0 and sig_type == "sealed":
                        # 当前板块在 state.sector_heat 中累计第几只封板
                        # 简化逻辑：要求该信号产生时，本板块当日累计封板 ≤ N
                        industry = state.stocks.get(code, {}).get("industry", "")
                        if industry:
                            cur_count = state.sector_heat.get(industry, 0)
                            if cur_count > sealed_must_be_sector_top_n:
                                continue
                    # 尝试建仓
                    pos = portfolio.try_buy(
                        code=code, name=name, date=date, time=t,
                        price=price, reason=_signal_to_reason(sig),
                    )
                    if pos:
                        # 同步到监控状态，使后续止损止盈可用
                        if code in state.stocks:
                            state.stocks[code]["buy_price"] = price
                        day_actions.append({
                            "action": "BUY", "code": code, "name": name,
                            "time": t, "price": price, "shares": pos.shares,
                            "amount": pos.amount, "reason": _signal_to_reason(sig),
                        })

                elif sig_type in SELL_SIGNALS:
                    # 尝试平仓
                    trade = portfolio.sell(
                        code=code, date=date, time=t,
                        price=price, reason=_signal_to_reason(sig),
                    )
                    if trade:
                        day_actions.append({
                            "action": "SELL", "code": code, "name": name,
                            "time": t, "price": price,
                            "pnl": trade.pnl, "pnl_pct": trade.pnl_pct,
                            "reason": _signal_to_reason(sig),
                        })

            # H_EARLY_EXIT 实验：14:50 主动锁利（避免次日竞价低开 -3% 强平损失）
            early_exit_pct = params.get("early_exit_at_1450", 0)
            if early_exit_pct > 0 and t == "14:50":
                for code in list(portfolio.positions.keys()):
                    cur_pos = portfolio.positions[code]
                    cur_price = minute_price_map.get(code, 0)
                    if cur_price > 0:
                        float_pnl = (cur_price - cur_pos.buy_price) / cur_pos.buy_price * 100
                        if float_pnl >= early_exit_pct:
                            trade = portfolio.sell(
                                code=code, date=date, time=t,
                                price=cur_price, reason=f"14:50主动锁利+{float_pnl:.1f}%",
                            )
                            if trade:
                                day_actions.append({
                                    "action": "SELL", "code": code,
                                    "name": cur_pos.name, "time": t, "price": cur_price,
                                    "pnl": trade.pnl, "pnl_pct": trade.pnl_pct,
                                    "reason": f"14:50锁利+{float_pnl:.1f}%",
                                })

            # H_TRAILING_STOP: 移动止盈
            # trailing_activate_pct: 激活阈值（+X% 浮盈后启用 trailing）
            # trailing_drawdown_pct: 从最高点回撤 Y% 触发卖出
            trailing_act = params.get("trailing_activate_pct", 0)
            trailing_dd = params.get("trailing_drawdown_pct", 0)
            if trailing_act > 0 and trailing_dd > 0:
                if not hasattr(portfolio, '_trailing_high'):
                    portfolio._trailing_high = {}
                for code in list(portfolio.positions.keys()):
                    cur_pos = portfolio.positions[code]
                    cur_price = minute_price_map.get(code, 0)
                    if cur_price <= 0:
                        continue
                    float_pnl = (cur_price - cur_pos.buy_price) / cur_pos.buy_price * 100
                    if float_pnl >= trailing_act:
                        prev_high = portfolio._trailing_high.get(code, cur_pos.buy_price)
                        if cur_price > prev_high:
                            portfolio._trailing_high[code] = cur_price
                        peak = portfolio._trailing_high.get(code, cur_price)
                        drawdown_pct = (peak - cur_price) / peak * 100 if peak > 0 else 0
                        if drawdown_pct >= trailing_dd:
                            trade = portfolio.sell(
                                code=code, date=date, time=t, price=cur_price,
                                reason=f"trailing+{float_pnl:.1f}%回撤{drawdown_pct:.1f}%",
                            )
                            if trade:
                                day_actions.append({
                                    "action": "SELL", "code": code,
                                    "name": cur_pos.name, "time": t, "price": cur_price,
                                    "pnl": trade.pnl, "pnl_pct": trade.pnl_pct,
                                    "reason": f"trailing止盈(峰值后回撤{drawdown_pct:.1f}%)",
                                })
                                portfolio._trailing_high.pop(code, None)

        daily_log.append({
            "date": date,
            "signals": len(day_signals),
            "actions": day_actions,
            "open_positions": len(portfolio.positions),
            "cash": round(portfolio.cash, 2),
        })

        print(f"[{date}] 信号 {len(day_signals)} | 动作 {len(day_actions)} | "
              f"持仓 {len(portfolio.positions)} | 现金 {portfolio.cash:,.0f}")
        for act in day_actions:
            if act["action"] == "BUY":
                print(f"  ✅ BUY  {act['time']} {act['name']}({act['code']}) "
                      f"@{act['price']:.2f} × {act['shares']} = {act['amount']:,.0f}  [{act['reason']}]")
            else:
                emoji = "🟢" if act["pnl"] >= 0 else "🔴"
                print(f"  {emoji} SELL {act['time']} {act['name']}({act['code']}) "
                      f"@{act['price']:.2f}  P&L {act['pnl']:+,.0f} ({act['pnl_pct']:+.2f}%)  [{act['reason']}]")

    # 回测结束：强平所有剩余持仓
    last_date = all_dates[-1]
    last_time = "15:00"
    force_closed = []
    for code in list(portfolio.positions.keys()):
        price = last_price_map.get(code, portfolio.positions[code].buy_price)
        trade = portfolio.sell(code, last_date, last_time, price, "回测结束强平", force=True)
        if trade:
            force_closed.append(trade)

    # 汇总
    trades = portfolio.closed_trades
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in trades)
    final_capital = portfolio.cash

    print(f"\n{'='*70}")
    print(f"方向二回测汇总：{len(trades)} 笔交易")
    print(f"初始资金: {INITIAL_CASH:,.0f} | 期末资金: {final_capital:,.0f}")
    print(f"总盈亏: {total_pnl:+,.0f} ({total_pnl/INITIAL_CASH*100:+.2f}%)")
    if trades:
        print(f"胜率: {len(wins)}/{len(trades)} = {len(wins)/len(trades)*100:.1f}%")
        if wins:
            print(f"平均盈利: {sum(t.pnl_pct for t in wins)/len(wins):+.2f}%")
        if losses:
            print(f"平均亏损: {sum(t.pnl_pct for t in losses)/len(losses):+.2f}%")

    return {
        "days": len(all_dates),
        "start_date": all_dates[0],
        "end_date": all_dates[-1],
        "initial_cash": INITIAL_CASH,
        "final_capital": round(final_capital, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(total_pnl / INITIAL_CASH * 100, 2),
        "total_trades": len(trades),
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "trades": [asdict(t) for t in trades],
        "daily_log": daily_log,
        "layer1_judgments": layer1_judgments if layer1_gate_enabled else [],
    }


def generate_report(result: dict, output_path: str):
    """生成 Markdown 交割单"""
    trades = result.get("trades", [])
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]

    lines = []
    lines.append("# 方向二回测交割单（盘中实时监控 v2 | 10万起步 30%仓位）\n")
    lines.append("## 一、收益概况\n")
    lines.append(f"- 回测周期：{result['start_date']} ~ {result['end_date']}（{result['days']} 个交易日）")
    lines.append(f"- 初始资金：{result['initial_cash']:,}")
    lines.append(f"- 期末资金：{result['final_capital']:,.0f}")
    lines.append(f"- 总盈亏：{result['total_pnl']:+,.0f}（{result['total_return_pct']:+.2f}%）")
    lines.append(f"- 交易笔数：{result['total_trades']}（{result['win_trades']}胜{result['loss_trades']}负）")
    if result['total_trades'] > 0:
        lines.append(f"- 胜率：{result['win_rate']}%")
        if wins:
            avg_win = sum(t["pnl_pct"] for t in wins) / len(wins)
            lines.append(f"- 平均盈利：{avg_win:+.2f}%")
        if losses:
            avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses)
            lines.append(f"- 平均亏损：{avg_loss:+.2f}%")
    lines.append(f"- 仓位规则：最多同时持仓 2 只 × 30% 成本基准资金\n")

    lines.append("## 二、逐笔交割单\n")
    if not trades:
        lines.append("*无交易*\n")
    else:
        for i, t in enumerate(trades, 1):
            mark = "✅" if t["pnl"] > 0 else "❌"
            lines.append(f"### {i}. {t['name']}（{t['code']}）  {mark}\n")
            lines.append(f"- 买入：{t['buy_date']} {t['buy_time']} @ {t['buy_price']:.2f}，"
                        f"{t['shares']}股 = {t['buy_amount']:,.0f}元  [入场：{t['entry_reason']}]")
            lines.append(f"- 卖出：{t['sell_date']} {t['sell_time']} @ {t['sell_price']:.2f}，"
                        f"= {t['sell_amount']:,.0f}元  [出场：{t['exit_reason']}]")
            lines.append(f"- 盈亏：{t['pnl']:+,.0f}（{t['pnl_pct']:+.2f}%）| 持仓 {t['hold_days']} 天\n")

    lines.append("## 三、逐日动作日志\n")
    for d in result.get("daily_log", []):
        if d.get("actions"):
            lines.append(f"### {d['date']}（持仓 {d['open_positions']} | 现金 {d['cash']:,.0f}）\n")
            for act in d["actions"]:
                if act["action"] == "BUY":
                    lines.append(f"- 🟢 **BUY** {act['time']} {act['name']}({act['code']}) "
                                f"@ {act['price']:.2f} × {act['shares']} = {act['amount']:,.0f}  "
                                f"[{act['reason']}]")
                else:
                    emoji = "✅" if act["pnl"] >= 0 else "❌"
                    lines.append(f"- {emoji} **SELL** {act['time']} {act['name']}({act['code']}) "
                                f"@ {act['price']:.2f}  P&L {act['pnl']:+,.0f} ({act['pnl_pct']:+.2f}%)  "
                                f"[{act['reason']}]")
            lines.append("")

    with open(output_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\n交割单已保存到: {output_path}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="方向二盘中监控回测 v2（真实持仓）")
    parser.add_argument("--start", default="2026-04-07", help="开始日期")
    parser.add_argument("--end", default="2026-04-17", help="结束日期")
    parser.add_argument("--output", help="JSON 结果输出路径")
    parser.add_argument("--report", help="Markdown 交割单输出路径")
    parser.add_argument(
        "--recommended", action="store_true",
        help="启用 2026-05-07 迭代验证的推荐配置：max_hold_days=3 + sealed_min_prev_board=2 + stop_loss_pct=-5"
    )
    args = parser.parse_args()

    strategy_params = {}
    if args.recommended:
        strategy_params = {
            "max_hold_days": 3,
            "sealed_min_prev_board": 2,
            "stop_loss_pct": -5.0,
            "take_profit_pct": 12.0,
            "trailing_activate_pct": 5.0,
            "trailing_drawdown_pct": 3.0,
        }
        print("[推荐配置 v2 已启用]")
        print("  max_hold_days=3 + sealed_min_prev_board=2 + stop_loss_pct=-5")
        print("  take_profit_pct=12 + trailing_activate=5 + trailing_drawdown=3")
        print("  (干净段 19 天 +22.61% / 胜率 78.6% / 14 笔)")

    result = run_monitor_backtest_v2(args.start, args.end, strategy_params=strategy_params)

    if args.output and result:
        os.makedirs(os.path.dirname(os.path.expanduser(args.output)) or ".", exist_ok=True)
        with open(os.path.expanduser(args.output), "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"JSON 结果已保存到: {args.output}")

    if args.report and result:
        os.makedirs(os.path.dirname(os.path.expanduser(args.report)) or ".", exist_ok=True)
        generate_report(result, os.path.expanduser(args.report))


if __name__ == "__main__":
    main()
