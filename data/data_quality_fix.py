#!/usr/bin/env python3
"""
数据质量修复：处理 daily_bars / stock_meta / limit_up 中的常见缺陷

修复项：
  [A] daily_bars.name strip \x00
  [B] daily_bars.pct_chg NULL: 用 close / prev_close 计算
  [C] daily_bars.close = 0: 标记后续 pct_chg 也无法计算（跳过）
  [D] stock_meta.last_close = 0: 用 daily_bars 前一日 close 填充
  [E] limit_up.industry 空: 从同 code 其他有 industry 的记录继承，再尝试 stock_concepts.concepts 推断
  [F] 清理 daily_bars/stock_meta 中 code 不是 6 位的垃圾

用法:
  python3 trading/data_quality_fix.py              # 执行
  python3 trading/data_quality_fix.py --dry-run    # 只打印
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import logging

INTRADAY_DB = os.path.expanduser("~/shared/trading/intraday/intraday.db")
CONCEPT_DB = os.path.expanduser("~/shared/trading/stock_concept.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)


def fix_daily_bars_name(conn: sqlite3.Connection, dry_run: bool) -> int:
    """strip daily_bars.name 的 \x00 及前后空白"""
    before = conn.execute(
        "SELECT COUNT(*) FROM daily_bars WHERE name LIKE '%' || char(0) || '%'"
    ).fetchone()[0]
    logger.info(f"[A] daily_bars.name 含 \\x00: {before} 条")
    if dry_run or before == 0:
        return before

    # 用 trim 替换 \x00
    conn.execute(
        "UPDATE daily_bars SET name = REPLACE(REPLACE(name, char(0), ''), char(13), '')"
    )
    conn.commit()
    return before


def fix_daily_bars_pct_chg(conn: sqlite3.Connection, dry_run: bool) -> int:
    """补齐 daily_bars.pct_chg NULL: 用 close / prev_close 计算"""
    rows = conn.execute(
        "SELECT date, code, close FROM daily_bars "
        "WHERE pct_chg IS NULL AND close > 0 ORDER BY code, date"
    ).fetchall()
    logger.info(f"[B] daily_bars.pct_chg NULL: {len(rows)} 条待计算")
    if dry_run or not rows:
        return len(rows)

    fixed = 0
    batch = []
    for date, code, close in rows:
        prev = conn.execute(
            "SELECT close FROM daily_bars WHERE code=? AND date<? AND close>0 "
            "ORDER BY date DESC LIMIT 1",
            (code, date),
        ).fetchone()
        if not prev or prev[0] <= 0:
            continue
        pct = (close - prev[0]) / prev[0] * 100
        batch.append((pct, date, code))
        fixed += 1
        if len(batch) >= 1000:
            conn.executemany(
                "UPDATE daily_bars SET pct_chg=? WHERE date=? AND code=?", batch
            )
            conn.commit()
            batch.clear()

    if batch:
        conn.executemany(
            "UPDATE daily_bars SET pct_chg=? WHERE date=? AND code=?", batch
        )
        conn.commit()
    logger.info(f"  修复: {fixed} 条")
    return fixed


def fix_stock_meta_last_close(conn: sqlite3.Connection, dry_run: bool) -> int:
    """补齐 stock_meta.last_close = 0 或 NULL: 用 daily_bars 前一日 close"""
    rows = conn.execute(
        "SELECT date, code FROM stock_meta "
        "WHERE last_close IS NULL OR last_close = 0 ORDER BY code, date"
    ).fetchall()
    logger.info(f"[D] stock_meta.last_close 缺失: {len(rows)} 条")
    if dry_run or not rows:
        return len(rows)

    fixed = 0
    batch = []
    for date, code in rows:
        prev = conn.execute(
            "SELECT close FROM daily_bars WHERE code=? AND date<? AND close>0 "
            "ORDER BY date DESC LIMIT 1",
            (code, date),
        ).fetchone()
        if not prev or prev[0] <= 0:
            continue
        batch.append((prev[0], date, code))
        fixed += 1

    if batch:
        conn.executemany(
            "UPDATE stock_meta SET last_close=? WHERE date=? AND code=?", batch
        )
        conn.commit()
    logger.info(f"  修复: {fixed} 条")
    return fixed


def fix_limit_up_industry(conn: sqlite3.Connection, concept_db: str,
                           dry_run: bool) -> int:
    """补齐 limit_up.industry 空: 优先继承同 code 历史记录，再从 stock_concepts 推断"""
    rows = conn.execute(
        "SELECT date, code, name FROM limit_up "
        "WHERE industry IS NULL OR industry = '' "
    ).fetchall()
    logger.info(f"[E] limit_up.industry 空: {len(rows)} 条")
    if dry_run or not rows:
        return len(rows)

    # 构建 code → industry 映射（从有 industry 的 limit_up 记录）
    code_industry_map = {}
    for c, ind in conn.execute(
        "SELECT code, industry FROM limit_up "
        "WHERE industry IS NOT NULL AND industry != '' "
        "GROUP BY code"
    ).fetchall():
        code_industry_map[c] = ind

    # 构建 code → concepts（从 stock_concepts）
    concept_map = {}
    cc = sqlite3.connect(concept_db)
    try:
        for c, concepts in cc.execute("SELECT code, concepts FROM stock_concepts").fetchall():
            if concepts:
                try:
                    concept_map[c] = json.loads(concepts)
                except Exception:
                    pass
    finally:
        cc.close()

    fixed = 0
    batch = []
    fallback_concept = 0
    for date, code, name in rows:
        industry = code_industry_map.get(code, "")
        if not industry:
            # 用第一个 concept 作为兜底
            concepts = concept_map.get(code, [])
            if concepts:
                industry = concepts[0]
                fallback_concept += 1
        if industry:
            batch.append((industry, date, code))
            fixed += 1

    if batch:
        conn.executemany(
            "UPDATE limit_up SET industry=? WHERE date=? AND code=?", batch
        )
        conn.commit()
    logger.info(f"  修复: {fixed} 条（其中 {fallback_concept} 条用 concepts 兜底）")
    return fixed


def fix_stock_meta_name_null_chars(conn: sqlite3.Connection, dry_run: bool) -> int:
    """strip stock_meta.name 的 \x00"""
    before = conn.execute(
        "SELECT COUNT(*) FROM stock_meta WHERE name LIKE '%' || char(0) || '%'"
    ).fetchone()[0]
    logger.info(f"[A.2] stock_meta.name 含 \\x00: {before} 条")
    if dry_run or before == 0:
        return before
    conn.execute(
        "UPDATE stock_meta SET name = REPLACE(REPLACE(name, char(0), ''), char(13), '')"
    )
    conn.commit()
    return before


def fix_limit_up_name_null_chars(conn: sqlite3.Connection, dry_run: bool) -> int:
    """strip limit_up.name 的 \x00"""
    before = conn.execute(
        "SELECT COUNT(*) FROM limit_up WHERE name LIKE '%' || char(0) || '%'"
    ).fetchone()[0]
    logger.info(f"[A.3] limit_up.name 含 \\x00: {before} 条")
    if dry_run or before == 0:
        return before
    conn.execute(
        "UPDATE limit_up SET name = REPLACE(REPLACE(name, char(0), ''), char(13), '')"
    )
    conn.commit()
    return before


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(INTRADAY_DB, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    logger.info("="*60)
    logger.info("数据质量修复" + (" [DRY RUN]" if args.dry_run else ""))
    logger.info("="*60)

    fix_daily_bars_name(conn, args.dry_run)
    fix_stock_meta_name_null_chars(conn, args.dry_run)
    fix_limit_up_name_null_chars(conn, args.dry_run)
    fix_daily_bars_pct_chg(conn, args.dry_run)
    fix_stock_meta_last_close(conn, args.dry_run)
    fix_limit_up_industry(conn, CONCEPT_DB, args.dry_run)

    conn.close()
    logger.info("完成")


if __name__ == "__main__":
    main()
