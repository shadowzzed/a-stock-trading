#!/usr/bin/env python3
"""判断今日/指定日期是否是 A 股交易日。

退出码：
  0 - 是交易日
  1 - 非交易日

用法：
  python3 tools/is_trading_day.py              # 今日
  python3 tools/is_trading_day.py 2026-04-19    # 指定日期
"""

from __future__ import annotations
import json
import os
import sys
from datetime import date, datetime

HOLIDAYS_JSON = os.path.expanduser(
    "~/src/a-stock-trading/data/chinese_holidays.json"
)


def is_trading_day(check: date) -> bool:
    # 周末：默认非交易日
    is_weekend = check.weekday() >= 5

    with open(HOLIDAYS_JSON) as f:
        data = json.load(f)

    year = str(check.year)
    if year not in data:
        # 无该年数据：按周末判断
        return not is_weekend

    year_data = data[year]
    date_str = check.isoformat()

    # 节假日列表（非交易日）
    holidays = set()
    for key in ("holidays", "节假日", "non_trading"):
        if key in year_data:
            holidays.update(year_data[key])
    # 周末补班日（是交易日）
    extras = set()
    for key in ("extra_trading", "补班", "work_days"):
        if key in year_data:
            extras.update(year_data[key])

    if date_str in holidays:
        return False
    if date_str in extras:
        return True
    return not is_weekend


def main():
    if len(sys.argv) > 1:
        d = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    else:
        d = date.today()
    sys.exit(0 if is_trading_day(d) else 1)


if __name__ == "__main__":
    main()
