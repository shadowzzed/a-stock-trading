#!/usr/bin/env python3
"""
A 股新闻监控推送脚本

数据源：
1. TrendRadar SQLite DB（AI 筛选后的热榜 + RSS）
2. 财联社电报 API（重要快讯，level A/B 或 jpush）
3. 华尔街见闻 A 股快讯 API

流程：批量 AI 解读 → 逐条飞书私聊推送 → 保存到 trading/daily/YYYY-MM-DD/新闻.md
"""

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
    "TRENDRADAR_OUTPUT", os.path.expanduser("~/src/TrendRadar/output")
))
TRADING_DIR = Path(os.environ.get("TRADING_DIR", _cfg["daily_dir"]))
STATE_DIR = Path(os.environ.get(
    "STATE_DIR", os.path.expanduser("~/src/TrendRadar/output/.news_monitor")
))
NEWS_DB_PATH = Path(os.environ.get("NEWS_DB_PATH", _cfg["news_db"]))

# DeepSeek API（火山引擎）— 直连不走代理
_no_proxy = os.environ.get("NO_PROXY", "")
_volc_domain = "ark.cn-beijing.volces.com"
if _volc_domain not in _no_proxy:
    os.environ["NO_PROXY"] = ("%s,%s" % (_no_proxy, _volc_domain)).strip(",")
    os.environ["no_proxy"] = os.environ["NO_PROXY"]

AI_API_BASE = _cfg["ai_api_base"]
AI_API_KEY = _cfg["ai_api_key"]
AI_MODEL = _cfg["ai_model"]

