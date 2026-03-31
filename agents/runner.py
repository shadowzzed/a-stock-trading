#!/usr/bin/env python3
"""
盘中分析 Agent Runner

可独立运行，也可被 HappyClaw 等框架调度。
支持 OpenAI-compatible API（火山引擎 DeepSeek、OpenAI、Claude 等）。

用法:
    # 运行开盘分析
    python -m agents.runner opening_analysis

    # 运行早盘机会分析
    python -m agents.runner early_session_analysis

    # 运行收盘复盘准备
    python -m agents.runner closing_review

    # 指定数据目录
    python -m agents.runner opening_analysis --data-dir /path/to/trading

    # 输出到文件
    python -m agents.runner opening_analysis -o report.md
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

from .config import (
    DATA_DIR, SCRIPTS_DIR, DAILY_DIR, INTRADAY_DB, STOCKS_FILE,
    AI_API_KEY, AI_API_BASE, AI_MODEL,
    load_prompt,
)


def get_today():
    """获取今天日期"""
    return datetime.now().strftime("%Y-%m-%d")


def get_trading_days(n=7):
    """获取最近 n 个交易日的日期列表（简化版：跳过周末）"""
    dates = []
    d = datetime.now()
    while len(dates) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # 周一到周五
            dates.append(d.strftime("%Y-%m-%d"))
    return dates


def run_snapshot(scripts_dir=None):
    """运行 intraday_data.py snapshot 拉取实时数据"""
    scripts_dir = scripts_dir or SCRIPTS_DIR
    script = os.path.join(scripts_dir, "intraday_data.py")
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


def load_stocks(stocks_file=None):
    """加载股票池"""
    stocks_file = stocks_file or STOCKS_FILE
    if not os.path.exists(stocks_file):
        return ""
    with open(stocks_file, "r", encoding="utf-8") as f:
        return f.read()


def load_daily_file(daily_dir, date, filename):
    """加载当日数据文件"""
    path = os.path.join(daily_dir or DAILY_DIR, date, filename)
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def find_recent_files(daily_dir, filename, days=2):
    """查找最近 N 天的文件"""
    daily_dir = daily_dir or DAILY_DIR
    results = []
    for date in get_trading_days(days + 5)[:days * 2]:
        path = os.path.join(daily_dir, date, filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                results.append((date, f.read()))
            if len(results) >= days:
                break
    return results


def query_intraday_db(sql, db_path=None):
    """查询 intraday.db"""
    db_path = db_path or INTRADAY_DB
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def call_ai(system_prompt: str, user_prompt: str) -> str:
    """调用 AI API（OpenAI-compatible）"""
    import requests

    if not AI_API_KEY:
        raise ValueError("ARK_API_KEY 未设置")
    if not AI_MODEL:
        raise ValueError("ARK_MODEL 未设置")

    resp = requests.post(
        "%s/chat/completions" % AI_API_BASE,
        headers={
            "Authorization": "Bearer %s" % AI_API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "model": AI_MODEL,
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
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def build_context(agent_name: str, today: str, daily_dir=None, scripts_dir=None) -> str:
    """构建 Agent 所需的上下文数据"""
    daily_dir = daily_dir or DAILY_DIR
    sections = []

    if agent_name == "opening_analysis":
        # 股票池
        stocks = load_stocks()
        if stocks:
            sections.append("## 股票池\n\n" + stocks)

        # 最近涨跌停 CSV
        for date, content in find_recent_files(daily_dir, "涨停板_*.csv", 2):
            # 找实际文件名
            day_dir = os.path.join(daily_dir, date)
            for f in os.listdir(day_dir):
                if f.startswith("涨停板_") and f.endswith(".csv"):
                    path = os.path.join(day_dir, f)
                    with open(path, "r", encoding="utf-8") as fh:
                        sections.append("## 涨停板 %s\n\n```csv\n%s\n```" % (date, fh.read()))
                if f.startswith("跌停板_") and f.endswith(".csv"):
                    path = os.path.join(day_dir, f)
                    with open(path, "r", encoding="utf-8") as fh:
                        sections.append("## 跌停板 %s\n\n```csv\n%s\n```" % (date, fh.read()))

        # 新闻和事件催化
        for date, content in find_recent_files(daily_dir, "新闻.md", 2):
            sections.append("## 新闻 %s\n\n%s" % (date, content[:3000]))
        for date, content in find_recent_files(daily_dir, "事件催化.md", 2):
            sections.append("## 事件催化 %s\n\n%s" % (date, content))

    elif agent_name == "early_session_analysis":
        # 今日开盘分析
        opening = load_daily_file(daily_dir, today, "开盘分析.md")
        if opening:
            sections.append("## 今日开盘分析报告\n\n" + opening)

        # 股票池
        stocks = load_stocks()
        if stocks:
            sections.append("## 股票池\n\n" + stocks)

        # 昨日复盘
        for date, content in find_recent_files(daily_dir, "阿意复盘.md", 1):
            sections.append("## 阿意复盘 %s\n\n%s" % (date, content[:2000]))
        for date, content in find_recent_files(daily_dir, "刺客复盘.md", 1):
            sections.append("## 刺客复盘 %s\n\n%s" % (date, content[:2000]))

        # 今日新闻
        news = load_daily_file(daily_dir, today, "新闻.md")
        if news:
            sections.append("## 今日新闻\n\n" + news[:3000])

    elif agent_name == "closing_review":
        # 检查收盘数据文件
        day_dir = os.path.join(daily_dir, today)
        if os.path.exists(day_dir):
            files = os.listdir(day_dir)
            sections.append("## 今日已有文件\n\n" + "\n".join("- " + f for f in sorted(files)))

            # 读取行情数据
            market_data = load_daily_file(daily_dir, today, "行情数据.md")
            if market_data:
                sections.append("## 行情数据\n\n" + market_data[:5000])

    return "\n\n---\n\n".join(sections) if sections else "（无可用数据）"


def run_agent(agent_name: str, output_path: str = None, data_dir: str = None,
              scripts_dir: str = None, daily_dir: str = None, dry_run: bool = False):
    """运行指定的盘中分析 Agent

    Args:
        agent_name: opening_analysis | early_session_analysis | closing_review
        output_path: 报告输出路径（默认保存到 daily 目录）
        data_dir: 数据根目录
        scripts_dir: 脚本目录
        daily_dir: 每日数据目录
        dry_run: 只构建 prompt 不调用 AI
    """
    today = get_today()
    daily_dir = daily_dir or DAILY_DIR
    scripts_dir = scripts_dir or SCRIPTS_DIR

    print("[%s] 启动 %s Agent..." % (datetime.now().strftime("%H:%M:%S"), agent_name))

    # 1. 拉取快照（开盘分析和早盘分析需要）
    if agent_name in ("opening_analysis", "early_session_analysis"):
        run_snapshot(scripts_dir)

    # 2. 加载 prompt
    prompt = load_prompt(
        agent_name,
        data_dir=data_dir or DATA_DIR,
        scripts_dir=scripts_dir,
        daily_dir=daily_dir,
    )

    # 3. 构建上下文
    context = build_context(agent_name, today, daily_dir, scripts_dir)

    # 4. 组装完整 prompt
    system_prompt = prompt
    user_prompt = "今天是 %s。以下是可用数据：\n\n%s" % (today, context)

    if dry_run:
        print("=" * 60)
        print("[SYSTEM PROMPT]")
        print(system_prompt[:500] + "...")
        print("=" * 60)
        print("[USER PROMPT]")
        print(user_prompt[:1000] + "...")
        print("=" * 60)
        return

    # 5. 调用 AI
    print("[%s] 调用 AI 生成报告..." % datetime.now().strftime("%H:%M:%S"))
    report = call_ai(system_prompt, user_prompt)

    # 6. 保存报告
    output_names = {
        "opening_analysis": "开盘分析.md",
        "early_session_analysis": "早盘机会分析.md",
        "closing_review": "收盘复盘.md",
    }

    if not output_path:
        day_dir = os.path.join(daily_dir, today)
        os.makedirs(day_dir, exist_ok=True)
        output_path = os.path.join(day_dir, output_names.get(agent_name, "%s.md" % agent_name))

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)

    print("[%s] 报告已保存: %s" % (datetime.now().strftime("%H:%M:%S"), output_path))
    print(report)
    return report


def main():
    parser = argparse.ArgumentParser(description="盘中分析 Agent Runner")
    parser.add_argument(
        "agent",
        choices=["opening_analysis", "early_session_analysis", "closing_review"],
        help="要运行的 Agent",
    )
    parser.add_argument("--data-dir", default=DATA_DIR, help="数据根目录")
    parser.add_argument("--scripts-dir", default=SCRIPTS_DIR, help="脚本目录")
    parser.add_argument("--daily-dir", default=DAILY_DIR, help="每日数据目录")
    parser.add_argument("-o", "--output", help="报告输出路径")
    parser.add_argument("--dry-run", action="store_true", help="只构建 prompt，不调用 AI")

    args = parser.parse_args()

    run_agent(
        agent_name=args.agent,
        output_path=args.output,
        data_dir=args.data_dir,
        scripts_dir=args.scripts_dir,
        daily_dir=args.daily_dir,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
