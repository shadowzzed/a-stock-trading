"""交易执行模拟 — 组合信号解析、价格推算、持仓管理完成一笔模拟交易"""

from __future__ import annotations

from typing import Optional

from .models import (
    TradeSignal, TradeRecord, Position, Portfolio, PortfolioSnapshot,
)
from .signal_parser import parse_trade_signals
from .price_resolver import resolve_buy_price, resolve_sell_price


class TradeSimulator:
    """交易模拟器

    用法:
        sim = TradeSimulator()
        for day_d, day_d1, day_d2 in triplets:
            sim.process_day(day_d, day_d1, day_d2, report, data_dir, loader)
        results = sim.get_results()
    """

    def __init__(self, initial_capital: float = 1_000_000.0):
        self.portfolio = Portfolio(initial_capital=initial_capital)
        self.trades: list[TradeRecord] = []
        self.snapshots: list[PortfolioSnapshot] = []
        self._prev_value = initial_capital
        # 外部注入的 data_loader（CSVStockDataProvider 实例）
        self.data_loader = None

    def set_data_loader(self, loader):
        """设置数据加载器（CSVStockDataProvider 实例）"""
        self.data_loader = loader

    def process_day(
        self,
        signal_date: str,
        target_date: str,
        sell_date: Optional[str],
        report: str,
        data_dir: str,
    ):
        """处理一天的交易模拟

        流程:
        1. 卖出前日持仓（T+1）— 用 sell_date (D+2) 的开盘价
        2. 解析报告中的交易信号
        3. 逐个信号尝试买入

        Args:
            signal_date: 报告日期 (Day D)
            target_date: 执行买入日期 (Day D+1)
            sell_date: 卖出日期 (Day D+2)，None=最后一天无法卖出
            report: Agent 裁决报告全文
            data_dir: 数据根目录
        """
        # Step 1: 卖出前日持仓（用 sell_date = D+2 的开盘价）
        if sell_date and self.portfolio.positions:
            sell_records = self._sell_positions(sell_date, data_dir)
            self.trades.extend(sell_records)

        # Step 2: 解析信号
        signals = parse_trade_signals(report, signal_date, target_date)

        # Step 3: 逐个信号尝试买入
        buy_count = 0
        for signal in signals:
            if signal.action_type == "观望":
                continue
            if len(self.portfolio.positions) >= self.portfolio.max_positions:
                break

            record = self._try_buy(signal, data_dir)
            if record:
                self.trades.append(record)
                if record.buy_executed:
                    buy_count += 1

        self._record_snapshot(target_date, buy_count)

    def get_results(self) -> list[dict]:
        """获取所有交易记录"""
        return [_trade_to_dict(t) for t in self.trades]

    def get_snapshots(self) -> list[dict]:
        """获取资金池快照"""
        return [
            {
                "date": s.date,
                "total_value": round(s.total_value, 2),
                "cash": round(s.cash, 2),
                "position_count": s.position_count,
                "daily_return": round(s.daily_return, 4),
                "trades_today": s.trades_today,
            }
            for s in self.snapshots
        ]

    # ── 内部方法 ──────────────────────────────────────────

    def _sell_positions(self, sell_date: str, data_dir: str) -> list[TradeRecord]:
        """卖出所有可卖持仓"""
        sellable = [p for p in self.portfolio.positions if p.buy_date < sell_date]
        records = []

        for pos in sellable:
            stock_data = None
            if self.data_loader:
                if pos.stock_code:
                    stock_data = self.data_loader.load_stock_daily_by_code(
                        data_dir, sell_date, pos.stock_code
                    )
                if not stock_data:
                    stock_data = self.data_loader.load_stock_daily(
                        data_dir, sell_date, pos.stock_name
                    )

            if not stock_data:
                # 无卖出日数据，用买入价平账（不亏不赚）
                self.portfolio.cash += pos.cost_amount
                self.portfolio.positions.remove(pos)
                records.append(TradeRecord(
                    signal_date=pos.buy_date,
                    target_date=pos.buy_date,
                    stock_name=pos.stock_name,
                    stock_code=pos.stock_code,
                    buy_intended=False,
                    buy_executed=True,
                    buy_price=pos.buy_price,
                    sell_date=sell_date,
                    sell_price=pos.buy_price,
                    sell_reason="无卖出日数据，按买入价平账",
                    pnl_pct=0.0,
                    pnl_amount=0.0,
                    position_pct=pos.position_pct,
                    shares=pos.shares,
                ))
                continue

            sell_price, reason = resolve_sell_price(stock_data)
            pnl_pct = (sell_price - pos.buy_price) / pos.buy_price * 100
            amount = pos.shares * sell_price
            self.portfolio.cash += amount

            records.append(TradeRecord(
                signal_date=pos.buy_date,
                target_date=pos.buy_date,
                stock_name=pos.stock_name,
                stock_code=pos.stock_code,
                buy_intended=False,
                buy_executed=True,
                buy_price=pos.buy_price,
                sell_date=sell_date,
                sell_price=sell_price,
                sell_reason=reason,
                pnl_pct=pnl_pct,
                pnl_amount=amount - pos.cost_amount,
                position_pct=pos.position_pct,
                shares=pos.shares,
            ))
            self.portfolio.positions.remove(pos)

        return records

    def _try_buy(self, signal: TradeSignal, data_dir: str) -> Optional[TradeRecord]:
        """尝试买入一只标的"""
        # 加载 D+1 行情：优先用代码查询，fallback 到名称
        stock_data = None
        if self.data_loader:
            if signal.stock_code:
                stock_data = self.data_loader.load_stock_daily_by_code(
                    data_dir, signal.target_date, signal.stock_code
                )
            if not stock_data:
                stock_data = self.data_loader.load_stock_daily(
                    data_dir, signal.target_date, signal.stock_name
                )

        if not stock_data:
            return TradeRecord(
                signal_date=signal.signal_date,
                target_date=signal.target_date,
                stock_name=signal.stock_name,
                stock_code=signal.stock_code,
                action_type=signal.action_type,
                buy_reason="无{}行情数据".format(signal.target_date),
            )

        # 补充 stock_code
        if not signal.stock_code and stock_data.get("code"):
            signal.stock_code = stock_data["code"]

        # 加载涨停板数据
        limit_up_info = None
        if self.data_loader and signal.action_type == "打板":
            limit_up_info = self.data_loader.load_limit_up_info(
                data_dir, signal.target_date, signal.stock_name
            )

        # 推算买入价
        buy_price, reason = resolve_buy_price(
            signal.action_type, signal.conditions, stock_data, limit_up_info,
        )

        if buy_price is None:
            return TradeRecord(
                signal_date=signal.signal_date,
                target_date=signal.target_date,
                stock_name=signal.stock_name,
                stock_code=stock_data.get("code", ""),
                action_type=signal.action_type,
                buy_price=stock_data.get("open"),
                buy_reason=reason,
            )

        # 仓位管理
        target_amount = self.portfolio.total_value * signal.position_pct
        available = self.portfolio.available_cash

        if target_amount > available:
            target_amount = available * 0.95

        if target_amount < self.portfolio.total_value * 0.1:
            return TradeRecord(
                signal_date=signal.signal_date,
                target_date=signal.target_date,
                stock_name=signal.stock_name,
                stock_code=stock_data.get("code", ""),
                action_type=signal.action_type,
                buy_price=buy_price,
                buy_reason="资金不足（需{:.0f}，可用{:.0f}）".format(
                    target_amount, available),
            )

        shares = int(target_amount / (buy_price * 100)) * 100
        if shares == 0:
            return TradeRecord(
                signal_date=signal.signal_date,
                target_date=signal.target_date,
                stock_name=signal.stock_name,
                stock_code=stock_data.get("code", ""),
                action_type=signal.action_type,
                buy_price=buy_price,
                buy_reason="资金不够买1手（需{:.0f}）".format(buy_price * 100),
            )

        # 执行买入
        cost = shares * buy_price
        self.portfolio.cash -= cost

        position = Position(
            stock_name=signal.stock_name,
            stock_code=stock_data.get("code", ""),
            buy_date=signal.target_date,
            buy_price=buy_price,
            shares=shares,
            position_pct=cost / self.portfolio.total_value,
            cost_amount=cost,
        )
        self.portfolio.positions.append(position)

        return TradeRecord(
            signal_date=signal.signal_date,
            target_date=signal.target_date,
            stock_name=signal.stock_name,
            stock_code=stock_data.get("code", ""),
            action_type=signal.action_type,
            buy_executed=True,
            buy_price=buy_price,
            buy_reason=reason,
            position_pct=position.position_pct,
            shares=shares,
        )

    def _record_snapshot(self, date: str, trades_today: int):
        current = self.portfolio.total_value
        daily_return = (current - self._prev_value) / self._prev_value

        self.snapshots.append(PortfolioSnapshot(
            date=date,
            total_value=current,
            cash=self.portfolio.cash,
            position_count=len(self.portfolio.positions),
            daily_return=daily_return,
            trades_today=trades_today,
        ))
        self._prev_value = current


def _trade_to_dict(t: TradeRecord) -> dict:
    return {
        "trade_id": t.trade_id,
        "signal_date": t.signal_date,
        "target_date": t.target_date,
        "stock_name": t.stock_name,
        "stock_code": t.stock_code,
        "action_type": t.action_type,
        "buy_executed": t.buy_executed,
        "buy_price": t.buy_price,
        "buy_reason": t.buy_reason,
        "sell_date": t.sell_date,
        "sell_price": t.sell_price,
        "sell_reason": t.sell_reason,
        "pnl_pct": round(t.pnl_pct, 2) if t.pnl_pct is not None else None,
        "pnl_amount": round(t.pnl_amount, 2) if t.pnl_amount is not None else None,
        "position_pct": round(t.position_pct, 3),
        "shares": t.shares,
    }
