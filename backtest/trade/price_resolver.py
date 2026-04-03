"""成交价推算 — 基于 OHLC 日线数据确定模拟成交价

核心原则：宁可模拟不成交，也不虚假成交。所有假设偏保守。
所有 stock_data 参数为 dict（来自 adapter 的 CSV 行）。
"""

from __future__ import annotations

import re
from typing import Optional


def resolve_buy_price(
    action_type: str,
    conditions: list[str],
    stock_data: dict,
    limit_up_info: Optional[dict] = None,
) -> tuple[Optional[float], str]:
    """推算买入成交价

    Args:
        action_type: 操作类型（打板/低吸/竞价买入/观望）
        conditions: 竞价/盘中条件列表
        stock_data: D+1 日线数据 dict（含 open/high/low/close/last_close 等）
        limit_up_info: 涨停板数据 dict（含 broken_count 等），可选

    Returns:
        (buy_price, reason) — price 为 None 表示不成交
    """
    if action_type == "观望":
        return None, "观望不参与"

    if action_type == "打板":
        return _resolve_limit_up_buy(stock_data, conditions, limit_up_info)
    elif action_type == "竞价买入":
        return _resolve_auction_buy(conditions, stock_data)
    elif action_type == "低吸":
        return _resolve_dip_buy(conditions, stock_data)
    else:
        return _resolve_dip_buy(conditions, stock_data)


def _limit_up_price(stock: dict) -> float:
    """计算涨停价"""
    last_close = stock.get("last_close", 0) or stock.get("close", 0)
    code = stock.get("code", "")
    pct = 20 if code.startswith(("300", "301", "688")) else 10
    return round(last_close * (1 + pct / 100), 2)


def _is_one_word_board(stock: dict) -> bool:
    """是否一字涨停"""
    lp = _limit_up_price(stock)
    return (
        stock.get("open") == lp
        and stock.get("low") == lp
        and stock.get("close") == lp
    )


def _resolve_limit_up_buy(
    stock: dict,
    conditions: list[str],
    limit_up_info: Optional[dict],
) -> tuple[Optional[float], str]:
    """打板成交推算"""
    limit_price = _limit_up_price(stock)
    high = stock.get("high", 0)

    # 条件检查
    if any("一字板" in c for c in conditions):
        if _is_one_word_board(stock):
            return None, "一字涨停，封死买不到"
        else:
            return None, "条件未满足（非一字板），不参与"

    # 检查是否触及涨停
    if high < limit_price * 0.995:
        return None, "未触及涨停价（最高{:.2f} < 涨停{:.2f}），无打板机会".format(
            high, limit_price)

    # 一字涨停
    if _is_one_word_board(stock):
        return None, "一字涨停，封死买不到"

    # 从 limit_up_info 获取炸板次数
    broken_count = 0
    if limit_up_info:
        broken_count = limit_up_info.get("broken_count", 0)

    if broken_count > 0:
        return limit_price, "炸板{}次，以涨停价{:.2f}成交".format(
            broken_count, limit_price)

    # 触及涨停 + 未炸板 → 全天封死
    open_price = stock.get("open", 0)
    if open_price < limit_price:
        return None, "触及涨停但全天封死（未炸板），买不到"

    return None, "开盘涨停封死，买不到"


def _resolve_auction_buy(
    conditions: list[str],
    stock: dict,
) -> tuple[Optional[float], str]:
    """竞价买入推算"""
    open_price = stock.get("open", 0)
    last_close = stock.get("last_close", 0) or stock.get("close", 0)

    for cond in conditions:
        pct = _extract_high_open_pct(cond)
        if pct is not None and last_close > 0:
            actual_pct = (open_price - last_close) / last_close * 100
            if actual_pct < pct:
                return None, "竞价条件不满足：需高开{:.1f}%，实际{:+.1f}%".format(
                    pct, actual_pct)
            slip_price = round(open_price * 1.001, 2)
            return slip_price, "竞价买入，高开{:.1f}%满足条件，成交价{:.2f}".format(
                actual_pct, slip_price)

    # 无明确竞价条件
    slip_price = round(open_price * 1.001, 2)
    if last_close > 0:
        open_pct = (open_price - last_close) / last_close * 100
        return slip_price, "竞价买入，开盘{:.2f}（{:+.1f}%），成交价{:.2f}".format(
            open_price, open_pct, slip_price)

    return slip_price, "竞价买入，成交价{:.2f}".format(slip_price)


def _resolve_dip_buy(
    conditions: list[str],
    stock: dict,
) -> tuple[Optional[float], str]:
    """低吸买入推算"""
    open_price = stock.get("open", 0)
    low = stock.get("low", 0)
    high = stock.get("high", 0)

    target_price = None
    reason_prefix = ""

    for cond in conditions:
        if "绿盘" in cond:
            target_price = max(open_price, low)
            reason_prefix = "绿盘买入"
            break
        if "低开" in cond:
            target_price = open_price
            reason_prefix = "低开买入"
            break

    if target_price is None:
        target_price = round(low * 1.005, 2)
        reason_prefix = "低吸买入"

    if target_price > high:
        return None, "目标价{:.2f} > 最高价{:.2f}，股价未跌到目标位，不成交".format(
            target_price, high)

    if target_price < low:
        target_price = low

    final_price = round(target_price * 1.002, 2)
    return final_price, "{}，目标价{:.2f}，成交价{:.2f}".format(
        reason_prefix, target_price, final_price)


def resolve_sell_price(stock_data: dict) -> tuple[float, str]:
    """卖出价推算（T+1，以 D+2 开盘价卖出）"""
    sell_price = stock_data.get("open", 0)
    return sell_price, "T+1卖出，D+2开盘价{:.2f}".format(sell_price)


def _extract_high_open_pct(condition: str) -> Optional[float]:
    """从条件文本中提取高开百分比"""
    m = re.search(r'高开\s*([\d.]+)\s*%?', condition)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m = re.search(r'([\d.]+)\s*%\s*以上', condition)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None
