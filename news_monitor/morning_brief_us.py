#!/usr/bin/env python3
"""早报美股部分 — 板块异动 + 明星股 + 动态发现大涨主题

数据流:
    yfinance 拉 19 个板块/主题 ETF + 14 只基础明星股最近 10 天日线
    → 计算昨夜涨跌幅 → 找出涨幅 > 1.5% 板块（"昨夜大涨主题"）
    → 取大涨板块代表股表现 → LLM 综合输出
    → A 股映射启示

用法:
    python -m news_monitor morning_brief_us       # 仅打印不发
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from config import get_ai_providers
from news_monitor.news_monitor import (
    track_tokens,
    log_error,
    _write_heartbeat,
)

AI_PROVIDERS = sorted(
    get_ai_providers(),
    key=lambda p: {"DeepSeek": 0, "Grok": 1, "GLM": 2}.get(p["name"], 9),
)

# ── 板块/主题 ETF 清单 ──
# (symbol, 中文名, [代表股 ticker]) — 涨幅显著时取代表股进早报
US_THEME_ETFS = [
    # 11 大 SPDR Sector
    ("XLK",  "科技",       ["NVDA", "MSFT", "AAPL"]),
    ("XLF",  "金融",       ["JPM", "V", "MA"]),
    ("XLE",  "能源",       ["XOM", "CVX", "COP"]),
    ("XLV",  "医疗",       ["UNH", "JNJ", "LLY"]),
    ("XLI",  "工业",       ["CAT", "GE", "HON"]),
    ("XLY",  "可选消费",   ["AMZN", "TSLA", "HD"]),
    ("XLP",  "必选消费",   ["WMT", "PG", "KO"]),
    ("XLU",  "公用事业",   ["NEE", "DUK", "SO"]),
    ("XLB",  "原材料",     ["LIN", "FCX", "NEM"]),
    ("XLRE", "房地产",     ["AMT", "PLD", "EQIX"]),
    ("XLC",  "通讯",       ["GOOGL", "META", "DIS"]),
    # 主题 ETF（细分热点）
    ("ARKX", "商业航天",   ["RKLB", "LMT", "BA", "SPCE"]),
    ("SOXX", "半导体",     ["NVDA", "AVGO", "TSM"]),
    ("ICLN", "清洁能源",   ["FSLR", "ENPH", "NEE"]),
    ("XBI",  "生物科技",   ["MRNA", "GILD", "REGN"]),
    ("BOTZ", "机器人/AI",  ["NVDA", "ROK", "ISRG"]),
    ("KWEB", "中概互联",   ["BABA", "PDD", "JD", "BIDU"]),
    ("CIBR", "网络安全",   ["CRWD", "PANW", "ZS"]),
    ("TAN",  "太阳能",     ["FSLR", "ENPH", "SEDG"]),
]

# ── 基础明星股清单 ──
# 无论板块表现如何都纳入早报
US_STAR_STOCKS = [
    # 科技七巨头
    ("NVDA", "英伟达", "AI 芯片"),
    ("TSLA", "特斯拉", "电动车"),
    ("AAPL", "苹果", "消费电子"),
    ("MSFT", "微软", "云/AI"),
    ("GOOG", "Google", "云/AI"),
    ("META", "Meta", "社交/AI"),
    ("AMZN", "亚马逊", "电商/云"),
    # 中概股
    ("BABA", "阿里巴巴", "电商"),
    ("PDD",  "拼多多", "电商"),
    ("JD",   "京东", "电商"),
    ("BIDU", "百度", "AI"),
    # 半导体（A 股映射重要）
    ("TSM",  "台积电", "半导体代工"),
    ("ASML", "阿斯麦", "光刻机"),
    ("AMD",  "AMD", "AI 芯片"),
]

# ── 阈值 ──
HOT_SECTOR_THRESHOLD = 1.5    # 涨幅 > 1.5% 视为板块异动
COOL_SECTOR_THRESHOLD = -1.5  # 跌幅 > 1.5% 视为板块异动
STAR_HIGHLIGHT_PCT = 3.0      # 涨幅 > 3% 视为明星股亮点
STAR_DROP_PCT = -3.0          # 跌幅 > 3% 视为明星股下跌


def fetch_yf_quote(symbol: str, retries: int = 2) -> Optional[dict]:
    """拉单只 symbol 的最近交易日数据。

    返回 {symbol, date, close, prev_close, pct, volume} 或 None。
    """
    import yfinance as yf
    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(symbol)
            h = t.history(period="10d", interval="1d", auto_adjust=False)
            if h.empty or len(h) < 2:
                if attempt < retries:
                    time.sleep(1)
                    continue
                return None
            last = h.iloc[-1]
            prev = h.iloc[-2]
            return {
                "symbol": symbol,
                "date": h.index[-1].strftime("%Y-%m-%d"),
                "close": float(last["Close"]),
                "prev_close": float(prev["Close"]),
                "pct": (float(last["Close"]) - float(prev["Close"])) / float(prev["Close"]) * 100,
                "volume": int(last["Volume"]),
            }
        except Exception as e:
            if attempt < retries:
                time.sleep(2)
                continue
            log_error("yfinance", "%s 拉取失败: %s" % (symbol, e))
            return None


def fetch_us_data() -> dict:
    """并发拉取所有 ETF + 明星股的最新数据。

    返回 {symbol: quote_dict}。失败的 symbol 不在 dict 中。
    """
    all_symbols = set()
    for etf, _, reps in US_THEME_ETFS:
        all_symbols.add(etf)
        all_symbols.update(reps)
    for sym, _, _ in US_STAR_STOCKS:
        all_symbols.add(sym)

    results = {}
    print("[美股早报] 拉取 %d 只 symbol..." % len(all_symbols), flush=True)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch_yf_quote, s): s for s in all_symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                q = fut.result()
                if q:
                    results[sym] = q
            except Exception as e:
                log_error("yfinance", "%s 任务异常: %s" % (sym, e))
    print("[美股早报] 成功拉到 %d/%d" % (len(results), len(all_symbols)), flush=True)
    return results


def find_hot_sectors(data: dict) -> tuple[list[dict], list[dict]]:
    """找出昨夜大涨/大跌的板块（按 ETF 涨跌幅）。"""
    hot, cool = [], []
    for etf, name, reps in US_THEME_ETFS:
        q = data.get(etf)
        if not q:
            continue
        pct = q["pct"]
        # 取代表股表现
        rep_data = []
        for r in reps:
            rq = data.get(r)
            if rq:
                rep_data.append({"symbol": r, "pct": rq["pct"], "close": rq["close"]})
        rep_data.sort(key=lambda x: x["pct"], reverse=True)

        sector_info = {
            "etf": etf,
            "name": name,
            "pct": pct,
            "close": q["close"],
            "reps": rep_data[:3],
        }
        if pct >= HOT_SECTOR_THRESHOLD:
            hot.append(sector_info)
        elif pct <= COOL_SECTOR_THRESHOLD:
            cool.append(sector_info)

    hot.sort(key=lambda x: x["pct"], reverse=True)
    cool.sort(key=lambda x: x["pct"])
    return hot, cool


def find_star_highlights(data: dict) -> tuple[list[dict], list[dict]]:
    """找出基础明星股的涨/跌亮点。"""
    rises, drops = [], []
    for sym, name, sector in US_STAR_STOCKS:
        q = data.get(sym)
        if not q:
            continue
        info = {
            "symbol": sym,
            "name": name,
            "sector": sector,
            "pct": q["pct"],
            "close": q["close"],
        }
        if q["pct"] >= STAR_HIGHLIGHT_PCT:
            rises.append(info)
        elif q["pct"] <= STAR_DROP_PCT:
            drops.append(info)

    rises.sort(key=lambda x: x["pct"], reverse=True)
    drops.sort(key=lambda x: x["pct"])
    return rises, drops


def llm_us_summary(hot: list, cool: list, star_rises: list, star_drops: list,
                   data: dict) -> str:
    """让 LLM 综合美股数据生成"对 A 股的启示"。

    返回纯文本（飞书 Markdown），失败时 fallback 到无 LLM 简化版。
    """
    # 构造 LLM 输入
    sections = []
    if hot:
        sec = "【🔥 昨夜大涨板块】\n"
        for h in hot[:5]:  # 最多 5 个
            sec += f"- {h['name']} ({h['etf']}) 涨 {h['pct']:+.2f}%"
            if h.get("reps"):
                rep_strs = [f"{r['symbol']} {r['pct']:+.1f}%" for r in h["reps"][:3]]
                sec += "，代表股：" + " / ".join(rep_strs)
            sec += "\n"
        sections.append(sec)
    if cool:
        sec = "【❄️ 昨夜大跌板块】\n"
        for c in cool[:3]:
            sec += f"- {c['name']} ({c['etf']}) 跌 {c['pct']:+.2f}%\n"
        sections.append(sec)
    if star_rises:
        sec = "【⭐ 明星股涨幅】\n"
        for s in star_rises[:6]:
            sec += f"- {s['name']} ({s['symbol']}, {s['sector']}) 涨 {s['pct']:+.2f}%\n"
        sections.append(sec)
    if star_drops:
        sec = "【⭐ 明星股跌幅】\n"
        for s in star_drops[:5]:
            sec += f"- {s['name']} ({s['symbol']}, {s['sector']}) 跌 {s['pct']:+.2f}%\n"
        sections.append(sec)

    if not sections:
        return "_昨夜美股板块无显著异动（涨跌幅均在 ±1.5% 内）_"

    raw_data = "\n\n".join(sections)

    prompt = """你是中美股票联动分析师，给 A 股短线交易员写早报。

