#!/usr/bin/env python3
"""
盘中数据拉取模块 — 全量A股 + SQLite 存储
供 Agent Team 调用，支持多时间点快照对比

用法:
  python3 trading/intraday_data.py pull              # 拉取全量行情并存入 SQLite
  python3 trading/intraday_data.py snapshot           # 拉取 + 输出综合快照 JSON
  python3 trading/intraday_data.py query <ts>         # 查询指定时间点数据
  python3 trading/intraday_data.py compare <ts1> <ts2> # 对比两个时间点
  python3 trading/intraday_data.py bid <代码>          # 单只五档盘口
  python3 trading/intraday_data.py minute <代码>       # 单只分时数据

数据库: trading/intraday/YYYY-MM-DD.db
"""

import sys
import os
import json
import re

# 抑制 mootdx 内部的 tqdm 进度条（避免定时任务输出乱码）
os.environ["TQDM_DISABLE"] = "1"
import sqlite3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
from mootdx.quotes import Quotes

STOCKS_MD = os.path.join(os.path.dirname(__file__), "stocks.md")
INTRADAY_DIR = os.path.join(os.path.dirname(__file__), "intraday")

# A 股代码前缀
A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688")


def get_client():
    return Quotes.factory(market="std")


# ═══════════════════════════════════════════════════════════════
# 股票池解析（186只标注股）
# ═══════════════════════════════════════════════════════════════

def parse_stocks_md():
    """解析股票池，返回 {code: (name, star, sector)}"""
    pool = {}
    current_sector = None
    name_map = _build_all_stocks_map()
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
                code = name_map.get(name)
                if code:
                    pool[code] = (name, star, current_sector)
    return pool


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


def _build_all_stocks_map():
    """构建 名称→code 映射（全 A 股）"""
    client = get_client()
    mapping = {}
    norm_mapping = {}
    for market in [0, 1]:
        stocks = client.stocks(market=market)
        for _, row in stocks.iterrows():
            name = row["name"].strip()
            code = row["code"]
            if not code.startswith(A_SHARE_PREFIXES):
                continue
            mapping[name] = code
            norm = _normalize_name(name)
            norm_mapping[norm] = code
            for pref in ["*ST", "ST"]:
                if norm.startswith(pref):
                    norm_mapping[norm[len(pref):]] = code
    # 合并归一化映射
    for k, v in norm_mapping.items():
        if k not in mapping:
            mapping[k] = v
    return mapping


# ═══════════════════════════════════════════════════════════════
# 全量拉取（多线程）
# ═══════════════════════════════════════════════════════════════

def get_all_a_codes():
    """获取全部 A 股代码列表"""
    client = get_client()
    all_codes = []
    code_to_name = {}
    for market in [0, 1]:
        stocks = client.stocks(market=market)
        for _, row in stocks.iterrows():
            code = row["code"]
            if code.startswith(A_SHARE_PREFIXES):
                all_codes.append(code)
                code_to_name[code] = row["name"].strip()
    return all_codes, code_to_name


def _fetch_batch(batch, retries=3):
    """拉取一批股票行情（供线程池调用），自动重试"""
    for attempt in range(retries):
        try:
            client = get_client()
            df = client.quotes(symbol=batch)
            if df is not None and not df.empty:
                return df
            return None
        except Exception as e:
            if attempt < retries - 1:
                import time
                time.sleep(1 * (attempt + 1))
                continue
            raise


def fetch_full_market(max_workers=8):
    """多线程全量拉取 A 股行情，返回 DataFrame"""
    all_codes, code_to_name = get_all_a_codes()

    # 分批
    batches = []
    for i in range(0, len(all_codes), 80):
        batches.append(all_codes[i:i + 80])

    # 多线程并发拉取
    all_dfs = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_batch, batch): i for i, batch in enumerate(batches)}
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                all_dfs.append(result)

    if not all_dfs:
        return pd.DataFrame(), code_to_name

    df = pd.concat(all_dfs, ignore_index=True)

    # 计算涨跌幅
    valid = df["last_close"] > 0
    df.loc[valid, "pctChg"] = ((df.loc[valid, "price"] - df.loc[valid, "last_close"]) / df.loc[valid, "last_close"] * 100).round(2)
    df.loc[~valid, "pctChg"] = 0.0
    df["amount_yi"] = (df["amount"] / 1e8).round(2)

    # 附加名称
    df["name"] = df["code"].map(code_to_name).fillna("")

    return df, code_to_name


