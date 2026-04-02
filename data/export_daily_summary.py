#!/usr/bin/env python3
"""
每日行情摘要导出 — 从盘中 SQLite 生成结构化 Markdown

用法:
  python3 trading/export_daily_summary.py [日期]     # 默认今天
  python3 trading/export_daily_summary.py 2026-03-30

输出: trading/daily/YYYY-MM-DD/行情数据.md
"""

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_config

_cfg = get_config()
DB_PATH = Path(_cfg["intraday_db"])
DAILY_DIR = Path(_cfg["daily_dir"])
STOCKS_MD = Path(_cfg["stocks_file"])


def parse_stocks_md():
    """解析 stocks.md 获取板块和⭐信息"""
    pool = {}
    sector = ""
    if not STOCKS_MD.exists():
        return pool
    for line in STOCKS_MD.read_text().splitlines():
        if line.startswith("## ") and "（" in line:
            sector = line[3:].split("（")[0].strip()
        elif line.startswith("|") and "---" not in line and "股票" not in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4:
                name = parts[1]
                star = "⭐" in parts[2]
                if name:
                    pool[name] = {"sector": sector, "star": star}
    return pool


def get_snapshots(db, date):
    """获取指定日期的所有快照时间"""
    rows = db.execute(
        "SELECT DISTINCT ts FROM snapshots WHERE date=? ORDER BY ts", (date,)
    ).fetchall()
    return [r[0] for r in rows]


def get_snapshot_data(db, date, ts):
    """获取指定快照的数据"""
    return db.execute(
        "SELECT code, name, price, pctChg, open, high, low, last_close, "
        "volume, amount, amount_yi, sector, star, in_pool "
        "FROM snapshots WHERE date=? AND ts=?",
        (date, ts),
    ).fetchall()


