"""
历史影响计算引擎

对每条历史新闻，计算关联个股在各时间窗口的实际涨跌：
- 盘中窗口：5min / 15min / 30min / 1h / 2h / 收盘
- 盘后窗口：次日 / 第2日 / 第3日 / 第5日
- 极值：最大涨幅 / 最大跌幅
- 恢复时间：恢复到新闻前价格所需分钟数
- 量能比：新闻后 1h 成交量 vs 新闻前同期
"""

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config import get_config

from . import db

_cfg = get_config()


def _parse_news_time(news_time_str, created_date):
    """解析新闻时间为 datetime 对象

    Args:
        news_time_str: "HH:MM" 或 "HH:MM:SS" 格式
        created_date: "YYYY-MM-DD" 格式

    Returns:
        (date_str, time_str) — ("YYYY-MM-DD", "HH:MM:SS")
    """
    if not news_time_str:
        return created_date, None

    time_str = news_time_str.strip()
    if len(time_str) == 5:
        time_str = time_str + ":00"
    return created_date, time_str


def _extract_stock_codes(stocks_json):
    """从 stocks JSON 中提取股票代码列表

    stocks_json 格式：'["神剑股份(002523)", "航天电器(002025)"]'
    或：'["002523", "002025"]'
    """
    if not stocks_json:
        return []

    try:
        stocks = json.loads(stocks_json) if isinstance(stocks_json, str) else stocks_json
    except (json.JSONDecodeError, TypeError):
        return []

    codes = []
    for s in stocks:
        if isinstance(s, str):
            # 匹配括号内的代码
            m = re.search(r'\((\d{6})\)', s)
            if m:
                codes.append(m.group(1))
            elif re.match(r'^\d{6}$', s):
                codes.append(s)
    return codes


def _time_add_minutes(time_str, minutes):
    """HH:MM:SS + minutes → HH:MM:SS"""
    try:
        parts = time_str.split(":")
        h, m = int(parts[0]), int(parts[1])
        s = int(parts[2]) if len(parts) > 2 else 0
        total_sec = h * 3600 + m * 60 + s + minutes * 60
        nh = total_sec // 3600
        nm = (total_sec % 3600) // 60
        ns = total_sec % 60
        return "%02d:%02d:%02d" % (nh, nm, ns)
    except Exception:
        return None


def _time_diff_minutes(t1, t2):
    """计算两个 HH:MM:SS 之间的分钟差"""
    try:
        def to_min(t):
            parts = t.split(":")
            return int(parts[0]) * 60 + int(parts[1])

        return to_min(t2) - to_min(t1)
    except Exception:
        return None


