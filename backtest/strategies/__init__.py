"""策略池 — 用于影子交易和健康度评估

每个策略是一组可配置参数 + 可选过滤器的组合。
Strategy 对象可被 monitor_backtest_v2.run_monitor_backtest_v2 使用。
"""
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Strategy:
    """策略定义：命名 + 参数 + 过滤器配置"""
    name: str                         # 策略名（唯一）
    description: str                  # 简述
    stop_loss_pct: float = -7.0       # 止损
    take_profit_pct: float = 15.0     # 止盈
    max_positions: int = 3            # 最大持仓
    max_hold_days: int = 5            # 最大持仓天数
    include_trend: bool = True        # 是否走趋势股路径
    include_reversal: bool = True     # 是否反包加分
    entry_filter: bool = True         # 入场过滤（高位分歧/反包）
    auction_strong_only: bool = False # 是否仅允许 auction_strong（剔除 sealed）
    sealed_only: bool = False         # 是否仅允许 sealed
    market_heat_min: int = 0          # 市场冷淡过滤（昨日涨停数 < 此数不买，0=关闭）
    notes: str = ""                   # 备注

    def as_params_dict(self) -> dict:
        """返回可传给 monitor_backtest_v2 的参数字典"""
        return {
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "max_positions": self.max_positions,
            "max_hold_days": self.max_hold_days,
            "include_trend": self.include_trend,
            "include_reversal": self.include_reversal,
            "entry_filter": self.entry_filter,
            "auction_strong_only": self.auction_strong_only,
            "sealed_only": self.sealed_only,
            "market_heat_min": self.market_heat_min,
        }


# ── 策略池 ────────────────────────────────────────────

STRATEGIES: dict[str, Strategy] = {
    "v8_base": Strategy(
        name="v8_base",
        description="当前生产基线（入场过滤+超时5天+3仓位+-7/+15）",
    ),
    "v8_tight": Strategy(
        name="v8_tight",
        description="紧止损止盈（-5/+10），适合震荡市",
        stop_loss_pct=-5.0,
        take_profit_pct=10.0,
    ),
    "v8_wide": Strategy(
        name="v8_wide",
        description="宽止损止盈（-10/+20），适合趋势市",
        stop_loss_pct=-10.0,
        take_profit_pct=20.0,
    ),
    "v8_sealed_only": Strategy(
        name="v8_sealed_only",
        description="仅封板入场，不接竞价高开（胜率导向）",
        sealed_only=True,
    ),
    "v8_auction_only": Strategy(
        name="v8_auction_only",
        description="仅竞价高开入场（卡位更早但胜率低）",
        auction_strong_only=True,
    ),
    "v8_no_trend": Strategy(
        name="v8_no_trend",
        description="关闭趋势股路径（纯涨停龙头）",
        include_trend=False,
    ),
    "v8_heat_gate": Strategy(
        name="v8_heat_gate",
        description="市场冷淡（昨日涨停<30）不买",
        market_heat_min=30,
    ),
    "v8_conservative": Strategy(
        name="v8_conservative",
        description="保守：紧止损 + 2 仓位 + 只封板",
        stop_loss_pct=-5.0,
        take_profit_pct=12.0,
        max_positions=2,
        sealed_only=True,
    ),
}


def get_strategy(name: str) -> Strategy:
    if name not in STRATEGIES:
        raise KeyError(f"策略不存在: {name}。已注册: {list(STRATEGIES.keys())}")
    return STRATEGIES[name]


def list_strategies() -> list[str]:
    return list(STRATEGIES.keys())
