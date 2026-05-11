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
    trailing_activate_pct: float = 0  # 移动止盈激活阈值（浮盈 ≥ 此值启用），0=关闭
    trailing_drawdown_pct: float = 0  # 移动止盈触发回撤（峰值后回撤 ≥ 此值卖出），0=关闭
    layer1_gate: bool = False         # 是否启用 Layer 1 GLM 大盘门控
    layer1_provider: str = "GLM"      # Layer 1 LLM 提供商
    sealed_min_prev_board: int = 0    # 封板入场要求：昨日 ≥X 板才接力（0=关闭）
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
            "trailing_activate_pct": self.trailing_activate_pct,
            "trailing_drawdown_pct": self.trailing_drawdown_pct,
            "layer1_gate": self.layer1_gate,
            "layer1_provider": self.layer1_provider,
            "sealed_min_prev_board": self.sealed_min_prev_board,
        }


# ── 策略池 ────────────────────────────────────────────

STRATEGIES: dict[str, Strategy] = {
    "v8_base": Strategy(
        name="v8_base",
        description="当前生产基线（入场过滤+超时5天+3仓位+-7/+15）",
    ),
    "v8_tight": Strategy(
        name="v8_tight",
        description="紧止损止盈（-5/+10）+ Layer1 deterministic 门控（生产默认）",
        stop_loss_pct=-5.0,
        take_profit_pct=10.0,
        layer1_gate=True,
        layer1_provider="deterministic",
    ),
    "v8_tight_naked": Strategy(
        name="v8_tight_naked",
        description="v8_tight 不含 Layer1（仅用于对比 Layer1 的边际价值）",
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
    "v8_tight_trail": Strategy(
        name="v8_tight_trail",
        description="v8_tight + 移动止盈（+5% 激活，回撤 3% 卖）",
        stop_loss_pct=-5.0,
        take_profit_pct=10.0,
        trailing_activate_pct=5.0,
        trailing_drawdown_pct=3.0,
    ),
    "v8_tight_trail_wide": Strategy(
        name="v8_tight_trail_wide",
        description="v8_tight + 宽 trailing（+5% 激活，回撤 5% 卖，让利润奔跑）",
        stop_loss_pct=-5.0,
        take_profit_pct=10.0,
        trailing_activate_pct=5.0,
        trailing_drawdown_pct=5.0,
    ),
    "v8_tight_layer1": Strategy(
        name="v8_tight_layer1",
        description="v8_tight + Layer 1 大盘情绪门控（deterministic，与生产一致，恐慌日跳过买入）",
        stop_loss_pct=-5.0,
        take_profit_pct=10.0,
        layer1_gate=True,
        layer1_provider="deterministic",
    ),
    "v8_tight_strict": Strategy(
        name="v8_tight_strict",
        description="v8_tight + 封板只接 ≥2 板（避免1板分歧）",
        stop_loss_pct=-5.0,
        take_profit_pct=10.0,
        layer1_gate=True,
        layer1_provider="deterministic",
        sealed_min_prev_board=2,
    ),
    "v8_tight_fast": Strategy(
        name="v8_tight_fast",
        description="v8_tight + 短持仓（max_hold_days=3，更快轮换）",
        stop_loss_pct=-5.0,
        take_profit_pct=10.0,
        max_hold_days=3,
        layer1_gate=True,
        layer1_provider="deterministic",
    ),
}


def get_strategy(name: str) -> Strategy:
    if name not in STRATEGIES:
        raise KeyError(f"策略不存在: {name}。已注册: {list(STRATEGIES.keys())}")
    return STRATEGIES[name]


def list_strategies() -> list[str]:
    return list(STRATEGIES.keys())
