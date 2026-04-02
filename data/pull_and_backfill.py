"""拉取今日全量行情 + 将新增股票回补到历史行情CSV中"""

import os
import sys
import baostock as bs
import pandas as pd

# baostock-batch package should be installed (e.g. pip install -e ~/src/happyclaw/container/skills/baostock-batch)
from baostock_batch import parse_stock_input

DAILY_DIR = os.path.join(os.path.dirname(__file__), "daily")

# 今日日期
TARGET_DATE = "2026-03-25"

# 新增的9只股票及其板块归属
NEW_STOCKS = {
    "顺网科技": "算力",
    "奥瑞德": "算力",
    "光环新网": "算力",
    "中复神鹰": "算力",
    "铭普光磁": "算力",
    "长飞光纤": "算力",
    "中利集团": "电力电网",
    "新能泰山": "电力电网",
    "浙江新能": "电力电网",
}

# 需要回补的历史交易日（最近7个）
BACKFILL_DATES = [
    "2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20",
    "2026-03-23", "2026-03-24",
]


def resolve_codes(stock_names):
    """解析股票名称到baostock代码"""
    resolved = {}
    for name in stock_names:
        info = parse_stock_input(name)
        if info["code"]:
            resolved[name] = f"{info['market']}.{info['code']}"
            print(f"  ✅ {name} -> {resolved[name]}")
        else:
            print(f"  ❌ {name} -> 未找到")
    return resolved


def fetch_stock_day(code, name, sector, date_str):
    """查询单只股票单日数据"""
    rs = bs.query_history_k_data_plus(
        code,
        "date,code,open,high,low,close,pctChg,turn,amount",
        start_date=date_str, end_date=date_str,
        frequency="d", adjustflag="2",
    )
    while rs.error_code == '0' and rs.next():
        row = rs.get_row_data()
        if row and row[0] == date_str:
            return {
                "日期": row[0],
                "代码": code,
                "名称": name,
                "开盘价": row[2] or "",
                "最高价": row[3] or "",
                "最低价": row[4] or "",
                "收盘价": row[5] or "",
                "涨跌幅": row[6] or "",
                "换手率": row[7] or "",
                "成交额": row[8] or "",
                "板块": sector,
            }
    return None


def pull_full_day(date_str):
    """拉取某日全量行情（基于已有行情CSV的股票池 + 新增股票）"""
    date_compact = date_str.replace("-", "")
    out_dir = os.path.join(DAILY_DIR, date_str)
    out_path = os.path.join(out_dir, f"行情_{date_compact}.csv")

    # 从最近一天的行情CSV获取完整股票池
    ref_date = "2026-03-24"
    ref_path = os.path.join(DAILY_DIR, ref_date, f"行情_{ref_date.replace('-','')}.csv")
    if not os.path.exists(ref_path):
        print(f"  ❌ 参考文件不存在: {ref_path}")
        return

    ref_df = pd.read_csv(ref_path, encoding="utf-8-sig")
    stock_pool = []
    for _, row in ref_df.iterrows():
        stock_pool.append({
            "code": row["代码"],
            "name": row["名称"],
            "sector": row["板块"],
        })

    # 加入新增股票（如果不在现有池中）
    existing_names = {s["name"] for s in stock_pool}
    for name, sector in NEW_STOCKS.items():
        if name not in existing_names:
            info = parse_stock_input(name)
            if info["code"]:
                stock_pool.append({
                    "code": f"{info['market']}.{info['code']}",
                    "name": name,
                    "sector": sector,
                })

    print(f"\n[拉取] {date_str} 全量行情（{len(stock_pool)} 只股票）...")
    rows = []
    for stock in stock_pool:
        result = fetch_stock_day(stock["code"], stock["name"], stock["sector"], date_str)
        if result:
            rows.append(result)

    if rows:
        os.makedirs(out_dir, exist_ok=True)
        df = pd.DataFrame(rows)
        df.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"  ✅ {len(rows)} 条 -> {out_path}")
    else:
        print(f"  ❌ {date_str} 无数据")


def backfill_new_stocks(code_map):
    """将新增股票追加到历史行情CSV"""
    for date_str in BACKFILL_DATES:
        date_compact = date_str.replace("-", "")
        csv_path = os.path.join(DAILY_DIR, date_str, f"行情_{date_compact}.csv")

        if not os.path.exists(csv_path):
            print(f"  [跳过] {csv_path} 不存在")
            continue

        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        existing_names = set(df["名称"].tolist())

        new_rows = []
        for name, code in code_map.items():
            if name in existing_names:
                continue
            sector = NEW_STOCKS[name]
            result = fetch_stock_day(code, name, sector, date_str)
            if result:
                new_rows.append(result)

        if new_rows:
            new_df = pd.DataFrame(new_rows)
            df = pd.concat([df, new_df], ignore_index=True)
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            print(f"  ✅ {date_str}: +{len(new_rows)} 只新股")
        else:
            print(f"  [无新增] {date_str}")


def main():
    bs.login()

    # 1. 解析新增股票代码
    print("解析新增股票代码...")
    code_map = resolve_codes(NEW_STOCKS.keys())

    # 2. 拉取今日全量行情
    pull_full_day(TARGET_DATE)

    # 3. 回补新增股票到历史行情
    print(f"\n回补 {len(code_map)} 只新股到 {len(BACKFILL_DATES)} 个历史交易日...")
    backfill_new_stocks(code_map)

    bs.logout()
    print("\n全部完成！")


if __name__ == "__main__":
    main()
