#!/usr/bin/env python3
"""
策略版本库 - 记录每个策略版本的元数据 + 历次回测结果

设计原则：
- 策略版本 = commit_hash + 参数快照 + 业务标签
- 每次回测结果附着到策略版本上，形成时间序列
- 健康度监控和淘汰决策基于此库

表结构：
  strategy_versions  - 策略元数据（一次定义）
  strategy_backtest_log - 回测结果（每次记录）

用法:
  python3 trading/strategy_registry.py init              # 初始化 DB
  python3 trading/strategy_registry.py register          # 登记当前 HEAD 作为新策略版本
  python3 trading/strategy_registry.py log               # 查看所有版本
  python3 trading/strategy_registry.py backtest-log      # 查看回测历史
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sqlite3
import sys
from datetime import datetime

DB_PATH = os.path.expanduser("~/shared/trading/strategy_registry.db")


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS strategy_versions (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            commit_hash     TEXT,
            params_json     TEXT,
            created_at      TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'candidate',
            note            TEXT
        );

        CREATE TABLE IF NOT EXISTS strategy_backtest_log (
            strategy_id     TEXT NOT NULL,
            run_at          TEXT NOT NULL,
            window_days     INTEGER NOT NULL,
            start_date      TEXT,
            end_date        TEXT,
            return_cost     REAL,
            return_market   REAL,
            trade_count     INTEGER,
            win_count       INTEGER,
            win_rate        REAL,
            sharpe          REAL,
            max_drawdown    REAL,
            metadata_json   TEXT,
            PRIMARY KEY (strategy_id, run_at, window_days),
            FOREIGN KEY (strategy_id) REFERENCES strategy_versions(id)
        );

        CREATE INDEX IF NOT EXISTS idx_backtest_strategy_window
            ON strategy_backtest_log(strategy_id, window_days, run_at);
    """)
    conn.commit()


def register(name: str, params: dict, note: str = "") -> str:
    """登记新策略版本，返回策略 ID"""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.expanduser("~/src/a-stock-trading"),
            text=True,
        ).strip()[:12]
    except Exception:
        commit = "UNKNOWN"

    strategy_id = f"{name}_{commit}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    conn = get_conn()
    init_schema(conn)
    conn.execute(
        "INSERT INTO strategy_versions (id, name, commit_hash, params_json, "
        "created_at, status, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (strategy_id, name, commit, json.dumps(params, ensure_ascii=False),
         datetime.now().isoformat(timespec="seconds"), "active", note),
    )
    conn.commit()
    conn.close()
    print(f"Registered: {strategy_id}")
    return strategy_id


def log_backtest(strategy_id: str, window_days: int, start: str, end: str,
                 metrics: dict):
    """记录一次回测结果"""
    conn = get_conn()
    init_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO strategy_backtest_log "
        "(strategy_id, run_at, window_days, start_date, end_date, return_cost, "
        " return_market, trade_count, win_count, win_rate, sharpe, max_drawdown, "
        " metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (strategy_id, datetime.now().isoformat(timespec="seconds"),
         window_days, start, end,
         metrics.get("return_cost"), metrics.get("return_market"),
         metrics.get("trade_count"), metrics.get("win_count"),
         metrics.get("win_rate"), metrics.get("sharpe"),
         metrics.get("max_drawdown"),
         json.dumps(metrics.get("metadata") or {}, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()


def list_versions():
    conn = get_conn()
    init_schema(conn)
    rows = conn.execute(
        "SELECT id, name, commit_hash, created_at, status, note "
        "FROM strategy_versions ORDER BY created_at DESC"
    ).fetchall()
    print(f"{'ID':<60}{'Name':<20}{'Commit':<14}{'Status':<12}{'Created':<20}")
    for r in rows:
        print(f"{r[0]:<60}{r[1]:<20}{r[2]:<14}{r[4]:<12}{r[3]}")
        if r[5]:
            print(f"  note: {r[5]}")
    conn.close()


def list_backtest_logs(limit: int = 20):
    conn = get_conn()
    init_schema(conn)
    rows = conn.execute(
        "SELECT strategy_id, run_at, window_days, return_cost, return_market, "
        "trade_count, win_rate, max_drawdown "
        "FROM strategy_backtest_log ORDER BY run_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    print(f"{'Run':<20}{'Strategy':<40}{'Win':<6}{'Return%':<10}{'Trades':<8}{'WR':<8}{'DD':<8}")
    for r in rows:
        rc = f"{r[3]:.2f}" if r[3] is not None else "-"
        tc = str(r[5]) if r[5] is not None else "-"
        wr = f"{r[6]:.1f}%" if r[6] is not None else "-"
        dd = f"{r[7]:.1f}%" if r[7] is not None else "-"
        print(f"{r[1]:<20}{r[0][:38]:<40}{r[2]:<6}{rc:<10}{tc:<8}{wr:<8}{dd:<8}")
    conn.close()


def change_status(strategy_id: str, new_status: str, note: str = ""):
    """修改策略状态：active / candidate / retired"""
    conn = get_conn()
    init_schema(conn)
    conn.execute(
        "UPDATE strategy_versions SET status=?, note=COALESCE(?, note) WHERE id=?",
        (new_status, note or None, strategy_id),
    )
    conn.commit()
    conn.close()
    print(f"{strategy_id} -> {new_status}")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init")

    sp = sub.add_parser("register")
    sp.add_argument("--name", required=True)
    sp.add_argument("--params", default="{}", help="JSON 字符串")
    sp.add_argument("--note", default="")

    sp = sub.add_parser("log")

    sp = sub.add_parser("backtest-log")
    sp.add_argument("--limit", type=int, default=20)

    sp = sub.add_parser("status")
    sp.add_argument("id")
    sp.add_argument("--status", required=True, choices=["active","candidate","retired"])
    sp.add_argument("--note", default="")

    args = parser.parse_args()

    if args.cmd == "init":
        init_schema(get_conn())
        print(f"DB ready at {DB_PATH}")
    elif args.cmd == "register":
        register(args.name, json.loads(args.params), args.note)
    elif args.cmd == "log":
        list_versions()
    elif args.cmd == "backtest-log":
        list_backtest_logs(args.limit)
    elif args.cmd == "status":
        change_status(args.id, args.status, args.note)


if __name__ == "__main__":
    main()
