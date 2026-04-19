#!/usr/bin/env python3
"""
检查所有定时任务的运行状态。

两类任务：
  1. HappyClaw scheduled_tasks（SQLite 里记录）
  2. LaunchAgents（launchctl list）

判断标准：
  - 盘中时段：intraday_tick 的 last_run 应在最近 2 分钟内
  - 盘后时段：各任务应已跑过（last_run == 今日）
  - 每个任务的 status 应为 active

输出：
  - 正常：stdout 打印 OK 行
  - 异常：stdout 打印 WARN/ERR 行，exit code > 0

退出码：
  0 - 全部正常
  1 - 有 WARN
  2 - 有 ERR
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sqlite3
import sys
from datetime import datetime, timedelta

HAPPYCLAW_DB = os.path.expanduser("~/src/happyclaw/data/db/messages.db")


def _now():
    return datetime.now()


def _today_str():
    return _now().strftime("%Y-%m-%d")


def _minutes_ago(dt_str: str) -> float:
    """把 ISO 时间字符串转成距今几分钟"""
    try:
        # 支持 2026-04-19T11:40:16Z 或 2026-04-19T11:40:16.000Z
        s = dt_str.replace("Z", "").split(".")[0]
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        # HappyClaw 存的是 UTC，转本地
        dt = dt + timedelta(hours=8)
        return (_now() - dt).total_seconds() / 60
    except Exception:
        return float("inf")


def _is_intraday_window() -> bool:
    """是否在盘中时段（9:30-11:30 或 13:00-15:00）"""
    now = _now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def _is_after_1510() -> bool:
    return _now().hour > 15 or (_now().hour == 15 and _now().minute >= 10)


def _is_after_1517() -> bool:
    return _now().hour > 15 or (_now().hour == 15 and _now().minute >= 17)


def _is_after_1800() -> bool:
    return _now().hour >= 18


def _is_after_1700() -> bool:
    return _now().hour >= 17


def check_happyclaw_tasks() -> list[tuple[str, str, str]]:
    """检查 HappyClaw scheduled_tasks 表，返回 [(level, name, detail)]"""
    results = []
    if not os.path.exists(HAPPYCLAW_DB):
        return [("ERR", "HappyClaw DB", f"不存在: {HAPPYCLAW_DB}")]

    db = sqlite3.connect(HAPPYCLAW_DB, timeout=10)

    # 盘中：intraday_tick
    row = db.execute(
        "SELECT id, schedule_value, last_run, status FROM scheduled_tasks "
        "WHERE status='active' AND script_command LIKE '%intraday_tick%' LIMIT 1"
    ).fetchone()
    if not row:
        results.append(("ERR", "intraday_tick", "找不到 active 任务"))
    else:
        task_id, cron, last_run, status = row
        if _is_intraday_window():
            if not last_run:
                results.append(("ERR", "intraday_tick", f"盘中时段但 last_run=None (id={task_id[:12]})"))
            else:
                ago = _minutes_ago(last_run)
                if ago > 3:
                    results.append(("WARN", "intraday_tick", f"上次运行 {ago:.0f} 分钟前 (期望 <3 分钟)"))
                else:
                    results.append(("OK", "intraday_tick", f"上次 {ago:.1f} 分钟前"))
        else:
            results.append(("OK", "intraday_tick", f"非盘中时段（status={status}）"))

    # 盘后 15:05 pull_eod_data
    if _is_after_1510():
        row = db.execute(
            "SELECT last_run FROM scheduled_tasks WHERE status='active' "
            "AND script_command LIKE '%pull_eod_data%' LIMIT 1"
        ).fetchone()
        if row and row[0] and row[0][:10] == _today_str():
            results.append(("OK", "pull_eod_data (15:05)", f"今日已跑"))
        else:
            results.append(("WARN", "pull_eod_data (15:05)", "今日未跑或未记录"))

    # 盘后 15:10 layered_analysis
    if _is_after_1510():
        row = db.execute(
            "SELECT last_run FROM scheduled_tasks WHERE status='active' "
            "AND schedule_value='10 15 * * 1-5' LIMIT 1"
        ).fetchone()
        if row and row[0] and row[0][:10] == _today_str():
            results.append(("OK", "layered_analysis (15:10)", "今日已跑"))
        else:
            results.append(("WARN", "layered_analysis (15:10)", "今日未跑或未记录"))

    # 盘后 15:17 data_quality_check
    if _is_after_1517():
        row = db.execute(
            "SELECT last_run FROM scheduled_tasks WHERE status='active' "
            "AND schedule_value='17 15 * * 1-5' LIMIT 1"
        ).fetchone()
        if row and row[0] and row[0][:10] == _today_str():
            results.append(("OK", "data_quality_check (15:17)", "今日已跑"))
        else:
            results.append(("WARN", "data_quality_check (15:17)", "今日未跑或未记录"))

    db.close()
    return results


def check_launchagents() -> list[tuple[str, str, str]]:
    """检查 LaunchAgents 状态 + 今日是否执行过"""
    results = []
    try:
        out = subprocess.check_output(["launchctl", "list"], text=True)
    except Exception as e:
        return [("ERR", "launchctl", str(e))]

    expected = [
        ("com.luoxin.astocktrading.daily", "17:00 daily_maintenance"),
        ("com.luoxin.astocktrading.closing", "18:00 closing_review"),
    ]

    loaded = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].startswith("com.luoxin.astocktrading"):
            pid, exit_code, label = parts
            loaded[label] = (pid, exit_code)

    for label, desc in expected:
        if label not in loaded:
            results.append(("ERR", label, f"未加载 ({desc})"))
            continue
        pid, exit_code = loaded[label]
        # 检查今日日志（如果时间已过调度点）
        log_dir = os.path.expanduser("~/shared/trading/logs")
        today_files = {
            "daily": f"daily_maintenance_{_today_str()}.log",
            "closing": f"closing_review_{_today_str()}.log",
        }
        short = label.split(".")[-1]
        fname = today_files.get(short)
        log_path = os.path.join(log_dir, fname) if fname else None

        # 判断是否该已跑
        should_run = False
        if short == "daily" and _is_after_1700():
            should_run = True
        if short == "closing" and _is_after_1800():
            should_run = True

        if should_run:
            if log_path and os.path.exists(log_path):
                results.append(("OK", label, f"今日日志存在 exit={exit_code}"))
            else:
                results.append(("WARN", label, f"{desc} 应已跑但无今日日志"))
        else:
            results.append(("OK", label, f"加载中，exit={exit_code} ({desc} 未到调度时间)"))

    return results


def main():
    now = _now()
    print(f"=== 定时任务健康检查 {now.strftime('%Y-%m-%d %H:%M:%S')} ===")

    # 非交易日跳过检查（周末+节假日）
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from is_trading_day import is_trading_day
        if not is_trading_day(now.date()):
            print("非交易日，跳过检查")
            sys.exit(0)
    except Exception:
        # fallback：周末跳过
        if now.weekday() >= 5:
            print("周末，跳过检查")
            sys.exit(0)

    print(f"盘中时段: {_is_intraday_window()}")
    print()

    all_results = []
    print("【HappyClaw scheduled_tasks】")
    hc = check_happyclaw_tasks()
    for level, name, detail in hc:
        print(f"  [{level}] {name}: {detail}")
    all_results.extend(hc)

    print()
    print("【LaunchAgents】")
    la = check_launchagents()
    for level, name, detail in la:
        print(f"  [{level}] {name}: {detail}")
    all_results.extend(la)

    print()
    err_count = sum(1 for r in all_results if r[0] == "ERR")
    warn_count = sum(1 for r in all_results if r[0] == "WARN")
    ok_count = sum(1 for r in all_results if r[0] == "OK")
    print(f"汇总: OK {ok_count} / WARN {warn_count} / ERR {err_count}")

    if err_count > 0:
        sys.exit(2)
    if warn_count > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
