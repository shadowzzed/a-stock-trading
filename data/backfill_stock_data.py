"""补齐缺失的行情CSV（从已有的行情CSV提取股票池，用baostock拉取）"""

import os
import csv
import baostock as bs
import pandas as pd
from datetime import datetime

# 从已有行情CSV提取股票池
REFERENCE_CSV = os.path.join(os.path.dirname(__file__), "daily/2026-03-09/行情_20260309.csv")
DAILY_DIR = os.path.join(os.path.dirname(__file__), "daily")

# 需要补齐的日期（2月2日 ~ 3月3日的工作日）
def _generate_dates(start, end):
    from datetime import date, timedelta
    dates = []
    d = date.fromisoformat(start)
    e = date.fromisoformat(end)
    while d <= e:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    return dates

MISSING_DATES = _generate_dates("2026-02-02", "2026-03-03")


def load_stock_pool():
    """从参考CSV加载股票池（代码+名称+板块）"""
    df = pd.read_csv(REFERENCE_CSV, encoding="utf-8-sig")
    pool = []
    for _, row in df.iterrows():
        pool.append({
            "code": row["代码"],
            "name": row["名称"],
            "sector": row["板块"],
        })
    return pool


def fetch_day_data(stock_pool, date_str):
    """用baostock拉取一天的行情数据"""
    rows = []
    for stock in stock_pool:
        code = stock["code"]
        # baostock 格式: sh.600000 或 sz.000001
        rs = bs.query_history_k_data_plus(
            code,
            "date,code,open,high,low,close,pctChg,turn,amount",
            start_date=date_str,
            end_date=date_str,
            frequency="d",
            adjustflag="2",  # 前复权
        )
        while rs.error_code == '0' and rs.next():
            row = rs.get_row_data()
            if row and row[0] == date_str:
                rows.append({
                    "日期": row[0],
                    "代码": code,
                    "名称": stock["name"],
                    "开盘价": row[2] or "",
                    "最高价": row[3] or "",
                    "最低价": row[4] or "",
                    "收盘价": row[5] or "",
                    "涨跌幅": row[6] or "",
                    "换手率": row[7] or "",
                    "成交额": row[8] or "",
                    "板块": stock["sector"],
                })
    return rows


def main():
    bs.login()
    stock_pool = load_stock_pool()
    print(f"股票池: {len(stock_pool)} 只")

    for date_str in MISSING_DATES:
        date_compact = date_str.replace("-", "")
        out_dir = os.path.join(DAILY_DIR, date_str)
        out_path = os.path.join(out_dir, f"行情_{date_compact}.csv")

        if os.path.exists(out_path):
            print(f"[跳过] {out_path} 已存在")
            continue

        print(f"[拉取] {date_str} ...")
        rows = fetch_day_data(stock_pool, date_str)

        if not rows:
            print(f"  [警告] {date_str} 无数据（可能非交易日）")
            continue

        os.makedirs(out_dir, exist_ok=True)
        df = pd.DataFrame(rows)
        df.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"  [完成] {len(rows)} 条 -> {out_path}")

    bs.logout()
    print("全部完成")


if __name__ == "__main__":
    main()
