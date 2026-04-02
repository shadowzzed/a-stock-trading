#!/usr/bin/env python3
"""
回填历史盘中分时数据到 intraday.db
使用 mootdx minutes() 接口拉取历史分时，提取关键时间点快照

用法:
  python3 trading/backfill_intraday.py              # 回填所有缺失日期
  python3 trading/backfill_intraday.py 20260327      # 回填指定日期
"""

import sys
import os
import re
import time
import sqlite3
from datetime import datetime, timedelta

os.environ["TQDM_DISABLE"] = "1"
import pandas as pd
from mootdx.quotes import Quotes

STOCKS_MD = os.path.join(os.path.dirname(__file__), "stocks.md")
DB_PATH = os.path.join(os.path.dirname(__file__), "intraday", "intraday.db")

# A 股代码前缀
A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688")

# 关键时间点（分钟索引 → 时间标签）
# 与盘中定时任务保持一致：09:25, 09:40, 10:00, 11:30, 14:40, 15:00
# row 0 = 09:31, 注意 09:25 是集合竞价阶段，分时数据从 09:31 开始
# 因此 09:25 无法从 minutes() 获取，用 09:31 (row 0) 近似
SNAPSHOT_POINTS = {
    0: "09:25:00",    # 竞价（用09:31近似，开盘第一笔）
    9: "09:40:00",    # 早盘10分钟
    29: "10:00:00",   # 早盘半小时
    119: "11:30:00",  # 上午收盘
    219: "14:40:00",  # 尾盘前20分钟
    239: "15:00:00",  # 收盘定格
}


def get_client():
    return Quotes.factory(market="std")


def _normalize_name(name):
    name = name.strip().replace("\u3000", "").replace(" ", "").replace("\x00", "")
    result = []
    for ch in name:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        else:
            result.append(ch)
    return "".join(result)


def build_all_stocks_map():
    """构建 code→name 和 name→code 映射"""
    client = get_client()
    code_to_name = {}
    for market in [0, 1]:
        stocks = client.stocks(market=market)
        for _, row in stocks.iterrows():
            code = row["code"]
            if code.startswith(A_SHARE_PREFIXES):
                code_to_name[code] = row["name"].strip()
    return code_to_name


def parse_stocks_md(code_to_name):
    """解析股票池，返回 {code: (name, star, sector)}"""
    # 反向映射
    name_to_code = {}
    norm_to_code = {}
    for code, name in code_to_name.items():
        name_to_code[name] = code
        norm = _normalize_name(name)
        norm_to_code[norm] = code
        for pref in ["*ST", "ST"]:
            if norm.startswith(pref):
                norm_to_code[norm[len(pref):]] = code

    pool = {}
    current_sector = None
    with open(STOCKS_MD, "r") as f:
        for line in f:
            m = re.match(r"^## (.+?)（", line)
            if m:
                current_sector = m.group(1)
                continue
            m = re.match(r"^\| (.+?) \| (.*?) \|", line)
            if m and current_sector:
                name = m.group(1).strip()
                if name in ("股票", "---", "------"):
                    continue
                star = "⭐" in m.group(2)
                code = name_to_code.get(name) or norm_to_code.get(_normalize_name(name))
                if code:
                    pool[code] = (name, star, current_sector)
    return pool


def get_all_a_codes():
    """获取全部 A 股代码"""
    client = get_client()
    codes = []
    for market in [0, 1]:
        stocks = client.stocks(market=market)
        for _, row in stocks.iterrows():
            if row["code"].startswith(A_SHARE_PREFIXES):
                codes.append(row["code"])
    return codes


