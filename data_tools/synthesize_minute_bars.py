"""用 daily_bars 的 OHLC 合成受污染的 minute_bars 数据。

mootdx 在某些日期返回的 bars 是同一个股票数据污染到所有 code，导致 minute_bars 同一时刻
所有股票 close 相同。本脚本对指定日期的分钟段进行合成：

- full: 整天合成（覆盖全部真实数据）
- morning: 只合成早盘 09:25-13:30，保留下午真实数据（需要下午 13:37 作为锚点）

用法：
  python3 synthesize_minute_bars.py 2026-04-07 full
  python3 synthesize_minute_bars.py 2026-04-13 morning
"""
import os
import sys
import sqlite3

DB_PATH = os.path.expanduser("~/shared/trading/intraday/intraday.db")


def _times_morning() -> list[str]:
    res = ["09:25"]
    for m in range(30, 60):
        res.append(f"09:{m:02d}")
    for h in (10, 11):
        for m in range(0, 60):
            if h == 11 and m > 30: continue
            res.append(f"{h:02d}:{m:02d}")
    for m in range(0, 37):
        res.append(f"13:{m:02d}")
    return res


def _times_full() -> list[str]:
    res = ["09:25"]
    for m in range(30, 60): res.append(f"09:{m:02d}")
    for h in (10, 11):
        for m in range(0, 60):
            if h == 11 and m > 30: continue
            res.append(f"{h:02d}:{m:02d}")
    for h in (13, 14):
        for m in range(0, 60): res.append(f"{h:02d}:{m:02d}")
    res.append("15:00")
    return res


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _to_float(v):
    if isinstance(v, (int, float)): return float(v)
    if isinstance(v, bytes):
        try:
            return float(int.from_bytes(v, 'little'))
        except Exception:
            return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


def synthesize_full(date: str):
    """整天合成：用 daily OHLC 生成完整 minute_bars。"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT code, open, high, low, close, volume FROM daily_bars WHERE date = ?",
        (date,),
    ).fetchall()
    rows = [(r[0], _to_float(r[1]), _to_float(r[2]), _to_float(r[3]), _to_float(r[4]), _to_float(r[5])) for r in rows]
    print(f"[{date}] daily_bars 有 {len(rows)} 条")
    conn.execute("DELETE FROM minute_bars WHERE date = ?", (date,))
    conn.commit()

    times = _times_full()
    i_0930 = times.index("09:30")
    i_1030 = times.index("10:30")
    i_1330 = times.index("13:30")
    i_1457 = times.index("14:57")

    batch = []
    for code, o, h, l, c, vol in rows:
        if o is None or c is None:
            continue
        per_min_vol = (vol or 0) / 240.0
        for j, t in enumerate(times):
            if j <= i_0930: price = o
            elif j <= i_1030: price = lerp(o, h, (j - i_0930) / max(i_1030 - i_0930, 1))
            elif j <= i_1330: price = lerp(h, l, (j - i_1030) / max(i_1330 - i_1030, 1))
            elif j <= i_1457: price = lerp(l, c, (j - i_1330) / max(i_1457 - i_1330, 1))
            else: price = c
            price = round(price, 2)
            batch.append((date, t, code, price, price, price, price, per_min_vol, per_min_vol * price))

    conn.executemany(
        "INSERT OR REPLACE INTO minute_bars VALUES (?,?,?,?,?,?,?,?,?)",
        batch,
    )
    conn.commit()
    conn.close()
    print(f"[{date}] 已写入 {len(batch)} 条合成分钟线（full）")


def synthesize_morning(date: str):
    """早盘合成：从 09:25 线性过渡到 13:37 真实价（保留下午真实数据）。"""
    conn = sqlite3.connect(DB_PATH)
    daily_rows = conn.execute(
        "SELECT code, open, close FROM daily_bars WHERE date = ?",
        (date,),
    ).fetchall()

    # 获取每只股票 13:37 的真实价
    real_1337 = {
        r[0]: r[1] for r in conn.execute(
            "SELECT code, close FROM minute_bars WHERE date = ? AND time = '13:37'",
            (date,),
        ).fetchall()
    }
    print(f"[{date}] daily 有 {len(daily_rows)} 条，real 13:37 有 {len(real_1337)} 条")

    # 删除早盘坏数据（09:25-13:30）
    morning = _times_morning()
    placeholders = ",".join("?" * len(morning))
    conn.execute(
        f"DELETE FROM minute_bars WHERE date = ? AND time IN ({placeholders})",
        (date, *morning),
    )
    conn.commit()

    batch = []
    for code, o, c in daily_rows:
        if o is None or c is None: continue
        anchor_end = real_1337.get(code, c)  # 锚点：下午 13:37 真实价
        n = len(morning)
        for j, t in enumerate(morning):
            if j == 0:
                price = o
            else:
                price = lerp(o, anchor_end, j / (n - 1))
            price = round(price, 2)
            batch.append((date, t, code, price, price, price, price, 0.0, 0.0))

    conn.executemany(
        "INSERT OR REPLACE INTO minute_bars VALUES (?,?,?,?,?,?,?,?,?)",
        batch,
    )
    conn.commit()
    conn.close()
    print(f"[{date}] 已写入 {len(batch)} 条合成早盘分钟线（保留下午原始）")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: synthesize_minute_bars.py <date> <full|morning>")
        sys.exit(1)
    date, mode = sys.argv[1], sys.argv[2]
    if mode == "full":
        synthesize_full(date)
    elif mode == "morning":
        synthesize_morning(date)
    else:
        print(f"未知模式: {mode}")
        sys.exit(1)
