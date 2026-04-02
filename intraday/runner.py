#!/usr/bin/env python3
"""
盘中分析 Agent Runner

Thin wrapper: delegates opening_analysis and early_session_analysis to graph.py.
Only closing_review runs locally (graph.py does not support it).

用法:
    python -m intraday opening_analysis
    python -m intraday early_session_analysis
    python -m intraday closing_review
    python -m intraday opening_analysis --dry-run
"""

import os
from datetime import datetime

from config import get_config
from intraday.config import load_prompt


# ── closing_review (local) ──────────────────────────────────

def _get_today():
    return datetime.now().strftime("%Y-%m-%d")


def _load_file(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def _build_closing_context(cfg: dict, today: str) -> str:
    """构建收盘复盘的上下文数据"""
    daily_dir = cfg["daily_dir"]
    day_dir = os.path.join(daily_dir, today)
    sections = []

    if os.path.exists(day_dir):
        files = os.listdir(day_dir)
        sections.append("## 今日已有文件\n\n" + "\n".join("- " + f for f in sorted(files)))
        market_data = _load_file(os.path.join(day_dir, "行情数据.md"))
        if market_data:
            sections.append("## 行情数据\n\n" + market_data[:5000])

    return "\n\n---\n\n".join(sections) if sections else "（无可用数据）"


def _call_ai(system_prompt: str, user_prompt: str) -> str:
    """调用 AI API（复用 config.py 的多提供商 fallback）"""
    from config import get_ai_providers
    providers = get_ai_providers()
    if not providers:
        raise ValueError("未配置任何 AI 提供商（XAI_API_KEY 或 ARK_API_KEY）")

    import requests

    last_error = None
    for provider in providers:
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
        except Exception as e:
            print("[WARN] %s 调用失败: %s" % (provider["name"], e))
            last_error = e
    raise RuntimeError("所有 AI 提供商均失败: %s" % last_error)


def _run_closing_review(output_path: str = None, dry_run: bool = False, date: str = None):
    """运行收盘复盘 Agent（本地实现）"""
    cfg = get_config()
    today = date or _get_today()

    print("[%s] 启动 closing_review Agent..." % datetime.now().strftime("%H:%M:%S"))

    prompt = load_prompt("closing_review")
    context = _build_closing_context(cfg, today)

    system_prompt = prompt
    user_prompt = "今天是 %s。以下是可用数据：\n\n%s" % (today, context)

    if dry_run:
        print("=" * 60)
        print("[SYSTEM PROMPT]\n%s..." % system_prompt[:500])
        print("=" * 60)
        print("[USER PROMPT]\n%s..." % user_prompt[:1000])
        return

    print("[%s] 调用 AI 生成报告..." % datetime.now().strftime("%H:%M:%S"))
    report = _call_ai(system_prompt, user_prompt)

    if not output_path:
        day_dir = os.path.join(cfg["daily_dir"], today)
        os.makedirs(day_dir, exist_ok=True)
        output_path = os.path.join(day_dir, "收盘复盘.md")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)

    print("[%s] 报告已保存: %s" % (datetime.now().strftime("%H:%M:%S"), output_path))
    print(report)
    return report


# ── Unified entry point ─────────────────────────────────────

def run_agent(agent_name: str, output_path: str = None, dry_run: bool = False, date: str = None):
    """运行指定的盘中分析 Agent

    - opening_analysis / early_session_analysis -> 委托给 graph.py
    - closing_review -> 本地实现
    """
    if agent_name == "closing_review":
        return _run_closing_review(output_path=output_path, dry_run=dry_run, date=date)

    # 委托给 LangGraph 实现
    from intraday.graph import run
    return run(agent_name=agent_name, date=date, dry_run=dry_run)