# 飞书 App Bot
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_RECEIVE_ID = os.environ.get("FEISHU_RECEIVE_ID", "")  # union_id

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
    try:
        db.execute("""
            INSERT OR IGNORE INTO news (key, title, source, url, news_time, brief, stocks, plates, interpretation, sent_at, created_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ))
        db.commit()
    except Exception as e:
        print("  [DB] 写入失败: %s" % e, flush=True)


def summarize_day_news(date_str):
    """为指定日期生成新闻摘要，保存到 trading/daily/ 目录"""
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
    """清理超过 7 天的新闻记录（先生成摘要再删除）"""
    db = get_news_db()
    try:
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        # 找出将被清理的日期
        dates = db.execute(
            "SELECT DISTINCT created_date FROM news WHERE created_date < ? ORDER BY created_date",
            (cutoff,)
        ).fetchall()

        # 先为每天生成摘要
        for (date_str,) in dates:
            try:
                summarize_day_news(date_str)
            except Exception as e:
                print("[摘要] %s 生成失败: %s" % (date_str, e), flush=True)

        # 再删除
        cursor = db.execute("DELETE FROM news WHERE created_date < ?", (cutoff,))
        deleted = cursor.rowcount
        db.commit()
        if deleted > 0:
            db.execute("VACUUM")
            print("[清理] 删除 %d 条 7 天前的新闻" % deleted, flush=True)
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

FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "")


def send_feishu(content):
    resp = requests.post(
        FEISHU_WEBHOOK_URL,
        json={
            "msg_type": "interactive",
            "card": {"elements": [{"tag": "markdown", "content": content}]},
        },
        timeout=15,
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
    """每小时发送一次 token 消耗统计"""
    current_hour = datetime.now().hour
    if current_hour == _token_stats["last_report_hour"]:
        return
    if _token_stats["last_report_hour"] == -1:
        # 首次运行不报告，只记录当前小时
        _token_stats["last_report_hour"] = current_hour
        return

    _token_stats["last_report_hour"] = current_hour

    total = _token_stats["total_tokens"]
    prompt = _token_stats["prompt_tokens"]
    completion = _token_stats["completion_tokens"]
    calls = _token_stats["api_calls"]
    news = _token_stats["news_count"]

    if total == 0 and calls == 0 and not _error_log:
        return

    msg = "📊 **新闻监控 Token 统计**（截至 %s）\n" % datetime.now().strftime("%H:%M")
    msg += "累计消耗：**%s** tokens（输入 %s + 输出 %s）\n" % (
        format_number(total), format_number(prompt), format_number(completion)
    )
    msg += "API 调用：%d 次 | 处理新闻：%d 条" % (calls, news)

    if _error_log:
        msg += "\n\n⚠️ **本小时错误（%d 次）**：\n" % len(_error_log)
        for entry in _error_log[-10:]:  # 最多显示10条
            msg += "- %s\n" % entry
        if len(_error_log) > 10:
            msg += "- ...及另外 %d 条\n" % (len(_error_log) - 10)
        _error_log.clear()

    send_feishu(msg)
    print("[统计] tokens=%d, calls=%d, news=%d, errors=%d" % (total, calls, news, len(_error_log)), flush=True)


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
    """批量 AI 解读，每次最多 8 条，返回 {index: text}"""
    results = {}
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

        for attempt in range(3):
            try:
                resp = requests.post(
                    "%s/chat/completions" % AI_API_BASE,
                    headers={"Authorization": "Bearer %s" % AI_API_KEY, "Content-Type": "application/json"},
                    json={
                        "model": AI_MODEL,
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

                print("  [AI] 解读 %d-%d/%d 完成" % (start + 1, min(start + batch_size, len(items)), len(items)), flush=True)
                _write_heartbeat()  # 每批成功后刷新心跳，避免看门狗误杀
                break  # 成功则跳出重试

            except Exception as e:
                _write_heartbeat()  # 重试前也刷新心跳，表明进程仍在推进
                if attempt < 2:
                    print("  [AI] 批次 %d-%d 第%d次失败，%ds后重试: %s" % (
                        start + 1, start + batch_size, attempt + 1, 5 * (attempt + 1), e), flush=True)
                    time.sleep(5 * (attempt + 1))
                else:
                    log_error("AI解读", "批次 %d-%d 3次重试均失败: %s" % (start + 1, start + batch_size, e))

        if start + batch_size < len(items):
            time.sleep(1)

    return results


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
        for item in data.get("data", {}).get("data", []):
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


def format_feishu(item, interpretation):
    t = item.get("time", "")
    t_part = " · %s" % t if t else ""

    # 已有的关联信息
    extra_parts = []
    if item.get("plates"):
        extra_parts.append("板块：%s" % "、".join(item["plates"][:3]))
    if item.get("stocks"):
        extra_parts.append("个股：%s" % "、".join(item["stocks"][:3]))
    extra_line = "\n" + " | ".join(extra_parts) if extra_parts else ""

    # 原文内容
    brief = item.get("brief", "").strip()
    brief_line = "\n\n> %s" % brief if brief else ""

    url_line = "\n[原文链接](%s)" % item["url"] if item.get("url") else ""

    return "**%s**\n`%s`%s%s%s\n\n%s%s" % (
        item["title"], item["source"], t_part, extra_line, brief_line, interpretation, url_line
    )


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


def load_aggregate_prompt():
    """加载聚合 Agent prompt"""
    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "news_aggregate.md")
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()


def ai_aggregate(items_with_interp):
    """调用 AI 聚合分析，输出 Top 10"""
    if not items_with_interp:
        return None

    prompt = load_aggregate_prompt()
    news_text = "\n".join(
        "%d. [%s][%s] %s" % (i + 1, it["item"].get("source", ""), it["item"].get("time", ""), it["item"]["title"])
        for i, it in enumerate(items_with_interp)
    )

    user_content = "时间窗口内共 %d 条原始新闻：\n\n%s" % (len(items_with_interp), news_text)

    for attempt in range(3):
        try:
            resp = requests.post(
                "%s/chat/completions" % AI_API_BASE,
                headers={"Authorization": "Bearer %s" % AI_API_KEY, "Content-Type": "application/json"},
                json={
                    "model": AI_MODEL,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 3000,
                },
                timeout=120,
            )
            data = resp.json()
            track_tokens(data.get("usage"), len(items_with_interp))
            _write_heartbeat()  # 聚合AI成功后刷新心跳
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            _write_heartbeat()  # 重试前也刷新心跳
            if attempt < 2:
                print("  [聚合AI] 第%d次失败，重试: %s" % (attempt + 1, e), flush=True)
                time.sleep(5 * (attempt + 1))
            else:
                log_error("聚合AI", "3次重试均失败: %s" % e)
    return None


def flush_aggregate_buffer(today, sent_keys):
    """将缓冲区内的新闻聚合后发送"""
    global _news_buffer
    if not _news_buffer:
        return 0

    buf = _news_buffer[:]
    _news_buffer = []

    window_start = min(it["item"].get("time", "??:??") for it in buf)
    window_end = datetime.now().strftime("%H:%M")

    print("[%s] 聚合 %d 条新闻（%s - %s）..." % (window_end, len(buf), window_start, window_end), flush=True)

    # 调用聚合 AI
    aggregate_text = ai_aggregate(buf)

    if aggregate_text:
        # 发送聚合结果到飞书
        send_feishu(aggregate_text)
        print("  ✅ 聚合报告已发送", flush=True)

    # 所有新闻保存到 daily 文件（不管聚合是否成功）
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
    interps = ai_batch_interpret(unique)

    trading = is_trading_hours()
    sent_count = 0

    for i, item in enumerate(unique):
        interpretation = interps.get(i, "（AI 解读暂不可用）")
        priority = classify_priority(item["title"], interpretation)

        if trading and priority:
            # 交易时间 + 高优先级 → 立即推送
            tag = {"supply_demand": "供需", "earnings": "业绩", "research": "研报", "geopolitics": "地缘"}.get(priority, "")
            msg = "🔔 **[%s]** %s" % (tag, format_feishu(item, interpretation))
            if send_feishu(msg):
                save_to_trading(today, item, interpretation)
                save_news_item(item, interpretation)
                _title_window.add(item["title"])
                sent_keys.add(item["key"])
                sent_count += 1
                print("  🔔 [%s] %s" % (tag, item["title"][:30]), flush=True)
            time.sleep(0.3)
        else:
            # 暂存到聚合缓冲区
            _news_buffer.append({"item": item, "interpretation": interpretation})
            print("  📦 缓存: %s" % item["title"][:35], flush=True)

    # 检查是否到了聚合时间
    if _news_buffer and time.time() - _last_aggregate_time[0] >= (AGGREGATE_INTERVAL_TRADING if is_trading_hours() else AGGREGATE_INTERVAL_OFF_HOURS):
        flush_aggregate_buffer(today, sent_keys)

    return sent_count


# ═══════════════════════════════════════════════════════════════
# 自检与自愈
# ═══════════════════════════════════════════════════════════════

HEARTBEAT_PATH = Path(__file__).parent / ".news_monitor_heartbeat"
WATCHDOG_TIMEOUT = 300  # run_once 超过 5 分钟视为卡死


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


def main():
    print("📰 A 股新闻监控启动", flush=True)
    print("   数据源: TrendRadar + 财联社 + 华尔街见闻 + 金十数据 + BlockBeats + 东方财富研报", flush=True)
    print("   轮询间隔: %ds" % POLL_INTERVAL, flush=True)
    print("   交易时间(09:25-15:00): 高优实时推送 | 低优%d分钟聚合" % (AGGREGATE_INTERVAL_TRADING // 60), flush=True)
    print("   非交易时间: %d分钟聚合推送" % (AGGREGATE_INTERVAL_OFF_HOURS // 60), flush=True)
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