def get_last_close_map(client, date_str, all_codes):
    """通过日K线获取指定日期的昨收价"""
    last_close = {}
    for i in range(0, len(all_codes), 80):
        batch = all_codes[i:i+80]
        for code in batch:
            try:
                df = client.bars(symbol=code, frequency=9, offset=5)
                if df is None or df.empty:
                    continue
                df["date_str"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y%m%d")
                row = df[df["date_str"] == date_str]
                if not row.empty:
                    # 用前一天的 close 作为昨收
                    idx = row.index[0]
                    if idx > 0:
                        last_close[code] = df.loc[idx - 1, "close"]
                    else:
                        last_close[code] = row.iloc[0]["open"]
            except Exception:
                pass
    return last_close


def backfill_date(date_str, db, code_to_name, pool, all_codes):
    """回填指定日期的分时数据"""
    date_fmt = "%Y-%m-%d"
    date_db = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    # 检查该日期已有的快照数
    existing = db.execute(
        "SELECT COUNT(DISTINCT ts) FROM snapshots WHERE date=?", (date_db,)
    ).fetchone()[0]
    if existing >= len(SNAPSHOT_POINTS):
        print(f"  {date_db}: 已有 {existing} 个快照，跳过")
        return 0

    client = get_client()

    # 先测试一只股票看是否有数据
    test_df = client.minutes(symbol="600000", date=date_str)
    if test_df is None or test_df.empty:
        print(f"  {date_db}: 无分时数据（非交易日或超出范围）")
        return 0

    print(f"  {date_db}: 开始拉取（已有 {existing} 个快照）...")

    # 批量拉取所有股票的分时数据
    success = 0
    failed = 0
    rows_to_insert = []

    for idx, code in enumerate(all_codes):
        if idx % 500 == 0 and idx > 0:
            print(f"    进度: {idx}/{len(all_codes)} 只, 成功 {success}, 失败 {failed}", flush=True)

        for attempt in range(3):
            try:
                df = client.minutes(symbol=code, date=date_str)
                if df is None or df.empty:
                    break

                if len(df) < 240:
                    break

                name = code_to_name.get(code, "")
                pool_info = pool.get(code)
                sector = pool_info[2] if pool_info else ""
                star = 1 if (pool_info and pool_info[1]) else 0
                in_pool = 1 if pool_info else 0

                # 计算昨收（用 row 0 的 price 和开盘行为推算）
                # 更好的方法：用第一笔的 price 作为开盘价
                open_price = df.iloc[0]["price"]

                for row_idx, ts_label in SNAPSHOT_POINTS.items():
                    if row_idx >= len(df):
                        continue

                    price = df.iloc[row_idx]["price"]
                    # 计算 high/low 到该时间点
                    high = df.iloc[:row_idx + 1]["price"].max()
                    low = df.iloc[:row_idx + 1]["price"].min()
                    # 累计成交量
                    vol = df.iloc[:row_idx + 1]["vol"].sum()

                    rows_to_insert.append((
                        date_db, ts_label, code, name,
                        price, open_price, high, low, vol, sector, star, in_pool
                    ))

                success += 1
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                else:
                    failed += 1

        # 避免请求过快
        if idx % 10 == 0:
            time.sleep(0.1)

    if not rows_to_insert:
        print(f"  {date_db}: 无有效数据")
        return 0

    # 获取昨收价（通过日K线 - 只对股票池内股票获取）
    print(f"    获取昨收价...", flush=True)
    last_close_map = {}
    pool_codes = [c for c in all_codes if c in pool]
    for code in pool_codes:
        try:
            kdf = client.bars(symbol=code, frequency=9, offset=30)
            if kdf is not None and not kdf.empty:
                kdf["d"] = pd.to_datetime(kdf["datetime"]).dt.strftime("%Y-%m-%d")
                match = kdf[kdf["d"] == date_db]
                if not match.empty:
                    idx_pos = kdf.index.get_loc(match.index[0])
                    if idx_pos > 0:
                        last_close_map[code] = kdf.iloc[idx_pos - 1]["close"]
        except Exception:
            pass
        time.sleep(0.05)

    # 删除已有数据（避免重复）
    for ts_label in SNAPSHOT_POINTS.values():
        db.execute("DELETE FROM snapshots WHERE date=? AND ts=?", (date_db, ts_label))

    # 插入数据
    inserted = 0
    for row in rows_to_insert:
        date_db_r, ts_label, code, name, price, open_price, high, low, vol, sector, star, in_pool = row
        lc = last_close_map.get(code, open_price)
        pct = round((price - lc) / lc * 100, 2) if lc and lc > 0 else 0
        amount_yi = 0  # 分时数据没有金额，设0

        # 判断涨跌停
        limit_pct = 20 if code.startswith(("300", "301", "688")) else 10
        limit_up_price = round(lc * (1 + limit_pct / 100), 2) if lc else 0
        limit_down_price = round(lc * (1 - limit_pct / 100), 2) if lc else 0
        is_limit_up = 1 if (limit_up_price > 0 and abs(price - limit_up_price) < 0.02) else 0
        is_limit_down = 1 if (limit_down_price > 0 and abs(price - limit_down_price) < 0.02) else 0

        db.execute("""
            INSERT INTO snapshots (date, ts, code, name, price, pctChg, open, high, low,
                                   last_close, volume, amount, amount_yi, limit_pct,
                                   is_limit_up, is_limit_down, sector, star, in_pool)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)
        """, (date_db_r, ts_label, code, name, price, pct, open_price, high, low,
              lc, vol, limit_pct, is_limit_up, is_limit_down, sector, star, in_pool))
        inserted += 1

    db.commit()
    print(f"  {date_db}: 插入 {inserted} 条（{success} 只股票 × {len(SNAPSHOT_POINTS)} 个时间点）")
    return inserted


def get_trading_dates():
    """生成3月所有可能的交易日"""
    dates = []
    # 0302-0331，跳过周末
    d = datetime(2026, 3, 2)
    end = datetime(2026, 3, 31)
    while d <= end:
        if d.weekday() < 5:  # 周一到周五
            dates.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return dates


def main():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")

    print("🔄 回填历史分时数据", flush=True)
    print("   数据库: %s" % DB_PATH, flush=True)
    print("   时间点: %s" % ", ".join(SNAPSHOT_POINTS.values()), flush=True)
    print(flush=True)

    # 构建映射
    print("加载全市场股票列表...", flush=True)
    code_to_name = build_all_stocks_map()
    all_codes = list(code_to_name.keys())
    print(f"  全A股: {len(all_codes)} 只", flush=True)

    pool = parse_stocks_md(code_to_name)
    print(f"  股票池: {len(pool)} 只", flush=True)
    print(flush=True)

    # 确定要回填的日期
    if len(sys.argv) > 1:
        dates = [sys.argv[1]]
    else:
        dates = get_trading_dates()

    total_inserted = 0
    for date_str in dates:
        total_inserted += backfill_date(date_str, db, code_to_name, pool, all_codes)

    db.close()
    print(f"\n✅ 完成，共插入 {total_inserted} 条记录")


if __name__ == "__main__":
    main()
