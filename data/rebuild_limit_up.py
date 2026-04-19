#!/usr/bin/env python3
"""
重建 limit_up 表：基于 daily_bars 的涨跌幅 + minute_bars 的封板时间

规则：
- 主板 10%: pct_chg >= 9.95%
- 创业板/科创板 20%: pct_chg >= 19.95%
- 北交所 30%: pct_chg >= 29.95%
- ST 5%: pct_chg >= 4.95%（按 name 含 "ST" 判断）

衍生字段：
- first_limit_time / last_limit_time: 从 minute_bars 找到首次/末次达到涨停价的时间
- blown_count: 封板后再跌破的次数
- board_count: 往前回溯连续涨停天数
- industry: 从现有 limit_up 表继承（若有），否则从 stock_concept 推断

用法:
  python3 trading/rebuild_limit_up.py                              # 重建全表
  python3 trading/rebuild_limit_up.py --start 2026-04-01           # 重建指定区间
  python3 trading/rebuild_limit_up.py --start 2026-04-07 --dry-run # 只打印对比，不写入
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import logging
import time
from typing import Optional

DB_PATH = os.path.expanduser("~/shared/trading/intraday/intraday.db")
ST_MARKER = ("ST", "*ST", "Ｓ")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)


def limit_pct_for(code: str, name: str, stock_meta_pct: Optional[int]) -> float:
    """按股票代码和名称确定涨停幅度

    规则优先级（与交易所实际规则对齐）：
    - 北交所（43/83/87/88/92）: 30%
    - 创业板（30）/科创板（68）: 20%（含 ST）
    - 主板 ST / *ST: 5%
    - 主板普通: 10%
    """
    is_st = name and any(m in name for m in ST_MARKER)
    # 北交所
    if code.startswith(("43", "83", "87", "88", "92")):
        return 30.0
    # 创业板/科创板（ST 也是 20%）
    if code.startswith(("30", "68")):
        return 20.0
    # 主板 ST
    if is_st:
        return 5.0
    # 主板普通
    return 10.0


def is_new_stock(name: str) -> bool:
    """N 开头 = 新股上市首日（或 C 开头 = 次新股前几日），涨幅不受限，不算涨停"""
    if not name:
        return False
    s = name.strip()
    return s.startswith(("N", "Ｎ", "C", "Ｃ"))


def is_index_or_noise(code: str) -> bool:
    """排除指数（000001/399001 等）"""
    # 上证指数 / 深证综指 / ... 不是个股
    if code in ("000001", "000002", "000003", "000004", "000005", "000006",
                "000007", "000008", "000009", "000010", "000016"):
        # 这些里既有指数也有个股，用 name 判断更准，但这里保守不拦截
        pass
    # 只要 6 位数字的股票代码都接受
    if not (code.isdigit() and len(code) == 6):
        return True
    return False


def _calc_limit_price(last_close: float, limit_pct: float) -> float:
    """计算涨停价，A股四舍五入到分"""
    raw = last_close * (1 + limit_pct / 100)
    return round(raw, 2)


def detect_limit_up_from_daily(conn: sqlite3.Connection, start: Optional[str],
                                end: Optional[str]) -> list[tuple]:
    """从 daily_bars 识别涨停记录

    Returns: [(date, code, name, pct_chg, close, amount, last_close, limit_pct), ...]
    """
    where = ["db.close > 0"]
    params = []
    if start:
        where.append("db.date >= ?")
        params.append(start)
    if end:
        where.append("db.date <= ?")
        params.append(end)

    sql = f"""
        SELECT db.date, db.code, db.name, db.pct_chg, db.close, db.amount,
               sm.limit_pct, sm.last_close, sm.name AS meta_name
        FROM daily_bars db
        LEFT JOIN stock_meta sm ON db.code = sm.code AND db.date = sm.date
        WHERE {' AND '.join(where)}
        ORDER BY db.date, db.code
    """
    rows = conn.execute(sql, params).fetchall()

    results = []
    for date, code, name, pct_chg, close, amount, meta_pct, last_close, meta_name in rows:
        # 排除指数和无效代码
        if not (code.isdigit() and len(code) == 6):
            continue

        # 有效 name
        real_name = (meta_name or name or "").strip()
        if not real_name:
            continue

        # 排除明显的指数名称
        if any(kw in real_name for kw in ("指数", "上证", "Ａ股", "创业板综",
                                           "深证综", "红筹指", "工业指",
                                           "商业指", "公用指", "地产指")):
            continue

        # 排除新股上市首日（N/C 前缀）—— 涨幅不受限，不算涨停
        if is_new_stock(real_name):
            continue

        # 获取前一日收盘：优先 stock_meta.last_close，否则 daily_bars 前日
        if last_close is None or last_close <= 0:
            prev = conn.execute(
                "SELECT close FROM daily_bars WHERE code=? AND date<? "
                "ORDER BY date DESC LIMIT 1",
                (code, date),
            ).fetchone()
            if prev and prev[0] > 0:
                last_close = prev[0]
            else:
                continue

        # pct_chg 缺失时用 close/last_close 兜底计算
        pct = pct_chg
        if pct is None and last_close > 0:
            pct = (close - last_close) / last_close * 100
        if pct is None:
            continue

        # 确定涨停幅度 —— 以 name_based 规则为准（ST/创业板/主板规则清晰）
        limit_pct_value = limit_pct_for(code, real_name, None)
        threshold = limit_pct_value - 0.05

        # 价格校验：close 达到或超过涨停价（留 0.02 冗余）
        limit_price = _calc_limit_price(last_close, limit_pct_value)
        price_reached = close >= limit_price - 0.02

        if pct >= threshold and price_reached:
            results.append((date, code, real_name, pct, close, amount or 0,
                            last_close, limit_pct_value))

    return results


def compute_minute_details(conn: sqlite3.Connection, date: str, code: str,
                            limit_price: float) -> tuple[str, str, int]:
    """从 minute_bars 算 first_limit_time, last_limit_time, blown_count"""
    rows = conn.execute(
        "SELECT time, close FROM minute_bars "
        "WHERE date=? AND code=? AND close > 0 ORDER BY time",
        (date, code),
    ).fetchall()

    if not rows:
        return "", "", 0

    first_time = ""
    last_time = ""
    blown = 0
    was_sealed = False

    for t, close in rows:
        # 0.01 冗余（A股最小价格单位）
        at_limit = close >= limit_price - 0.005
        if at_limit:
            if not first_time:
                first_time = t
            last_time = t
            if not was_sealed:
                was_sealed = True
        else:
            if was_sealed:
                # 封板后跌破 = 炸板
                blown += 1
                was_sealed = False

    # 时间格式 09:31 -> 093100（六位）
    def fmt(t: str) -> str:
        if not t:
            return ""
        if ":" in t:
            return t.replace(":", "") + "00"
        return t

    return fmt(first_time), fmt(last_time), blown


def compute_board_count(conn: sqlite3.Connection, date: str, code: str,
                         lookback: int = 30) -> int:
    """往前回溯连续涨停天数（含当日）

    连续条件：前一个交易日也涨停。pct_chg 可能为 NULL，需用 close/prev_close 兜底计算。
    """
    rows = conn.execute(
        "SELECT date, close, pct_chg FROM daily_bars "
        "WHERE code=? AND date<=? ORDER BY date DESC LIMIT ?",
        (code, date, lookback),
    ).fetchall()

    if not rows:
        return 1

    meta = conn.execute(
        "SELECT name FROM stock_meta WHERE code=? AND date=? LIMIT 1",
        (code, date),
    ).fetchone()
    name = meta[0] if meta else ""
    limit_pct_v = limit_pct_for(code, name, None)
    threshold = limit_pct_v - 0.05

    count = 0
    for i, (d, close, pct) in enumerate(rows):
        # pct_chg 缺失时，用下一条（更早的日K）close 推算
        if pct is None:
            if i + 1 < len(rows):
                prev_close = rows[i + 1][1]
                if prev_close and prev_close > 0:
                    pct = (close - prev_close) / prev_close * 100
        if pct is None:
            break
        if pct >= threshold:
            count += 1
        else:
            break

    return max(count, 1)


def get_industry(conn: sqlite3.Connection, code: str, date: str,
                 concept_conn: Optional[sqlite3.Connection] = None) -> str:
    """获取股票所属行业"""
    # 1. 尝试从现有 limit_up 继承
    row = conn.execute(
        "SELECT industry FROM limit_up WHERE code=? AND industry IS NOT NULL AND industry != '' "
        "ORDER BY date DESC LIMIT 1",
        (code,),
    ).fetchone()
    if row and row[0]:
        return row[0]

    return ""


def rebuild(start: Optional[str] = None, end: Optional[str] = None,
            dry_run: bool = False, clean_garbage: bool = True):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    t0 = time.time()

    # Step 1: 清除垃圾数据（不在 daily_bars 的日期）
    if clean_garbage:
        logger.info("[Step 1] 清除垃圾 limit_up 数据（date 不在 daily_bars 中）")
        before = conn.execute("SELECT COUNT(*) FROM limit_up").fetchone()[0]
        conn.execute(
            "DELETE FROM limit_up WHERE date NOT IN (SELECT DISTINCT date FROM daily_bars)"
        )
        after = conn.execute("SELECT COUNT(*) FROM limit_up").fetchone()[0]
        logger.info(f"  清除 {before - after} 条垃圾记录（{before} -> {after}）")
        if not dry_run:
            conn.commit()

    # Step 2: 从 daily_bars 识别涨停
    logger.info(f"[Step 2] 从 daily_bars 识别涨停（区间 {start} → {end}）")
    detected = detect_limit_up_from_daily(conn, start, end)
    logger.info(f"  识别到涨停记录: {len(detected)} 条")

    # Step 3: 对每条记录计算衍生字段
    logger.info("[Step 3] 计算衍生字段（first_limit_time / last_limit_time / blown / board）")
    records = []
    existing_industries = {}  # 缓存现有 industry

    # 预加载现有 industry
    for r in conn.execute(
        "SELECT code, industry FROM limit_up WHERE industry IS NOT NULL AND industry != ''"
    ).fetchall():
        existing_industries.setdefault(r[0], r[1])

    for i, (date, code, name, pct, close, amount, last_close, limit_pct_v) in enumerate(detected, 1):
        if i % 500 == 0:
            logger.info(f"  进度 {i}/{len(detected)}")

        limit_price = _calc_limit_price(last_close, limit_pct_v)
        first_t, last_t, blown = compute_minute_details(conn, date, code, limit_price)
        board = compute_board_count(conn, date, code)
        industry = existing_industries.get(code, "")

        records.append((
            date, code, name, pct, close, amount,
            first_t, last_t, blown, board, industry,
        ))

    # Step 4: 写入
    if dry_run:
        logger.info("[DRY RUN] 不写入数据库")
        logger.info(f"  总计 {len(records)} 条")
        # 示例前 5 条
        for rec in records[:5]:
            logger.info(f"    {rec}")
        conn.close()
        return

    logger.info(f"[Step 4] 写入 limit_up 表（{len(records)} 条）")
    # 删除目标区间内现有数据
    del_where = []
    del_params = []
    if start:
        del_where.append("date >= ?")
        del_params.append(start)
    if end:
        del_where.append("date <= ?")
        del_params.append(end)
    if del_where:
        del_sql = f"DELETE FROM limit_up WHERE {' AND '.join(del_where)}"
        n_del = conn.execute(del_sql, del_params).rowcount
        logger.info(f"  删除区间内旧记录: {n_del} 条")
    else:
        n_del = conn.execute("DELETE FROM limit_up").rowcount
        logger.info(f"  清空 limit_up 表: {n_del} 条")

    conn.executemany(
        "INSERT INTO limit_up "
        "(date, code, name, pct_chg, price, amount, first_limit_time, "
        " last_limit_time, blown_count, board_count, industry) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        records,
    )
    conn.commit()

    # Step 5: 统计
    elapsed = time.time() - t0
    logger.info(f"[Step 5] 完成，耗时 {elapsed:.1f}s")
    logger.info("  各日 limit_up 统计:")
    for r in conn.execute(
        "SELECT date, COUNT(*), MAX(board_count) "
        "FROM limit_up "
        + (f"WHERE date >= '{start}' " if start else "")
        + (f"AND date <= '{end}' " if end and start else
           f"WHERE date <= '{end}' " if end else "")
        + "GROUP BY date ORDER BY date DESC LIMIT 20"
    ).fetchall():
        logger.info(f"    {r[0]}: {r[1]} 只，最高 {r[2]} 连板")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="重建 limit_up 表")
    parser.add_argument("--start", help="起始日期（含）")
    parser.add_argument("--end", help="结束日期（含）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写入")
    parser.add_argument("--no-clean-garbage", action="store_true",
                        help="不清除垃圾数据")
    args = parser.parse_args()

    rebuild(start=args.start, end=args.end, dry_run=args.dry_run,
            clean_garbage=not args.no_clean_garbage)


if __name__ == "__main__":
    main()
