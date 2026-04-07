#!/usr/bin/env python3
"""ExpeL 批量经验蒸馏 CLI

用法:
    # 对单次回测蒸馏
    python -m backtest.distill ~/shared/backtest/20260405_v1.0.0/

    # 合并多次回测蒸馏
    python -m backtest.distill ~/shared/backtest/202604*_v1.0.0/

    # 合并回测 + 日常报告蒸馏
    python -m backtest.distill ~/shared/backtest/202604*/ ~/shared/trading/daily_reports/

    # 自动导入蒸馏出的规则
    python -m backtest.distill ~/shared/backtest/20260405_v1.0.0/ --auto-import
"""

from __future__ import annotations

import argparse
import glob
import os

from .experience.store import ExperienceStore
from .experience.distill import ExperienceDistiller


def main():
    parser = argparse.ArgumentParser(description="ExpeL 批量经验蒸馏")
    parser.add_argument("paths", nargs="+",
                        help="回测输出目录 或 日常报告目录（支持通配符）")
    parser.add_argument("--store",
                        default=os.path.expanduser("~/shared/trading/backtest/experience_store.json"),
                        help="经验库文件路径")
    parser.add_argument("--auto-import", action="store_true",
                        help="自动将新规则导入经验库")
    parser.add_argument("--output",
                        help="蒸馏报告输出目录（默认与第一个输入目录相同）")
    parser.add_argument("--min-group", type=int, default=6,
                        help="每组最少交易笔数（默认 6）")
    parser.add_argument("--version",
                        help="Agent 版本号（默认自动检测）",
                        default="")
    args = parser.parse_args()

    # 展开通配符
    data_dirs = []
    for p in args.paths:
        expanded = glob.glob(os.path.expanduser(p))
        data_dirs.extend(d for d in expanded if os.path.isdir(d))

    if not data_dirs:
        print("未找到有效的数据目录")
        return

    print(f"蒸馏数据源: {len(data_dirs)} 个目录")
    for d in data_dirs:
        print(f"  - {d}")

    # 版本号
    version = args.version
    if not version:
        try:
            from trading_agent.version import get_version
            version = get_version()
        except ImportError:
            version = "unknown"

    # 加载经验库
    store = ExperienceStore(args.store)
    print(f"经验库: {args.store} ({len(store.experiences)} 条已有经验)")

    # 蒸馏
    distiller = ExperienceDistiller(store)
    report = distiller.distill(
        data_dirs=data_dirs,
        min_group_size=args.min_group,
        agent_version=version,
        auto_import=args.auto_import,
    )

    # 保存报告
    output_dir = args.output or data_dirs[0]
    md_path = distiller.save_report(report, output_dir)

    print(f"\n蒸馏完成:")
    print(f"  总交易: {report.total_trades} 笔")
    print(f"  分析分组: {report.groups_analyzed} 组")
    print(f"  新规则: {len(report.new_rules)} 条")
    print(f"  强化已有: {len(report.reinforced)} 条")
    print(f"  报告: {md_path}")


if __name__ == "__main__":
    main()
