#!/usr/bin/env python3
"""
使用 mootdx 导入过去 15 个交易日的全量 A 股日线数据到 intraday.db

用法: python3 trading/import_history.py
"""

import os
import sys
import sqlite3
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
from mootdx.quotes import Quotes

INTRADAY_DIR = os.path.join(os.path.dirname(__file__), "intraday")
DB_PATH = os.path.join(INTRADAY_DIR, "intraday.db")

A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688")
# 取16天（多一天用于算 preclose）
KLINE_COUNT = 20  # 多取几天以确保覆盖15个交易日


def init_db(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_bars (
            date      TEXT NOT NULL,
            code      TEXT NOT NULL,
            name      TEXT,
            open      REAL,
            high      REAL,
            low       REAL,
            close     REAL,
            volume    INTEGER,
            amount    REAL,
            pct_chg   REAL,
            PRIMARY KEY (date, code)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_bars_code ON daily_bars(code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_bars_date ON daily_bars(date)")
    conn.commit()


def get_all_codes_and_names():
    """获取全部 A 股代码和名称"""
    client = Quotes.factory(market="std")
    codes = []
    code_to_name = {}
    for market in [0, 1]:
        stocks = client.stocks(market=market)
        for _, row in stocks.iterrows():
            code = row["code"]
            if code.startswith(A_SHARE_PREFIXES):
                codes.append(code)
                code_to_name[code] = row["name"].strip()
    return codes, code_to_name


def fetch_kline(code):
    """拉取单只股票日 K 线"""
    try:
        client = Quotes.factory(market="std")
        df = client.bars(symbol=code, frequency=9, offset=KLINE_COUNT)
        if df is not None and not df.empty:
            return code, df
    except Exception as e:
        print(f"  [WARN] {code}: {e}", file=sys.stderr, flush=True)
    return code, None


def main():
    os.makedirs(INTRADAY_DIR, exist_ok=True)

    print("获取全 A 股票列表...", flush=True)
    all_codes, code_to_name = get_all_codes_and_names()
    print(f"共 {len(all_codes)} 只 A 股", flush=True)

    # 多线程拉取日 K 线
    print(f"开始多线程拉取日 K 线（8线程）...", flush=True)
    t0 = time.time()

    results = {}  # code -> DataFrame
    errors = 0
    done = 0

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_kline, code): code for code in all_codes}
        for future in as_completed(futures):
            code, df = future.result()
            done += 1
            if df is not None:
                results[code] = df
            else:
                errors += 1
            if done % 500 == 0:
                print(f"  进度: {done}/{len(all_codes)} ({done*100//len(all_codes)}%)", flush=True)

    t1 = time.time()
    print(f"拉取完成: {len(results)} 只成功, {errors} 失败, 耗时 {t1-t0:.1f}s", flush=True)

    # 确定要导入的15个交易日
    # 从任意一只有数据的股票取日期列表
    sample_df = next(iter(results.values()))
    all_dates = sorted(sample_df.index.strftime("%Y-%m-%d").unique())
    # 取最后16个日期（第一个用于 preclose，后15个导入）
    if len(all_dates) > 16:
        all_dates = all_dates[-16:]

    import_dates = all_dates[1:]  # 后15天
    preclose_date = all_dates[0]  # 第一天仅用于 preclose

    print(f"导入日期范围: {import_dates[0]} ~ {import_dates[-1]} ({len(import_dates)}天)", flush=True)
    print(f"前一交易日（用于首日 preclose）: {preclose_date}", flush=True)

    # 写入 SQLite
    print("写入 SQLite...", flush=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    init_db(conn)

    total_rows = 0
    batch_rows = []

    for code, df in results.items():
        name = code_to_name.get(code, "")

        # 按日期排序
        df = df.sort_index()
        dates_in_df = df.index.strftime("%Y-%m-%d").tolist()
        closes = df["close"].tolist()

        for i, row in df.iterrows():
            date_str = i.strftime("%Y-%m-%d")
            if date_str not in import_dates:
                continue

            close_price = float(row["close"])
            open_price = float(row["open"])
            high_price = float(row["high"])
            low_price = float(row["low"])
            volume = int(row["vol"]) if pd.notna(row["vol"]) else 0
            amount = float(row["amount"]) if pd.notna(row["amount"]) else 0.0

            # 找 preclose：前一个交易日的 close
            idx = dates_in_df.index(date_str) if date_str in dates_in_df else -1
            if idx > 0:
                preclose = closes[idx - 1]
            else:
                preclose = 0.0

            # 计算涨跌幅
            if preclose > 0:
                pctChg = round((close_price - preclose) / preclose * 100, 2)
            else:
                pctChg = 0.0

            batch_rows.append((
                date_str, code, name,
                open_price, high_price, low_price, close_price,
                volume, amount, pctChg,
            ))
            total_rows += 1

        # 每10000行写入一次
        if len(batch_rows) >= 10000:
            conn.executemany("""
                INSERT OR REPLACE INTO daily_bars
                (date, code, name, open, high, low, close, volume, amount, pct_chg)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, batch_rows)
            conn.commit()
            batch_rows = []

    # 写入剩余
    if batch_rows:
        conn.executemany("""
            INSERT OR REPLACE INTO daily_bars
            (date, code, name, open, high, low, close, volume, amount, pct_chg)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, batch_rows)
        conn.commit()

    conn.close()

    db_size = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f"\n✓ 完成! 共导入 {total_rows} 条记录", flush=True)
    print(f"  日期: {import_dates[0]} ~ {import_dates[-1]} ({len(import_dates)}天)", flush=True)
    print(f"  数据库大小: {db_size:.1f} MB", flush=True)

    # 验证：按天统计
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.execute("""
        SELECT date, count(*)
        FROM daily_bars
        GROUP BY date ORDER BY date
    """)
    print(f"\n  按天统计:", flush=True)
    print(f"  {'日期':<12} {'股票数':>6}", flush=True)
    for row in cursor:
        print(f"  {row[0]:<12} {row[1]:>6}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
