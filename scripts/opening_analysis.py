#!/usr/bin/env python3
"""
开盘分析 Agent — 9:25 集合竞价结束后执行

分析内容：
1. 高开过顶：过去7天有涨停 + 昨天非涨停 + 今日开盘为7日最高
2. 板块总结：哪些板块批量高开/低开 + 结合新闻分析原因
3. 断板反包：前天涨停 + 昨天非涨停 + 今天高开

依赖：
- trading/intraday/intraday.db（历史日线 + 当日9:25快照）
- trading/news_monitor.py 产生的新闻文件
- 火山引擎 DeepSeek API

用法:
  python3 trading/opening_analysis.py           # 执行开盘分析（先拉数据再分析）
  python3 trading/opening_analysis.py --dry-run  # 仅输出原始数据，不调 AI
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

INTRADAY_DIR = os.path.join(os.path.dirname(__file__), "intraday")
DB_PATH = os.path.join(INTRADAY_DIR, "intraday.db")
DAILY_DIR = os.path.join(os.path.dirname(__file__), "daily")

# 火山引擎 DeepSeek
DEEPSEEK_API_KEY = os.environ.get("ARK_API_KEY", "")
DEEPSEEK_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
DEEPSEEK_MODEL = os.environ.get("ARK_MODEL", "")

# 飞书推送（复用 news_monitor 的 bot）
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_UNION_ID = os.environ.get("FEISHU_RECEIVE_ID", "")


# ═══════════════════════════════════════════════════════════════
# 数据库查询
# ═══════════════════════════════════════════════════════════════

def get_conn():
    return sqlite3.connect(DB_PATH, timeout=10)


def get_trading_days(conn, n=10):
    """获取数据库中最近 n 个交易日（按日线 ts=15:00:00 去重）"""
    rows = conn.execute("""
        SELECT DISTINCT date FROM snapshots
        WHERE ts = '15:00:00'
        ORDER BY date DESC LIMIT ?
    """, (n,)).fetchall()
    return [r[0] for r in rows]  # 最近的在前


def get_today_opening(conn, today):
    """获取今日 9:25 快照（开盘价数据）"""
    # 找今天最早的时间戳（应该是 09:25 左右）
    ts_row = conn.execute("""
        SELECT ts FROM snapshots
        WHERE date = ? AND ts != '15:00:00'
        ORDER BY ts ASC LIMIT 1
    """, (today,)).fetchone()

    if not ts_row:
        return None, None

    ts = ts_row[0]
    rows = conn.execute("""
        SELECT code, name, price as open_price, pctChg, last_close,
               sector, star, in_pool, limit_pct
        FROM snapshots WHERE date = ? AND ts = ?
    """, (today, ts)).fetchall()

    cols = ["code", "name", "open_price", "pctChg", "last_close",
            "sector", "star", "in_pool", "limit_pct"]
    result = {}
    for row in rows:
        d = dict(zip(cols, row))
        result[d["code"]] = d
    return result, ts


def get_daily_close(conn, date_str):
    """获取某天的收盘数据"""
    rows = conn.execute("""
        SELECT code, name, price, pctChg, last_close, high, low, open,
               is_limit_up, is_limit_down, sector, star, in_pool, limit_pct
        FROM snapshots WHERE date = ? AND ts = '15:00:00'
    """, (date_str,)).fetchall()

    cols = ["code", "name", "price", "pctChg", "last_close", "high", "low", "open",
            "is_limit_up", "is_limit_down", "sector", "star", "in_pool", "limit_pct"]
    result = {}
    for row in rows:
        d = dict(zip(cols, row))
        result[d["code"]] = d
    return result


# ═══════════════════════════════════════════════════════════════
# 分析模块
# ═══════════════════════════════════════════════════════════════

def calc_limit_price(last_close, pct):
    return round(last_close * (1 + pct / 100), 2)


def analyze_gap_up_over_top(conn, today, trading_days):
    """
    高开过顶：
    - 过去7天内有涨停
    - 昨天不是涨停
    - 今日开盘价 >= 过去7天最高价
    """
    opening, ts = get_today_opening(conn, today)
    if not opening:
        return {"error": "今日无开盘数据", "stocks": []}

    # 取过去7个交易日（不含今天）
    past_days = [d for d in trading_days if d < today][:7]
    if len(past_days) < 2:
        return {"error": "历史数据不足", "stocks": []}

    yesterday = past_days[0]

    # 过去7天每只股票的数据
    placeholders = ",".join(["?"] * len(past_days))
    rows = conn.execute(f"""
        SELECT code, date, price, high, is_limit_up
        FROM snapshots
        WHERE date IN ({placeholders}) AND ts = '15:00:00'
    """, past_days).fetchall()

    # 按 code 聚合
    stock_history = {}  # code -> {dates, had_limit_up, yesterday_limit_up, max_high}
    for code, date, price, high, is_limit_up in rows:
        if code not in stock_history:
            stock_history[code] = {
                "had_limit_up": False,
                "yesterday_limit_up": False,
                "max_high": 0,
            }
        s = stock_history[code]
        if is_limit_up:
            s["had_limit_up"] = True
        if date == yesterday and is_limit_up:
            s["yesterday_limit_up"] = True
        if high and high > s["max_high"]:
            s["max_high"] = high

    # 筛选
    results = []
    for code, hist in stock_history.items():
        if not hist["had_limit_up"]:
            continue
        if hist["yesterday_limit_up"]:
            continue
        if code not in opening:
            continue

        today_open = opening[code]["open_price"]
        if today_open <= 0:
            continue

        if today_open >= hist["max_high"]:
            info = opening[code]
            results.append({
                "code": code,
                "name": info["name"],
                "open_price": today_open,
                "open_pctChg": info["pctChg"],
                "past_7d_high": hist["max_high"],
                "sector": info["sector"],
                "star": bool(info["star"]),
                "in_pool": bool(info["in_pool"]),
            })

    results.sort(key=lambda x: (-int(x["in_pool"]), -int(x["star"]), -x["open_pctChg"]))
    return {"ts": ts, "count": len(results), "stocks": results}


def analyze_sector_summary(conn, today, trading_days):
    """
    板块总结：
    - 哪些板块批量高开/低开
    - ⭐辨识度股票权重更高
    """
    opening, ts = get_today_opening(conn, today)
    if not opening:
        return {"error": "今日无开盘数据", "sectors": []}

    # 按板块聚合（仅股票池内）
    sector_data = {}
    for code, info in opening.items():
        if not info["in_pool"] or not info["sector"]:
            continue
        sector = info["sector"]
        if sector not in sector_data:
            sector_data[sector] = {"stocks": [], "star_stocks": []}
        sector_data[sector]["stocks"].append(info)
        if info["star"]:
            sector_data[sector]["star_stocks"].append(info)

    sectors = []
    for sector, data in sector_data.items():
        stocks = data["stocks"]
        star_stocks = data["star_stocks"]

        avg_pct = sum(s["pctChg"] for s in stocks) / len(stocks) if stocks else 0

        # 加权平均（⭐股权重2倍）
        total_weight = 0
        weighted_sum = 0
        for s in stocks:
            w = 2 if s["star"] else 1
            weighted_sum += s["pctChg"] * w
            total_weight += w
        weighted_avg = weighted_sum / total_weight if total_weight > 0 else 0

        high_open = [s for s in stocks if s["pctChg"] > 1]
        low_open = [s for s in stocks if s["pctChg"] < -1]

        leader = max(stocks, key=lambda x: x["pctChg"])

        sectors.append({
            "sector": sector,
            "avg_pctChg": round(avg_pct, 2),
            "weighted_avg_pctChg": round(weighted_avg, 2),
            "total": len(stocks),
            "high_open_count": len(high_open),
            "low_open_count": len(low_open),
            "star_count": len(star_stocks),
            "star_avg_pct": round(sum(s["pctChg"] for s in star_stocks) / len(star_stocks), 2) if star_stocks else 0,
            "leader": {"name": leader["name"], "code": leader["code"], "pctChg": leader["pctChg"]},
            "stocks": [{"name": s["name"], "code": s["code"], "pctChg": s["pctChg"], "star": s["star"]}
                       for s in sorted(stocks, key=lambda x: -x["pctChg"])],
        })

    sectors.sort(key=lambda x: -x["weighted_avg_pctChg"])

    # 分类
    high_sectors = [s for s in sectors if s["weighted_avg_pctChg"] > 0.5]
    low_sectors = [s for s in sectors if s["weighted_avg_pctChg"] < -0.5]
    neutral_sectors = [s for s in sectors if -0.5 <= s["weighted_avg_pctChg"] <= 0.5]

    return {
        "ts": ts,
        "high_open_sectors": high_sectors,
        "low_open_sectors": low_sectors,
        "neutral_sectors": neutral_sectors,
    }


def analyze_broken_board_reversal(conn, today, trading_days):
    """
    断板反包：
    - 前天涨停
    - 昨天不涨停
    - 今天高开（pctChg > 1%）
    """
    past_days = [d for d in trading_days if d < today]
    if len(past_days) < 2:
        return {"error": "历史数据不足", "stocks": []}

    yesterday = past_days[0]
    day_before = past_days[1]

    opening, ts = get_today_opening(conn, today)
    if not opening:
        return {"error": "今日无开盘数据", "stocks": []}

    # 前天数据
    dby_data = get_daily_close(conn, day_before)
    # 昨天数据
    yd_data = get_daily_close(conn, yesterday)

    results = []
    for code, dby in dby_data.items():
        if not dby["is_limit_up"]:
            continue
        yd = yd_data.get(code)
        if not yd or yd["is_limit_up"]:
            continue
        op = opening.get(code)
        if not op:
            continue
        if op["pctChg"] <= 1:
            continue

        results.append({
            "code": code,
            "name": op["name"],
            "day_before_close": dby["price"],
            "yesterday_close": yd["price"],
            "yesterday_pctChg": yd["pctChg"],
            "today_open": op["open_price"],
            "today_open_pctChg": op["pctChg"],
            "sector": op["sector"],
            "star": bool(op["star"]),
            "in_pool": bool(op["in_pool"]),
        })

    results.sort(key=lambda x: (-int(x["in_pool"]), -x["today_open_pctChg"]))
    return {"ts": ts, "day_before": day_before, "yesterday": yesterday, "count": len(results), "stocks": results}


# ═══════════════════════════════════════════════════════════════
# 新闻加载
# ═══════════════════════════════════════════════════════════════

def load_recent_news(today, days=2):
    """加载近两天的新闻文件"""
    news_text = ""
    dt = datetime.strptime(today, "%Y-%m-%d")
    for i in range(days + 1):  # 今天 + 前两天
        d = (dt - timedelta(days=i)).strftime("%Y-%m-%d")
        news_path = os.path.join(DAILY_DIR, d, "新闻.md")
        if os.path.exists(news_path):
            content = Path(news_path).read_text()
            # 截取前3000字（避免太长）
            if len(content) > 3000:
                content = content[:3000] + "\n...(截断)"
            news_text += f"\n--- {d} 新闻 ---\n{content}\n"

        # 也检查事件催化
        catalyst_path = os.path.join(DAILY_DIR, d, "事件催化.md")
        if os.path.exists(catalyst_path):
            content = Path(catalyst_path).read_text()
            if len(content) > 2000:
                content = content[:2000] + "\n...(截断)"
            news_text += f"\n--- {d} 事件催化 ---\n{content}\n"

    return news_text if news_text else "（无近期新闻数据）"


# ═══════════════════════════════════════════════════════════════
# AI 分析
# ═══════════════════════════════════════════════════════════════

def call_deepseek(system_prompt, user_prompt):
    """调用火山引擎 DeepSeek"""
    if not DEEPSEEK_API_KEY:
        return "（未配置 ARK_API_KEY，无法调用 AI）"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 3000,
        "temperature": 0.3,
    }

    try:
        resp = requests.post(DEEPSEEK_ENDPOINT, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"（AI 调用失败: {e}）"


def ai_analyze(gap_up_data, sector_data, broken_board_data, news_text):
    """用 DeepSeek 综合分析"""
    system_prompt = """你是一位资深 A 股短线分析师，擅长集合竞价阶段的盘面解读。
