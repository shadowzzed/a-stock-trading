#!/usr/bin/env python3
"""经验自动导入 CLI

用法:
    # 预览将导入哪些经验
    python -m backtest.import_experience ~/shared/backtest/20260405_120000/ --dry-run

    # 交互式导入（逐条确认）
    python -m backtest.import_experience ~/shared/backtest/20260405_120000/

    # 全自动导入（流水线中使用）
    python -m backtest.import_experience ~/shared/backtest/20260405_120000/ --auto

    # 指定经验库路径
    python -m backtest.import_experience ~/shared/backtest/20260405_120000/ --store ~/shared/trading/backtest/experience_store.json
"""

from __future__ import annotations

import argparse
import os
import sys

from .experience.store import ExperienceStore
from .experience.auto_import import ExperienceAutoImporter


def main():
    parser = argparse.ArgumentParser(description="回测经验自动导入")
    parser.add_argument("path", help="回测输出目录 或 经验总结.json 路径")
    parser.add_argument("--auto", action="store_true",
                        help="全自动导入（不需要确认）")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览模式（只显示将导入的经验，不实际写入）")
    parser.add_argument("--store", help="经验库文件路径",
                        default=os.path.expanduser("~/shared/trading/backtest/experience_store.json"))
    parser.add_argument("--min-confidence", type=float, default=0.3,
                        help="最低置信度阈值（默认 0.3）")
    parser.add_argument("--version", help="Agent 版本号（标记在导入的经验中）",
                        default="")
    args = parser.parse_args()

    # 加载经验库
    store = ExperienceStore(args.store)
    print(f"经验库: {args.store} ({len(store.all_experiences)} 条已有经验)")

    importer = ExperienceAutoImporter(store)

    # 预览模式
    if args.dry_run:
        path = args.path
        if os.path.isdir(path):
            path = os.path.join(path, "经验总结.json")
        candidates = importer.dry_run(path, min_confidence=args.min_confidence)
        if not candidates:
            print("无可导入的经验")
            return

        print(f"\n将导入 {len(candidates)} 条经验：")
        print("-" * 60)
        for i, exp in enumerate(candidates, 1):
            print(f"{i}. [{exp.error_type}] {exp.lesson[:80]}")
            print(f"   修正规则: {exp.correction_rule[:80]}")
            print(f"   置信度: {exp.confidence:.2f}")
            print()
        return

    # 导入
    if os.path.isdir(args.path):
        stats = importer.import_from_backtest_dir(
            args.path,
            min_confidence=args.min_confidence,
            auto_approve=args.auto,
            agent_version=args.version,
        )
    else:
        stats = importer.import_from_review(
            args.path,
            min_confidence=args.min_confidence,
            auto_approve=args.auto,
            agent_version=args.version,
        )

    print(stats)


if __name__ == "__main__":
    main()