输入是昨夜美股的板块异动 + 明星股表现。请输出（200-300 字）：

1. **板块异动**：哪几个板块/主题大涨大跌？背后的可能催化（基于代表股表现推断）
2. **明星股聚焦**：突出 1-3 只明星股变动，给一句简短分析
3. **A 股映射**：对今日 A 股开盘有哪些启示？哪些板块/概念可能联动（如美股半导体涨 → A 股半导体设备/材料；ARKX 商业航天 → A 股航天链；KWEB 中概 → A 股核心资产）
4. **风险提示**：1 句话提示需警惕的板块或宏观信号

要求：直接输出 Markdown，不要 JSON。简洁犀利，给短线交易员看。"""

    for provider in AI_PROVIDERS:
        for attempt in range(2):
            try:
                resp = requests.post(
                    "%s/chat/completions" % provider["base"],
                    headers={
                        "Authorization": "Bearer %s" % provider["key"],
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": provider["model"],
                        "messages": [
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": raw_data},
                        ],
                        "temperature": 0.4,
                        "max_tokens": 800,
                    },
                    timeout=90,
                )
                data = resp.json()
                track_tokens(data.get("usage"), 0)
                _write_heartbeat()
                content = data["choices"][0]["message"]["content"].strip()
                print("  [美股 LLM:%s] 完成" % provider["name"], flush=True)
                return content
            except Exception as e:
                _write_heartbeat()
                if attempt < 1:
                    print("  [美股 LLM:%s] 第%d次失败: %s" % (provider["name"], attempt + 1, e), flush=True)
                    time.sleep(3)
                else:
                    print("  [美股 LLM:%s] 失败: %s" % (provider["name"], e), flush=True)

    # 全部失败 → fallback：纯数据展示
    log_error("美股早报", "所有 AI 提供商失败，fallback 到纯数据展示")
    return raw_data + "\n\n_（AI 解读暂不可用，仅展示原始数据）_"


def format_us_section(data: dict, hot: list, cool: list,
                      star_rises: list, star_drops: list,
                      llm_summary: str) -> str:
    """组装美股部分到飞书 Markdown。"""
    if not data:
        return "## 🇺🇸 美股早报\n\n_数据拉取失败，请检查 yfinance 连接_"

    # 推断"昨夜"日期
    sample_q = next(iter(data.values()))
    last_date = sample_q.get("date", "")

    parts = [
        "## 🇺🇸 美股早报",
        f"_数据日期：{last_date}（最新交易日）_",
        "",
    ]

    if llm_summary:
        parts.append(llm_summary)
        parts.append("")

    # 板块异动一览
    if hot or cool:
        parts.append("---")
        parts.append("**📊 板块异动一览**")
        parts.append("")
        for h in hot[:8]:
            parts.append(f"- 🔥 **{h['name']}** ({h['etf']}) `{h['pct']:+.2f}%`")
        for c in cool[:5]:
            parts.append(f"- ❄️ **{c['name']}** ({c['etf']}) `{c['pct']:+.2f}%`")
        parts.append("")

    # 明星股一览（不论涨跌都列出来）
    parts.append("---")
    parts.append("**⭐ 明星股表现**")
    parts.append("")
    star_lines = []
    for sym, name, sector in US_STAR_STOCKS:
        q = data.get(sym)
        if q:
            pct = q["pct"]
            emoji = "🟢" if pct > 0 else ("🔴" if pct < 0 else "⚪")
            star_lines.append(f"{emoji} {name} {pct:+.2f}%")
    # 每行 3 只，紧凑展示
    for i in range(0, len(star_lines), 3):
        parts.append(" · ".join(star_lines[i:i+3]))
    parts.append("")

    return "\n".join(parts)


def generate_us_brief() -> str:
    """生成美股部分早报。"""
    data = fetch_us_data()
    if not data:
        return "## 🇺🇸 美股早报\n\n_yfinance 数据拉取失败_"

    hot, cool = find_hot_sectors(data)
    star_rises, star_drops = find_star_highlights(data)

    print(f"[美股早报] 大涨 {len(hot)} 板块 / 大跌 {len(cool)} 板块 / 明星股涨 {len(star_rises)} 跌 {len(star_drops)}", flush=True)

    summary = llm_us_summary(hot, cool, star_rises, star_drops, data)
    return format_us_section(data, hot, cool, star_rises, star_drops, summary)


def main():
    parser = argparse.ArgumentParser(description="早报美股部分（独立测试入口）")
    parser.add_argument("--dry", action="store_true", help="不发，只打印")
    args = parser.parse_args()

    text = generate_us_brief()
    print("\n========== 美股早报 ==========")
    print(text)
    print("==============================\n")


if __name__ == "__main__":
    main()