请根据以下数据，给出简洁有力的开盘分析报告。

分析要求：
1. 重点关注⭐辨识度股票和池内(in_pool)股票
2. 板块分析要结合新闻找到高开的原因
3. 语言简洁，直奔要害
4. 用 Markdown 格式输出"""

    user_content = f"""# 今日开盘数据（9:25 集合竞价）

## 一、高开过顶
定义：过去7天有涨停 + 昨天不是涨停 + 今日开盘价创7日新高

{json.dumps(gap_up_data, ensure_ascii=False, indent=2)}

## 二、板块总结
定义：按板块聚合开盘涨跌幅，⭐股权重2倍

### 高开板块
{json.dumps(sector_data.get("high_open_sectors", []), ensure_ascii=False, indent=2)}

### 低开板块
{json.dumps(sector_data.get("low_open_sectors", []), ensure_ascii=False, indent=2)}

### 中性板块
{json.dumps(sector_data.get("neutral_sectors", []), ensure_ascii=False, indent=2)}

## 三、断板反包
定义：前天涨停 + 昨天不涨停 + 今天高开(>1%)

{json.dumps(broken_board_data, ensure_ascii=False, indent=2)}

## 四、近期新闻参考
{news_text}

---

请输出三个板块的分析（高开过顶、板块总结、断板反包），每块简要分析核心要点。
板块总结中要明确指出哪些新闻/事件可能导致了板块的高开或低开。"""

    return call_deepseek(system_prompt, user_content)


# ═══════════════════════════════════════════════════════════════
# 飞书推送
# ═══════════════════════════════════════════════════════════════

def get_feishu_token():
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10,
    )
    return resp.json().get("tenant_access_token", "")


def send_feishu(text):
    token = get_feishu_token()
    if not token:
        print("[WARN] 飞书 token 获取失败", file=sys.stderr)
        return

    # 分片发送（飞书单条限制约4000字符）
    MAX_LEN = 3800
    parts = []
    while text:
        if len(text) <= MAX_LEN:
            parts.append(text)
            break
        # 在 MAX_LEN 前找最后一个换行
        cut = text[:MAX_LEN].rfind("\n")
        if cut < 100:
            cut = MAX_LEN
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")

    for part in parts:
        requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=union_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": FEISHU_UNION_ID,
                "msg_type": "interactive",
                "content": json.dumps({
                    "type": "template",
                    "data": {
                        "template_id": "AAqkZcBRbHJKx",
                        "template_variable": {"content": part},
                    },
                }),
            },
            timeout=10,
        )


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def main():
    dry_run = "--dry-run" in sys.argv

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"开盘分析: {today}", flush=True)

    # 1. 先拉取今日行情快照
    print("拉取今日行情...", flush=True)
    os.system(f"python3 {os.path.join(os.path.dirname(__file__), 'intraday_data.py')} pull")

    # 2. 连接数据库
    if not os.path.exists(DB_PATH):
        print("错误: 数据库不存在", file=sys.stderr)
        sys.exit(1)

    conn = get_conn()
    trading_days = get_trading_days(conn, 10)
    print(f"可用交易日: {trading_days}", flush=True)

    # 3. 运行三个分析模块
    print("分析: 高开过顶...", flush=True)
    gap_up = analyze_gap_up_over_top(conn, today, trading_days)
    print(f"  → {gap_up.get('count', 0)} 只", flush=True)

    print("分析: 板块总结...", flush=True)
    sectors = analyze_sector_summary(conn, today, trading_days)
    high_count = len(sectors.get("high_open_sectors", []))
    low_count = len(sectors.get("low_open_sectors", []))
    print(f"  → 高开{high_count}板块, 低开{low_count}板块", flush=True)

    print("分析: 断板反包...", flush=True)
    broken = analyze_broken_board_reversal(conn, today, trading_days)
    print(f"  → {broken.get('count', 0)} 只", flush=True)

    conn.close()

    if dry_run:
        output = {
            "today": today,
            "gap_up_over_top": gap_up,
            "sector_summary": sectors,
            "broken_board_reversal": broken,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    # 4. 加载新闻
    print("加载近期新闻...", flush=True)
    news_text = load_recent_news(today)

    # 5. AI 分析
    print("调用 AI 分析...", flush=True)
    report = ai_analyze(gap_up, sectors, broken, news_text)
    print(report, flush=True)

    # 6. 构建完整报告
    header = f"# 开盘分析 ({today} 9:25)\n\n"

    # 原始数据摘要
    data_summary = ""
    if gap_up.get("stocks"):
        data_summary += "## 高开过顶原始数据\n"
        for s in gap_up["stocks"][:10]:
            star = "⭐" if s["star"] else ""
            pool = "🏊" if s["in_pool"] else ""
            data_summary += f"- {star}{pool}**{s['name']}**({s['code']}) 开盘{s['open_pctChg']:+.2f}% 7日高{s['past_7d_high']}\n"
        data_summary += "\n"

    if broken.get("stocks"):
        data_summary += "## 断板反包原始数据\n"
        for s in broken["stocks"][:10]:
            star = "⭐" if s["star"] else ""
            pool = "🏊" if s["in_pool"] else ""
            data_summary += f"- {star}{pool}**{s['name']}**({s['code']}) 昨{s['yesterday_pctChg']:+.2f}% → 今开{s['today_open_pctChg']:+.2f}%\n"
        data_summary += "\n"

    full_report = header + report + "\n\n---\n" + data_summary

    # 7. 推送飞书
    print("推送飞书...", flush=True)
    send_feishu(full_report)
    print("完成!", flush=True)


if __name__ == "__main__":
    main()
