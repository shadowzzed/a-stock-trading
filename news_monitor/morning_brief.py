#!/usr/bin/env python3
"""早报生成模块 — A 股部分

用法:
    python -m news_monitor morning_brief                # 生成并发送
    python -m news_monitor morning_brief --dry          # 仅打印不发
    python -m news_monitor morning_brief --no-us        # 跳过美股部分

数据流:
    morning_brief_pool (盘后新闻入池) → 粗筛 Top 30 → LLM 精排 Top 12 +
    一句话开盘启示 → 飞书 Markdown 输出
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

# 项目根加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from config import get_ai_providers, get_config
from news_monitor.news_monitor import (
    get_news_db,
    send_feishu,
    track_tokens,
    log_error,
    _write_heartbeat,
)

# AI 提供商（DeepSeek 优先，与 news_monitor 保持一致）
AI_PROVIDERS = sorted(
    get_ai_providers(),
    key=lambda p: {"DeepSeek": 0, "Grok": 1, "GLM": 2}.get(p["name"], 9),
)

# ── 粗筛参数 ──
COARSE_TOP_N = 30          # 粗筛保留 30 条
FINAL_TOP_N = 12           # LLM 精排保留 12 条
MIN_POOL_FOR_BRIEF = 5     # 池中少于 5 条则视为"无新闻"

# 优先级权重（粗筛打分用）
PRIORITY_SCORE = {
    "earnings": 5,         # 财报最高优先级
    "supply_demand": 4,    # 供需变动
    "research": 3,         # 研报
    "geopolitics": 4,      # 地缘
    "": 1,                 # 无优先级
}

# 事件类型权重（与优先级叠加）
EVENT_TYPE_SCORE = {
    "业绩预增": 5, "业绩预减": 5, "业绩快报": 4, "暴雷": 5, "扭亏": 4,
    "首次覆盖": 4, "目标价上调": 4, "评级上调": 3, "评级下调": 3,
    "减产": 4, "扩产": 3, "停产": 4, "限产": 4, "涨价": 3, "降价": 3,
    "并购": 4, "重大合同": 4, "中标": 3, "新产品": 3,
    "制裁": 5, "战争": 5, "禁令": 4, "关税": 4,
    "监管处罚": 4, "调查": 3,
}


def fetch_brief_pool_since(hours_back: int = 18) -> list[dict]:
    """从早报候选池读取最近 N 小时的未使用新闻。

    默认 18 小时，覆盖前一交易日 15:00 收盘 ~ 当日 9:00。
    """
    db = get_news_db()
    cutoff = (datetime.now() - timedelta(hours=hours_back)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.execute(
        """
        SELECT id, source, title, brief, interpretation, priority, event_type,
               plates, stocks, url, news_time, created_at
        FROM morning_brief_pool
        WHERE used_in_brief = 0 AND created_at >= ?
        ORDER BY created_at DESC
        """,
        (cutoff,),
    ).fetchall()

    items = []
    for r in rows:
        try:
            plates = json.loads(r[7] or "[]")
        except Exception:
            plates = []
        try:
            stocks = json.loads(r[8] or "[]")
        except Exception:
            stocks = []
        items.append({
            "id": r[0],
            "source": r[1],
            "title": r[2] or "",
            "brief": r[3] or "",
            "interpretation": r[4] or "",
            "priority": r[5] or "",
            "event_type": r[6] or "",
            "plates": plates,
            "stocks": stocks,
            "url": r[9] or "",
            "news_time": r[10] or "",
            "created_at": r[11] or "",
        })
    return items


def coarse_rank(items: list[dict], top_n: int = COARSE_TOP_N) -> list[dict]:
    """粗筛打分：优先级 + 事件类型 + 是否有个股标记 + 时效性。"""
    def score(item):
        s = PRIORITY_SCORE.get(item.get("priority", ""), 1)
        s += EVENT_TYPE_SCORE.get(item.get("event_type", ""), 0)
        if item.get("stocks"):
            s += 2  # 有具体个股标记加分
        if item.get("plates"):
            s += 1  # 有板块标记加分
        # 时效性：越新越优（最近 6h 加分）
        try:
            created = datetime.fromisoformat(item["created_at"].replace(" ", "T"))
            age_h = (datetime.now() - created).total_seconds() / 3600
            if age_h < 3:
                s += 2
            elif age_h < 6:
                s += 1
        except Exception:
            pass
        # 解读长度（过短的解读说明 AI 没有提取出实质内容）
        if len(item.get("interpretation", "")) >= 30:
            s += 1
        return s

    scored = sorted(items, key=score, reverse=True)
    return scored[:top_n]


def llm_final_rank(items: list[dict]) -> tuple[list[int], str]:
    """LLM 精排：从 30 条选 Top 12 + 生成一句话开盘启示。

    返回 (selected_indices, opening_hint)。如果 LLM 全部失败，fallback 用粗筛前 12。
    """
    if not items:
        return [], ""

    # 构造输入
    lines = []
    for i, it in enumerate(items, 1):
        tags = []
        if it.get("priority"):
            pri_map = {"earnings": "财报", "supply_demand": "供需",
                       "research": "研报", "geopolitics": "地缘"}
            tags.append("[%s]" % pri_map.get(it["priority"], it["priority"]))
        if it.get("event_type"):
            tags.append("[%s]" % it["event_type"])
        for p in (it.get("plates", []) or [])[:2]:
            tags.append("[%s]" % p)
        for s in (it.get("stocks", []) or [])[:2]:
            tags.append("[%s]" % s.split("(")[0].strip())
        tag_str = "".join(tags)
        title = it.get("title", "").strip()
        interp = (it.get("interpretation") or "").replace("\n", " ").strip()[:120]
        lines.append("%d. %s %s\n   解读：%s" % (i, tag_str, title, interp))
    summaries_text = "\n\n".join(lines)

    prompt = """你是 A 股短线交易员的早报编辑。以下是昨夜+今晨的财经新闻列表（已带优先级、事件类型、板块、个股标签）。

