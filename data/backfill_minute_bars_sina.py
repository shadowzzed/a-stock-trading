#!/usr/bin/env python3
"""
用新浪 API 回填指定日期的全市场 1 分钟 K 线

新浪 datalen 硬上限 1970 行 ≈ 最近 8-9 个交易日。超出窗口无数据。

用法:
  python3 trading/backfill_minute_bars_sina.py 2026-04-13 2026-04-16
  python3 trading/backfill_minute_bars_sina.py 2026-04-13 --workers 15 --limit 100  # 测试100只
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import logging
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import json
import pandas as pd

DB_PATH = os.path.expanduser("~/shared/trading/intraday/intraday.db")
SINA_URL = "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)


def code_to_sina_symbol(code: str) -> str:
    if code.startswith(("60", "68", "90")):
        return f"sh{code}"
    if code.startswith(("00", "30", "20")):
        return f"sz{code}"
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"bj{code}"
    return f"sh{code}"


def fetch_sina_1min(symbol: str, datalen: int = 1970,
                    timeout: int = 15, retries: int = 2):
    params = {"symbol": symbol, "scale": "1", "ma": "no", "datalen": str(datalen)}
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(SINA_URL, params=params, timeout=timeout)
            text = r.text
            if "null" in text[:60]:
                return None
            data_json = json.loads(text.split("=(")[1].split(");")[0])
            return pd.DataFrame(data_json)
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise last_err


def get_target_codes(conn: sqlite3.Connection, date: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT code FROM stock_meta WHERE date = ? ORDER BY code",
        (date,),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT DISTINCT code FROM daily_bars WHERE date = ? ORDER BY code",
            (date,),
        ).fetchall()
    return [r[0] for r in rows]


def fetch_one(code: str, target_dates: set[str]):
    """拉单只股票，返回 (code, 目标日期的行列表)"""
    symbol = code_to_sina_symbol(code)
    try:
        df = fetch_sina_1min(symbol)
    except Exception as e:
        return code, None, str(e)

    if df is None or df.empty:
        return code, [], None

    df = df[df["day"].str[:10].isin(target_dates)].copy()
    if df.empty:
        return code, [], None

    rows = []
    for _, row in df.iterrows():
        day = str(row["day"])
        rows.append((
            day[:10],                           # date
            day[11:16],                         # time
            code,
            float(row.get("open", 0) or 0),
            float(row.get("high", 0) or 0),
            float(row.get("low", 0) or 0),
            float(row.get("close", 0) or 0),
            float(row.get("volume", 0) or 0),
            float(row.get("volume", 0) or 0) * float(row.get("close", 0) or 0),  # 估算成交额
        ))
    return code, rows, None


def backfill(target_dates: list[str], workers: int = 10, limit: Optional[int] = None):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    # 从所有目标日期的 stock_meta 合并股票清单
    all_codes: set[str] = set()
    for date in target_dates:
        codes = get_target_codes(conn, date)
        logger.info(f"[{date}] 股票清单: {len(codes)} 只")
        all_codes.update(codes)

    all_codes = sorted(all_codes)
    if limit:
        all_codes = all_codes[:limit]
    logger.info(f"合并后总股票数: {len(all_codes)}")

    target_set = set(target_dates)
    inserted = 0
    failed = []
    empty = 0
    t0 = time.time()
    buffer: list[tuple] = []

    def flush_buffer():
        nonlocal inserted
        if not buffer:
            return
        conn.executemany(
            "INSERT OR REPLACE INTO minute_bars "
            "(date, time, code, open, high, low, close, volume, amount) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            buffer,
        )
        conn.commit()
        inserted += len(buffer)
        buffer.clear()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_one, code, target_set): code for code in all_codes}
        for i, fut in enumerate(as_completed(futures), 1):
            code, rows, err = fut.result()
            if err:
                failed.append((code, err))
            elif not rows:
                empty += 1
            else:
                buffer.extend(rows)
                if len(buffer) >= 5000:
                    flush_buffer()

            if i % 500 == 0:
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed > 0 else 0
                eta = (len(all_codes) - i) / rate if rate > 0 else 0
                logger.info(f"进度 {i}/{len(all_codes)} "
                            f"({rate:.1f} 只/s, ETA {eta:.0f}s) | "
                            f"失败 {len(failed)} | 空 {empty}")

    flush_buffer()

    elapsed = time.time() - t0
    logger.info(f"完成：总耗时 {elapsed:.1f}s")
    logger.info(f"股票总数: {len(all_codes)} | 插入行: {inserted} | "
                f"失败: {len(failed)} | 空: {empty}")

    # 数据健康度校验
    for date in target_dates:
        row = conn.execute(
            "SELECT COUNT(DISTINCT code), COUNT(DISTINCT close), "
            "(SELECT COUNT(*) FROM minute_bars WHERE date=?) "
            "FROM minute_bars WHERE date=? AND time='09:31'",
            (date, date),
        ).fetchone()
        if row:
            codes_n, closes_n, total = row
            status = "OK" if (codes_n and closes_n / codes_n > 0.3) else "WARN"
            logger.info(f"[{date}] {status}: 09:31 股票 {codes_n}, "
                        f"不同close {closes_n}, 全天行数 {total}")

    conn.close()

    if failed[:10]:
        logger.warning(f"前10个失败样本: {failed[:10]}")

    return inserted, failed


def main():
    parser = argparse.ArgumentParser(description="新浪 1min 回填")
    parser.add_argument("dates", nargs="+", help="目标日期，如 2026-04-13 2026-04-16")
    parser.add_argument("--workers", type=int, default=10, help="并发数")
    parser.add_argument("--limit", type=int, help="测试用，只跑前 N 只")
    args = parser.parse_args()

    backfill(args.dates, workers=args.workers, limit=args.limit)


if __name__ == "__main__":
    main()