def export_summary(date_str):
    if not DB_PATH.exists():
        print("数据库不存在: %s" % DB_PATH)
        return

    db = sqlite3.connect(str(DB_PATH))
    snapshots = get_snapshots(db, date_str)
    if not snapshots:
        print("无 %s 的快照数据" % date_str)
        return

    # 取开盘（最早）和收盘（最晚）快照
    open_ts = snapshots[0]
    close_ts = snapshots[-1]

    open_data = get_snapshot_data(db, date_str, open_ts)
    close_data = get_snapshot_data(db, date_str, close_ts)

    # 转为 dict: code -> row
    open_map = {r[0]: r for r in open_data}
    close_map = {r[0]: r for r in close_data}

    pool_info = parse_stocks_md()

    lines = []
    lines.append("# 行情数据（%s）\n" % date_str)
    lines.append("> 开盘快照: %s | 收盘快照: %s | 共 %d 个时间点\n" % (open_ts, close_ts, len(snapshots)))

    # ── 一、全市场概览 ──
    active = [r for r in close_data if r[2] > 0]  # price > 0
    up = sum(1 for r in active if r[3] > 0)
    down = sum(1 for r in active if r[3] < 0)
    flat = len(active) - up - down
    total_amount = sum(r[10] for r in active)

    # 涨跌停
    limit_up = sum(1 for r in active if r[12] and r[3] > 0 or _is_limit(r, up=True))
    limit_down = sum(1 for r in active if _is_limit(r, up=False))

    lines.append("## 一、全市场概览\n")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append("| 活跃个股 | %d |" % len(active))
    lines.append("| 上涨 / 下跌 / 平盘 | %d / %d / %d |" % (up, down, flat))
    lines.append("| 涨停 / 跌停 | %d / %d |" % (limit_up, limit_down))
    lines.append("| 总成交额 | %.0f 亿 |" % total_amount)
    lines.append("")

    # ── 二、板块排名（⭐加权） ──
    pool_stocks = [r for r in close_data if r[13]]  # in_pool
    sectors = defaultdict(list)
    for r in pool_stocks:
        sec = r[11] or "未分类"
        sectors[sec].append(r)

    ranked = []
    for sec, stocks in sectors.items():
        weighted = sum(r[3] * (2 if r[12] else 1) for r in stocks)
        avg = sum(r[3] for r in stocks) / len(stocks)
        stars = [r for r in stocks if r[12]]
        total_amt = sum(r[10] for r in stocks)
        ranked.append((sec, stocks, weighted, avg, stars, total_amt))
    ranked.sort(key=lambda x: -x[2])

    lines.append("## 二、板块排名（⭐辨识度加权）\n")
    lines.append("| # | 板块 | 只数 | ⭐ | 加权分 | 均涨幅 | 成交额(亿) | 状态 |")
    lines.append("|---|------|------|---|--------|--------|-----------|------|")
    for i, (sec, stocks, weighted, avg, stars, total_amt) in enumerate(ranked):
        status = "🔥强势" if avg > 3 else ("✅偏强" if avg > 0 else ("⚠️震荡" if avg > -3 else "❌弱势"))
        lines.append("| %d | %s | %d | %d | %.1f | %+.2f%% | %.1f | %s |" % (
            i + 1, sec, len(stocks), len(stars), weighted, avg, total_amt, status))
    lines.append("")

    # ── 三、板块详情（TOP5 + 退潮） ──
    lines.append("## 三、板块详情\n")

    for i, (sec, stocks, weighted, avg, stars, total_amt) in enumerate(ranked[:5]):
        lines.append("### %d. %s（均涨 %+.2f%%，成交 %.1f 亿）\n" % (i + 1, sec, avg, total_amt))
        if stars:
            lines.append("**⭐辨识度核心：**")
            for r in sorted(stars, key=lambda x: -x[3]):
                chg_from_open = ""
                if r[0] in open_map:
                    o = open_map[r[0]]
                    chg_from_open = "（开盘 %+.2f%%）" % o[3]
                lines.append("- %s（%s）**%+.2f%%** 成交 %.1f 亿 %s" % (r[1], r[0], r[3], r[10], chg_from_open))
            lines.append("")
        top = sorted(stocks, key=lambda x: -x[3])[:5]
        lines.append("**涨幅前5：**")
        for r in top:
            mark = "⭐" if r[12] else ""
            lines.append("- %s%s（%s）**%+.2f%%** 成交 %.1f 亿" % (mark, r[1], r[0], r[3], r[10]))
        lines.append("")

    # 退潮板块
    if len(ranked) > 5:
        lines.append("### 退潮板块\n")
        for sec, stocks, weighted, avg, stars, total_amt in ranked[-3:]:
            detail = ""
            if stars:
                down_stars = [r for r in stars if r[3] < -5]
                if down_stars:
                    detail = " ⭐核心：" + "、".join("%s %+.1f%%" % (r[1], r[3]) for r in down_stars)
            lines.append("- **%s**：均涨 %+.2f%%%s" % (sec, avg, detail))
        lines.append("")

    # ── 四、涨停板明细 ──
    limit_ups = [r for r in active if _is_limit(r, up=True)]
    limit_ups.sort(key=lambda x: -x[10])  # 按成交额排序

    lines.append("## 四、涨停板（%d 只）\n" % len(limit_ups))
    if limit_ups:
        lines.append("| 股票 | 代码 | 涨幅 | 成交额(亿) | 板块 | ⭐ |")
        lines.append("|------|------|------|-----------|------|---|")
        for r in limit_ups:
            sec = r[11] if r[13] else ""
            star = "⭐" if r[12] else ""
            lines.append("| %s | %s | %+.2f%% | %.1f | %s | %s |" % (r[1], r[0], r[3], r[10], sec, star))
    lines.append("")

    # ── 五、跌停板明细 ──
    limit_downs = [r for r in active if _is_limit(r, up=False)]
    limit_downs.sort(key=lambda x: x[3])

    lines.append("## 五、跌停板（%d 只）\n" % len(limit_downs))
    if limit_downs:
        lines.append("| 股票 | 代码 | 跌幅 | 成交额(亿) | 板块 | ⭐ |")
        lines.append("|------|------|------|-----------|------|---|")
        for r in limit_downs:
            sec = r[11] if r[13] else ""
            star = "⭐" if r[12] else ""
            lines.append("| %s | %s | %+.2f%% | %.1f | %s | %s |" % (r[1], r[0], r[3], r[10], sec, star))
    lines.append("")

    # ── 六、⭐辨识度核心股全览 ──
    star_stocks = [r for r in close_data if r[12]]
    star_stocks.sort(key=lambda x: -x[3])

    lines.append("## 六、⭐辨识度核心股（%d 只）\n" % len(star_stocks))
    if star_stocks:
        lines.append("| 股票 | 代码 | 涨幅 | 成交额(亿) | 板块 | 开盘涨幅 | 全天变化 |")
        lines.append("|------|------|------|-----------|------|---------|---------|")
        for r in star_stocks:
            open_pct = ""
            delta = ""
            if r[0] in open_map:
                o = open_map[r[0]]
                open_pct = "%+.2f%%" % o[3]
                delta = "%+.2f%%" % (r[3] - o[3])
            lines.append("| %s | %s | **%+.2f%%** | %.1f | %s | %s | %s |" % (
                r[1], r[0], r[3], r[10], r[11] or "", open_pct, delta))
    lines.append("")

    # ── 七、全市场 TOP20 涨幅 ──
    top20 = sorted(active, key=lambda x: -x[3])[:20]
    lines.append("## 七、全市场涨幅 TOP20\n")
    lines.append("| # | 股票 | 代码 | 涨幅 | 成交额(亿) | 池内 | 板块 |")
    lines.append("|---|------|------|------|-----------|------|------|")
    for i, r in enumerate(top20):
        in_pool = "✓" if r[13] else ""
        sec = r[11] if r[13] else ""
        lines.append("| %d | %s | %s | **%+.2f%%** | %.1f | %s | %s |" % (
            i + 1, r[1], r[0], r[3], r[10], in_pool, sec))
    lines.append("")

    # ── 八、成交额 TOP20 ──
    vol_top20 = sorted(active, key=lambda x: -x[10])[:20]
    lines.append("## 八、成交额 TOP20\n")
    lines.append("| # | 股票 | 代码 | 涨幅 | 成交额(亿) | 池内 | 板块 |")
    lines.append("|---|------|------|------|-----------|------|------|")
    for i, r in enumerate(vol_top20):
        in_pool = "✓" if r[13] else ""
        sec = r[11] if r[13] else ""
        lines.append("| %d | %s | %s | %+.2f%% | **%.1f** | %s | %s |" % (
            i + 1, r[1], r[0], r[3], r[10], in_pool, sec))
    lines.append("")

    # ── 九、开盘 vs 收盘 板块变化 ──
    if open_ts != close_ts:
        open_sectors = defaultdict(list)
        for r in open_data:
            if r[13]:
                open_sectors[r[11] or "未分类"].append(r)

        lines.append("## 九、板块全天走势（开盘 → 收盘）\n")
        lines.append("| 板块 | 开盘均涨 | 收盘均涨 | 变化 | 趋势 |")
        lines.append("|------|---------|---------|------|------|")

        for sec, stocks, weighted, avg, stars, total_amt in ranked:
            if sec in open_sectors:
                open_avg = sum(r[3] for r in open_sectors[sec]) / len(open_sectors[sec])
                delta = avg - open_avg
                trend = "📈 走强" if delta > 1 else ("📉 走弱" if delta < -1 else "➡️ 持平")
                lines.append("| %s | %+.2f%% | %+.2f%% | %+.2f%% | %s |" % (
                    sec, open_avg, avg, delta, trend))
        lines.append("")

    db.close()

    # 写入文件
    out_dir = DAILY_DIR / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "行情数据.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print("已导出: %s (%d 行)" % (out_path, len(lines)))

    # 导出涨跌停 CSV + 行情 CSV
    date_compact = date_str.replace("-", "")
    _export_limit_csv(out_dir, date_compact, limit_ups, "涨停板")
    _export_limit_csv(out_dir, date_compact, limit_downs, "跌停板", is_down=True)
    _export_market_csv(out_dir, date_compact, close_data, pool_info)

    return str(out_path)


