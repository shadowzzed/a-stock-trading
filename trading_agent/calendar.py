"""A股交易日历服务 — 处理中国法定节假日和周末补班。

用法:
    from trading_agent.calendar import is_trading_day, recent_trading_days, trading_days_between

    is_trading_day("2026-01-01")  # False（元旦）
    recent_trading_days(5, "2026-03-24")  # ['2026-03-24', '2026-03-23', ...]
    trading_days_between("2026-03-02", "2026-03-13")  # 跳过周末
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Optional

# 模块所在目录
_DIR = os.path.dirname(os.path.abspath(__file__))
_HOLIDAYS_JSON = os.path.join(_DIR, "..", "data", "chinese_holidays.json")

# 缓存
_holidays_cache: dict | None = None


def _load_holidays() -> dict:
    """加载节假日数据（懒加载 + 缓存）。"""
    global _holidays_cache
    if _holidays_cache is not None:
        return _holidays_cache

    if not os.path.exists(_HOLIDAYS_JSON):
        import logging
        logging.getLogger(__name__).warning(
            "交易日历: %s 不存在，仅按周末判断", _HOLIDAYS_JSON
        )
        _holidays_cache = {}
        return _holidays_cache

    try:
        with open(_HOLIDAYS_JSON, "r", encoding="utf-8") as f:
            _holidays_cache = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        import logging
        logging.getLogger(__name__).warning("交易日历加载失败: %s", e)
        _holidays_cache = {}

    return _holidays_cache


def _date_from_str(s: str) -> date:
    """解析日期字符串。"""
    return datetime.strptime(s, "%Y-%m-%d").date()


def _date_key(d: date) -> str:
    """date → JSON key (YYYY-MM-DD)。"""
    return d.strftime("%Y-%m-%d")


def _get_holidays_for_year(year: int) -> set[str]:
    """获取某年的假期日期集合。"""
    data = _load_holidays()
    year_key = str(year)
    return set(data.get(year_key, {}).get("holidays", []))


def _get_makeup_days_for_year(year: int) -> set[str]:
    """获取某年的周末补班日期集合。"""
    data = _load_holidays()
    year_key = str(year)
    return set(data.get(year_key, {}).get("makeup_days", []))


def is_trading_day(d: str | date) -> bool:
    """判断某天是否为 A 股交易日。

    规则:
    1. 周末补班日 → 是交易日
    2. 周末且非补班 → 不是交易日
    3. 工作日但在假期列表中 → 不是交易日
    4. 其余工作日 → 是交易日
    """
    if isinstance(d, str):
        d = _date_from_str(d)

    key = _date_key(d)
    year = d.year
    weekday = d.weekday()  # 0=Mon, 6=Sun

    makeup = _get_makeup_days_for_year(year)
    if key in makeup:
        return True

    if weekday >= 5:  # 周末
        return False

    holidays = _get_holidays_for_year(year)
    if key in holidays:
        return False

    return True


def recent_trading_days(
    n: int,
    before: Optional[str | date] = None,
) -> list[str]:
    """获取某日之前的最近 n 个交易日（含 before 本身）。

    Args:
        n: 需要的交易日数量
        before: 截止日期，默认今天

    Returns:
        日期字符串列表，最近的在前
    """
    if before is None:
        before = date.today()
    elif isinstance(before, str):
        before = _date_from_str(before)

    result: list[str] = []
    current = before
    while len(result) < n:
        if is_trading_day(current):
            result.append(_date_key(current))
        current -= timedelta(days=1)

        # 安全阀：防止无限循环（回溯超过 60 天）
        if (before - current).days > 60:
            break

    return result


def trading_days_between(
    start: str | date,
    end: str | date,
) -> list[str]:
    """获取 [start, end] 范围内的所有交易日。

    Args:
        start: 起始日期（含）
        end: 结束日期（含）

    Returns:
        日期字符串列表，按时间正序
    """
    if isinstance(start, str):
        start = _date_from_str(start)
    if isinstance(end, str):
        end = _date_from_str(end)

    result: list[str] = []
    current = start
    while current <= end:
        if is_trading_day(current):
            result.append(_date_key(current))
        current += timedelta(days=1)

    return result
