"""交易模拟数据模型"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StockDailyData:
    """单只股票的日线 OHLC 数据"""
    date: str
    code: str
    name: str
    open: float
    high: float
    low: float
    close: float
    pct_chg: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    last_close: float = 0.0
    # 涨停板数据（可选）
    is_limit_up: bool = False
    broken_count: int = 0           # 炸板次数
    first_seal_time: str = ""       # 首次封板时间

    @property
    def limit_up_price(self) -> float:
        """涨停价"""
        pct = 20 if self.code.startswith(("300", "301", "688")) else 10
        return round(self.last_close * (1 + pct / 100), 2)

    @property
    def limit_down_price(self) -> float:
        """跌停价"""
        pct = 20 if self.code.startswith(("300", "301", "688")) else 10
        return round(self.last_close * (1 - pct / 100), 2)

    @property
    def is_one_word_board(self) -> bool:
        """是否一字涨停（开盘=最低=涨停价）"""
        return (
            self.open == self.limit_up_price
            and self.low == self.limit_up_price
            and self.close == self.limit_up_price
        )


@dataclass
class TradeSignal:
    """从 Agent 报告中解析出的交易信号"""
    signal_date: str               # Day D（报告日期）
    target_date: str               # Day D+1（计划执行日期）
    stock_name: str                # 标的名称
    stock_code: str = ""           # 标的代码
    action_type: str = ""          # 打板 / 低吸 / 竞价买入 / 观望
    conditions: list[str] = field(default_factory=list)  # 竞价/盘中条件
    position_pct: float = 0.3      # 建议仓位
    priority: int = 1              # 1=首选, 2=备选
    raw_text: str = ""             # 原始策略文本
    source: str = "markdown"       # 解析来源: json / markdown / fallback


@dataclass
class TradeRecord:
    """模拟交易记录"""
    trade_id: str = ""
    signal_date: str = ""          # 信号日期
    target_date: str = ""          # 执行日期
    stock_name: str = ""
    stock_code: str = ""
    action_type: str = ""          # 原始操作意图（打板/低吸/竞价买入）
    # 买入
    buy_intended: bool = True      # 是否有买入意图
    buy_executed: bool = False     # 是否实际成交
    buy_price: Optional[float] = None
    buy_reason: str = ""           # 成交/未成交原因
    # 卖出
    sell_date: Optional[str] = None
    sell_price: Optional[float] = None
    sell_reason: str = ""
    # 盈亏
    pnl_pct: Optional[float] = None
    pnl_amount: Optional[float] = None
    position_pct: float = 0.0
    shares: int = 0

    def __post_init__(self):
        if not self.trade_id:
            self.trade_id = uuid.uuid4().hex[:8]


@dataclass
class Position:
    """持仓记录"""
    stock_name: str
    stock_code: str
    buy_date: str
    buy_price: float
    shares: int
    position_pct: float
    cost_amount: float


@dataclass
class Portfolio:
    """模拟资金池"""
    initial_capital: float = 1_000_000.0    # 初始 100 万
    cash: float = 0.0
    positions: list[Position] = field(default_factory=list)
    max_positions: int = 3                   # 最多同时持有 3 只
    min_position_pct: float = 0.2           # 单只最低 2 成
    max_position_pct: float = 0.5           # 单只最高 5 成

    def __post_init__(self):
        if self.cash == 0.0:
            self.cash = self.initial_capital

    @property
    def position_value(self) -> float:
        """持仓市值（用买入价估算，简化版）"""
        return sum(p.cost_amount for p in self.positions)

    @property
    def total_value(self) -> float:
        return self.cash + self.position_value

    @property
    def available_cash(self) -> float:
        """可开新仓的现金"""
        return self.cash


@dataclass
class PortfolioSnapshot:
    """资金池日快照"""
    date: str
    total_value: float
    cash: float
    position_count: int
    daily_return: float = 0.0       # 当日收益率
    trades_today: int = 0           # 当日成交笔数
