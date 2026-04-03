"""
数据库操作 — news_embeddings + news_impacts 表

所有表建在 news_monitor.db 中，与 news 表共享同一数据库。
"""

import json
import os
import sqlite3
import struct
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config import get_config

_cfg = get_config()
NEWS_DB_PATH = _cfg["news_db"]
INTRADAY_DB_PATH = _cfg["intraday_db"]


def _get_news_db():
    """获取新闻数据库连接"""
    conn = sqlite3.connect(NEWS_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_tables():
    """初始化 impact 相关表"""
    conn = _get_news_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS news_embeddings (
                news_id INTEGER PRIMARY KEY,
                embedding BLOB NOT NULL,
                model_version TEXT DEFAULT 'bge-m3',
                created_at TEXT NOT NULL,
                FOREIGN KEY (news_id) REFERENCES news(id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_emb_model ON news_embeddings(model_version)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS news_impacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT DEFAULT '',
                pre_price REAL,
                pct_5min REAL,
                pct_15min REAL,
                pct_30min REAL,
                pct_1h REAL,
                pct_2h REAL,
                pct_eod REAL,
                pct_next1d REAL,
                pct_next2d REAL,
                pct_next3d REAL,
                pct_next5d REAL,
                max_gain_pct REAL,
                max_loss_pct REAL,
                time_to_peak TEXT,
                time_to_trough TEXT,
                recovery_minutes INTEGER,
                vol_ratio_1h REAL,
                news_time TEXT DEFAULT '',
                news_date TEXT DEFAULT '',
                computed_at TEXT NOT NULL,
                UNIQUE(news_id, stock_code),
                FOREIGN KEY (news_id) REFERENCES news(id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_impact_news ON news_impacts(news_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_impact_stock ON news_impacts(stock_code, news_date)
        """)
        conn.commit()
    finally:
        conn.close()


def embedding_to_blob(vec):
    """float32 numpy array → SQLite BLOB"""
    import numpy as np
    return vec.astype(np.float32).tobytes()


def blob_to_embedding(blob):
    """SQLite BLOB → float32 numpy array"""
    import numpy as np
    return np.frombuffer(blob, dtype=np.float32)


def save_embedding(news_id, embedding_vec, model_version="bge-m3"):
    """保存单条新闻的 embedding"""
    conn = _get_news_db()
    try:
        blob = embedding_to_blob(embedding_vec)
        conn.execute("""
            INSERT OR REPLACE INTO news_embeddings (news_id, embedding, model_version, created_at)
            VALUES (?, ?, ?, ?)
        """, (news_id, blob, model_version, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()


def save_embeddings_batch(records):
    """批量保存 embeddings: [(news_id, vec, model_version), ...]"""
    conn = _get_news_db()
    try:
        data = [
            (rid, embedding_to_blob(vec), mv, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            for rid, vec, mv in records
        ]
        conn.executemany("""
            INSERT OR REPLACE INTO news_embeddings (news_id, embedding, model_version, created_at)
            VALUES (?, ?, ?, ?)
        """, data)
        conn.commit()
    finally:
        conn.close()


def get_unembedded_news_ids():
    """获取尚未生成 embedding 的新闻列表"""
    conn = _get_news_db()
    try:
        rows = conn.execute("""
            SELECT n.id, n.title, n.brief, n.interpretation
            FROM news n
            LEFT JOIN news_embeddings e ON n.id = e.news_id
            WHERE e.news_id IS NULL
            ORDER BY n.id DESC
        """).fetchall()
        return rows
    finally:
        conn.close()


def search_similar(query_embedding, top_k=10, threshold=0.5):
    """向量相似度检索，返回 Top-K 相似新闻

    返回: list of dicts
    """
    import numpy as np
    conn = _get_news_db()
    try:
        rows = conn.execute("""
            SELECT e.news_id, e.embedding, n.title, n.stocks, n.plates, n.interpretation,
                   n.news_time, n.created_date
            FROM news_embeddings e
            JOIN news n ON n.id = e.news_id
        """).fetchall()

        if not rows:
            return []

        results = []
        query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-8)
        for news_id, blob, title, stocks_json, plates_json, interp, news_time, news_date in rows:
            vec = blob_to_embedding(blob)
            vec_norm = vec / (np.linalg.norm(vec) + 1e-8)
            sim = float(np.dot(query_norm, vec_norm))
            if sim >= threshold:
                results.append({
                    "news_id": news_id,
                    "similarity": round(sim, 4),
                    "title": title,
                    "stocks": json.loads(stocks_json) if stocks_json else [],
                    "plates": json.loads(plates_json) if plates_json else [],
                    "interpretation": interp or "",
                    "news_time": news_time or "",
                    "news_date": news_date or "",
                })

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]
    finally:
        conn.close()


def save_impact(record):
    """保存单条影响记录"""
    conn = _get_news_db()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO news_impacts
            (news_id, stock_code, stock_name, pre_price,
             pct_5min, pct_15min, pct_30min, pct_1h, pct_2h, pct_eod,
             pct_next1d, pct_next2d, pct_next3d, pct_next5d,
             max_gain_pct, max_loss_pct, time_to_peak, time_to_trough,
             recovery_minutes, vol_ratio_1h, news_time, news_date, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record["news_id"], record["stock_code"], record.get("stock_name", ""),
            record.get("pre_price"),
            record.get("pct_5min"), record.get("pct_15min"), record.get("pct_30min"),
            record.get("pct_1h"), record.get("pct_2h"), record.get("pct_eod"),
            record.get("pct_next1d"), record.get("pct_next2d"),
            record.get("pct_next3d"), record.get("pct_next5d"),
            record.get("max_gain_pct"), record.get("max_loss_pct"),
            record.get("time_to_peak"), record.get("time_to_trough"),
            record.get("recovery_minutes"), record.get("vol_ratio_1h"),
            record.get("news_time", ""), record.get("news_date", ""),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))
        conn.commit()
    finally:
        conn.close()


def save_impacts_batch(records):
    """批量保存影响记录"""
    conn = _get_news_db()
    try:
        data = [
            (
                r["news_id"], r["stock_code"], r.get("stock_name", ""),
                r.get("pre_price"),
                r.get("pct_5min"), r.get("pct_15min"), r.get("pct_30min"),
                r.get("pct_1h"), r.get("pct_2h"), r.get("pct_eod"),
                r.get("pct_next1d"), r.get("pct_next2d"),
                r.get("pct_next3d"), r.get("pct_next5d"),
                r.get("max_gain_pct"), r.get("max_loss_pct"),
                r.get("time_to_peak"), r.get("time_to_trough"),
                r.get("recovery_minutes"), r.get("vol_ratio_1h"),
                r.get("news_time", ""), r.get("news_date", ""),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            for r in records
        ]
        conn.executemany("""
            INSERT OR REPLACE INTO news_impacts
            (news_id, stock_code, stock_name, pre_price,
             pct_5min, pct_15min, pct_30min, pct_1h, pct_2h, pct_eod,
             pct_next1d, pct_next2d, pct_next3d, pct_next5d,
             max_gain_pct, max_loss_pct, time_to_peak, time_to_trough,
             recovery_minutes, vol_ratio_1h, news_time, news_date, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, data)
        conn.commit()
    finally:
        conn.close()


def get_impacts_for_news_ids(news_ids):
    """获取一批新闻的影响记录"""
    if not news_ids:
        return []
    conn = _get_news_db()
    try:
        placeholders = ",".join("?" * len(news_ids))
        rows = conn.execute("""
            SELECT * FROM news_impacts WHERE news_id IN (%s)
        """ % placeholders, news_ids).fetchall()
        columns = [desc[0] for desc in conn.execute("SELECT * FROM news_impacts LIMIT 0").description]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        conn.close()


def get_news_with_stocks(limit=1000):
    """获取有关联个股的新闻列表（用于计算影响）"""
    conn = _get_news_db()
    try:
        rows = conn.execute("""
            SELECT id, title, stocks, news_time, sent_at, created_date
            FROM news
            WHERE stocks IS NOT NULL AND stocks != '' AND stocks != '[]'
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()
        results = []
        for row in rows:
            stocks = json.loads(row[2]) if row[2] else []
            results.append({
                "id": row[0], "title": row[1], "stocks": stocks,
                "news_time": row[3], "sent_at": row[4], "created_date": row[5],
            })
        return results
    finally:
        conn.close()


def get_snapshot_for_stock(code, date_str, ts=None):
    """从 intraday.db 获取某只股票的快照数据"""
    import os
    if not os.path.exists(INTRADAY_DB_PATH):
        return None

    conn = sqlite3.connect(INTRADAY_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        if ts:
            row = conn.execute("""
                SELECT * FROM snapshots
                WHERE code = ? AND date = ? AND ts <= ?
                ORDER BY ts DESC LIMIT 1
            """, (code, date_str, ts)).fetchone()
        else:
            row = conn.execute("""
                SELECT * FROM snapshots
                WHERE code = ? AND date = ?
                ORDER BY ts ASC LIMIT 1
            """, (code, date_str)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_snapshots_range(code, date_str, ts_start, ts_end):
    """获取某只股票在指定时间范围内的所有快照"""
    import os
    if not os.path.exists(INTRADAY_DB_PATH):
        return []

    conn = sqlite3.connect(INTRADAY_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT * FROM snapshots
            WHERE code = ? AND date = ? AND ts >= ? AND ts <= ?
            ORDER BY ts ASC
        """, (code, date_str, ts_start, ts_end)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_available_snapshot_dates():
    """获取 intraday.db 中有数据的日期列表"""
    import os
    if not os.path.exists(INTRADAY_DB_PATH):
        return []

    conn = sqlite3.connect(INTRADAY_DB_PATH, timeout=5)
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM snapshots ORDER BY date DESC"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def get_next_trading_day_snapshot(code, date_str):
    """获取某只股票在下一个交易日的收盘快照"""
    import os
    if not os.path.exists(INTRADAY_DB_PATH):
        return None

    conn = sqlite3.connect(INTRADAY_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT * FROM snapshots
            WHERE code = ? AND date > ?
            ORDER BY date ASC, ts DESC LIMIT 1
        """, (code, date_str)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_snapshot_at_or_after(code, date_str, ts_start):
    """获取某只股票在指定时间点或之后的最早快照"""
    import os
    if not os.path.exists(INTRADAY_DB_PATH):
        return None

    conn = sqlite3.connect(INTRADAY_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT * FROM snapshots
            WHERE code = ? AND date = ? AND ts >= ?
            ORDER BY ts ASC LIMIT 1
        """, (code, date_str, ts_start)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