任务：
1. 选出对今日 A 股开盘最可能产生影响的 Top 12 条
2. 优先选择：业绩/财报、政策/监管、产业链供需、巨头研报评级、地缘事件
3. 同质化新闻（同一事件多个来源）只保留 1 条
4. 输出一句"开盘启示"，30 字内，给短线交易员的方向感（不是预测，是值得关注的信号）

输出 JSON（严格 JSON 格式，不要 markdown 代码块）：
{
  "ranked_ids": [3, 1, 7, ...],   // 选中的新闻编号，最多 12 个
  "opening_hint": "一句开盘启示"
}"""

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
                            {"role": "user", "content": summaries_text},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 800,
                        "response_format": {"type": "json_object"},
                    },
                    timeout=90,
                )
                data = resp.json()
                track_tokens(data.get("usage"), 0)
                _write_heartbeat()
                content = data["choices"][0]["message"]["content"].strip()
                # 清理可能的 markdown 包裹
                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                    content = content.rsplit("```", 1)[0].strip()
                obj = json.loads(content)
                ranked_ids = obj.get("ranked_ids", []) or []
                opening_hint = obj.get("opening_hint", "") or ""
                # 1-based → 0-based 转换 + 校验
                indices = [i - 1 for i in ranked_ids if isinstance(i, int) and 1 <= i <= len(items)]
                indices = indices[:FINAL_TOP_N]
                print("  [早报精排:%s] 完成，选出 %d 条" % (provider["name"], len(indices)), flush=True)
                return indices, opening_hint
            except Exception as e:
                _write_heartbeat()
                if attempt < 1:
                    print("  [早报精排:%s] 第%d次失败，重试: %s" % (provider["name"], attempt + 1, e), flush=True)
                    time.sleep(3)
                else:
                    print("  [早报精排:%s] 失败，切下一个: %s" % (provider["name"], e), flush=True)

    # 全部 LLM 失败 → fallback：直接取粗筛前 N 条
    log_error("早报精排", "所有 AI 提供商失败，fallback 到粗筛前 %d 条" % FINAL_TOP_N)
    return list(range(min(FINAL_TOP_N, len(items)))), ""


def format_a_share_section(picked_items: list[dict], opening_hint: str) -> str:
    """格式化 A 股部分为飞书 Markdown 文本。"""
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = ["## 🌅 A 股早报 · 重点新闻", ""]
    if opening_hint:
        parts.append("**💡 开盘启示**：%s" % opening_hint)
        parts.append("")

    for i, it in enumerate(picked_items, 1):
        # 标签
        tags = []
        pri_map = {"earnings": "📊财报", "supply_demand": "📦供需",
                   "research": "📝研报", "geopolitics": "🌐地缘"}
        if it.get("priority"):
            tags.append("`%s`" % pri_map.get(it["priority"], it["priority"]))
        if it.get("event_type"):
            tags.append("`%s`" % it["event_type"])
        plates = (it.get("plates") or [])[:2]
        for p in plates:
            tags.append("`%s`" % p)
        stocks = (it.get("stocks") or [])[:2]
        for s in stocks:
            stock_name = s.split("(")[0].strip()
            tags.append("`%s`" % stock_name)

        tag_str = " ".join(tags)
        title = it.get("title", "").strip()

        parts.append("**%d.** %s %s" % (i, tag_str, title))

        # 解读
        interp = (it.get("interpretation") or "").strip()
        if interp:
            # 取第一段或前 150 字
            lines_interp = [ln for ln in interp.split("\n") if ln.strip()]
            if lines_interp:
                first = lines_interp[0][:150]
                # 去掉重复的标签行
                if not any(t in first for t in ["板块：", "个股：", "标签："]):
                    parts.append("> %s" % first)

        # 来源
        source = it.get("source", "")
        url = it.get("url", "")
        if url:
            parts.append("> _来源：%s · [原文](%s)_" % (source, url))
        else:
            parts.append("> _来源：%s_" % source)
        parts.append("")

    return "\n".join(parts)


def mark_used(item_ids: list[int]) -> int:
    """标记已用于早报的新闻。"""
    if not item_ids:
        return 0
    db = get_news_db()
    placeholders = ",".join(["?"] * len(item_ids))
    db.execute(
        "UPDATE morning_brief_pool SET used_in_brief = 1 WHERE id IN (%s)" % placeholders,
        item_ids,
    )
    db.commit()
    return len(item_ids)


def generate_a_share_brief(dry_run: bool = False) -> tuple[str, list[int]]:
    """生成 A 股早报正文。

    返回 (markdown_text, used_item_ids)；调用方负责发送 + 标记已用。
    """
    raw_items = fetch_brief_pool_since(hours_back=18)
    print("[早报] 候选池：%d 条" % len(raw_items), flush=True)

    if len(raw_items) < MIN_POOL_FOR_BRIEF:
        # 池太空，可能是 News Monitor 没在跑
        msg = "## 🌅 A 股早报 · 重点新闻\n\n_候选池仅 %d 条新闻（< %d），可能数据采集异常，请检查 news_monitor 进程_" % (
            len(raw_items), MIN_POOL_FOR_BRIEF)
        return msg, []

    coarse = coarse_rank(raw_items, top_n=COARSE_TOP_N)
    print("[早报] 粗筛：%d 条" % len(coarse), flush=True)

    indices, opening_hint = llm_final_rank(coarse)
    picked = [coarse[i] for i in indices]
    print("[早报] 精排：%d 条 | 启示：%s" % (len(picked), opening_hint[:30]), flush=True)

    text = format_a_share_section(picked, opening_hint)
    used_ids = [it["id"] for it in picked]
    return text, used_ids


def main():
    parser = argparse.ArgumentParser(description="早报 A 股部分（独立测试入口）")
    parser.add_argument("--dry", action="store_true", help="不发送，只打印")
    args = parser.parse_args()

    text, used_ids = generate_a_share_brief()

    print("\n========== A 股早报 ==========")
    print(text)
    print("==============================\n")

    if not args.dry:
        if send_feishu(text):
            mark_used(used_ids)
            print("[早报] ✅ 已发送 + 标记 %d 条已用" % len(used_ids), flush=True)
        else:
            print("[早报] ❌ 发送失败", flush=True)


if __name__ == "__main__":
    main()
