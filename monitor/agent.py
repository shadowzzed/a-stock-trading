#!/usr/bin/env python3
"""
新闻监控 Agent

在 news_monitor.py 采集的新闻基础上，提供更深层的分析能力：
- 新闻解读：逐条判断对 A 股板块/个股的影响（已内嵌在 news_monitor.py 中）
- 事件催化提取：从当日新闻中提取对次日盘面有影响的事件
- 盘前简报：汇总隔夜新闻，生成盘前交易参考

用法:
    # 生成事件催化（盘后运行，分析当日新闻）
    python -m monitor.agent catalyst

    # 生成盘前简报（09:00 前运行）
    python -m monitor.agent briefing

    # 指定日期
    python -m monitor.agent catalyst --date 2026-03-31

    # 调试模式
    python -m monitor.agent catalyst --dry-run
"""

import argparse
import json
import os
import sys
from datetime import datetime

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 配置
AI_API_KEY = os.environ.get("ARK_API_KEY", "")
AI_API_BASE = os.environ.get("ARK_API_BASE", "https://ark.cn-beijing.volces.com/api/v3")
AI_MODEL = os.environ.get("ARK_MODEL", "")
DATA_DIR = os.environ.get("TRADING_DATA_DIR", PROJECT_ROOT)
DAILY_DIR = os.environ.get("TRADING_DAILY_DIR", os.path.join(DATA_DIR, "daily"))
STOCKS_FILE = os.environ.get("TRADING_STOCKS_FILE", os.path.join(DATA_DIR, "stocks.md"))


def load_prompt(name: str) -> str:
    """加载 prompt 文件"""
    path = os.path.join(os.path.dirname(__file__), "prompts", "%s.md" % name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_file(path: str) -> str:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def call_ai(system_prompt: str, user_prompt: str) -> str:
    """调用 AI API"""
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
    return resp.json()["choices"][0]["message"]["content"]


def run_catalyst(date: str, dry_run: bool = False):
    """从当日新闻提取事件催化"""
    news = load_file(os.path.join(DAILY_DIR, date, "新闻.md"))
    if not news:
        print("[WARN] 未找到 %s 的新闻文件" % date)
        return

    stocks = load_file(STOCKS_FILE)
    prompt = load_prompt("catalyst_extract")

    user_content = "今天是 %s。\n\n## 今日新闻\n\n%s" % (date, news[:8000])
    if stocks:
        user_content += "\n\n## 股票池\n\n%s" % stocks[:3000]

    if dry_run:
        print("[SYSTEM]\n%s\n\n[USER]\n%s" % (prompt[:300], user_content[:500]))
        return

    print("[%s] 提取事件催化..." % datetime.now().strftime("%H:%M:%S"))
    result = call_ai(prompt, user_content)

    output_path = os.path.join(DAILY_DIR, date, "事件催化.md")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result)

    print("[%s] 已保存: %s" % (datetime.now().strftime("%H:%M:%S"), output_path))
    print(result)
    return result


def run_briefing(date: str, dry_run: bool = False):
    """生成盘前新闻简报"""
    # 读取今日和昨日新闻
    news_today = load_file(os.path.join(DAILY_DIR, date, "新闻.md"))
    catalyst = load_file(os.path.join(DAILY_DIR, date, "事件催化.md"))
    stocks = load_file(STOCKS_FILE)

    prompt = load_prompt("morning_briefing")

    user_content = "今天是 %s。\n\n" % date
    if news_today:
        user_content += "## 新闻\n\n%s\n\n" % news_today[:6000]
    if catalyst:
        user_content += "## 事件催化\n\n%s\n\n" % catalyst
    if stocks:
        user_content += "## 股票池\n\n%s" % stocks[:3000]

    if dry_run:
        print("[SYSTEM]\n%s\n\n[USER]\n%s" % (prompt[:300], user_content[:500]))
        return

    print("[%s] 生成盘前简报..." % datetime.now().strftime("%H:%M:%S"))
    result = call_ai(prompt, user_content)

    output_path = os.path.join(DAILY_DIR, date, "盘前简报.md")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result)

    print("[%s] 已保存: %s" % (datetime.now().strftime("%H:%M:%S"), output_path))
    print(result)
    return result


def main():
    parser = argparse.ArgumentParser(description="新闻监控 Agent")
    parser.add_argument(
        "action",
        choices=["catalyst", "briefing"],
        help="catalyst=事件催化提取, briefing=盘前简报",
    )
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="日期")
    parser.add_argument("--dry-run", action="store_true", help="只看 prompt 不调 AI")

    args = parser.parse_args()

    if args.action == "catalyst":
        run_catalyst(args.date, args.dry_run)
    elif args.action == "briefing":
        run_briefing(args.date, args.dry_run)


if __name__ == "__main__":
    main()