def calc_news_impact(news_id, stock_code, news_date, news_time):
    """计算单条新闻对单只个股的影响

    Args:
        news_id: 新闻 ID
        stock_code: 股票代码（如 "002523"）
        news_date: 新闻日期 "YYYY-MM-DD"
        news_time: 新闻时间 "HH:MM" 或 "HH:MM:SS"

    Returns:
        dict — 影响记录，或 None（数据不足时）
    """
    if len(news_time) == 5:
        news_time += ":00"

    # 1. 获取新闻前的基准价格（新闻前最近的快照）
    pre_snapshot = db.get_snapshot_for_stock(stock_code, news_date, news_time)
    if not pre_snapshot or pre_snapshot.get("price", 0) <= 0:
        return None

    pre_price = pre_snapshot["price"]
    pre_ts = pre_snapshot.get("ts", "")
    pre_vol = pre_snapshot.get("volume", 0)

    record = {
        "news_id": news_id,
        "stock_code": stock_code,
        "stock_name": pre_snapshot.get("name", ""),
        "pre_price": pre_price,
        "news_time": news_time[:5],
        "news_date": news_date,
    }

    # 2. 盘中窗口计算
    windows = {
        "pct_5min": 5,
        "pct_15min": 15,
        "pct_30min": 30,
        "pct_1h": 60,
        "pct_2h": 120,
    }

    for field, minutes in windows.items():
        target_ts = _time_add_minutes(news_time, minutes)
        if target_ts:
            snap = db.get_snapshot_at_or_after(stock_code, news_date, target_ts)
            if snap and snap.get("price", 0) > 0:
                record[field] = round((snap["price"] - pre_price) / pre_price * 100, 2)

    # 3. 收盘价（当天最后一个快照）
    all_snaps = db.get_snapshots_range(
        stock_code, news_date,
        news_time, "23:59:59"
    )
    if all_snaps:
        eod = all_snaps[-1]
        if eod.get("price", 0) > 0:
            record["pct_eod"] = round((eod["price"] - pre_price) / pre_price * 100, 2)

    # 4. 盘后窗口（次日、第2日、第3日、第5日）
    next_day_fields = {
        "pct_next1d": 1,
        "pct_next2d": 2,
        "pct_next3d": 3,
        "pct_next5d": 5,
    }
    available_dates = db.get_available_snapshot_dates()
    news_date_idx = None
    for i, d in enumerate(available_dates):
        if d > news_date:
            news_date_idx = i
            break

    if news_date_idx is not None:
        for field, days_offset in next_day_fields.items():
            if news_date_idx + days_offset - 1 < len(available_dates):
                target_date = available_dates[news_date_idx + days_offset - 1]
                snap = db.get_snapshot_for_stock(stock_code, target_date)
                if snap and snap.get("price", 0) > 0:
                    record[field] = round((snap["price"] - pre_price) / pre_price * 100, 2)

    # 5. 极值计算
    if all_snaps and len(all_snaps) > 0:
        max_gain = 0.0
        max_loss = 0.0
        for snap in all_snaps:
            if snap.get("price", 0) > 0:
                pct = (snap["price"] - pre_price) / pre_price * 100
                if pct > max_gain:
                    max_gain = pct
                    record["time_to_peak"] = snap.get("ts", "")
                if pct < max_loss:
                    max_loss = pct
                    record["time_to_trough"] = snap.get("ts", "")
        record["max_gain_pct"] = round(max_gain, 2)
        record["max_loss_pct"] = round(max_loss, 2)

        # 6. 恢复时间（价格回到 pre_price 的时间）
        for snap in all_snaps:
            if snap.get("price", 0) > 0 and snap["price"] >= pre_price and snap["ts"] > pre_ts:
                diff = _time_diff_minutes(pre_ts, snap["ts"])
                if diff and diff > 0:
                    record["recovery_minutes"] = diff
                    break

    # 7. 量能比（新闻后 1h vs 新闻前 1h）
    if pre_ts and len(pre_ts) >= 8:
        before_start = _time_add_minutes(pre_ts[:8], -60)
        before_snaps = db.get_snapshots_range(stock_code, news_date, before_start, pre_ts)
        vol_before = sum(s.get("volume", 0) for s in before_snaps)

        after_end = _time_add_minutes(news_time, 60)
        after_snaps = db.get_snapshots_range(stock_code, news_date, news_time, after_end or "23:59:59")
        vol_after = sum(s.get("volume", 0) for s in after_snaps)

        if vol_before > 0:
            record["vol_ratio_1h"] = round(vol_after / vol_before, 2)

    return record


def calc_impacts_for_news(news_record):
    """计算一条新闻对所有关联个股的影响

    Args:
        news_record: dict with keys: id, stocks, news_time, created_date

    Returns:
        list of impact dicts
    """
    stocks_json = news_record.get("stocks", "")
    codes = _extract_stock_codes(stocks_json)
    if not codes:
        return []

    news_date = news_record.get("created_date", "")
    news_time = news_record.get("news_time", "") or news_record.get("sent_at", "")[11:16] if news_record.get("sent_at") else ""

    if not news_date or not news_time:
        return []

    results = []
    for code in codes[:5]:  # 最多计算前5只个股
        try:
            impact = calc_news_impact(news_record["id"], code, news_date, news_time)
            if impact:
                results.append(impact)
        except Exception as e:
            print("  [Impact:Calc] news_id=%d code=%s 失败: %s" % (news_record["id"], code, e), flush=True)

    return results


def batch_calc_impacts(limit=500):
    """批量计算所有未计算过的新闻影响

    Returns:
        int — 计算了多少条影响记录
    """
    from . import db as db_mod

    news_list = db.get_news_with_stocks(limit)
    if not news_list:
        print("[Impact:Calc] 没有找到有关联个股的新闻", flush=True)
        return 0

    total = 0
    t0 = time.time()
    for i, news in enumerate(news_list):
        impacts = calc_impacts_for_news(news)
        if impacts:
            db.save_impacts_batch(impacts)
            total += len(impacts)

        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            print("[Impact:Calc] 进度 %d/%d（%.1f 条/秒）" % (
                i + 1, len(news_list), (i + 1) / max(elapsed, 0.1)), flush=True)

    elapsed = time.time() - t0
    print("[Impact:Calc] 完成：计算 %d 条影响记录，耗时 %.1fs" % (total, elapsed), flush=True)
    return total
