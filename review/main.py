"""CLI 入口"""

import argparse
import json
import os
import sys


def _load_prompt_overrides(path):
    """加载 prompt 覆盖配置文件

    支持 JSON 格式：
    {
        "sentiment_analyst": "额外关注北向资金数据",
        "leader_analyst": "重点分析龙头的封单变化",
        "bull": "更激进一些"
    }
    """
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def cli():
    parser = argparse.ArgumentParser(description="A股短线多Agent复盘系统")
    parser.add_argument("date", help="分析日期，如 2026-03-24")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="trading 数据目录",
    )
    parser.add_argument("--debug", action="store_true", help="打印中间过程")
    parser.add_argument("--model", help="覆盖默认模型")
    parser.add_argument("--base-url", help="覆盖 API base URL")
    parser.add_argument("--output", "-o", help="输出文件路径（默认打印到 stdout）")
    parser.add_argument("--max-rounds", type=int, default=1, help="多空辩论轮数")
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="交互模式：AI 出初步报告后等待你终审",
    )
    parser.add_argument(
        "--review",
        help="直接传入终审反馈文本（非交互式终审，用于脚本调用）",
    )
    parser.add_argument(
        "--review-file",
        help="从文件读取终审反馈",
    )
    parser.add_argument(
        "--prompt-override",
        action="append",
        metavar="AGENT=INSTRUCTION",
        help="单独调教某个 Agent，如 --prompt-override 'leader_analyst=重点关注封单变化'",
    )
    parser.add_argument(
        "--prompt-config",
        help="Agent prompt 覆盖配置文件（JSON），默认读取 data-dir 下的 agent_prompts.json",
    )

    args = parser.parse_args()

    if args.data_dir is None:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from config import get_config
        args.data_dir = get_config()["data_root"]

    # 加载 prompt 覆盖
    prompt_overrides = {}
    # 优先读配置文件
    config_path = args.prompt_config or os.path.join(args.data_dir, "agent_prompts.json")
    prompt_overrides.update(_load_prompt_overrides(config_path))
    # 命令行参数覆盖配置文件
    if args.prompt_override:
        for override in args.prompt_override:
            if "=" in override:
                agent_name, instruction = override.split("=", 1)
                prompt_overrides[agent_name.strip()] = instruction.strip()

    config = {
        "max_debate_rounds": args.max_rounds,
        "prompt_overrides": prompt_overrides,
    }
    if args.model:
        config["model"] = args.model
    if args.base_url:
        config["base_url"] = args.base_url

    from .graph import run, run_with_review, apply_review

    need_review = args.interactive or args.review or args.review_file

    if need_review:
        # 两阶段模式
        result = run_with_review(
            data_dir=args.data_dir,
            date=args.date,
            config=config,
            debug=args.debug,
        )

        print("=" * 60)
        print("AI 初步复盘报告")
        print("=" * 60)
        print(result["report"])
        print("=" * 60)

        # 获取终审反馈
        feedback = None

        if args.review:
            feedback = args.review
        elif args.review_file:
            with open(args.review_file, "r", encoding="utf-8") as f:
                feedback = f.read()
        elif args.interactive:
            print("\n请输入你的终审反馈（输入空行后按 Ctrl+D 结束）：")
            try:
                lines = []
                while True:
                    line = input()
                    lines.append(line)
            except EOFError:
                pass
            feedback = "\n".join(lines).strip()

        if feedback:
            print("\n正在根据你的反馈修订报告...\n")
            report = apply_review(result["state"], feedback, config)
        else:
            print("\n无反馈，使用 AI 初版报告。")
            report = result["report"]
    else:
        # 直通模式
        report = run(
            data_dir=args.data_dir,
            date=args.date,
            config=config,
            debug=args.debug,
        )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print("报告已保存到 {}".format(args.output))
    elif need_review:
        print("\n" + "=" * 60)
        print("终审后报告")
        print("=" * 60)
        print(report)
    else:
        print(report)


if __name__ == "__main__":
    cli()
