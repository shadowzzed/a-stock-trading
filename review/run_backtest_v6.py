#!/usr/bin/env python3
"""经验驱动回测 v6 独立脚本 - 可后台运行

用法:
    # 回测指定日期范围
    python -m review.run_backtest_v6 --data-dir ~/trading-data --start 2026-03-01 --end 2026-03-31

    # 回测所有可用日期
    python -m review.run_backtest_v6 --data-dir ~/trading-data

    # 后台运行
    nohup python -m review.run_backtest_v6 --data-dir ~/trading-data --start 2026-03-01 > backtest_v6.log 2>&1 &
"""

from .experience_backtest import main

if __name__ == "__main__":
    main()
