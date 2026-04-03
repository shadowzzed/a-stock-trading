"""
相似新闻检索与影响聚合

流程：
1. 对新新闻做 embedding
2. 向量检索 Top-K 相似历史新闻
3. 从 news_impacts 汇总历史影响数据
4. 输出量化影响评估报告
"""

import statistics
import sys
import time

from . import db
from .embed import encode_single, is_available as embed_available


def search_similar_news(title, brief="", top_k=10, threshold=0.5):
    """搜索与给定标题相似的历史新闻

    Args:
        title: 新闻标题
        brief: 新闻摘要（可选）
        top_k: 返回最多 K 条
        threshold: 相似度阈值（0-1）

    Returns:
        list of dicts — 相似新闻列表（含 impact 数据）
    """
    if not embed_available():
        return []

    text = title
    if brief:
        text = title + "。 " + brief

    vec = encode_single(text)
    if vec is None:
        return []

    similar = db.search_similar(vec, top_k=top_k, threshold=threshold)
    return similar


def aggregate_impacts(similar_news):
    """聚合相似历史新闻的影响数据

    Args:
        similar_news: search_similar_news 返回的结果

    Returns:
        dict — 聚合影响统计
    """
    if not similar_news:
        return None

    # 获取所有相似新闻的影响记录
    news_ids = [n["news_id"] for n in similar_news]
    impacts = db.get_impacts_for_news_ids(news_ids)

    if not impacts:
        return {
            "similar_count": len(similar_news),
            "has_impact_data": False,
            "top_similar": [
                {"title": n["title"], "similarity": n["similarity"], "date": n.get("news_date", "")}
                for n in similar_news[:5]
            ],
        }

    # 按字段聚合
    pct_fields = [
        "pct_5min", "pct_15min", "pct_30min", "pct_1h", "pct_2h", "pct_eod",
        "pct_next1d", "pct_next2d", "pct_next3d", "pct_next5d",
        "max_gain_pct", "max_loss_pct",
    ]

    stats = {}
    for field in pct_fields:
        values = [r[field] for r in impacts if r.get(field) is not None]
        if values:
            stats[field] = {
                "mean": round(statistics.mean(values), 2),
                "median": round(statistics.median(values), 2),
                "min": round(min(values), 2),
                "max": round(max(values), 2),
                "count": len(values),
            }

    # 量能比统计
    vol_ratios = [r["vol_ratio_1h"] for r in impacts if r.get("vol_ratio_1h") is not None]
    if vol_ratios:
        stats["vol_ratio_1h"] = {
            "mean": round(statistics.mean(vol_ratios), 2),
            "median": round(statistics.median(vol_ratios), 2),
            "count": len(vol_ratios),
        }

    # 恢复时间统计
    recovery = [r["recovery_minutes"] for r in impacts if r.get("recovery_minutes") is not None]
    if recovery:
        stats["recovery_minutes"] = {
            "mean": round(statistics.mean(recovery), 0),
            "median": round(statistics.median(recovery), 0),
            "min": min(recovery),
            "max": max(recovery),
            "count": len(recovery),
        }

    # 次日方向延续概率
    eod_vals = [r["pct_eod"] for r in impacts if r.get("pct_eod") is not None]
    next1d_vals = [r["pct_next1d"] for r in impacts if r.get("pct_next1d") is not None]

    continuation_prob = None
    if eod_vals and next1d_vals and len(eod_vals) == len(next1d_vals):
        same_dir = sum(
            1 for e, n in zip(eod_vals, next1d_vals)
            if (e > 0 and n > 0) or (e < 0 and n < 0)
        )
        continuation_prob = round(same_dir / len(eod_vals) * 100, 1)

    return {
        "similar_count": len(similar_news),
        "has_impact_data": True,
        "impact_sample_count": len(impacts),
        "stats": stats,
        "continuation_prob": continuation_prob,
        "top_similar": [
            {
                "title": n["title"],
                "similarity": n["similarity"],
                "date": n.get("news_date", ""),
            }
            for n in similar_news[:5]
        ],
    }


def format_impact_report(agg):
    """格式化影响评估报告（用于附加在推送消息中）

    Args:
        agg: aggregate_impacts 返回的统计结果

    Returns:
        str — Markdown 格式的报告片段
    """
    if not agg:
        return ""

    if not agg.get("has_impact_data"):
        count = agg.get("similar_count", 0)
        if count > 0:
            return "\n\n---\n📊 **历史相似事件**（%d 条相似新闻，暂无行情影响数据）" % count
        return ""

    count = agg["similar_count"]
    sample = agg["impact_sample_count"]
    stats = agg.get("stats", {})

    lines = ["\n\n---\n📊 **历史相似事件影响统计**（基于 %d 条相似新闻，%d 条影响样本）" % (count, sample)]

    # 盘中影响
    if "pct_eod" in stats:
        s = stats["pct_eod"]
        direction = "偏多" if s["mean"] > 0 else "偏空" if s["mean"] < 0 else "中性"
        lines.append("- 当日收盘平均：**%+.2f%%**（%s，范围 %+.2f%% ~ %+.2f%%）" % (
            s["mean"], direction, s["min"], s["max"]))

    if "max_gain_pct" in stats:
        s = stats["max_gain_pct"]
        lines.append("- 平均最大涨幅：**%+.2f%%**" % s["mean"])

    if "max_loss_pct" in stats:
        s = stats["max_loss_pct"]
        lines.append("- 平均最大跌幅：**%+.2f%%**" % s["mean"])

    # 持续时间
    if "recovery_minutes" in stats:
        s = stats["recovery_minutes"]
        if s["mean"] > 60:
            lines.append("- 影响持续：约 **%.0f 小时**" % (s["mean"] / 60))
        else:
            lines.append("- 影响持续：约 **%.0f 分钟**" % s["mean"])

    # 次日延续
    cp = agg.get("continuation_prob")
    if cp is not None:
        lines.append("- 次日延续概率：**%.0f%%** 继续同方向" % cp)

    # 量能
    if "vol_ratio_1h" in stats:
        s = stats["vol_ratio_1h"]
        lines.append("- 量能比（新闻后1h/前1h）：**%.1fx**" % s["mean"])

    return "\n".join(lines)


def analyze_news_impact(title, brief="", top_k=10, timeout_sec=3.0):
    """一站式影响分析：检索 + 聚合 + 格式化报告

    Args:
        title: 新闻标题
        brief: 新闻摘要
        top_k: 检索相似新闻数量
        timeout_sec: 超时保护（秒）

    Returns:
        str — Markdown 格式的影响报告（空字符串表示无数据或超时）
    """
    t0 = time.time()

    try:
        similar = search_similar_news(title, brief, top_k=top_k)
        if time.time() - t0 > timeout_sec:
            return ""

        agg = aggregate_impacts(similar)
        if time.time() - t0 > timeout_sec:
            return ""

        return format_impact_report(agg)

    except Exception as e:
        print("[Impact:Search] 影响分析失败: %s" % e, flush=True)
        return ""