# ═══════════════════════════════════════════════════════════════
# SQLite 存储
# ═══════════════════════════════════════════════════════════════

DB_PATH = os.path.join(INTRADAY_DIR, "intraday.db")


def calc_limit_price(last_close, pct):
    """计算涨停/跌停价（四舍五入到分）"""
    return round(last_close * (1 + pct / 100), 2)


def check_limit(code, price, last_close):
    """判断是否涨停/跌停，返回 (limit_pct, is_limit_up, is_limit_down)
    limit_pct: 10 或 20（涨跌停幅度）
    """
    if last_close <= 0 or price <= 0:
        return 10, 0, 0

    # 20cm: 创业板(300/301) + 科创板(688)
    is_20cm = code.startswith(("300", "301", "688"))
    limit_pct = 20 if is_20cm else 10

    limit_up_price = calc_limit_price(last_close, limit_pct)
    limit_down_price = calc_limit_price(last_close, -limit_pct)

    is_up = 1 if price >= limit_up_price else 0
    is_down = 1 if price <= limit_down_price else 0

    return limit_pct, is_up, is_down


def get_db_path():
    os.makedirs(INTRADAY_DIR, exist_ok=True)
    return DB_PATH


def init_db(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            date        TEXT NOT NULL,
            ts          TEXT NOT NULL,
            code        TEXT NOT NULL,
            name        TEXT,
            price       REAL,
            pctChg      REAL,
            open        REAL,
            high        REAL,
            low         REAL,
            last_close  REAL,
            volume      INTEGER,
            amount      REAL,
            amount_yi   REAL,
            limit_pct   INTEGER DEFAULT 10,
            is_limit_up INTEGER DEFAULT 0,
            is_limit_down INTEGER DEFAULT 0,
            sector      TEXT DEFAULT '',
            star        INTEGER DEFAULT 0,
            in_pool     INTEGER DEFAULT 0,
            PRIMARY KEY (date, ts, code)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_date_ts ON snapshots(date, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_code_date ON snapshots(code, date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_pool ON snapshots(in_pool, date, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_pctChg ON snapshots(date, ts, pctChg)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_limit ON snapshots(date, is_limit_up)")
    conn.commit()


def save_to_db(df, pool, date_str=None, ts=None):
    """将 DataFrame 存入 SQLite"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    if ts is None:
        ts = datetime.now().strftime("%H:%M:%S")

    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=10)
    init_db(conn)

    rows = []
    for _, r in df.iterrows():
        code = r["code"]
        pool_info = pool.get(code)
        in_pool = 1 if pool_info else 0
        sector = pool_info[2] if pool_info else ""
        star = 1 if pool_info and pool_info[1] else 0

        price = float(r["price"])
        last_close = float(r["last_close"])
        limit_pct, is_up, is_down = check_limit(code, price, last_close)

        rows.append((
            date_str, ts, code, r.get("name", ""),
            price, float(r.get("pctChg", 0)),
            float(r["open"]), float(r["high"]), float(r["low"]), last_close,
            int(r["vol"]), float(r["amount"]), float(r.get("amount_yi", 0)),
            limit_pct, is_up, is_down,
            sector, star, in_pool,
        ))

    conn.executemany("""
        INSERT OR REPLACE INTO snapshots
        (date, ts, code, name, price, pctChg, open, high, low, last_close, volume, amount, amount_yi,
         limit_pct, is_limit_up, is_limit_down, sector, star, in_pool)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    conn.close()

    return db_path, ts, len(rows)


# ═══════════════════════════════════════════════════════════════
# 命令：pull — 拉取并存储
# ═══════════════════════════════════════════════════════════════

def cmd_pull():
    pool = parse_stocks_md()
    print("拉取全量 A 股行情...", file=sys.stderr, flush=True)

    import time
    t0 = time.time()
    df, _ = fetch_full_market()
    t1 = time.time()

    if df.empty:
        print(json.dumps({"error": "未获取到行情数据"}, ensure_ascii=False))
        return

    db_path, ts, count = save_to_db(df, pool)
    elapsed = t1 - t0

    pool_count = sum(1 for _, r in df.iterrows() if r["code"] in pool)
    output = {
        "status": "ok",
        "time": ts,
        "total": count,
        "in_pool": pool_count,
        "elapsed_sec": round(elapsed, 1),
        "db": db_path,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ═══════════════════════════════════════════════════════════════
# 命令：snapshot — 拉取 + 综合分析
# ═══════════════════════════════════════════════════════════════

def cmd_snapshot():
    pool = parse_stocks_md()
    df, _ = fetch_full_market()
    if df.empty:
        print(json.dumps({"error": "未获取到行情数据"}, ensure_ascii=False))
        return

    # 存库
    _, ts, total = save_to_db(df, pool)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 过滤掉停牌（价格为0）
    active = df[df["price"] > 0].copy()

    # === 全市场统计 ===
    total_active = len(active)
    up = int((active["pctChg"] > 0).sum())
    down = int((active["pctChg"] < 0).sum())

    # 涨停/跌停精确判断
    def _is_limit_up(row):
        if row["last_close"] <= 0:
            return False
        is_20cm = row["code"].startswith(("300", "301", "688"))
        lp = calc_limit_price(row["last_close"], 20 if is_20cm else 10)
        return row["price"] >= lp

    def _is_limit_down(row):
        if row["last_close"] <= 0:
            return False
        is_20cm = row["code"].startswith(("300", "301", "688"))
        lp = calc_limit_price(row["last_close"], -(20 if is_20cm else 10))
        return row["price"] <= lp

    limit_up = int(active.apply(_is_limit_up, axis=1).sum())
    limit_down = int(active.apply(_is_limit_down, axis=1).sum())

    # === 全市场涨幅 TOP20 ===
    top20 = active.nlargest(20, "pctChg")
    top20_list = [_stock_record(r, pool) for _, r in top20.iterrows()]

    # === 全市场跌幅 TOP10 ===
    bottom10 = active.nsmallest(10, "pctChg")
    bottom10_list = [_stock_record(r, pool) for _, r in bottom10.iterrows()]

    # === 全市场成交额 TOP10 ===
    vol_top10 = active.nlargest(10, "amount_yi")
    vol_top10_list = [_stock_record(r, pool) for _, r in vol_top10.iterrows()]

    # === 股票池板块聚合（仅池内股票）===
    pool_df = active[active["code"].isin(pool)].copy()
    pool_df["sector"] = pool_df["code"].map(lambda c: pool.get(c, ("", False, ""))[2])
    pool_df["star"] = pool_df["code"].map(lambda c: pool.get(c, ("", False, ""))[1])

    sectors = []
    if not pool_df.empty:
        for sector, group in pool_df.groupby("sector"):
            avg_pct = group["pctChg"].mean()
            total_amount = group["amount_yi"].sum()
            leader = group.loc[group["pctChg"].idxmax()]
            sectors.append({
                "sector": sector,
                "avg_pctChg": round(float(avg_pct), 2),
                "total_amount_yi": round(float(total_amount), 2),
                "count": len(group),
                "up": int((group["pctChg"] > 0).sum()),
                "down": int((group["pctChg"] < 0).sum()),
                "leader": leader["name"],
                "leader_pct": float(leader["pctChg"]),
            })
        sectors.sort(key=lambda x: x["avg_pctChg"], reverse=True)

    # === 异动（全市场扫描）===
    alerts = _scan_alerts(active, pool)

    # === 辨识度⭐核心股 ===
    star_list = []
    if not pool_df.empty:
        stars = pool_df[pool_df["star"] == True].sort_values("pctChg", ascending=False)
        star_list = [_stock_record(r, pool) for _, r in stars.iterrows()]

    output = {
        "time": now,
        "snapshot_ts": ts,
        "db": get_db_path(),
        "market": {
            "total_active": total_active,
            "up": up,
            "down": down,
            "flat": total_active - up - down,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "total_amount_yi": round(float(active["amount_yi"].sum()), 2),
        },
        "pool_summary": {
            "total": len(pool_df),
            "up": int((pool_df["pctChg"] > 0).sum()) if not pool_df.empty else 0,
            "down": int((pool_df["pctChg"] < 0).sum()) if not pool_df.empty else 0,
        },
        "top20_gainers": top20_list,
        "top10_losers": bottom10_list,
        "top10_volume": vol_top10_list,
        "sectors": sectors,
        "alerts": alerts,
        "star_stocks": star_list,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


def _stock_record(r, pool):
    code = r["code"]
    pool_info = pool.get(code)
    return {
        "code": code,
        "name": r.get("name", ""),
        "price": float(r["price"]),
        "pctChg": float(r["pctChg"]),
        "amount_yi": float(r.get("amount_yi", 0)),
        "in_pool": bool(pool_info),
        "sector": pool_info[2] if pool_info else "",
        "star": bool(pool_info[1]) if pool_info else False,
    }


def _scan_alerts(df, pool):
    """全市场异动扫描"""
    alerts = []
    for _, r in df.iterrows():
        pct = r["pctChg"]
        code = r["code"]
        price = r["price"]
        last_close = r["last_close"]

        pool_info = pool.get(code)
        base = {
            "name": r["name"], "code": code, "pctChg": float(pct),
            "amount_yi": float(r.get("amount_yi", 0)),
            "in_pool": bool(pool_info),
            "sector": pool_info[2] if pool_info else "",
        }

        # 精确涨停/跌停判断
        _, is_up, is_down = check_limit(code, price, last_close)
        if is_up:
            alerts.append({**base, "type": "涨停"})
        elif is_down:
            alerts.append({**base, "type": "跌停"})

        # 冲高回落
        if r["high"] > 0 and r["last_close"] > 0:
            hp = (r["high"] - r["last_close"]) / r["last_close"] * 100
            if hp > 5 and pct < 1:
                alerts.append({**base, "type": "冲高回落", "high_pct": round(float(hp), 2)})

        # 低开高走
        if r["open"] > 0 and r["last_close"] > 0:
            op = (r["open"] - r["last_close"]) / r["last_close"] * 100
            if op < -2 and pct > 1:
                alerts.append({**base, "type": "低开高走", "open_pct": round(float(op), 2)})

    # 池内优先，然后按涨跌幅排序
    alerts.sort(key=lambda x: (0 if x["in_pool"] else 1, -abs(x["pctChg"])))
    return alerts


# ═══════════════════════════════════════════════════════════════
# 命令：query — 查询指定时间点
# ═══════════════════════════════════════════════════════════════

def cmd_query(date_str, ts, pool_only=False):
    db_path = get_db_path()
    if not os.path.exists(db_path):
        print(json.dumps({"error": "数据库不存在: %s" % db_path}, ensure_ascii=False))
        return

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row

    if len(ts) == 5:
        ts = ts + ":00"

    where = "WHERE date = ? AND ts = ?" if not pool_only else "WHERE date = ? AND ts = ? AND in_pool = 1"
    rows = conn.execute(
        "SELECT * FROM snapshots %s ORDER BY pctChg DESC" % where, (date_str, ts)
    ).fetchall()
    conn.close()

    if not rows:
        conn2 = sqlite3.connect(db_path, timeout=5)
        available = [{"date": r[0], "ts": r[1]} for r in conn2.execute(
            "SELECT DISTINCT date, ts FROM snapshots ORDER BY date DESC, ts DESC LIMIT 20"
        ).fetchall()]
        conn2.close()
        print(json.dumps({"error": "未找到 %s %s" % (date_str, ts), "available": available}, ensure_ascii=False, indent=2))
        return

    records = [dict(r) for r in rows]
    output = {
        "date": date_str,
        "ts": ts,
        "count": len(records),
        "stocks": records,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ═══════════════════════════════════════════════════════════════
# 命令：compare — 对比两个时间点
# ═══════════════════════════════════════════════════════════════

def cmd_compare(date1, ts1, date2, ts2):
    db_path = get_db_path()
    if not os.path.exists(db_path):
        print(json.dumps({"error": "数据库不存在"}, ensure_ascii=False))
        return

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row

    if len(ts1) == 5:
        ts1 += ":00"
    if len(ts2) == 5:
        ts2 += ":00"

    rows = conn.execute("""
        SELECT a.code, a.name, a.sector, a.star, a.in_pool,
               a.price AS price1, a.pctChg AS pct1, a.amount_yi AS amt1,
               b.price AS price2, b.pctChg AS pct2, b.amount_yi AS amt2,
               b.pctChg - a.pctChg AS pct_delta,
               b.amount_yi - a.amount_yi AS amt_delta
        FROM snapshots a
        JOIN snapshots b ON a.code = b.code
        WHERE a.date = ? AND a.ts = ? AND b.date = ? AND b.ts = ?
        ORDER BY pct_delta DESC
    """, (date1, ts1, date2, ts2)).fetchall()
    conn.close()

    if not rows:
        print(json.dumps({"error": "对比数据为空，请检查时间点"}, ensure_ascii=False))
        return

    records = [dict(r) for r in rows]

    up_accel = sum(1 for r in records if r["pct_delta"] > 0)
    down_accel = sum(1 for r in records if r["pct_delta"] < 0)

    output = {
        "from": "%s %s" % (date1, ts1),
        "to": "%s %s" % (date2, ts2),
        "total": len(records),
        "accelerating": up_accel,
        "decelerating": down_accel,
        "top20_accelerating": records[:20],
        "top10_decelerating": records[-10:][::-1],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ═══════════════════════════════════════════════════════════════
# 命令：bid — 五档盘口
# ═══════════════════════════════════════════════════════════════

def cmd_bid(code):
    client = get_client()
    df = client.quotes(symbol=[code])
    if df is None or df.empty:
        print(json.dumps({"error": "未获取到盘口数据"}, ensure_ascii=False))
        return

    r = df.iloc[0]
    bids = []
    for i in range(1, 6):
        bids.append({"level": i, "price": float(r["bid%d" % i]), "vol": int(r["bid_vol%d" % i])})
    asks = []
    for i in range(1, 6):
        asks.append({"level": i, "price": float(r["ask%d" % i]), "vol": int(r["ask_vol%d" % i])})

    output = {
        "code": r["code"],
        "price": float(r["price"]),
        "pctChg": round((r["price"] - r["last_close"]) / r["last_close"] * 100, 2),
        "bids": bids,
        "asks": asks,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ═══════════════════════════════════════════════════════════════
# 命令：minute — 分时数据
# ═══════════════════════════════════════════════════════════════

def cmd_minute(code):
    client = get_client()
    df = client.minute(symbol=code)
    if df is None or df.empty:
        print(json.dumps({"error": "未获取到分时数据"}, ensure_ascii=False))
        return

    records = []
    for _, r in df.iterrows():
        records.append({
            "time": str(r.get("datetime", r.name) if "datetime" in df.columns else r.name),
            "price": float(r["price"]),
            "vol": int(r["vol"]),
        })

    output = {"code": code, "minutes": records}
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ═══════════════════════════════════════════════════════════════
# 命令：times — 列出今日已拉取的时间点
# ═══════════════════════════════════════════════════════════════

def cmd_times(date_str=None):
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    db_path = get_db_path()
    if not os.path.exists(db_path):
        print(json.dumps({"error": "数据库不存在"}, ensure_ascii=False))
        return

    conn = sqlite3.connect(db_path, timeout=5)
    rows = conn.execute("""
        SELECT date, ts, COUNT(*) as count,
               SUM(in_pool) as pool_count,
               ROUND(AVG(pctChg), 2) as avg_pct
        FROM snapshots WHERE date = ?
        GROUP BY date, ts ORDER BY ts
    """, (date_str,)).fetchall()

    # 也列出所有有数据的日期
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM snapshots ORDER BY date DESC").fetchall()]
    conn.close()

    times = [{"ts": r[1], "stocks": r[2], "in_pool": r[3], "avg_pctChg": r[4]} for r in rows]
    print(json.dumps({"date": date_str, "available_dates": dates, "snapshots": times}, ensure_ascii=False, indent=2))


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "pull":
        cmd_pull()
    elif cmd == "snapshot":
        cmd_snapshot()
    elif cmd == "query":
        # query [date] [ts] [--pool]
        args = [a for a in sys.argv[2:] if a != "--pool"]
        pool_only = "--pool" in sys.argv
        if len(args) >= 2:
            cmd_query(args[0], args[1], pool_only)
        elif len(args) == 1:
            cmd_query(datetime.now().strftime("%Y-%m-%d"), args[0], pool_only)
        else:
            cmd_query(datetime.now().strftime("%Y-%m-%d"), datetime.now().strftime("%H:%M:%S"), pool_only)
    elif cmd == "compare":
        # compare <date1> <ts1> <date2> <ts2>  或  compare <ts1> <ts2>（默认今天）
        args = sys.argv[2:]
        if len(args) == 4:
            cmd_compare(args[0], args[1], args[2], args[3])
        elif len(args) == 2:
            today = datetime.now().strftime("%Y-%m-%d")
            cmd_compare(today, args[0], today, args[1])
        else:
            print("用法: compare <ts1> <ts2> 或 compare <date1> <ts1> <date2> <ts2>")
            sys.exit(1)
    elif cmd == "times":
        date_str = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_times(date_str)
    elif cmd == "bid":
        if len(sys.argv) < 3:
            print("用法: intraday_data.py bid <代码>")
            sys.exit(1)
        cmd_bid(sys.argv[2])
    elif cmd == "minute":
        if len(sys.argv) < 3:
            print("用法: intraday_data.py minute <代码>")
            sys.exit(1)
        cmd_minute(sys.argv[2])
    else:
        print("未知命令: %s" % cmd)
        print(__doc__)
        sys.exit(1)
