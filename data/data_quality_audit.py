#!/usr/bin/env python3
"""
数据质量每日体检：检查 intraday.db 各表的完整性并输出警告

区别于 data_quality_check.py（专门检测 minute_bars 污染并合成修复），
本脚本做全表综合体检，输出状态报告。

用法:
  python3 trading/data_quality_audit.py              # 检查最近 5 天
  python3 trading/data_quality_audit.py --days 10    # 检查最近 N 天
  python3 trading/data_quality_audit.py --date 2026-04-17

退出码:
  0 - 所有检查通过
  1 - 有警告
  2 - 严重问题
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

DB_PATH = os.path.expanduser("~/shared/trading/intraday/intraday.db")


def check_date(conn: sqlite3.Connection, date: str) -> list[tuple[str, str, str]]:
    issues = []

    n_daily = conn.execute(
        "SELECT COUNT(*) FROM daily_bars WHERE date=?", (date,)
    ).fetchone()[0]
    if n_daily == 0:
        return [("ERR", "daily_bars 无数据", "该日期完全缺失")]
    if n_daily < 4500:
        issues.append(("WARN", "daily_bars 股票数偏少", f"{n_daily} < 4500"))

    n_null_pct = conn.execute(
        "SELECT COUNT(*) FROM daily_bars WHERE date=? AND pct_chg IS NULL", (date,)
    ).fetchone()[0]
    if n_null_pct > 100:
        issues.append(("WARN", "daily_bars.pct_chg NULL 过多", f"{n_null_pct} 条"))

    n_meta = conn.execute(
        "SELECT COUNT(*) FROM stock_meta WHERE date=?", (date,)
    ).fetchone()[0]
    if n_meta < n_daily - 50:
        issues.append(("WARN", "stock_meta 覆盖不全",
                       f"{n_meta} vs daily_bars {n_daily}"))

    # minute_bars 污染检查
    row = conn.execute(
        "SELECT COUNT(DISTINCT code), COUNT(DISTINCT close) FROM minute_bars "
        "WHERE date=? AND time='09:31' AND close > 0",
        (date,),
    ).fetchone()
    if row and row[0] > 1000:
        codes_n, closes_n = row
        diversity = closes_n / codes_n
        if diversity < 0.3:
            issues.append(("ERR", "minute_bars 09:31 疑似批量损坏",
                           f"{codes_n} 股票 仅 {closes_n} 不同 close"))

    # limit_up 对账
    rows = conn.execute("""
        SELECT db.code, db.pct_chg, db.name
        FROM daily_bars db
        WHERE db.date=? AND db.close > 0 AND db.pct_chg IS NOT NULL
    """, (date,)).fetchall()

    expected_zt = 0
    for code, pct, name in rows:
        if not (code.isdigit() and len(code) == 6):
            continue
        real_name = (name or "").strip()
        if not real_name or any(kw in real_name for kw in ("指数","上证","Ａ股")):
            continue
        if real_name.startswith(("N", "Ｎ", "C", "Ｃ")):
            continue
        if code.startswith(("43","83","87","88","92")):
            thr = 29.95
        elif code.startswith(("30","68")):
            thr = 19.95
        elif any(m in real_name for m in ("ST","*ST","Ｓ")):
            thr = 4.95
        else:
            thr = 9.95
        if pct >= thr:
            expected_zt += 1

    actual_lu = conn.execute("SELECT COUNT(*) FROM limit_up WHERE date=?", (date,)).fetchone()[0]
    diff = expected_zt - actual_lu
    if abs(diff) > 5:
        level = "ERR" if abs(diff) > 20 else "WARN"
        issues.append((level, "limit_up 与 daily_bars 不一致",
                       f"实际 {actual_lu}, 按日K算 {expected_zt}, 差 {diff:+d}"))

    return issues


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--date")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)

    if args.date:
        dates = [args.date]
    else:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_bars ORDER BY date DESC LIMIT ?",
            (args.days,),
        ).fetchall()]
        dates.reverse()

    max_level = 0
    for date in dates:
        issues = check_date(conn, date)
        if not issues:
            print(f"OK {date}: 数据健康")
        else:
            print(f"!! {date}: 发现 {len(issues)} 个问题")
            for level, item, detail in issues:
                print(f"   [{level}] {item}: {detail}")
                if level == "ERR":
                    max_level = max(max_level, 2)
                else:
                    max_level = max(max_level, 1)

    conn.close()
    sys.exit(max_level)


if __name__ == "__main__":
    main()
