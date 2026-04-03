#!/usr/bin/env python3
"""
冷启动脚本 — 回填历史数据

步骤：
1. 初始化数据库表
2. 对已有 news 表记录批量生成 embedding
3. 计算历史新闻影响（需要 intraday 快照数据覆盖）

用法：
  python -m news_monitor.impact.bootstrap           # 全量回填
  python -m news_monitor.impact.bootstrap --embed    # 仅回填 embedding
  python -m news_monitor.impact.bootstrap --impact   # 仅计算影响
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from news_monitor.impact import db
from news_monitor.impact.embed import encode_batch, is_available as embed_available, get_model_info
from news_monitor.impact.calc import batch_calc_impacts


def step_init():
    """Step 1: 初始化数据库表"""
    print("\n" + "=" * 60, flush=True)
    print("Step 1: 初始化数据库表", flush=True)
    print("=" * 60, flush=True)
    db.init_tables()
    print("✅ 数据库表初始化完成", flush=True)


def step_embed():
    """Step 2: 对已有 news 批量生成 embedding"""
    print("\n" + "=" * 60, flush=True)
    print("Step 2: 批量生成新闻 Embedding", flush=True)
    print("=" * 60, flush=True)

    info = get_model_info()
    print("模型信息: %s" % info, flush=True)

    if not embed_available():
        print("❌ Embedding 模型不可用，跳过", flush=True)
        return 0

    # 获取未编码的新闻
    unembedded = db.get_unembedded_news_ids()
    if not unembedded:
        print("✅ 所有新闻已有 embedding，无需处理", flush=True)
        return 0

    print("待处理: %d 条新闻" % len(unembedded), flush=True)

    # 分批处理
    batch_size = 64
    total = 0
    t0 = time.time()

    for start in range(0, len(unembedded), batch_size):
        batch = unembedded[start:start + batch_size]

        texts = []
        ids = []
        for news_id, title, brief, interp in batch:
            text = title or ""
            if brief:
                text += "。 " + brief
            if interp:
                # 取解读的第一行
                first_line = (interp or "").split("\n")[0].strip()
                if first_line and first_line.startswith("解读"):
                    text += "。 " + first_line
            texts.append(text[:500])  # 截断避免过长
            ids.append(news_id)

        vecs = encode_batch(texts, show_progress=False)
        if vecs is not None:
            from news_monitor.impact.embed import _model_version
            records = [(ids[i], vecs[i], _model_version) for i in range(len(ids))]
            db.save_embeddings_batch(records)
            total += len(records)

        elapsed = time.time() - t0
        print("  进度 %d/%d（%.1f 条/秒）" % (
            min(start + batch_size, len(unembedded)), len(unembedded),
            total / max(elapsed, 0.1)), flush=True)

    elapsed = time.time() - t0
    print("✅ Embedding 完成: %d 条，耗时 %.1fs" % (total, elapsed), flush=True)
    return total


def step_impact():
    """Step 3: 计算历史新闻影响"""
    print("\n" + "=" * 60, flush=True)
    print("Step 3: 计算历史新闻影响", flush=True)
    print("=" * 60, flush=True)

    # 检查快照数据可用性
    dates = db.get_available_snapshot_dates()
    if not dates:
        print("⚠️ 无盘中快照数据（需要先运行 intraday_data.py pull 采集数据）", flush=True)
        return 0

    print("可用快照日期: %s ~ %s（%d 天）" % (dates[-1], dates[0], len(dates)), flush=True)

    count = batch_calc_impacts(limit=5000)
    print("✅ 影响计算完成: %d 条记录" % count, flush=True)
    return count


def main():
    import argparse
    parser = argparse.ArgumentParser(description="新闻影响分析冷启动")
    parser.add_argument("--embed", action="store_true", help="仅回填 embedding")
    parser.add_argument("--impact", action="store_true", help="仅计算影响")
    args = parser.parse_args()

    print("🚀 新闻影响分析冷启动", flush=True)

    t0 = time.time()

    if args.embed:
        step_init()
        step_embed()
    elif args.impact:
        step_init()
        step_impact()
    else:
        step_init()
        step_embed()
        step_impact()

    elapsed = time.time() - t0
    print("\n⏱️ 总耗时: %.1fs" % elapsed, flush=True)


if __name__ == "__main__":
    main()
