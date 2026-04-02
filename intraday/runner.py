#!/usr/bin/env python3
"""
盘中分析 Agent Runner

可独立运行，也可被 HappyClaw 等框架调度。
支持 OpenAI-compatible API（火山引擎 DeepSeek、OpenAI、Claude 等）。

用法:
    python -m intraday opening_analysis
    python -m intraday early_session_analysis
    python -m intraday closing_review
    python -m intraday opening_analysis --dry-run
"""

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

from .config import get_config, load_prompt


def get_today():
    return datetime.now().strftime("%Y-%m-%d")


def get_trading_days(n=7):
    """获取最近 n 个交易日的日期列表（跳过周末）"""
    dates = []
    d = datetime.now()
    while len(dates) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
    return dates


def run_snapshot():
    """运行 intraday_data.py snapshot"""
    cfg = get_config()
    script = os.path.join(cfg["project_root"], "data", "intraday_data.py")
    if not os.path.exists(script):
        print("[WARN] intraday_data.py 不存在: %s" % script)
        return None

    result = subprocess.run(
        [sys.executable, "-u", script, "snapshot"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print("[ERROR] snapshot 失败: %s" % result.stderr[:500])
        return None
    print("[OK] snapshot 完成")
    return result.stdout


def load_file(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def find_recent_files(daily_dir, filename, days=2):
    """查找最近 N 天的文件"""
    results = []
    for date in get_trading_days(days + 5)[:days * 2]:
        path = os.path.join(daily_dir, date, filename)
        if os.path.exists(path):
            results.append((date, load_file(path)))
            if len(results) >= days:
                break
    return results


def call_ai(system_prompt: str, user_prompt: str) -> str:
    """调用 AI API（OpenAI-compatible）"""
    import requests

    cfg = get_config()
    if not cfg["ai_api_key"]:
        raise ValueError("ARK_API_KEY 未设置")
    if not cfg["ai_model"]:
        raise ValueError("ARK_MODEL 未设置")

    resp = requests.post(
        "%s/chat/completions" % cfg["ai_api_base"],
        headers={
            "Authorization": "Bearer %s" % cfg["ai_api_key"],
            "Content-Type": "application/json",
        },
        json={
            "model": cfg["ai_model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 4096,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def build_context(agent_name: str, today: str) -> str:
    """构建 Agent 所需的上下文数据"""
    cfg = get_config()
    daily_dir = cfg["daily_dir"]
    sections = []

    if agent_name == "opening_analysis":
        stocks = load_file(cfg["stocks_file"])
        if stocks:
            sections.append("## 股票池\n\n" + stocks)

        # 最近涨跌停 CSV
        for date in get_trading_days(4)[:2]:
            day_dir = os.path.join(daily_dir, date)
            if not os.path.exists(day_dir):
                continue
            for f in os.listdir(day_dir):
                if f.startswith("涨停板_") and f.endswith(".csv"):
                    sections.append("## 涨停板 %s\n\n```csv\n%s\n```" % (date, load_file(os.path.join(day_dir, f))))
                if f.startswith("跌停板_") and f.endswith(".csv"):
                    sections.append("## 跌停板 %s\n\n```csv\n%s\n```" % (date, load_file(os.path.join(day_dir, f))))

        for date, content in find_recent_files(daily_dir, "新闻.md", 2):
            sections.append("## 新闻 %s\n\n%s" % (date, content[:3000]))
        for date, content in find_recent_files(daily_dir, "事件催化.md", 2):
            sections.append("## 事件催化 %s\n\n%s" % (date, content))

    elif agent_name == "early_session_analysis":
        opening = load_file(os.path.join(daily_dir, today, "开盘分析.md"))
        if opening:
            sections.append("## 今日开盘分析报告\n\n" + opening)

        stocks = load_file(cfg["stocks_file"])
        if stocks:
            sections.append("## 股票池\n\n" + stocks)

        # 加载 review_docs/ 目录下所有 .md 文件
        for date in get_trading_days(4)[:2]:
            review_dir = os.path.join(daily_dir, date, "review_docs")
            if os.path.isdir(review_dir):
                for f in sorted(os.listdir(review_dir)):
                    if f.endswith(".md"):
                        name = f[:-3]
                        content = load_file(os.path.join(review_dir, f))
                        if content:
                            sections.append("## %s %s\n\n%s" % (name, date, content[:2000]))

        news = load_file(os.path.join(daily_dir, today, "新闻.md"))
        if news:
            sections.append("## 今日新闻\n\n" + news[:3000])

    elif agent_name == "closing_review":
        day_dir = os.path.join(daily_dir, today)
        if os.path.exists(day_dir):
            files = os.listdir(day_dir)
            sections.append("## 今日已有文件\n\n" + "\n".join("- " + f for f in sorted(files)))
            market_data = load_file(os.path.join(day_dir, "行情数据.md"))
            if market_data:
                sections.append("## 行情数据\n\n" + market_data[:5000])

    return "\n\n---\n\n".join(sections) if sections else "（无可用数据）"


def run_agent(agent_name: str, output_path: str = None, dry_run: bool = False):
    """运行指定的盘中分析 Agent"""
    cfg = get_config()
    today = get_today()

    print("[%s] 启动 %s Agent..." % (datetime.now().strftime("%H:%M:%S"), agent_name))

    if agent_name in ("opening_analysis", "early_session_analysis"):
        run_snapshot()

    prompt = load_prompt(agent_name)
    context = build_context(agent_name, today)

    system_prompt = prompt
    user_prompt = "今天是 %s。以下是可用数据：\n\n%s" % (today, context)

    if dry_run:
        print("=" * 60)
        print("[SYSTEM PROMPT]\n%s..." % system_prompt[:500])
        print("=" * 60)
        print("[USER PROMPT]\n%s..." % user_prompt[:1000])
        return

    print("[%s] 调用 AI 生成报告..." % datetime.now().strftime("%H:%M:%S"))
    report = call_ai(system_prompt, user_prompt)

    output_names = {
        "opening_analysis": "开盘分析.md",
        "early_session_analysis": "早盘机会分析.md",
        "closing_review": "收盘复盘.md",
    }

    if not output_path:
        day_dir = os.path.join(cfg["daily_dir"], today)
        os.makedirs(day_dir, exist_ok=True)
        output_path = os.path.join(day_dir, output_names.get(agent_name, "%s.md" % agent_name))

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)

    print("[%s] 报告已保存: %s" % (datetime.now().strftime("%H:%M:%S"), output_path))
    print(report)
    return report


def main():
    # 初始化数据目录
    from config import init_data_dirs
    init_data_dirs()

    parser = argparse.ArgumentParser(description="盘中分析 Agent Runner")
    parser.add_argument(
        "agent",
        choices=["opening_analysis", "early_session_analysis", "closing_review"],
        help="要运行的 Agent",
    )
    parser.add_argument("-o", "--output", help="报告输出路径")
    parser.add_argument("--dry-run", action="store_true", help="只构建 prompt，不调用 AI")

    args = parser.parse_args()
    run_agent(agent_name=args.agent, output_path=args.output, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
