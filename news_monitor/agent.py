#!/usr/bin/env python3
"""
新闻监控 Agent

在 news_monitor.py 采集的新闻基础上，提供更深层的分析能力：
- 新闻解读：逐条判断对 A 股板块/个股的影响（已内嵌在 news_monitor.py 中）
- 事件催化提取：从当日新闻中提取对次日盘面有影响的事件
- 盘前简报：汇总隔夜新闻，生成盘前交易参考

用法:
    # 早报（每日 8:55 触发）— A 股精选 + 美股板块/明星股
    python -m news_monitor morning_brief
    python -m news_monitor morning_brief --dry         # 不发，仅打印
    python -m news_monitor morning_brief --no-us       # 跳过美股部分
    python -m news_monitor morning_brief --no-a        # 跳过 A 股部分

    # 生成事件催化（盘后运行，分析当日新闻）
    python -m news_monitor catalyst

    # 生成盘前简报（09:00 前运行）
    python -m news_monitor briefing

    # 指定日期
    python -m news_monitor catalyst --date 2026-03-31

    # 调试模式
    python -m news_monitor catalyst --dry-run
"""

import argparse
import json
import os
import sys
from datetime import datetime

# 全局配置
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_config


def load_prompt(name: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "prompts", "%s.md" % name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_file(path: str) -> str:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def call_ai(system_prompt: str, user_prompt: str) -> tuple:
    """调用 AI（Grok 优先，失败 fallback 到 DeepSeek），返回 (text, provider_name)"""
    import requests
    from config import get_ai_providers

    providers = get_ai_providers()
    if not providers:
        raise ValueError("未配置任何 AI 提供商（XAI_API_KEY 或 ARK_API_KEY）")

    for provider in providers:
        for attempt in range(3):
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
                text = resp.json()["choices"][0]["message"]["content"]
                print("  [AI:%s] 调用成功" % provider["name"], flush=True)
                return text, provider["name"]
            except Exception as e:
                if attempt < 2:
                    print("  [AI:%s] 第%d次失败，重试: %s" % (provider["name"], attempt + 1, e), flush=True)
                else:
                    print("  [AI:%s] 3次失败，切换下一个提供商" % provider["name"], flush=True)

    raise RuntimeError("所有 AI 提供商均调用失败")


def run_catalyst(date: str, dry_run: bool = False):
    """从当日新闻提取事件催化"""
    cfg = get_config()
    news = load_file(os.path.join(cfg["daily_dir"], date, "新闻.md"))
    if not news:
        print("[WARN] 未找到 %s 的新闻文件" % date)
        return

    stocks = load_file(cfg["stocks_file"])
    prompt = load_prompt("catalyst_extract")

    user_content = "今天是 %s。\n\n## 今日新闻\n\n%s" % (date, news[:8000])
    if stocks:
        user_content += "\n\n## 股票池\n\n%s" % stocks[:3000]

    if dry_run:
        print("[SYSTEM]\n%s\n\n[USER]\n%s" % (prompt[:300], user_content[:500]))
        return

    print("[%s] 提取事件催化..." % datetime.now().strftime("%H:%M:%S"))
    result, ai_provider = call_ai(prompt, user_content)

    output_path = os.path.join(cfg["daily_dir"], date, "事件催化.md")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result)
        f.write("\n\n`AI: %s`\n" % ai_provider)

    print("[%s] 已保存: %s (AI: %s)" % (datetime.now().strftime("%H:%M:%S"), output_path, ai_provider))
    print(result)
    return result


def run_briefing(date: str, dry_run: bool = False):
    """生成盘前新闻简报"""
    cfg = get_config()
    news_today = load_file(os.path.join(cfg["daily_dir"], date, "新闻.md"))
    catalyst = load_file(os.path.join(cfg["daily_dir"], date, "事件催化.md"))
    stocks = load_file(cfg["stocks_file"])

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
    result, ai_provider = call_ai(prompt, user_content)

    output_path = os.path.join(cfg["daily_dir"], date, "盘前简报.md")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result)
        f.write("\n\n`AI: %s`\n" % ai_provider)

    print("[%s] 已保存: %s (AI: %s)" % (datetime.now().strftime("%H:%M:%S"), output_path, ai_provider))
    print(result)
    return result


def main():
    parser = argparse.ArgumentParser(description="新闻监控 Agent")
    parser.add_argument("--no-us", action="store_true", help="(morning_brief) 跳过美股部分")
    parser.add_argument("--no-a", action="store_true", help="(morning_brief) 跳过 A 股部分")
    parser.add_argument("--dry", action="store_true", help="(morning_brief) 不发送，仅打印")
    parser.add_argument(
        "action",
        choices=["catalyst", "briefing", "morning_brief"],
        help="catalyst=事件催化提取, briefing=盘前简报, morning_brief=每日早报（A 股+美股）",
    )
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="日期")
    parser.add_argument("--dry-run", action="store_true", help="只看 prompt 不调 AI")

    args = parser.parse_args()

    if args.action == "catalyst":
        run_catalyst(args.date, args.dry_run)
    elif args.action == "briefing":
        run_briefing(args.date, args.dry_run)
    elif args.action == "morning_brief":
        run_morning_brief(dry=args.dry, no_us=args.no_us, no_a=args.no_a)


def run_morning_brief(dry: bool = False, no_us: bool = False, no_a: bool = False):
    """生成并发送早报：A 股精选（来自候选池） + 美股板块/明星股。

    8:55 LaunchAgent 触发，9:00 完成发送。失败 fallback 到无 LLM 简化版。
    """
    from news_monitor.morning_brief import generate_a_share_brief, mark_used
    from news_monitor.morning_brief_us import generate_us_brief
    from news_monitor.news_monitor import send_feishu

    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = [f"# 📰 早报 · {today}", ""]

    a_used_ids = []
    if not no_a:
        try:
            a_text, a_used_ids = generate_a_share_brief()
            parts.append(a_text)
        except Exception as e:
            print(f"[早报] A 股部分失败: {e}", flush=True)
            parts.append(f"## 🌅 A 股早报\n\n_生成失败：{e}_")
    parts.append("")

    if not no_us:
        try:
            us_text = generate_us_brief()
            parts.append(us_text)
        except Exception as e:
            print(f"[早报] 美股部分失败: {e}", flush=True)
            parts.append(f"## 🇺🇸 美股早报\n\n_生成失败：{e}_")
    parts.append("")
    parts.append("---")
    parts.append(f"_由 News Monitor 早报生成于 {today}_")

    final_text = "\n".join(parts)

    print("\n========== 完整早报 ==========")
    print(final_text)
    print("==============================\n")

    if dry:
        print("[早报] dry-run 模式，未发送", flush=True)
        return

    if send_feishu(final_text):
        if a_used_ids:
            mark_used(a_used_ids)
        print(f"[早报] ✅ 已发送，标记 {len(a_used_ids)} 条 A 股新闻已用", flush=True)
    else:
        print("[早报] ❌ 发送失败", flush=True)


if __name__ == "__main__":
    main()
