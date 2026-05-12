#!/usr/bin/env python3
"""
A 股新闻监控推送脚本

数据源（30 秒轮询，共 8 个）：
1. TrendRadar SQLite DB（AI 筛选后的热榜 + RSS）
2. 财联社电报 API（重要快讯，level A/B 或 jpush）
3. 华尔街见闻 A 股快讯 API
4. 金十数据（重要标记）
5. BlockBeats、TechFlow、PANews（行业资讯）
6. 东方财富研报（首次覆盖、目标价、评级变更）

流程：批量 AI 解读 → 逐条飞书私聊推送 → 保存到 {data_root}/daily/YYYY-MM-DD/新闻.md
"""

import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

import requests

# ═══════════════════════════════════════════════════════════════
# 配置（从全局 config.yaml 读取，环境变量可覆盖）
# ═══════════════════════════════════════════════════════════════

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import get_config as _get_global_config
_cfg = _get_global_config()

TRENDRADAR_OUTPUT = Path(os.environ.get(
    "TRENDRADAR_OUTPUT", _cfg["trendradar_output"]
))
TRADING_DIR = Path(os.environ.get("TRADING_DIR", _cfg["daily_dir"]))
STATE_DIR = Path(os.environ.get(
    "NEWS_STATE_DIR", _cfg["news_state_dir"]
))
NEWS_DB_PATH = Path(os.environ.get("NEWS_DB_PATH", _cfg["news_db"]))

# DeepSeek API（火山引擎）— 直连不走代理
_no_proxy = os.environ.get("NO_PROXY", "")
_volc_domain = "ark.cn-beijing.volces.com"
if _volc_domain not in _no_proxy:
    os.environ["NO_PROXY"] = ("%s,%s" % (_no_proxy, _volc_domain)).strip(",")
    os.environ["no_proxy"] = os.environ["NO_PROXY"]

# AI 提供商列表（newsmonitor 专用：DeepSeek 优先）
from config import get_ai_providers as _get_ai_providers
AI_PROVIDERS = sorted(_get_ai_providers(), key=lambda p: {"DeepSeek": 0, "Grok": 1, "GLM": 2}.get(p["name"], 9))

# 飞书 App Bot
FEISHU_APP_ID = _cfg["feishu_app_id"]
FEISHU_APP_SECRET = _cfg["feishu_app_secret"]
FEISHU_RECEIVE_ID = _cfg["feishu_receive_id"]  # union_id

# TrendRadar AI 筛选阈值
MIN_RELEVANCE_SCORE = 0.7

# 轮询间隔（秒）
POLL_INTERVAL = 30


# ═══════════════════════════════════════════════════════════════
# 新闻数据库（SQLite）— 去重 + 持久化存储
# ═══════════════════════════════════════════════════════════════