def _export_limit_csv(out_dir, date_compact, stocks, prefix, is_down=False):
    """导出涨停/跌停 CSV，兼容 AKShare 格式"""
    fname = "%s_%s.csv" % (prefix, date_compact)
    path = out_dir / fname
    # code, name, pctChg, price, amount_yi, sector(from pool), star
    header = "序号,代码,名称,涨跌幅,最新价,成交额,所属行业"
    rows = []
    for i, r in enumerate(stocks):
        code = r[0]
        # 加市场前缀
        rows.append("%d,%s,%s,%.2f,%.2f,%.0f,%s" % (
            i + 1, code, r[1], r[3], r[2], r[10] * 1e8, r[11] or ""))
    path.write_text("\ufeff" + header + "\n" + "\n".join(rows), encoding="utf-8")
    print("已导出: %s (%d 只)" % (path, len(stocks)))


def _export_market_csv(out_dir, date_compact, close_data, pool_info):
    """导出股票池行情 CSV，兼容历史格式"""
    fname = "行情_%s.csv" % date_compact
    path = out_dir / fname
    # 只导出池内股票
    pool_stocks = [r for r in close_data if r[13]]
    pool_stocks.sort(key=lambda x: x[11] or "")  # 按板块排序

    header = "date,code,名称,open,high,low,close,volume,amount,turn,pctChg,板块"
    rows = []
    date_str = date_compact[:4] + "-" + date_compact[4:6] + "-" + date_compact[6:]
    for r in pool_stocks:
        # code, name, price, pctChg, open, high, low, last_close, volume, amount, amount_yi, sector, star, in_pool
        mkt = "sh" if r[0].startswith(("6", "9")) else "sz"
        rows.append("%s,%s.%s,%s,%.4f,%.4f,%.4f,%.4f,%d,%.4f,,%s,%s" % (
            date_str, mkt, r[0], r[1], r[4], r[5], r[6], r[2], r[8], r[9], r[3], r[11] or ""))
    path.write_text("\ufeff" + header + "\n" + "\n".join(rows), encoding="utf-8")
    print("已导出: %s (%d 只)" % (path, len(pool_stocks)))


def _is_limit(row, up=True):
    """判断是否涨停/跌停"""
    price, last_close, code = row[2], row[7], row[0]
    if last_close <= 0:
        return False
    is_20cm = code.startswith(("300", "301", "688"))
    pct = 20 if is_20cm else 10
    if up:
        limit = round(last_close * (1 + pct / 100), 2)
        return price >= limit
    else:
        limit = round(last_close * (1 - pct / 100), 2)
        return price <= limit


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    export_summary(date)
