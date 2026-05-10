"""修复 stock_meta.last_close — 用 daily_bars 前一日 close 填充

背景：3月大量 stock_meta 记录的 last_close 字段是当日开盘价，不是前日收盘价。
这导致 monitor.py 的涨停价计算错误，进而让 封板 信号误触发。
"""
import os
import sqlite3
import sys

DB_PATH = os.path.expanduser("~/shared/trading/intraday/intraday.db")


def _to_float(v):
    if v is None: return 0.0
    if isinstance(v, (int, float)): return float(v)
    if isinstance(v, bytes):
        try: return float(int.from_bytes(v, 'little'))
        except: return 0.0
    try: return float(v)
    except: return 0.0


def fix_range(start: str, end: str):
    conn = sqlite3.connect(DB_PATH)
    # 所有需要修复的 (date, code)
    rows = conn.execute(
        """
        SELECT sm.date, sm.code, sm.last_close,
               (SELECT close FROM daily_bars db WHERE db.code=sm.code AND db.date < sm.date ORDER BY db.date DESC LIMIT 1) AS prev_close
        FROM stock_meta sm
        WHERE sm.date BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchall()

    updates = []
    for date, code, meta_lc, prev_close in rows:
        prev_close_f = _to_float(prev_close)
        if prev_close_f > 0 and abs((meta_lc or 0) - prev_close_f) > 0.01:
            updates.append((prev_close_f, date, code))

    print(f"[{start}~{end}] 需修复 {len(updates)} 条 stock_meta.last_close")
    if updates:
        conn.executemany(
            "UPDATE stock_meta SET last_close=? WHERE date=? AND code=?",
            updates,
        )
        conn.commit()
    conn.close()
    print(f"修复完成")


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-03-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-04-30"
    fix_range(start, end)
