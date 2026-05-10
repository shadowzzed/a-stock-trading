"""每日数据质量校验：检测 minute_bars 污染并自动用 daily_bars 合成修复。

触发条件：某分钟时间点的 close 唯一值比例 < 30% 或唯一值 < 100

用法：
  python3 trading/data_quality_check.py                    # 检查今天
  python3 trading/data_quality_check.py 2026-04-16         # 检查指定日期
  python3 trading/data_quality_check.py --scan 2026-04-01 2026-04-30  # 扫描区间
"""
import os
import sys
import sqlite3
import logging
from datetime import datetime

DB_PATH = os.path.expanduser("~/shared/trading/intraday/intraday.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

CORRUPTION_UNIQ_RATIO_THRESHOLD = 0.3   # 唯一价比例 < 30% 视为污染
CORRUPTION_UNIQ_MIN_COUNT = 100         # 唯一价 < 100 视为污染
CHECK_TIMES = ["09:31", "10:00", "11:00", "13:00", "14:00", "14:57"]


def detect_corruption(date: str) -> list[str]:
    """检测指定日期被污染的分钟时间点。返回坏分钟列表（若整天大面积污染则返回特殊标记 ['__ALL__']）"""
    conn = sqlite3.connect(DB_PATH)
    try:
        bad_times = []
        for t in CHECK_TIMES:
            row = conn.execute(
                "SELECT COUNT(DISTINCT close), COUNT(*) FROM minute_bars WHERE date=? AND time=?",
                (date, t),
            ).fetchone()
            uniq, total = row[0] or 0, row[1] or 0
            if total == 0:
                continue
            ratio = uniq / total if total > 0 else 0
            if ratio < CORRUPTION_UNIQ_RATIO_THRESHOLD or uniq < CORRUPTION_UNIQ_MIN_COUNT:
                bad_times.append(t)
                logger.warning(
                    "[%s %s] 污染: 唯一价 %d/%d (%.1f%%)",
                    date, t, uniq, total, ratio * 100,
                )

        # 全天广泛污染（>= 4 个检查点坏） → 标记全天
        if len(bad_times) >= 4:
            return ["__ALL__"]
        return bad_times
    finally:
        conn.close()


def synthesize_day_full(date: str):
    """整天合成 minute_bars。"""
    sys.path.insert(0, os.path.dirname(__file__))
    from synthesize_minute_bars import synthesize_full  # 复用已有逻辑
    synthesize_full(date)


def find_afternoon_anchor_time(date: str):
    """找到下午第一个未污染的时间点（作为早盘合成的右锚）"""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT time, COUNT(DISTINCT close), COUNT(*) FROM minute_bars "
            "WHERE date=? AND time >= '13:00' AND time <= '15:00' "
            "GROUP BY time ORDER BY time",
            (date,),
        ).fetchall()
        for t, uniq, total in rows:
            if total > 0 and (uniq / total) >= CORRUPTION_UNIQ_RATIO_THRESHOLD and uniq >= CORRUPTION_UNIQ_MIN_COUNT:
                return t
        return None
    finally:
        conn.close()


def synthesize_day_morning(date: str, anchor_time: str):
    """早盘合成：从 09:25 线性插值到 anchor_time 的真实价。"""
    conn = sqlite3.connect(DB_PATH)
    daily_rows = conn.execute(
        "SELECT code, open, close FROM daily_bars WHERE date=?", (date,),
    ).fetchall()

    anchor_rows = {
        r[0]: r[1] for r in conn.execute(
            "SELECT code, close FROM minute_bars WHERE date=? AND time=?",
            (date, anchor_time),
        ).fetchall()
    }

    # 生成 09:25 到 anchor_time 前一分钟的时间序列
    def times_to_morning_end(end: str) -> list[str]:
        all_t = ["09:25"]
        for m in range(30, 60): all_t.append(f"09:{m:02d}")
        for h in (10, 11):
            for m in range(0, 60):
                if h == 11 and m > 30: continue
                all_t.append(f"{h:02d}:{m:02d}")
        for m in range(0, 60):
            t = f"13:{m:02d}"
            if t >= end: break
            all_t.append(t)
        return all_t

    morning = times_to_morning_end(anchor_time)
    placeholders = ",".join("?" * len(morning))
    conn.execute(
        f"DELETE FROM minute_bars WHERE date=? AND time IN ({placeholders})",
        (date, *morning),
    )
    conn.commit()

    batch = []
    for code, o, c in daily_rows:
        if o is None or c is None:
            continue
        end_price = anchor_rows.get(code, c)
        n = len(morning)
        for j, t in enumerate(morning):
            if j == 0:
                price = o
            else:
                price = o + (end_price - o) * (j / (n - 1))
            price = round(price, 2)
            batch.append((date, t, code, price, price, price, price, 0.0, 0.0))

    conn.executemany(
        "INSERT OR REPLACE INTO minute_bars VALUES (?,?,?,?,?,?,?,?,?)",
        batch,
    )
    conn.commit()
    conn.close()
    logger.info("[%s] 早盘合成: 写入 %d 条，锚点 %s", date, len(batch), anchor_time)


def check_and_fix(date: str, auto_fix: bool = True) -> dict:
    """检查单日数据质量，必要时自动修复。"""
    bad_times = detect_corruption(date)
    result = {"date": date, "bad_times": bad_times, "fixed": False}

    if not bad_times:
        logger.info("[%s] 数据健康", date)
        return result

    if not auto_fix:
        return result

    if bad_times == ["__ALL__"]:
        logger.info("[%s] 全天污染 → 整天合成", date)
        synthesize_day_full(date)
        result["fixed"] = True
        result["method"] = "full"
        return result

    # 部分污染：看下午是否有可用锚点
    anchor = find_afternoon_anchor_time(date)
    if anchor and all(t < anchor for t in bad_times):
        logger.info("[%s] 早盘污染，锚点 %s → 早盘合成", date, anchor)
        synthesize_day_morning(date, anchor)
        result["fixed"] = True
        result["method"] = "morning"
    else:
        # 下午也有污染 → 全天合成
        logger.info("[%s] 下午无锚点 → 整天合成", date)
        synthesize_day_full(date)
        result["fixed"] = True
        result["method"] = "full"
    return result


def scan_range(start: str, end: str) -> list[dict]:
    """扫描日期区间。"""
    conn = sqlite3.connect(DB_PATH)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM minute_bars WHERE date BETWEEN ? AND ? ORDER BY date",
        (start, end),
    ).fetchall()]
    conn.close()

    results = []
    for d in dates:
        results.append(check_and_fix(d, auto_fix=True))
    return results


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--scan":
        start = sys.argv[2] if len(sys.argv) > 2 else "2026-04-01"
        end = sys.argv[3] if len(sys.argv) > 3 else datetime.now().strftime("%Y-%m-%d")
        results = scan_range(start, end)
        fixed = [r for r in results if r.get("fixed")]
        print(f"\n扫描 {start} ~ {end}: {len(results)} 天，修复 {len(fixed)} 天")
        for r in fixed:
            print(f"  {r['date']} ({r.get('method', '?')})")
    else:
        date = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
        check_and_fix(date, auto_fix=True)
