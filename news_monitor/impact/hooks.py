"""
与 news_monitor.py 的集成钩子

在现有新闻处理管线中插入影响分析：
新闻入库 → AI解读 → 【影响分析】 → 优先级分类 → 推送通知

仅对盘中高优先级新闻触发
超时保护：embedding + 检索 < 3s，否则跳过
"""

import json
import time

from . import db
from .embed import encode_single, is_available as embed_available
from .search import analyze_news_impact


def on_news_saved(news_id, title, brief="", interpretation=""):
    """新闻入库后的钩子 — 生成 embedding（异步友好）

    Args:
        news_id: 新闻 ID
        title: 新闻标题
        brief: 新闻摘要
        interpretation: AI 解读文本

    Returns:
        bool — 是否成功生成 embedding
    """
    if not embed_available():
        return False

    text = title
    if brief:
        text = title + "。 " + brief
    if interpretation:
        text += "。 " + interpretation.split("\n")[0]  # 只取第一行解读

    vec = encode_single(text)
    if vec is None:
        return False

    db.save_embedding(news_id, vec)
    return True


def on_high_priority_news(title, brief="", timeout_sec=3.0):
    """高优先级新闻的实时影响分析钩子

    Args:
        title: 新闻标题
        brief: 新闻摘要
        timeout_sec: 超时保护（秒）

    Returns:
        str — Markdown 格式的影响报告（空字符串表示跳过或无数据）
    """
    if not embed_available():
        return ""

    t0 = time.time()
    try:
        report = analyze_news_impact(title, brief, top_k=10, timeout_sec=timeout_sec)
        elapsed = time.time() - t0
        if report:
            print("  [Impact] 影响分析完成（%.1fs）" % elapsed, flush=True)
        return report
    except Exception as e:
        print("  [Impact] 影响分析异常: %s" % e, flush=True)
        return ""


def on_batch_news_saved(items_with_ids):
    """批量新闻入库后 — 批量生成 embedding

    Args:
        items_with_ids: [(news_id, title, brief, interpretation), ...]

    Returns:
        int — 成功生成 embedding 的数量
    """
    if not embed_available():
        return 0

    from .embed import encode_batch

    texts = []
    valid_items = []
    for news_id, title, brief, interp in items_with_ids:
        text = title
        if brief:
            text += "。 " + brief
        if interp:
            text += "。 " + interp.split("\n")[0]
        texts.append(text)
        valid_items.append(news_id)

    if not texts:
        return 0

    vecs = encode_batch(texts, show_progress=False)
    if vecs is None:
        return 0

    from .embed import _model_version
    records = [
        (news_id, vecs[i], _model_version)
        for i, news_id in enumerate(valid_items)
    ]
    db.save_embeddings_batch(records)
    return len(records)