def init_news_db():
    """初始化新闻数据库，返回连接"""
    NEWS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(NEWS_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            url TEXT DEFAULT '',
            news_time TEXT DEFAULT '',
            brief TEXT DEFAULT '',
            stocks TEXT DEFAULT '',
            plates TEXT DEFAULT '',
            interpretation TEXT DEFAULT '',
            sent_at TEXT NOT NULL,
            created_date TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_key ON news(key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_date ON news(created_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_source ON news(source)")
    # Phase 3: 事件分类字段（兼容旧数据）
    try:
        conn.execute("ALTER TABLE news ADD COLUMN event_type TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # 字段已存在
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_event_type ON news(event_type)")
    # Phase 4: 新闻情绪指数表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_sentiment_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            sentiment_score REAL NOT NULL,
            bullish_count INTEGER NOT NULL DEFAULT 0,
            bearish_count INTEGER NOT NULL DEFAULT 0,
            neutral_count INTEGER NOT NULL DEFAULT 0,
            total_count INTEGER NOT NULL DEFAULT 0,
            created_date TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sentiment_ts ON news_sentiment_index(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sentiment_date ON news_sentiment_index(created_date)")
    # Phase 5 (2026-05-11): 早报候选池 — 盘后/夜间新闻不再聚合推送，全部入池等次日 9:00 早报精选
    conn.execute("""
        CREATE TABLE IF NOT EXISTS morning_brief_pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_key TEXT UNIQUE NOT NULL,
            source TEXT,
            title TEXT,
            brief TEXT,
            interpretation TEXT,
            priority TEXT,
            event_type TEXT,
            plates TEXT,
            stocks TEXT,
            url TEXT,
            news_time TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            used_in_brief INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_brief_pool_used ON morning_brief_pool(used_in_brief)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_brief_pool_created ON morning_brief_pool(created_at)")
    conn.commit()
    return conn


_news_db = None

def get_news_db():
    global _news_db
    if _news_db is None:
        _news_db = init_news_db()
    return _news_db


def load_sent_keys(today):
    """从 SQLite 加载已发送的 key（最近 3 天，支持跨天去重）"""
    db = get_news_db()
    try:
        rows = db.execute(
            "SELECT key FROM news WHERE created_date >= date(?, '-3 days')",
            (today,)
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


class TitleWindow:
    """1 小时滑动窗口，内存维护已发送标题，用于跨轮次语义去重"""
    WINDOW_SECONDS = 3600  # 1 小时

    def __init__(self):
        self._entries = []  # [(timestamp, title), ...]

    def _evict(self):
        cutoff = time.time() - self.WINDOW_SECONDS
        self._entries = [(ts, t) for ts, t in self._entries if ts > cutoff]

    def add(self, title):
        self._entries.append((time.time(), title))

    def get_titles(self):
        self._evict()
        return [t for _, t in self._entries]

    def __len__(self):
        self._evict()
        return len(self._entries)


_title_window = TitleWindow()


def save_news_item(item, interpretation):
    """将新闻条目存入 SQLite"""
    db = get_news_db()
    now = datetime.now()
    event_type = item.get("event_type", "")
    try:
        db.execute("""
            INSERT OR IGNORE INTO news (key, title, source, url, news_time, brief, stocks, plates, interpretation, sent_at, created_date, event_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item["key"],
            item["title"],
            item.get("source", ""),
            item.get("url", ""),
            item.get("time", ""),
            item.get("brief", ""),
            json.dumps(item.get("stocks", []), ensure_ascii=False),
            json.dumps(item.get("plates", []), ensure_ascii=False),
            interpretation,
            now.strftime("%Y-%m-%d %H:%M:%S"),
            now.strftime("%Y-%m-%d"),
            event_type,
        ))
        db.commit()
    except Exception as e:
        print("  [DB] 写入失败: %s" % e, flush=True)


def summarize_day_news(date_str):
    """为指定日期生成新闻摘要，保存到 {data_root}/daily/ 目录"""
    db = get_news_db()
    rows = db.execute(
        "SELECT title, source, news_time, stocks, plates, interpretation FROM news WHERE created_date = ? ORDER BY sent_at",
        (date_str,)
    ).fetchall()
    if not rows:
        return

    summary_dir = TRADING_DIR / date_str
    summary_file = summary_dir / "新闻摘要.md"
    # 已有摘要则跳过
    if summary_file.exists():
        return

    summary_dir.mkdir(parents=True, exist_ok=True)

    # 评估重点：有关联个股/板块的、含利好/利空判断的优先
    important = []
    normal = []
    for title, source, news_time, stocks_json, plates_json, interp in rows:
        stocks = json.loads(stocks_json) if stocks_json else []
        plates = json.loads(plates_json) if plates_json else []
        interp = interp or ""

        has_impact = "利好" in interp or "利空" in interp
        has_association = bool(stocks or plates)

        entry = {
            "title": title, "source": source, "time": news_time,
            "stocks": stocks, "plates": plates, "interp": interp,
        }
        if has_impact or has_association:
            important.append(entry)
        else:
            normal.append(entry)

    # 构建摘要 markdown
    lines = ["# 新闻摘要（%s）\n" % date_str]
    lines.append("当日共 **%d** 条新闻，其中重点 **%d** 条。\n" % (len(rows), len(important)))

    if important:
        lines.append("## 重点新闻\n")
        # 按板块聚合
        plate_map = {}  # plate -> [entries]
        no_plate = []
        for e in important:
            if e["plates"]:
                for p in e["plates"]:
                    plate_map.setdefault(p, []).append(e)
            else:
                no_plate.append(e)

        for plate, entries in sorted(plate_map.items()):
            lines.append("### %s\n" % plate)
            seen = set()
            for e in entries:
                if e["title"] in seen:
                    continue
                seen.add(e["title"])
                t = " %s" % e["time"] if e["time"] else ""
                lines.append("- **%s** `%s%s`" % (e["title"], e["source"], t))
                if e["interp"]:
                    # 取解读的第一行（精简）
                    first_line = e["interp"].split("\n")[0].strip()
                    lines.append("  - %s" % first_line)
                if e["stocks"]:
                    lines.append("  - 关联个股：%s" % "、".join(e["stocks"][:5]))
                lines.append("")

        if no_plate:
            lines.append("### 其他重点\n")
            for e in no_plate:
                t = " %s" % e["time"] if e["time"] else ""
                lines.append("- **%s** `%s%s`" % (e["title"], e["source"], t))
                if e["interp"]:
                    first_line = e["interp"].split("\n")[0].strip()
                    lines.append("  - %s" % first_line)
                lines.append("")

    lines.append("## 统计\n")
    # 按来源统计
    source_count = {}
    for title, source, *_ in rows:
        source_count[source] = source_count.get(source, 0) + 1
    for src, cnt in sorted(source_count.items(), key=lambda x: -x[1]):
        lines.append("- %s：%d 条" % (src, cnt))
    lines.append("")

    summary_file.write_text("\n".join(lines), encoding="utf-8")
    print("[摘要] 已生成 %s（重点 %d / 全部 %d）" % (date_str, len(important), len(rows)), flush=True)


def cleanup_old_news():
    """清理超过 30 天的新闻正文（保留元数据用于历史分析）

    保留字段：id, key, title, source, news_time, stocks, plates, interpretation, sent_at, created_date
    清空字段：brief, url（节省空间，这些对历史分析无用）
    永久保留：news_embeddings, news_impacts 表（向量和影响数据不清理）
    """
    db = get_news_db()
    try:
        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        # 找出将被清理的日期，先生成摘要
        dates = db.execute(
            "SELECT DISTINCT created_date FROM news WHERE created_date < ? AND brief != '' ORDER BY created_date",
            (cutoff,)
        ).fetchall()

        for (date_str,) in dates:
            try:
                summarize_day_news(date_str)
            except Exception as e:
                print("[摘要] %s 生成失败: %s" % (date_str, e), flush=True)

        # 只清空 brief 和 url，保留其他元数据
        cursor = db.execute(
            "UPDATE news SET brief = '', url = '' WHERE created_date < ? AND brief != ''",
            (cutoff,)
        )
        cleaned = cursor.rowcount
        db.commit()
        if cleaned > 0:
            print("[清理] 精简 %d 条 30 天前新闻的正文（元数据已保留）" % cleaned, flush=True)
    except Exception as e:
        print("[清理] 失败: %s" % e, flush=True)


_last_cleanup_date = None

def check_weekly_cleanup():
    """每周一凌晨 1 点执行清理"""
    global _last_cleanup_date
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.weekday() == 0 and now.hour >= 1 and _last_cleanup_date != today:
        _last_cleanup_date = today
        cleanup_old_news()


# 兼容旧版 JSON 状态文件（可选迁移）
def _state_file(today):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / ("%s.json" % today)


def make_key(source, title):
    h = hashlib.md5(("%s:%s" % (source, title)).encode()).hexdigest()[:12]
    return "%s:%s" % (source, h)


def _extract_keywords(title):
    """提取标题中的中文关键词（去掉标点和常见虚词）"""
    import re as _re
    # 去标点
    clean = _re.sub(r'[^\u4e00-\u9fff\w]', ' ', title)
    # 按空格分词 + 按2-4字滑动窗口提取片段
    words = set()
    chars = _re.sub(r'\s+', '', clean)
    # 2字、3字片段
    for n in (2, 3):
        for i in range(len(chars) - n + 1):
            words.add(chars[i:i+n])
    return words


def _is_similar_to_any(title, existing_titles, threshold=0.4):
    """检查标题与已有标题列表是否有语义重复"""
    if not existing_titles:
        return False
    kw = _extract_keywords(title)
    if len(kw) < 3:
        return False
    for et in existing_titles:
        ekw = _extract_keywords(et)
        if len(ekw) < 3:
            continue
        overlap = len(kw & ekw)
        shorter = min(len(kw), len(ekw))
        if shorter > 0 and overlap / shorter > threshold:
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# 飞书
# ═══════════════════════════════════════════════════════════════

FEISHU_WEBHOOK_URL = _cfg["feishu_webhook_url"]


_NO_PROXY = {"http": None, "https": None}  # 绕过系统代理直连


def send_feishu(content):
    resp = requests.post(
        FEISHU_WEBHOOK_URL,
        json={
            "msg_type": "interactive",
            "card": {"elements": [{"tag": "markdown", "content": content}]},
        },
        timeout=15,
        proxies=_NO_PROXY,
    )
    data = resp.json()
    if data.get("code") == 0 or data.get("StatusCode") == 0:
        return True
    print("  [飞书] 失败: %s" % data, flush=True)
    return False


# ═══════════════════════════════════════════════════════════════
# Token 统计
# ═══════════════════════════════════════════════════════════════

_token_stats = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "api_calls": 0,
    "news_count": 0,
    "last_report_hour": -1,
}

_error_log = []  # 记录抓取/解读错误，每小时汇报后清空


def log_error(source, msg):
    """记录错误，供每小时汇报"""
    ts = datetime.now().strftime("%H:%M:%S")
    entry = "[%s] %s: %s" % (ts, source, msg)
    _error_log.append(entry)
    print("  %s" % entry, flush=True)


def track_tokens(usage_data, news_count=0):
    """累加 token 用量"""
    if usage_data:
        _token_stats["prompt_tokens"] += usage_data.get("prompt_tokens", 0)
        _token_stats["completion_tokens"] += usage_data.get("completion_tokens", 0)
        _token_stats["total_tokens"] += usage_data.get("total_tokens", 0)
    _token_stats["api_calls"] += 1
    _token_stats["news_count"] += news_count


def check_hourly_report():
    """每小时本地打印 token 统计 + 异常情况推送

    2026-05-12 改：去掉常规 token 播报（飞书刷屏），仅本地日志。
    仅在有错误时才推送（错误诊断不能丢）。
    """
    current_hour = datetime.now().hour
    if current_hour == _token_stats["last_report_hour"]:
        return
    if _token_stats["last_report_hour"] == -1:
        _token_stats["last_report_hour"] = current_hour
        return

    _token_stats["last_report_hour"] = current_hour

    total = _token_stats["total_tokens"]
    calls = _token_stats["api_calls"]
    news = _token_stats["news_count"]

    # 本地日志（便于事后排查）
    print("[统计] tokens=%d, calls=%d, news=%d, errors=%d" % (
        total, calls, news, len(_error_log)), flush=True)

    # 仅在有错误时才推送飞书
    if _error_log:
        msg = "⚠️ **News Monitor 本小时错误（%d 次）**\n" % len(_error_log)
        for entry in _error_log[-10:]:
            msg += "- %s\n" % entry
        if len(_error_log) > 10:
            msg += "- ...及另外 %d 条\n" % (len(_error_log) - 10)
        send_feishu(msg)
        _error_log.clear()


def format_number(n):
    if n >= 1000000:
        return "%.1fM" % (n / 1000000)
    if n >= 1000:
        return "%.1fK" % (n / 1000)
    return str(n)


# ═══════════════════════════════════════════════════════════════
# AI 批量解读
# ═══════════════════════════════════════════════════════════════

def ai_batch_interpret(items):
    """批量 AI 解读，每次最多 8 条，返回 ({index: text}, provider_name)"""
    results = {}
    used_providers = set()
    batch_size = 8
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        news_text = "\n".join(
            "%d. [%s] %s" % (i + 1, it.get("source", ""), it["title"])
            for i, it in enumerate(batch)
        )
        prompt = """A股短线分析师，对以下新闻逐条简析。每条输出：关联板块、关联个股（名称+代码，无则写"无"）、利好/利空/中性、一句话解读。

%s

格式（严格按编号，每条2行）：
1. 板块：xx | 个股：xx(代码) | 利好
解读：一句话

2. ...""" % news_text

        batch_done = False
        for provider in AI_PROVIDERS:
            if batch_done:
                break
            for attempt in range(3):
                try:
                    resp = requests.post(
                        "%s/chat/completions" % provider["base"],
                        headers={"Authorization": "Bearer %s" % provider["key"], "Content-Type": "application/json"},
                        json={
                            "model": provider["model"],
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.3,
                            "max_tokens": 1500,
                        },
                        timeout=90,
                    )
                    data = resp.json()
                    track_tokens(data.get("usage"), len(batch))
                    text = data["choices"][0]["message"]["content"].strip()

                    # 按编号拆分
                    parts = []
                    current_lines = []
                    for line in text.split("\n"):
                        s = line.strip()
                        if s and s[0].isdigit() and "." in s[:4] and current_lines:
                            parts.append("\n".join(current_lines))
                            current_lines = [s]
                        elif s:
                            current_lines.append(s)
                    if current_lines:
                        parts.append("\n".join(current_lines))

                    for i, part in enumerate(parts):
                        idx = start + i
                        if idx < len(items):
                            # 去掉编号前缀 "1. "
                            cleaned = part
                            dot_pos = cleaned.find(".")
                            if dot_pos > 0 and dot_pos <= 3 and cleaned[:dot_pos].isdigit():
                                cleaned = cleaned[dot_pos + 1:].strip()
                            results[idx] = cleaned

                    print("  [AI:%s] 解读 %d-%d/%d 完成" % (provider["name"], start + 1, min(start + batch_size, len(items)), len(items)), flush=True)
                    _write_heartbeat()
                    used_providers.add(provider["name"])
                    batch_done = True
                    break  # 成功则跳出重试

                except Exception as e:
                    _write_heartbeat()
                    if attempt < 2:
                        print("  [AI:%s] 批次 %d-%d 第%d次失败，%ds后重试: %s" % (
                            provider["name"], start + 1, start + batch_size, attempt + 1, 5 * (attempt + 1), e), flush=True)
                        time.sleep(5 * (attempt + 1))
                    else:
                        print("  [AI:%s] 批次 %d-%d 3次失败，切换下一个提供商" % (
                            provider["name"], start + 1, start + batch_size), flush=True)

            if not batch_done and provider == AI_PROVIDERS[-1]:
                log_error("AI解读", "批次 %d-%d 所有提供商均失败" % (start + 1, start + batch_size))

        if start + batch_size < len(items):
            time.sleep(1)

    provider_label = "+".join(sorted(used_providers)) if used_providers else "无"
    return results, provider_label


# ═══════════════════════════════════════════════════════════════
# 数据源 1: TrendRadar DB
# ═══════════════════════════════════════════════════════════════

def fetch_trendradar(sent_keys):
    today = datetime.now().strftime("%Y-%m-%d")
    db_path = TRENDRADAR_OUTPUT / "news" / ("%s.db" % today)
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    items = []
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "ai_filter_results" not in tables:
            return []

        rows = conn.execute("""
            SELECT n.id, n.title, n.platform_id, n.url, n.created_at,
                   MAX(afr.relevance_score) as max_score,
                   GROUP_CONCAT(DISTINCT aft.tag) as tags
            FROM news_items n
            JOIN ai_filter_results afr ON afr.news_item_id = n.id AND afr.source_type = 'hotlist'
            JOIN ai_filter_tags aft ON afr.tag_id = aft.id AND aft.status = 'active'
            WHERE afr.relevance_score >= ? AND afr.status = 'active'
            GROUP BY n.id ORDER BY n.created_at DESC
        """, (MIN_RELEVANCE_SCORE,)).fetchall()

        for row in rows:
            key = make_key("tr", row["title"])
            if key in sent_keys:
                continue
            created = row["created_at"] or ""
            # 提取时间部分
            time_str = ""
            if created and " " in created:
                time_str = created.split(" ")[1][:5]
            items.append({
                "key": key,
                "title": row["title"],
                "source": row["platform_id"],
                "url": row["url"] or "",
                "time": time_str,
            })
    finally:
        conn.close()
    return items


# ═══════════════════════════════════════════════════════════════
# 数据源 2: 财联社电报（重要新闻）
# ═══════════════════════════════════════════════════════════════

def fetch_cls(sent_keys):
    items = []
    try:
        resp = requests.get(
            "https://www.cls.cn/nodeapi/telegraphList",
            params={"app": "CailianpressWeb", "os": "web", "rn": 50},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=15,
        )
        data = resp.json()
        for item in data.get("data", {}).get("roll_data", []):
            # 只取重要新闻: level A/B 或被推送(jpush) 或加粗(bold) 或推荐(recommend)
            level = item.get("level", "C")
            if level not in ("A", "B") and not item.get("jpush") and not item.get("bold") and not item.get("recommend"):
                continue

            title = item.get("title") or item.get("brief", "")
            if not title:
                continue
            title = title[:200]

            key = make_key("cls", title)
            if key in sent_keys:
                continue

            ctime = item.get("ctime", 0)
            time_str = datetime.fromtimestamp(ctime).strftime("%H:%M") if ctime else ""

            # 提取关联股票和板块（财联社 API 自带）
            stocks = []
            for s in item.get("stock_list", []):
                name = s.get("name", "")
                code = s.get("symbol", "")
                if name and code:
                    stocks.append("%s(%s)" % (name, code))
            plates = [p.get("name", "") for p in item.get("plate_list", []) if p.get("name")]

            # 内容摘要
            content = item.get("content", "")
            brief = content[:300] if content else ""

            items.append({
                "key": key,
                "title": title,
                "source": "财联社",
                "url": item.get("shareurl", ""),
                "time": time_str,
                "level": level,
                "stocks": stocks,
                "plates": plates,
                "brief": brief,
            })
    except Exception as e:
        log_error("财联社", "抓取失败: %s" % e)
    return items


# ═══════════════════════════════════════════════════════════════
# 数据源 3: 华尔街见闻 A 股快讯
# ═══════════════════════════════════════════════════════════════

def fetch_wallstreetcn(sent_keys):
    items = []
    try:
        resp = requests.get(
            "https://api-prod.wallstreetcn.com/apiv1/content/lives",
            params={"channel": "a-stock-channel", "client": "pc", "limit": 30},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=15,
        )
        data = resp.json()
        for item in data.get("data", {}).get("items", []):
            title = item.get("title", "") or ""
            content = item.get("content_text", "") or ""
            display = title or content[:200]
            if not display:
                continue

            key = make_key("wsj", display[:100])
            if key in sent_keys:
                continue

            ts = item.get("display_time", 0)
            time_str = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""
            uri = item.get("uri", "")
            url = "https://wallstreetcn.com/live/%s" % uri if uri else ""

            items.append({
                "key": key,
                "title": display[:200],
                "source": "华尔街见闻",
                "url": url,
                "time": time_str,
                "brief": content[:300] if content and content != display else "",
            })
    except Exception as e:
        log_error("华尔街见闻", "抓取失败: %s" % e)
    return items


# ═══════════════════════════════════════════════════════════════
# 数据源 4: 金十数据快讯
# ═══════════════════════════════════════════════════════════════

def fetch_jin10(sent_keys):
    import re
    items = []
    try:
        resp = requests.get(
            "https://flash-api.jin10.com/get_flash_list",
            params={"channel": "-8200", "vip": "1"},
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Referer": "https://www.jin10.com/",
                "x-app-id": "bVBF4FyRTn5NJF5n",
                "x-version": "1.0.0",
            },
            timeout=15,
        )
        data = resp.json()
        for item in data.get("data", []):
            # 只取重要快讯
            important = item.get("important", 0)
            if not important:
                continue

            content = item.get("data", {}).get("content", "")
            if not content:
                continue
            # 去除 HTML 标签
            content = re.sub(r"<[^>]+>", "", content).strip()
            if not content:
                continue
            title = content[:200]

            key = make_key("j10", title[:100])
            if key in sent_keys:
                continue

            time_str = ""
            raw_time = item.get("time", "")
            if raw_time and " " in raw_time:
                time_str = raw_time.split(" ")[1][:5]

            news_id = item.get("id", "")
            url = "https://www.jin10.com/flash_detail/%s.html" % news_id if news_id else ""
            # 原文：content 本身就是全文
            raw_content = re.sub(r"<[^>]+>", "", item.get("data", {}).get("content", "")).strip()
            items.append({
                "key": key,
                "title": title,
                "source": "金十数据",
                "url": url,
                "time": time_str,
                "brief": raw_content[:500] if len(raw_content) > len(title) + 10 else "",
            })
    except Exception as e:
        log_error("金十数据", "抓取失败: %s" % e)
    return items


# ═══════════════════════════════════════════════════════════════
# 数据源 5: BlockBeats (律动) 快讯
# ═══════════════════════════════════════════════════════════════

def fetch_blockbeats(sent_keys):
    items = []
    try:
        resp = requests.get(
            "https://api.theblockbeats.news/v1/open-api/open-flash",
            params={"size": 30, "page": 1, "type": "push", "lang": "cn"},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=15,
        )
        data = resp.json()
        # 2026-05 API 调整：data 字段直接是 list（之前嵌套两层）
        raw = data.get("data", [])
        if isinstance(raw, dict):
            raw = raw.get("data", [])
        for item in raw:
            if not isinstance(item, dict):
                continue
            title = item.get("title", "").strip()
            if not title:
                continue

            key = make_key("bb", title[:100])
            if key in sent_keys:
                continue

            ts = item.get("create_time", "")
            time_str = ""
            if ts:
                try:
                    time_str = datetime.fromtimestamp(int(ts)).strftime("%H:%M")
                except (ValueError, OSError):
                    pass

            link = item.get("link", "") or ""
            desc = (item.get("description", "") or "").strip()
            items.append({
                "key": key,
                "title": title,
                "source": "BlockBeats",
                "url": link,
                "time": time_str,
                "brief": desc[:500] if desc and len(desc) > len(title) + 10 else "",
            })
    except Exception as e:
        log_error("BlockBeats", "抓取失败: %s" % e)
    return items


# ═══════════════════════════════════════════════════════════════
# 数据源 6: TechFlow (深潮) 快讯 — RSS
# ═══════════════════════════════════════════════════════════════

def fetch_techflow(sent_keys):
    import xml.etree.ElementTree as ET
    items = []
    try:
        resp = requests.get(
            "https://www.techflowpost.com/api/client/common/rss.xml",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=15,
        )
        root = ET.fromstring(resp.text)
        for entry in root.findall(".//item"):
            title = (entry.findtext("title") or "").strip()
            if not title:
                continue

            key = make_key("tf", title[:100])
            if key in sent_keys:
                continue

            link = (entry.findtext("link") or "").strip()
            pub_date = entry.findtext("pubDate") or ""
            time_str = ""
            if pub_date:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(pub_date)
                    time_str = dt.strftime("%H:%M")
                except Exception:
                    pass

            import re as _re
            desc = (entry.findtext("description") or "").strip()
            desc = _re.sub(r"<[^>]+>", "", desc).strip()
            items.append({
                "key": key,
                "title": title[:200],
                "source": "TechFlow",
                "url": link,
                "time": time_str,
                "brief": desc[:500] if desc and len(desc) > len(title) + 10 else "",
            })
    except Exception as e:
        log_error("TechFlow", "抓取失败: %s" % e)
    return items


# ═══════════════════════════════════════════════════════════════
# 数据源 7: PANews 快讯 — RSS
# ═══════════════════════════════════════════════════════════════

def fetch_panews(sent_keys):
    import xml.etree.ElementTree as ET
    items = []
    try:
        resp = requests.get(
            "https://www.panewslab.com/rss.xml",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=15,
        )
        root = ET.fromstring(resp.text)
        for entry in root.findall(".//item"):
            title = (entry.findtext("title") or "").strip()
            if not title:
                continue

            key = make_key("pa", title[:100])
            if key in sent_keys:
                continue

            link = (entry.findtext("link") or "").strip()
            pub_date = entry.findtext("pubDate") or ""
            time_str = ""
            if pub_date:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(pub_date)
                    time_str = dt.strftime("%H:%M")
                except Exception:
                    pass

            import re as _re2
            desc = (entry.findtext("description") or "").strip()
            desc = _re2.sub(r"<[^>]+>", "", desc).strip()
            items.append({
                "key": key,
                "title": title[:200],
                "source": "PANews",
                "url": link,
                "time": time_str,
                "brief": desc[:500] if desc and len(desc) > len(title) + 10 else "",
            })
    except Exception as e:
        log_error("PANews", "抓取失败: %s" % e)
    return items


# ═══════════════════════════════════════════════════════════════
# 数据源 8: 东方财富研报（个股 + 行业）
# ═══════════════════════════════════════════════════════════════

def fetch_research_reports(sent_keys):
    items = []
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    for qtype, label in [(0, "个股研报"), (1, "行业研报")]:
        try:
            resp = requests.get(
                "https://reportapi.eastmoney.com/report/list",
                params={
                    "industryCode": "*",
                    "pageSize": 30,
                    "industry": "*",
                    "rating": "*",
                    "ratingChange": "*",
                    "beginTime": yesterday,
                    "endTime": today,
                    "pageNo": 1,
                    "qType": qtype,
                },
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                timeout=15,
            )
            data = resp.json()
            for item in data.get("data", []):
                title = item.get("title", "").strip()
                if not title:
                    continue

                info_code = item.get("infoCode", "")
                key = make_key("rpt", info_code or title[:80])
                if key in sent_keys:
                    continue

                org = item.get("orgSName", "")
                rating = item.get("emRatingName", "")
                stock = item.get("stockName", "")
                stock_code = item.get("stockCode", "")
                industry = item.get("industryName", "")
                researcher = item.get("researcher", "")

                # 构建标题：机构+评级+原标题
                if qtype == 0 and stock:
                    display = "%s（%s）%s｜%s" % (stock, stock_code, " " + rating if rating else "", title)
                else:
                    display = "%s%s｜%s" % (industry or "", " " + rating if rating else "", title)

                url = "https://data.eastmoney.com/report/zw_stock.jshtml?infocode=%s" % info_code if info_code else ""

                source_label = "%s·%s" % (label, org) if org else label
                items.append({
                    "key": key,
                    "title": display[:200],
                    "source": source_label,
                    "url": url,
                    "time": "",
                    "brief": "研究员：%s" % researcher if researcher else "",
                })
        except Exception as e:
            log_error("研报(%s)" % label, "抓取失败: %s" % e)
    return items


# ═══════════════════════════════════════════════════════════════
# 保存 & 格式化
# ═══════════════════════════════════════════════════════════════

def save_to_trading(today, item, interpretation):
    day_dir = TRADING_DIR / today
    day_dir.mkdir(parents=True, exist_ok=True)
    news_file = day_dir / "新闻.md"

    is_new = not news_file.exists() or news_file.stat().st_size == 0
    t = item.get("time", "") or datetime.now().strftime("%H:%M")

    entry = ""
    if is_new:
        entry = "# 新闻速递（%s）\n\n" % today

    # 构建额外信息
    extra = ""
    if item.get("stocks"):
        extra += "- **关联个股**：%s\n" % "、".join(item["stocks"])
    if item.get("plates"):
        extra += "- **关联板块**：%s\n" % "、".join(item["plates"])

    entry += "## %s\n\n" % item["title"]
    entry += "- **来源**：%s | **时间**：%s\n" % (item["source"], t)
    if extra:
        entry += extra
    entry += "\n%s\n\n---\n" % interpretation

    with open(news_file, "a", encoding="utf-8") as f:
        f.write(entry)


def format_feishu(item, interpretation, ai_provider="", priority=None):
    """格式化单条新闻的飞书卡片内容"""
    t = item.get("time", "")
    source = item.get("source", "")

    # ── 标题行 ──
    lines = ["**%s**" % item["title"]]

    # ── 元信息行：来源 · 时间 ──
    meta_parts = []
    if source:
        meta_parts.append(source)
    if t:
        meta_parts.append(t)
    if meta_parts:
        lines.append("`%s`" % " · ".join(meta_parts))

    # ── 标签行：优先级 + 板块 + 个股 ──
    tags = []
    if item.get("plates"):
        tags.extend(item["plates"][:3])
    if item.get("stocks"):
        for s in item["stocks"][:4]:
            name = s.split("(")[0].strip() if "(" in s else s
            tags.append(name)
    if tags:
        lines.append(" ".join("[%s]" % tg for tg in tags))

    # ── 摘要（引用块）──
    brief = item.get("brief", "").strip()
    if brief and len(brief) > 15:
        lines.append("> %s" % brief[:200])

    # ── AI 解读 ──
    if interpretation:
        # 提取解读中的核心内容（去掉重复的板块/个股行）
        interp_lines = []
        for il in interpretation.strip().split("\n"):
            il = il.strip()
            if not il:
                continue
            # 跳过纯标签行（已在上方展示）
            if il.startswith("板块：") or il.startswith("个股："):
                continue
            interp_lines.append(il)
        if interp_lines:
            lines.append("**解读**：%s" % " ".join(interp_lines))

    # ── 链接 ──
    if item.get("url"):
        lines.append("[查看原文](%s)" % item["url"])

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 新闻影响分析集成（可选模块，不影响主流程）
# ═══════════════════════════════════════════════════════════════

try:
    from news_monitor.impact.hooks import on_high_priority_news
    _impact_available = True
except ImportError:
    _impact_available = False


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# 窗口化聚合机制
# ═══════════════════════════════════════════════════════════════

AGGREGATE_INTERVAL_TRADING = 20 * 60   # 交易时间聚合窗口：20 分钟
AGGREGATE_INTERVAL_OFF_HOURS = 60 * 60  # 非交易时间聚合窗口：1 小时

# 高优先级关键词（交易时间内命中则立即推送）
_PRIORITY_KEYWORDS = {
    "supply_demand": ["减产", "扩产", "限产", "停产", "产能", "供需", "供给", "涨价", "降价", "短缺", "库存"],
    "earnings": ["业绩预增", "业绩预减", "净利润增长", "净利润下降", "营收增长", "营收下降", "业绩快报", "业绩暴雷", "扭亏", "首亏"],
    "research": ["研报", "首次覆盖", "目标价", "评级上调", "评级下调", "买入评级", "增持评级"],
    "geopolitics": ["制裁", "冲突", "战争", "军事", "袭击", "威胁", "封锁", "禁令", "关税"],
}

_news_buffer = []  # 聚合窗口内暂存的新闻
_last_aggregate_time = [0.0]  # 上次聚合时间

# 同板块/同股 30 分钟限流（避免刷屏）
# key: 板块名 或 股票名, value: 上次推送时间戳
_topic_dedup_window = {}
TOPIC_DEDUP_SECONDS = 1800  # 30 分钟


def _topic_recently_pushed(plates: list, stocks: list) -> str:
    """返回 30 分钟内已推过的板块/股票名，否则空串。"""
    now = time.time()
    for topic in (plates or []) + (stocks or []):
        last = _topic_dedup_window.get(topic, 0)
        if now - last < TOPIC_DEDUP_SECONDS:
            return topic
    return ""


def _mark_topic_pushed(plates: list, stocks: list):
    now = time.time()
    for topic in (plates or []) + (stocks or []):
        _topic_dedup_window[topic] = now
    # 简单清理：超过 2 小时的清掉
    if len(_topic_dedup_window) > 200:
        cutoff = now - 2 * TOPIC_DEDUP_SECONDS
        for k in list(_topic_dedup_window.keys()):
            if _topic_dedup_window[k] < cutoff:
                del _topic_dedup_window[k]


# ═══════════════════════════════════════════════════════════════
# Phase 5 (2026-05-11): 早报候选池 + critical 实时兜底
# ═══════════════════════════════════════════════════════════════

# 非交易时间的"超紧急"白名单：即使盘后/夜间也立即推送
# 选词原则：能让 A 股次日开盘直接跳空 / 全市场停摆级别的事件
CRITICAL_KEYWORDS = [
    # 战争/地缘
    "宣战", "核打击", "核试验", "战争爆发", "全面入侵", "动用核武",
    "台海冲突", "封锁台湾",
    # 货币政策极端事件
    "美联储紧急加息", "美联储紧急降息", "央行紧急", "降准降息", "意外加息",
    # 市场极端事件
    "熔断", "全球股市熔断", "美股熔断", "雷曼时刻",
    # 重大监管 / 突发
    "暂停IPO", "暂停交易", "停牌全市场",
]


def is_critical_news(title: str, brief: str = "") -> bool:
    """判断是否为非交易时间也必须实时推送的超紧急新闻。"""
    text = (title + " " + brief).strip()
    if not text:
        return False
    return any(kw in text for kw in CRITICAL_KEYWORDS)


def save_to_morning_pool(item: dict, interpretation: str, priority: str = None) -> bool:
    """把新闻写入早报候选池（盘后/夜间不立即推送，等次日 9:00 早报精选）。

    返回 True 表示新增成功，False 表示已存在（去重）。
    """
    db = get_news_db()
    try:
        plates_json = json.dumps(item.get("plates", []), ensure_ascii=False)
        stocks_json = json.dumps(item.get("stocks", []), ensure_ascii=False)
        db.execute(
            "INSERT OR IGNORE INTO morning_brief_pool "
            "(news_key, source, title, brief, interpretation, priority, event_type, "
            "plates, stocks, url, news_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.get("key", ""),
                item.get("source", ""),
                item.get("title", ""),
                item.get("brief", ""),
                interpretation,
                priority or "",
                item.get("event_type", ""),
                plates_json,
                stocks_json,
                item.get("url", ""),
                item.get("time", ""),
            ),
        )
        db.commit()
        return db.total_changes > 0
    except Exception as e:
        log_error("morning_pool", "save 失败: %s" % e)
        return False


def is_trading_hours():
    """判断当前是否 A 股交易时间（09:25-15:00）"""
    now = datetime.now()
    t = now.hour * 60 + now.minute
    return 9 * 60 + 25 <= t <= 15 * 60


def classify_priority(title, interpretation=""):
    """判断新闻优先级，返回级别名或 None"""
    text = title + " " + interpretation
    for level, keywords in _PRIORITY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return level
    return None


def _parse_ai_tags(interpretation):
    """从 AI 解读文本中提取板块、个股和事件类型，用于打标"""
    import re as _re
    plates = []
    stocks = []
    event_type = ""
    if not interpretation:
        return plates, stocks, event_type

    # 匹配 "板块：xxx" 模式（支持多种分隔符）
    plate_match = _re.search(r'板块[：:]\s*(.+?)(?:\s*[|｜]|\n|$)', interpretation)
    if plate_match:
        plate_str = plate_match.group(1).strip()
        if plate_str and plate_str != "无":
            plates = [p.strip() for p in _re.split(r'[、,，\s]+', plate_str)
                      if p.strip() and p.strip() not in ("无", "—", "-")]

    # 匹配 "个股：xxx(代码)" 模式
    stock_match = _re.search(r'个股[：:]\s*(.+?)(?:\s*[|｜]|\n|$)', interpretation)
    if stock_match:
        stock_str = stock_match.group(1).strip()
        if stock_str and stock_str != "无":
            stocks = [s.strip() for s in _re.split(r'[、,，]+', stock_str)
                      if s.strip() and s.strip() not in ("无", "—", "-")]

    # 匹配 "事件：xxx" 模式
    event_match = _re.search(r'事件[：:]\s*(.+?)(?:\n|$)', interpretation)
    if event_match:
        event_type = event_match.group(1).strip()

    return plates, stocks, event_type


def load_aggregate_prompt():
    """加载聚合 Agent prompt"""
    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "news_aggregate.md")
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()


def _rank_importance(interpretation):
    """根据 AI 解读内容判断重要度分数（越高越重要）"""
    text = interpretation or ""
    score = 0
    # 供需类（最高）
    for kw in ["减产", "扩产", "限产", "停产", "产能", "供需", "涨价", "降价", "短缺"]:
        if kw in text:
            score = max(score, 50)
    # 业绩类
    for kw in ["业绩预增", "业绩预减", "净利润", "营收增长", "营收下降", "扭亏", "首亏", "暴雷"]:
        if kw in text:
            score = max(score, 40)
    # 利好/利空明确标注
    if "利好" in text:
        score = max(score, 30)
    if "利空" in text:
        score = max(score, 30)
    # 地缘
    for kw in ["制裁", "冲突", "战争", "军事", "袭击", "关税", "封锁"]:
        if kw in text:
            score = max(score, 35)
    # 有具体个股代码
    import re
    if re.search(r'\d{6}', text):
        score += 10
    # 有板块关联
    for kw in ["板块", "概念", "涨停", "跌停"]:
        if kw in text:
            score += 5
    return score


def ai_rank_summaries(summaries_text):
    """用 AI 对已有摘要做排序去重，只返回排序后的编号列表"""
    prompt = """你是A股短线新闻编辑。以下是编号的新闻摘要列表，请：
1. 去重（同一事件只保留一个编号）
2. 按对A股短线交易的影响程度从高到低排序
3. 最多保留前30条

**只输出排序后的编号**，用逗号分隔，不要输出其他任何内容。
示例输出：3,1,7,2,5"""

    for provider in AI_PROVIDERS:
        for attempt in range(3):
            try:
                resp = requests.post(
                    "%s/chat/completions" % provider["base"],
                    headers={"Authorization": "Bearer %s" % provider["key"], "Content-Type": "application/json"},
                    json={
                        "model": provider["model"],
                        "messages": [
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": summaries_text},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 1000,
                    },
                    timeout=90,
                )
                data = resp.json()
                track_tokens(data.get("usage"), 0)
                _write_heartbeat()
                result = data["choices"][0]["message"]["content"].strip()
                print("  [排序AI:%s] 完成" % provider["name"], flush=True)
                return result
            except Exception as e:
                _write_heartbeat()
                if attempt < 2:
                    print("  [排序AI:%s] 第%d次失败，重试: %s" % (provider["name"], attempt + 1, e), flush=True)
                    time.sleep(5 * (attempt + 1))
                else:
                    print("  [排序AI:%s] 3次失败，切换下一个提供商" % provider["name"], flush=True)
    log_error("排序AI", "所有提供商均失败")
    return None


def flush_aggregate_buffer(today, sent_keys):
    """将缓冲区内的新闻摘要排序后发送"""
    global _news_buffer
    if not _news_buffer:
        return 0

    buf = _news_buffer[:]

    # 过滤掉已单独推送的新闻
    already_sent = [it for it in buf if it.get("sent_immediately")]
    buf_for_aggregate = [it for it in buf if not it.get("sent_immediately")]

    window_start = min((it["item"].get("time", "??:??") for it in buf), default="??:??")
    window_end = datetime.now().strftime("%H:%M")

    print("[%s] 聚合 %d 条新闻（%s - %s），其中 %d 条已单独推送跳过" % (
        window_end, len(buf), window_start, window_end, len(already_sent)), flush=True)

    if not buf_for_aggregate:
        _news_buffer = []
        _last_aggregate_time[0] = time.time()
        return 0

    # 用已有的 AI 摘要（interpretation）构建排序输入
    # 每条只有标题+一行摘要，非常短，不会超时
    summaries = []
    for it in buf_for_aggregate:
        title = it["item"]["title"]
        source = it["item"].get("source", "")
        t = it["item"].get("time", "")
        interp = it.get("interpretation", "")
        # 构建标签：优先级 + 关联板块 + 关联个股
        tags = []
        if it.get("priority"):
            tag_map = {"supply_demand": "供需", "earnings": "财报", "research": "研报", "geopolitics": "地缘"}
            tags.append(tag_map.get(it["priority"], it["priority"]))
        if it["item"].get("plates"):
            tags.extend(it["item"]["plates"][:2])
        if it["item"].get("stocks"):
            for s in it["item"]["stocks"][:3]:
                tags.append(s.split("(")[0].strip() if "(" in s else s)
        tag_str = " ".join("[%s]" % tg for tg in tags) + " " if tags else ""
        summaries.append("%s[%s][%s] %s\n%s" % (tag_str, source, t, title, interp))

    # 先按规则粗排（importance score）
    scored = list(zip(buf_for_aggregate, summaries))
    scored.sort(key=lambda x: _rank_importance(x[1]), reverse=True)

    # 取前60条送 AI 做精排+去重（AI 只返回排序后的编号）
    top_items = scored[:60]  # [(buf_entry, summary_text), ...]
    summaries_text = "共 %d 条新闻摘要（已按重要度粗排）：\n\n" % len(top_items)
    summaries_text += "\n\n".join("%d. %s" % (i + 1, s) for i, (_, s) in enumerate(top_items))

    # AI 精排去重（返回编号列表如 "3,1,7,2"）
    rank_result = ai_rank_summaries(summaries_text)

    # 解析编号列表，回退到规则排序
    import re as _re_agg
    ordered_items = []
    if rank_result:
        nums = _re_agg.findall(r'\d+', rank_result)
        seen = set()
        for n in nums:
            idx = int(n) - 1  # 编号从1开始
            if 0 <= idx < len(top_items) and idx not in seen:
                seen.add(idx)
                ordered_items.append(top_items[idx][0])
        if ordered_items:
            print("  ✅ AI排序完成，%d 条" % len(ordered_items), flush=True)

    if not ordered_items:
        # AI 排序失败或解析失败，用规则排序
        print("  ⚠️ AI排序失败，使用规则排序", flush=True)
        ordered_items = [it for it, _ in top_items[:30]]

    # 用代码渲染每条新闻（确保标签不丢失）
    header = "📋 **新闻聚合**  `%s - %s`  共 %d 条\n---\n" % (window_start, window_end, len(buf_for_aggregate))
    parts = []
    impact_limit = 3  # 前 N 条高重要度新闻附加影响分析
    for i, it in enumerate(ordered_items[:30]):
        item = it["item"]
        interp = it.get("interpretation", "")
        # 标签行
        tags = []
        if it.get("priority"):
            tag_map = {"supply_demand": "供需", "earnings": "财报", "research": "研报", "geopolitics": "地缘"}
            tags.append(tag_map.get(it["priority"], it["priority"]))
        if item.get("plates"):
            tags.extend(item["plates"][:2])
        if item.get("stocks"):
            for s in item["stocks"][:3]:
                name = s.split("(")[0].strip() if "(" in s else s
                tags.append(name)
        tag_str = " ".join("[%s]" % tg for tg in tags) + " " if tags else ""
        # 提取解读核心
        interp_core = ""
        for il in interp.strip().split("\n"):
            il = il.strip()
            if il and not il.startswith("板块：") and not il.startswith("个股："):
                interp_core = il
                break
        entry = "**%d.** %s**%s**" % (i + 1, tag_str, item["title"])
        if interp_core:
            entry += "\n%s" % interp_core
        # 前几条附加历史影响分析
        if i < impact_limit and _impact_available:
            try:
                impact_report = on_high_priority_news(
                    item["title"], item.get("brief", ""), timeout_sec=3.0)
                if impact_report:
                    entry += "\n%s" % impact_report
            except Exception:
                pass
        parts.append(entry)
    send_feishu(header + "\n\n".join(parts))

    # 清空缓冲区并保存
    _news_buffer = []
    for it in buf:
        save_to_trading(today, it["item"], it["interpretation"])
        save_news_item(it["item"], it["interpretation"])
        _title_window.add(it["item"]["title"])
        sent_keys.add(it["item"]["key"])

    _last_aggregate_time[0] = time.time()
    return len(buf)


def run_once():
    today = datetime.now().strftime("%Y-%m-%d")
    sent_keys = load_sent_keys(today)

    # 收集各数据源
    all_items = []

    try:
        tr = fetch_trendradar(sent_keys)
    except Exception as e:
        tr = []
        log_error("TrendRadar", "抓取失败: %s" % e)
    if tr:
        print("  [TrendRadar] %d 条" % len(tr), flush=True)
        all_items.extend(tr)

    cls = fetch_cls(sent_keys)
    if cls:
        print("  [财联社] %d 条重要" % len(cls), flush=True)
        all_items.extend(cls)

    wsj = fetch_wallstreetcn(sent_keys)
    if wsj:
        print("  [华尔街见闻] %d 条" % len(wsj), flush=True)
        all_items.extend(wsj)

    j10 = fetch_jin10(sent_keys)
    if j10:
        print("  [金十数据] %d 条重要" % len(j10), flush=True)
        all_items.extend(j10)

    bb = fetch_blockbeats(sent_keys)
    if bb:
        print("  [BlockBeats] %d 条" % len(bb), flush=True)
        all_items.extend(bb)

    rpt = fetch_research_reports(sent_keys)
    if rpt:
        print("  [研报] %d 条" % len(rpt), flush=True)
        all_items.extend(rpt)

    if not all_items:
        # 即使没有新闻，也检查是否需要刷新聚合缓冲
        if _news_buffer and time.time() - _last_aggregate_time[0] >= (AGGREGATE_INTERVAL_TRADING if is_trading_hours() else AGGREGATE_INTERVAL_OFF_HOURS):
            flush_aggregate_buffer(today, sent_keys)
        return 0

    # 标题去重（精确 + 模糊，含 1 小时滑动窗口）
    window_titles = _title_window.get_titles()
    unique = []
    seen_exact = set()
    seen_titles = list(window_titles)
    for item in all_items:
        title = item["title"].strip()
        short = title[:40]
        if short in seen_exact:
            continue
        if _is_similar_to_any(title, seen_titles):
            continue
        seen_exact.add(short)
        seen_titles.append(title)
        unique.append(item)

    if not unique:
        if _news_buffer and time.time() - _last_aggregate_time[0] >= (AGGREGATE_INTERVAL_TRADING if is_trading_hours() else AGGREGATE_INTERVAL_OFF_HOURS):
            flush_aggregate_buffer(today, sent_keys)
        return 0

    print("[%s] 处理 %d 条新闻" % (datetime.now().strftime("%H:%M:%S"), len(unique)), flush=True)

    # 批量 AI 解读
    interps, ai_provider = ai_batch_interpret(unique)

    trading = is_trading_hours()
    sent_count = 0

    for i, item in enumerate(unique):
        interpretation = interps.get(i, "（AI 解读暂不可用）")
        priority = classify_priority(item["title"], interpretation)

        # 从 AI 解读中提取板块、个股和事件类型（数据源未提供时回填，用于打标）
        ai_plates, ai_stocks, ai_event_type = _parse_ai_tags(interpretation)
        if not item.get("plates") and ai_plates:
            item["plates"] = ai_plates
        if not item.get("stocks") and ai_stocks:
            item["stocks"] = ai_stocks
        if ai_event_type:
            item["event_type"] = ai_event_type

        # 是否走"立即推送"路径：交易时间高优 OR 非交易时间 critical 兜底
        critical = is_critical_news(item["title"], item.get("brief", ""))
        push_now = critical  # critical 始终推
        push_filter_reason = ""

        if trading and priority and not critical:
            # 2026-05-12 收紧盘中推送条件（用户反馈刷屏）：
            # 1. 个股研报 → 不实时推（信息密度低，每天几百条）
            # 2. 无具体板块/股票映射 → 不实时推（无操作性）
            # 3. 30 分钟内同板块/同股已推过 → 不再推（同主题去重）
            if item.get("source", "").startswith("个股研报"):
                push_filter_reason = "个股研报"
            elif not item.get("plates") and not item.get("stocks"):
                push_filter_reason = "无板块/无股票"
            else:
                dup_topic = _topic_recently_pushed(item.get("plates"), item.get("stocks"))
                if dup_topic:
                    push_filter_reason = "30 分钟内同主题已推: %s" % dup_topic
                else:
                    push_now = True

        if push_now:
            # 立即推送
            tag = {"supply_demand": "供需", "earnings": "财报", "research": "研报", "geopolitics": "地缘"}.get(priority, "")
            tag_emoji = {"供需": "📦", "财报": "📊", "研报": "📝", "地缘": "🌐"}.get(tag, "🔔")
            if critical and not trading:
                tag = "紧急"
                tag_emoji = "🚨"
            # 影响分析（可选，超时跳过）
            impact_report = ""
            if _impact_available:
                try:
                    impact_report = on_high_priority_news(
                        item["title"], item.get("brief", ""), timeout_sec=3.0)
                except Exception:
                    pass
            msg = "%s `%s`\n%s" % (tag_emoji, tag, format_feishu(item, interpretation, ai_provider, priority=priority))
            if impact_report:
                msg += "\n---\n%s" % impact_report
            if send_feishu(msg):
                save_to_trading(today, item, interpretation)
                save_news_item(item, interpretation)
                _title_window.add(item["title"])
                sent_keys.add(item["key"])
                sent_count += 1
                # 标记同主题已推，下次 30 分钟内同板块/股新闻不再实时推
                _mark_topic_pushed(item.get("plates"), item.get("stocks"))
                print("  🔔 [%s] %s" % (tag, item["title"][:30]), flush=True)
            time.sleep(0.3)
            if trading:
                # 交易时间标记到聚合缓冲（聚合时跳过）
                _news_buffer.append({"item": item, "interpretation": interpretation, "priority": priority, "sent_immediately": True})
            # 非交易时间 critical 已实时推送，无需再入聚合或候选池
        elif trading:
            # 交易时间低优/被过滤 → 暂存聚合缓冲（保持原有 20 分钟聚合行为）
            _news_buffer.append({"item": item, "interpretation": interpretation, "priority": priority, "sent_immediately": False})
            if push_filter_reason:
                print("  📦 缓存(聚合，%s): %s" % (push_filter_reason, item["title"][:30]), flush=True)
            else:
                print("  📦 缓存(聚合): %s" % item["title"][:35], flush=True)
        else:
            # 非交易时间普通新闻 → 入早报候选池，不再 60 分钟聚合推送
            save_to_morning_pool(item, interpretation, priority)
            save_news_item(item, interpretation)  # 同时入主表（用于历史归档/搜索）
            _title_window.add(item["title"])
            sent_keys.add(item["key"])
            print("  🌅 入早报池: %s" % item["title"][:35], flush=True)

    # 检查是否到了聚合时间
    if _news_buffer and time.time() - _last_aggregate_time[0] >= (AGGREGATE_INTERVAL_TRADING if is_trading_hours() else AGGREGATE_INTERVAL_OFF_HOURS):
        flush_aggregate_buffer(today, sent_keys)

    # 更新情绪指数（每轮有新闻时计算）
    if unique:
        _update_sentiment_index(unique, interps)

    return sent_count


# ═══════════════════════════════════════════════════════════════
# Phase 4: 新闻情绪指数
# ═══════════════════════════════════════════════════════════════

def _update_sentiment_index(items, interps):
    """基于本轮新闻的利好/利空比例，更新情绪指数"""
    bullish = 0
    bearish = 0
    neutral = 0
    for i, item in enumerate(items):
        interp = interps.get(i, "")
        if "利好" in interp:
            bullish += 1
        elif "利空" in interp:
            bearish += 1
        else:
            neutral += 1

    total = bullish + bearish + neutral
    if total == 0:
        return

    # 情绪值: -1(全利空) 到 +1(全利好)
    score = (bullish - bearish) / total

    db = get_news_db()
    now = datetime.now()
    try:
        db.execute("""
            INSERT INTO news_sentiment_index (timestamp, sentiment_score, bullish_count, bearish_count, neutral_count, total_count, created_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            now.strftime("%Y-%m-%d %H:%M:%S"),
            round(score, 3),
            bullish,
            bearish,
            neutral,
            total,
            now.strftime("%Y-%m-%d"),
        ))
        db.commit()
    except Exception as e:
        print("[情绪] 写入失败: %s" % e, flush=True)


# ═══════════════════════════════════════════════════════════════
# 自检与自愈
# ═══════════════════════════════════════════════════════════════

HEARTBEAT_PATH = Path(os.path.join(_cfg["logs_dir"], ".news_monitor_heartbeat"))
WATCHDOG_TIMEOUT = 1800  # run_once 超过 30 分钟视为卡死（首次启动 LLM 解读 80+ 条需要时间）


def _write_heartbeat():
    """写入心跳文件，供外部检测是否存活"""
    try:
        HEARTBEAT_PATH.write_text(
            json.dumps({"pid": os.getpid(), "ts": time.time(),
                        "time": datetime.now().strftime("%H:%M:%S")}))
    except Exception:
        pass


def _watchdog(main_thread, timeout):
    """看门狗线程：如果 main_thread 在 timeout 内没有更新心跳，
    说明 run_once() 卡住（通常是网络请求挂起），直接中断主线程"""
    while main_thread.is_alive():
        time.sleep(30)  # 每 30s 检查一次
        try:
            if not HEARTBEAT_PATH.exists():
                continue
            data = json.loads(HEARTBEAT_PATH.read_text())
            age = time.time() - data.get("ts", 0)
            if age > timeout:
                print("[看门狗] 心跳超时 %.0fs > %ds，发送中断信号" % (age, timeout), flush=True)
                # 向主线程发送 SIGUSR1 触发异常
                os.kill(os.getpid(), signal.SIGUSR1)
                time.sleep(5)
        except Exception:
            pass


class WatchdogInterrupt(Exception):
    """看门狗中断异常"""
    pass


def _sigusr1_handler(signum, frame):
    """收到 SIGUSR1 时在主线程抛出异常，中断卡住的网络请求"""
    raise WatchdogInterrupt("看门狗触发：run_once 超时")


_lock_file = None

def _acquire_lock():
    """文件锁确保单实例运行，获取失败则退出"""
    global _lock_file
    lock_path = os.path.join(os.environ.get("TRADING_DIR", os.path.expanduser("~/shared/trading")), ".news_monitor.lock")
    _lock_file = open(lock_path, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file.write(str(os.getpid()))
        _lock_file.flush()
    except OSError:
        print("❌ 另一个 news_monitor 实例已在运行，退出", flush=True)
        sys.exit(1)


def main():
    _acquire_lock()
    print("📰 A 股新闻监控启动", flush=True)
    print("   数据源: TrendRadar + 财联社 + 华尔街见闻 + 金十数据 + BlockBeats + TechFlow + PANews + 研报", flush=True)
    print("   轮询间隔: %ds" % POLL_INTERVAL, flush=True)
    print("   交易时间(09:25-15:00): 高门槛实时推送 | 低优%d分钟聚合" % (AGGREGATE_INTERVAL_TRADING // 60), flush=True)
    print("     实时门槛(2026-05-12 收紧): priority 命中 + 必须有板块/个股 + 30 分钟同主题去重 + 非个股研报", flush=True)
    print("   非交易时间: 全部入早报候选池（次日 9:00 由 morning_brief 精选 Top 12 推送）", flush=True)
    print("   非交易时间例外: 战争/熔断/紧急加息等 critical 关键词仍立即推送（白名单兜底）", flush=True)
    print("   小时报告: 仅本地日志 + 错误推送（已去掉 token 统计飞书播报）", flush=True)
    print("   看门狗超时: %ds" % WATCHDOG_TIMEOUT, flush=True)
    print(flush=True)
    _last_aggregate_time[0] = time.time()  # 初始化聚合计时器

    if "--once" in sys.argv:
        count = run_once()
        print("单次完成，发送 %d 条" % count, flush=True)
        return

    # 注册看门狗信号处理
    signal.signal(signal.SIGUSR1, _sigusr1_handler)

    # 启动看门狗线程
    wd = threading.Thread(target=_watchdog, args=(threading.main_thread(), WATCHDOG_TIMEOUT), daemon=True)
    wd.start()

    consecutive_errors = 0

    while True:
        try:
            _write_heartbeat()
            run_once()
            _write_heartbeat()
            check_hourly_report()
            check_weekly_cleanup()
            consecutive_errors = 0
        except WatchdogInterrupt as e:
            print("[自愈] %s，跳过本轮继续" % e, flush=True)
            consecutive_errors += 1
        except Exception as e:
            print("[错误] %s" % e, flush=True)
            traceback.print_exc()
            consecutive_errors += 1

        if consecutive_errors >= 5:
            print("[自愈] 连续 %d 次错误，等待 60s 后重试" % consecutive_errors, flush=True)
            time.sleep(60)
            consecutive_errors = 0
        else:
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
