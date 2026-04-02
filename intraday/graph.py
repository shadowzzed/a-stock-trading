"""
盘中分析 LangGraph Agent

线性流水线：fetch_data → analyze → save_and_push

用法：
    python -m intraday opening_analysis
    python -m intraday early_session_analysis
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from langgraph.graph import StateGraph, START, END

# 确保项目根在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_config, init_data_dirs, get_ai_providers
from intraday.config import load_prompt
from intraday.state import IntradayState


# ── Node 1: fetch_data ────────────────────────────────────────

def fetch_data(state: IntradayState) -> dict:
    """从 SQLite + 文件系统拉取数据，组装上下文"""
    cfg = get_config()
    today = state["date"]
    agent = state["agent_name"]

    if agent == "opening_analysis":
        return _fetch_opening(cfg, today)
    elif agent == "early_session_analysis":
        return _fetch_early_session(cfg, today)
    else:
        return {"error": "未知 agent: %s" % agent, "context_text": ""}


def _fetch_opening(cfg: dict, today: str) -> dict:
    """开盘分析数据"""
    db_path = cfg["intraday_db"]
    if not os.path.exists(db_path):
        return {"error": "数据库不存在: %s" % db_path, "context_text": ""}

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        # 复用 tools/opening_analysis.py 的查询函数
        from tools.opening_analysis import (
            get_trading_days,
            analyze_gap_up_over_top,
            analyze_sector_summary,
            analyze_broken_board_reversal,
        )

        trading_days = get_trading_days(conn, 10)
        print("[fetch] 交易日: %s" % trading_days[:5], flush=True)

        gap_up = analyze_gap_up_over_top(conn, today, trading_days)
        print("[fetch] 高开过顶: %d 只" % gap_up.get("count", 0), flush=True)

        sectors = analyze_sector_summary(conn, today, trading_days)
        high_n = len(sectors.get("high_open_sectors", []))
        low_n = len(sectors.get("low_open_sectors", []))
        print("[fetch] 板块: 高开%d / 低开%d" % (high_n, low_n), flush=True)

        broken = analyze_broken_board_reversal(conn, today, trading_days)
        print("[fetch] 断板反包: %d 只" % broken.get("count", 0), flush=True)
    finally:
        conn.close()

    # 加载新闻
    news_text = _load_recent_news(cfg["daily_dir"], today)

    # 加载股票池
    stocks_text = _load_file(cfg["stocks_file"])

    # 组装上下文
    context_parts = []

    if stocks_text:
        context_parts.append("## 股票池\n\n" + stocks_text[:3000])

    context_parts.append("## 高开过顶数据\n\n```json\n%s\n```" %
                         json.dumps(gap_up, ensure_ascii=False, indent=2)[:4000])
    context_parts.append("## 板块总结数据\n\n```json\n%s\n```" %
                         json.dumps(sectors, ensure_ascii=False, indent=2)[:4000])
    context_parts.append("## 断板反包数据\n\n```json\n%s\n```" %
                         json.dumps(broken, ensure_ascii=False, indent=2)[:3000])

    # 加载涨跌停 CSV
    daily_dir = cfg["daily_dir"]
    for date in _recent_trading_days(today, 2):
        day_dir = os.path.join(daily_dir, date)
        if not os.path.isdir(day_dir):
            continue
        for f in sorted(os.listdir(day_dir)):
            if f.startswith("涨停板_") and f.endswith(".csv"):
                content = _load_file(os.path.join(day_dir, f))
                if content:
                    context_parts.append("## 涨停板 %s\n\n```csv\n%s\n```" % (date, content[:2000]))
            if f.startswith("跌停板_") and f.endswith(".csv"):
                content = _load_file(os.path.join(day_dir, f))
                if content:
                    context_parts.append("## 跌停板 %s\n\n```csv\n%s\n```" % (date, content[:2000]))

    if news_text:
        context_parts.append("## 近期新闻\n\n" + news_text[:3000])

    context_text = "\n\n---\n\n".join(context_parts)
    return {
        "context_text": context_text,
        "data_raw": {"gap_up": gap_up, "sectors": sectors, "broken": broken},
    }


def _fetch_early_session(cfg: dict, today: str) -> dict:
    """早盘机会分析数据"""
    daily_dir = cfg["daily_dir"]
    context_parts = []

    # 依赖开盘分析报告
    opening_report = _load_file(os.path.join(daily_dir, today, "开盘分析.md"))
    if opening_report:
        context_parts.append("## 今日开盘分析报告\n\n" + opening_report)
    else:
        print("[WARN] 开盘分析报告不存在，尝试从 DB 直接拉取数据", flush=True)
        # fallback: 直接查 09:25 和 09:40 数据
        db_path = cfg["intraday_db"]
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path, timeout=10)
            rows = conn.execute("""
                SELECT code, name, price, pctChg, open, high, low, last_close,
                       sector, star, in_pool, is_limit_up
                FROM snapshots WHERE date = ? AND ts IN ('09:26:35','09:41:34') AND in_pool = 1
                ORDER BY ts, pctChg DESC
            """, (today,)).fetchall()
            conn.close()
            if rows:
                context_parts.append("## 池内股票行情（09:25 + 09:40）\n")
                for r in rows[:50]:
                    context_parts.append(
                        "%s | %s | %.2f%% | sector=%s star=%d" %
                        (r[0], r[1], r[3], r[8], r[9])
                    )

    # 股票池
    stocks_text = _load_file(cfg["stocks_file"])
    if stocks_text:
        context_parts.append("## 股票池\n\n" + stocks_text[:3000])

    # 最近复盘（从 review_docs/ 目录加载所有 .md 文件）
    for date in _recent_trading_days(today, 2):
        review_dir = os.path.join(daily_dir, date, "review_docs")
        if os.path.isdir(review_dir):
            for f in sorted(os.listdir(review_dir)):
                if f.endswith(".md"):
                    name = f[:-3]  # 去掉 .md 后缀
                    content = _load_file(os.path.join(review_dir, f))
                    if content:
                        context_parts.append("## %s %s\n\n%s" % (name, date, content[:2000]))

    # 今日新闻
    news = _load_file(os.path.join(daily_dir, today, "新闻.md"))
    if news:
        context_parts.append("## 今日新闻\n\n" + news[:3000])

    # 涨跌停 CSV
    for date in _recent_trading_days(today, 2):
        day_dir = os.path.join(daily_dir, date)
        if not os.path.isdir(day_dir):
            continue
        for f in sorted(os.listdir(day_dir)):
            if f.startswith("涨停板_") and f.endswith(".csv"):
                content = _load_file(os.path.join(day_dir, f))
                if content:
                    context_parts.append("## 涨停板 %s\n\n```csv\n%s\n```" % (date, content[:2000]))

    context_text = "\n\n---\n\n".join(context_parts) if context_parts else "（无可用数据）"
    return {"context_text": context_text, "data_raw": {}}


# ── Node 2: analyze ────────────────────────────────────────────

def _create_llm(temperature: float = 0.3):
    """创建 LLM（Grok 优先，DeepSeek fallback）"""
    from langchain_openai import ChatOpenAI

    providers = get_ai_providers()
    if not providers:
        raise ValueError("未配置任何 AI 提供商（XAI_API_KEY 或 ARK_API_KEY）")

    primary = providers[0]
    llm = ChatOpenAI(
        model=primary["model"],
        base_url=primary["base"],
        api_key=primary["key"],
        temperature=temperature,
        max_tokens=4096,
    )

    if len(providers) > 1:
        fallbacks = []
        for p in providers[1:]:
            fallbacks.append(ChatOpenAI(
                model=p["model"],
                base_url=p["base"],
                api_key=p["key"],
                temperature=temperature,
                max_tokens=4096,
            ))
        llm = llm.with_fallbacks(fallbacks)
        print("[LLM] %s (fallback: %s)" % (
            primary["name"], ", ".join(p["name"] for p in providers[1:])), flush=True)
    else:
        print("[LLM] %s" % primary["name"], flush=True)

    return llm


def analyze(state: IntradayState) -> dict:
    """调用 AI 生成分析报告"""
    if state.get("dry_run"):
        print("[analyze] dry-run 模式，跳过 AI 调用", flush=True)
        print(state.get("context_text", "")[:2000])
        return {"report": "(dry run)", "ai_provider": "none"}

    prompt = load_prompt(state["agent_name"])
    llm = _create_llm()

    from langchain_core.messages import SystemMessage, HumanMessage

    today = state["date"]
    user_msg = "今天是 %s。以下是可用数据：\n\n%s" % (today, state["context_text"])

    print("[analyze] 调用 AI 生成报告...", flush=True)
    response = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=user_msg),
    ])

    return {"report": response.content, "ai_provider": "used"}


# ── Node 3: save_and_push ──────────────────────────────────────

def save_and_push(state: IntradayState) -> dict:
    """保存报告 + 推送飞书"""
    if state.get("dry_run"):
        return {"output_path": "", "feishu_sent": False}

    cfg = get_config()
    today = state["date"]
    agent = state["agent_name"]
    report = state.get("report", "")

    if not report:
        return {"output_path": "", "feishu_sent": False, "error": "无报告内容"}

    # 保存文件
    filenames = {
        "opening_analysis": "开盘分析.md",
        "early_session_analysis": "早盘机会分析.md",
    }
    day_dir = os.path.join(cfg["daily_dir"], today)
    os.makedirs(day_dir, exist_ok=True)
    output_path = os.path.join(day_dir, filenames[agent])

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)
    print("[save] 报告已保存: %s" % output_path, flush=True)

    # 推送飞书
    feishu_sent = _push_feishu(cfg, report)
    return {"output_path": output_path, "feishu_sent": feishu_sent}


# ── Feishu 推送 ────────────────────────────────────────────────

def _push_feishu(cfg: dict, text: str) -> bool:
    """通过飞书 Bot API 推送报告给用户"""
    import requests

    app_id = cfg.get("feishu_app_id", "")
    app_secret = cfg.get("feishu_app_secret", "")
    receive_id = cfg.get("feishu_receive_id", "")

    if not all([app_id, app_secret, receive_id]):
        print("[feishu] 未配置飞书 Bot 凭据，跳过推送", flush=True)
        return False

    # 获取 tenant_access_token
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        token = resp.json().get("tenant_access_token", "")
        if not token:
            print("[feishu] token 获取失败", flush=True)
            return False
    except Exception as e:
        print("[feishu] token 获取异常: %s" % e, flush=True)
        return False

    # 分片发送（飞书单条限制 ~4000 字符）
    MAX_LEN = 3800
    parts = _split_text(text, MAX_LEN)

    success = True
    for part in parts:
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=union_id",
                headers={
                    "Authorization": "Bearer %s" % token,
                    "Content-Type": "application/json",
                },
                json={
                    "receive_id": receive_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": part}),
                },
                timeout=15,
            )
            data = resp.json()
            if data.get("code", -1) != 0:
                print("[feishu] 发送失败: %s" % data, flush=True)
                success = False
        except Exception as e:
            print("[feishu] 发送异常: %s" % e, flush=True)
            success = False

    if success:
        print("[feishu] Bot 推送成功 (%d 片)" % len(parts), flush=True)
    return success


def _split_text(text: str, max_len: int) -> list:
    """按换行符分片"""
    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        cut = text[:max_len].rfind("\n")
        if cut < 100:
            cut = max_len
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


# ── 工具函数 ────────────────────────────────────────────────────

def _load_file(path: str) -> str:
    if os.path.exists(path):
        return Path(path).read_text(encoding="utf-8")
    return ""


def _load_recent_news(daily_dir: str, today: str, days: int = 2) -> str:
    """加载近 N 天新闻"""
    parts = []
    dt = datetime.strptime(today, "%Y-%m-%d")
    for i in range(days + 1):
        d = (dt - timedelta(days=i)).strftime("%Y-%m-%d")
        for filename in ["新闻.md", "事件催化.md"]:
            content = _load_file(os.path.join(daily_dir, d, filename))
            if content:
                tag = "事件催化" if "催化" in filename else "新闻"
                parts.append("--- %s %s ---\n%s" % (d, tag, content[:2000]))
    return "\n\n".join(parts) if parts else ""


def _recent_trading_days(today: str, n: int) -> list:
    """简单估算最近 n 个交易日（跳周末）"""
    dates = []
    dt = datetime.strptime(today, "%Y-%m-%d")
    while len(dates) < n:
        dt -= timedelta(days=1)
        if dt.weekday() < 5:
            dates.append(dt.strftime("%Y-%m-%d"))
    return dates


# ── Graph 构建 & 运行 ──────────────────────────────────────────

def build_graph() -> StateGraph:
    """构建 LangGraph 图"""
    graph = StateGraph(IntradayState)

    graph.add_node("fetch_data", fetch_data)
    graph.add_node("analyze", analyze)
    graph.add_node("save_and_push", save_and_push)

    graph.add_edge(START, "fetch_data")
    graph.add_edge("fetch_data", "analyze")
    graph.add_edge("analyze", "save_and_push")
    graph.add_edge("save_and_push", END)

    return graph.compile()


def run(agent_name: str, date: str = None, dry_run: bool = False) -> str:
    """运行盘中分析 Agent

    Returns:
        生成的报告文本
    """
    init_data_dirs()
    today = date or datetime.now().strftime("%Y-%m-%d")

    print("=" * 60, flush=True)
    print("[intraday] %s | %s | dry_run=%s" % (agent_name, today, dry_run), flush=True)
    print("=" * 60, flush=True)

    initial_state: IntradayState = {
        "agent_name": agent_name,
        "date": today,
        "dry_run": dry_run,
        "context_text": "",
        "data_raw": {},
        "report": "",
        "ai_provider": "",
        "output_path": "",
        "feishu_sent": False,
        "error": "",
    }

    graph = build_graph()
    result = graph.invoke(initial_state)

    if result.get("error"):
        print("[ERROR] %s" % result["error"], flush=True)

    return result.get("report", "")
