"""加载每日行情数据（涨停板、跌停板、个股行情CSV）

数据源优先级：本地 CSV > 东方财富 API > 返回空 DataFrame
"""

from __future__ import annotations

import io
import json
import logging
import os
import glob
import sqlite3
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import requests
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── 数据质量标记 ──────────────────────────────────────────────


@dataclass
class DataResult:
    """带质量标记的数据返回值。

    对 LLM 消费者透明：str() 返回带警告前缀的文本。
    对代码消费者可检查 warnings / data_sources_missing。
    """
    content: str
    warnings: list[str] = field(default_factory=list)
    data_sources_missing: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        if self.warnings:
            prefix = "\n".join(f"> ⚠️ 数据警告: {w}" for w in self.warnings)
            return f"{prefix}\n\n{self.content}"
        return self.content

    def __bool__(self) -> bool:
        return bool(self.content)


@dataclass
class DailyData:
    """当日行情数据包"""
    date: str
    limit_up: pd.DataFrame       # 涨停板
    limit_down: pd.DataFrame     # 跌停板
    stock_data: pd.DataFrame     # 个股行情（跟踪股票池）
    reviews: dict                # 复盘文档 {文件名: 内容}
    events: str                  # 事件催化
    history: list = field(default_factory=list)  # 近几日历史摘要 [{date, limit_up_count, limit_down_count, max_board, blown_rate}]


def load_daily_data(data_dir: str, date: str, history_days: int = 7,
                    backtest_mode: bool = False) -> DailyData:
    """从 trading/daily/YYYY-MM-DD/ 目录加载当日数据

    Args:
        data_dir: trading 根目录（如 /path/to/trading）
        date: 日期字符串，如 "2026-03-24"
        history_days: 历史回溯天数
        backtest_mode: 回测模式下，review_docs 只加载 D-1 及之前（避免前瞻偏差）
    """
    daily_dir = os.path.join(data_dir, "daily", date)

    # 涨停板（CSV 优先，东方财富 API fallback）
    limit_up = _load_csv(daily_dir, f"涨停板_{date.replace('-', '')}.csv")
    if limit_up.empty:
        limit_up = fetch_limit_up_from_eastmoney(date)
        if not limit_up.empty:
            os.makedirs(daily_dir, exist_ok=True)
            csv_path = os.path.join(daily_dir, f"涨停板_{date.replace('-', '')}.csv")
            limit_up.to_csv(csv_path, index=False, encoding="utf-8-sig")
            logger.info("已通过东方财富 API 获取涨停板数据并缓存到 %s", csv_path)

    # 跌停板（CSV 优先，东方财富 API fallback）
    limit_down = _load_csv(daily_dir, f"跌停板_{date.replace('-', '')}.csv")
    if limit_down.empty:
        limit_down = fetch_limit_down_from_eastmoney(date)
        if not limit_down.empty:
            os.makedirs(daily_dir, exist_ok=True)
            csv_path = os.path.join(daily_dir, f"跌停板_{date.replace('-', '')}.csv")
            limit_down.to_csv(csv_path, index=False, encoding="utf-8-sig")
            logger.info("已通过东方财富 API 获取跌停板数据并缓存到 %s", csv_path)

    # 个股行情
    stock_data = _load_csv(daily_dir, f"行情_{date.replace('-', '')}.csv")

    # 复盘文档
    reviews = {}
    if os.path.isdir(daily_dir):
        if backtest_mode:
            reviews = _load_prev_reviews(data_dir, date)
        else:
            reviews = _load_current_reviews(daily_dir)

    # 事件催化
    events = ""
    events_path = os.path.join(daily_dir, "事件催化.md")
    if os.path.exists(events_path):
        with open(events_path, "r", encoding="utf-8") as f:
            events = f.read()

    # 加载近几日历史数据
    history = _load_history(data_dir, date, history_days)

    return DailyData(
        date=date,
        limit_up=limit_up,
        limit_down=limit_down,
        stock_data=stock_data,
        reviews=reviews,
        events=events,
        history=history,
    )


def _load_history(data_dir: str, current_date: str, days: int = 5) -> list:
    """加载前 N 个交易日的涨跌停概要数据（含板块分布、连板梯队、龙头信息）"""
    history = []
    daily_root = os.path.join(data_dir, "daily")
    if not os.path.isdir(daily_root):
        return history

    # 列出所有日期目录，排序取当前日期之前的
    all_dates = sorted([
        d for d in os.listdir(daily_root)
        if os.path.isdir(os.path.join(daily_root, d)) and d < current_date
    ])

    for hist_date in all_dates[-days:]:
        hist_dir = os.path.join(daily_root, hist_date)
        date_compact = hist_date.replace("-", "")

        lu = _load_csv(hist_dir, "涨停板_{}.csv".format(date_compact))
        if lu.empty:
            lu = fetch_limit_up_from_eastmoney(hist_date)
            if not lu.empty:
                os.makedirs(hist_dir, exist_ok=True)
                lu.to_csv(os.path.join(hist_dir, "涨停板_{}.csv".format(date_compact)),
                          index=False, encoding="utf-8-sig")
        ld = _load_csv(hist_dir, "跌停板_{}.csv".format(date_compact))
        if ld.empty:
            ld = fetch_limit_down_from_eastmoney(hist_date)
            if not ld.empty:
                os.makedirs(hist_dir, exist_ok=True)
                ld.to_csv(os.path.join(hist_dir, "跌停板_{}.csv".format(date_compact)),
                          index=False, encoding="utf-8-sig")

        lu_count = len(lu)
        ld_count = len(ld)
        max_board = int(lu["连板数"].max()) if not lu.empty and "连板数" in lu.columns else 0
        blown_rate = 0.0
        if not lu.empty and "炸板次数" in lu.columns and lu_count > 0:
            blown_rate = len(lu[lu["炸板次数"] > 0]) / lu_count * 100

        # 连板梯队分布
        board_tiers = {}
        if not lu.empty and "连板数" in lu.columns:
            for tier, group in lu.groupby("连板数"):
                tier = int(tier)
                if tier >= 2:
                    names = group["名称"].tolist() if "名称" in group.columns else []
                    board_tiers[tier] = names

        # 板块涨停分布 top5
        sector_dist = {}
        if not lu.empty and "所属行业" in lu.columns:
            counts = lu["所属行业"].value_counts()
            for industry, count in counts.head(5).items():
                sector_dist[industry] = int(count)

        # 最高连板龙头
        top_leaders = []
        if not lu.empty and "连板数" in lu.columns and "名称" in lu.columns:
            top = lu.nlargest(3, "连板数")
            for _, row in top.iterrows():
                leader = {"name": row["名称"], "board": int(row["连板数"])}
                if "代码" in row:
                    leader["code"] = row["代码"]
                top_leaders.append(leader)

        history.append({
            "date": hist_date,
            "limit_up_count": lu_count,
            "limit_down_count": ld_count,
            "max_board": max_board,
            "blown_rate": round(blown_rate, 1),
            "board_tiers": board_tiers,
            "sector_dist": sector_dist,
            "top_leaders": top_leaders,
        })

    return history


def summarize_history(history: list) -> str:
    """将历史数据转为文本摘要（含板块分布、连板梯队、龙头存续）"""
    if not history:
        return DataResult(
            content="（无历史数据）",
            warnings=["无历史数据，无法进行跨日情绪对比，情绪阶段判断可能不可靠"],
            data_sources_missing=["history"],
        )

    lines = ["## 近期情绪数据对比"]
    lines.append("| 日期 | 涨停数 | 跌停数 | 最高连板 | 炸板率 |")
    lines.append("|------|--------|--------|---------|--------|")
    for h in history:
        lines.append("| {} | {} | {} | {}板 | {:.1f}% |".format(
            h["date"], h["limit_up_count"], h["limit_down_count"],
            h["max_board"], h["blown_rate"]
        ))

    # 连板梯队变化
    lines.append("")
    lines.append("## 近期连板梯队变化")
    for h in history:
        tiers = h.get("board_tiers", {})
        if tiers:
            tier_parts = []
            for board in sorted(tiers.keys(), reverse=True):
                names = tiers[board]
                tier_parts.append("{}板{}只({})".format(
                    board, len(names), "/".join(names[:3])
                ))
            lines.append("- {}：{}".format(h["date"], "；".join(tier_parts)))
        else:
            lines.append("- {}：无连板".format(h["date"]))

    # 板块涨停分布变化
    lines.append("")
    lines.append("## 近期板块涨停分布（各日 top5）")
    for h in history:
        dist = h.get("sector_dist", {})
        if dist:
            parts = ["{}{}只".format(k, v) for k, v in dist.items()]
            lines.append("- {}：{}".format(h["date"], "、".join(parts)))

    # 龙头存续追踪
    lines.append("")
    lines.append("## 近期龙头追踪（各日最高连板前3）")
    for h in history:
        leaders = h.get("top_leaders", [])
        if leaders:
            parts = ["{}({}板)".format(l["name"], l["board"]) for l in leaders]
            lines.append("- {}：{}".format(h["date"], "、".join(parts)))

    return "\n".join(lines)


# ── 东方财富 API 数据获取 ──────────────────────────────────────


def fetch_limit_up_from_eastmoney(date: str) -> pd.DataFrame:
    """从东方财富 API 获取涨停板数据（含连板数）。

    API 地址：push2ex.eastmoney.com/getTopicZTPool
    数据保留约 2 周，盘中实时可用。

    Args:
        date: 日期字符串，如 "2026-04-03" 或 "20260403"
    """
    date_compact = date.replace("-", "")
    url = "https://push2ex.eastmoney.com/getTopicZTPool"
    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": "0",
        "pagesize": "10000",
        "sort": "fbt:asc",
        "date": date_compact,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("data") is None or not data["data"].get("pool"):
            return pd.DataFrame()

        pool = data["data"]["pool"]
        df = pd.DataFrame(pool)
        df = df.rename(columns={
            "c": "代码", "n": "名称", "p": "最新价", "zdp": "涨跌幅",
            "amount": "成交额", "ltsz": "流通市值", "tshare": "总市值",
            "hs": "换手率", "lbc": "连板数", "fbt": "首次封板时间",
            "lbt": "最后封板时间", "fund": "封板资金", "zbc": "炸板次数",
            "hybk": "所属行业",
        })

        # 涨停统计字段是嵌套 dict {days, ct}
        if "zttj" in df.columns:
            df["涨停统计"] = (
                df["zttj"].apply(lambda x: f"{x['days']}/{x['ct']}" if isinstance(x, dict) and x.get("days") else "")
            )
        else:
            df["涨停统计"] = ""

        # 格式化封板时间（补零到 6 位 HHMMSS）
        for col in ("首次封板时间", "最后封板时间"):
            if col in df.columns:
                df[col] = df[col].astype(str).str.zfill(6)

        # 价格/金额单位转换
        if "最新价" in df.columns:
            df["最新价"] = pd.to_numeric(df["最新价"], errors="coerce") / 1000
        for col in ("成交额", "流通市值", "总市值", "封板资金"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        for col in ("涨跌幅", "换手率"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "连板数" in df.columns:
            df["连板数"] = pd.to_numeric(df["连板数"], errors="coerce").astype(int)
        if "炸板次数" in df.columns:
            df["炸板次数"] = pd.to_numeric(df["炸板次数"], errors="coerce").astype(int)

        # 序号
        df.insert(0, "序号", range(1, len(df) + 1))

        # 只保留标准列
        keep = ["序号", "代码", "名称", "涨跌幅", "最新价", "成交额",
                "流通市值", "总市值", "换手率", "封板资金",
                "首次封板时间", "最后封板时间", "炸板次数",
                "涨停统计", "连板数", "所属行业"]
        df = df[[c for c in keep if c in df.columns]]

        logger.info("东方财富 API 获取涨停板 %s: %d 只", date_compact, len(df))
        return df

    except Exception as e:
        logger.warning("东方财富涨停板 API 请求失败 %s: %s", date_compact, e)
        return pd.DataFrame()


def fetch_limit_down_from_eastmoney(date: str) -> pd.DataFrame:
    """从东方财富 API 获取跌停板数据。

    API 地址：push2ex.eastmoney.com/getTopicDTPool
    数据保留约 30 个交易日。

    Args:
        date: 日期字符串，如 "2026-04-03" 或 "20260403"
    """
    date_compact = date.replace("-", "")
    url = "https://push2ex.eastmoney.com/getTopicDTPool"
    params = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": "0",
        "pagesize": "10000",
        "sort": "fund:asc",
        "date": date_compact,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("data") is None or not data["data"].get("pool"):
            return pd.DataFrame()

        pool = data["data"]["pool"]
        df = pd.DataFrame(pool)
        df = df.rename(columns={
            "c": "代码", "n": "名称", "p": "最新价", "zdp": "涨跌幅",
            "amount": "成交额", "ltsz": "流通市值", "tshare": "总市值",
            "hs": "换手率", "fund": "封单资金",
            "lbt": "最后封板时间", "amt": "板上成交额",
            "hybk": "所属行业",
        })

        if "zbc" in df.columns:
            df = df.rename(columns={"zbc": "开板次数"})
        if "lbc" in df.columns:
            df = df.rename(columns={"lbc": "连续跌停"})

        # 格式化封板时间
        if "最后封板时间" in df.columns:
            df["最后封板时间"] = df["最后封板时间"].astype(str).str.zfill(6)

        # 价格/金额单位转换
        if "最新价" in df.columns:
            df["最新价"] = pd.to_numeric(df["最新价"], errors="coerce") / 1000
        for col in ("成交额", "流通市值", "总市值", "封单资金", "板上成交额"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        for col in ("涨跌幅", "换手率"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        for col in ("连续跌停", "开板次数"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(int)

        # 序号
        df.insert(0, "序号", range(1, len(df) + 1))

        # 只保留标准列
        keep = ["序号", "代码", "名称", "涨跌幅", "最新价", "成交额",
                "流通市值", "总市值", "换手率", "封单资金",
                "最后封板时间", "板上成交额", "连续跌停", "开板次数", "所属行业"]
        df = df[[c for c in keep if c in df.columns]]

        logger.info("东方财富 API 获取跌停板 %s: %d 只", date_compact, len(df))
        return df

    except Exception as e:
        logger.warning("东方财富跌停板 API 请求失败 %s: %s", date_compact, e)
        return pd.DataFrame()


def _load_csv(directory: str, filename: str) -> pd.DataFrame:
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        # 先读取文件内容并清除 NUL 字节，避免 pd.read_csv 崩溃
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = f.read().replace("\x00", "")
        df = pd.read_csv(io.StringIO(raw))
        # 兼容英文列名的行情 CSV（baostock 格式）
        col_map = {
            "code": "代码",
            "pctChg": "涨跌幅",
            "amount": "成交额",
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "volume": "成交量",
            "turn": "换手率",
            "date": "日期",
        }
        rename = {k: v for k, v in col_map.items() if k in df.columns and v not in df.columns}
        if rename:
            df = df.rename(columns=rename)
        return df
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _load_current_reviews(daily_dir: str) -> dict:
    """加载当天的复盘文档（实盘模式）"""
    reviews = {}
    review_dir = os.path.join(daily_dir, "review_docs")
    if os.path.isdir(review_dir):
        for md_path in sorted(glob.glob(os.path.join(review_dir, "*.md"))):
            name = os.path.basename(md_path)
            with open(md_path, "r", encoding="utf-8") as f:
                reviews[name] = f.read()
    else:
        # 兼容旧目录结构（复盘文件直接在日期目录下）
        for md_path in glob.glob(os.path.join(daily_dir, "*复盘*.md")):
            name = os.path.basename(md_path)
            with open(md_path, "r", encoding="utf-8") as f:
                reviews[name] = f.read()
    return reviews


def _load_prev_reviews(data_dir: str, current_date: str) -> dict:
    """回测模式：只加载 D-1 及之前的复盘文档（避免前瞻偏差）"""
    reviews = {}
    daily_root = os.path.join(data_dir, "daily")
    if not os.path.isdir(daily_root):
        return reviews

    prev_dates = sorted([
        d for d in os.listdir(daily_root)
        if os.path.isdir(os.path.join(daily_root, d)) and d < current_date
    ])
    # 只加载最近 3 天的复盘（避免上下文过长）
    for prev_date in prev_dates[-3:]:
        prev_dir = os.path.join(daily_root, prev_date)
        review_dir = os.path.join(prev_dir, "review_docs")
        if os.path.isdir(review_dir):
            for md_path in sorted(glob.glob(os.path.join(review_dir, "*.md"))):
                name = f"[{prev_date}] {os.path.basename(md_path)}"
                with open(md_path, "r", encoding="utf-8") as f:
                    reviews[name] = f.read()
        else:
            for md_path in glob.glob(os.path.join(prev_dir, "*复盘*.md")):
                name = f"[{prev_date}] {os.path.basename(md_path)}"
                with open(md_path, "r", encoding="utf-8") as f:
                    reviews[name] = f.read()
    return reviews


def summarize_limit_up(df: pd.DataFrame) -> str:
    """将涨停板 DataFrame 转为分析师可读的文本摘要"""
    if df.empty:
        return "无涨停板数据（涨停板数据为空，情绪分析将不可靠）"

    total = len(df)
    # 按行业统计
    industry_counts = df["所属行业"].value_counts()
    top_industries = industry_counts.head(10)

    # 连板统计
    if "连板数" in df.columns:
        max_board = df["连板数"].max()
        multi_board = df[df["连板数"] > 1].sort_values("连板数", ascending=False)
    else:
        max_board = 0
        multi_board = pd.DataFrame()

    # 炸板统计
    if "炸板次数" in df.columns:
        blown = df[df["炸板次数"] > 0]
        blown_rate = len(blown) / total * 100 if total > 0 else 0
    else:
        blown_rate = 0

    # 封板时间分析
    early_seal = 0  # 早盘封板（9:25-9:35）
    late_seal = 0   # 尾盘封板（14:00 以后）
    if "首次封板时间" in df.columns:
        for _, row in df.iterrows():
            t = str(row["首次封板时间"]).strip()
            if len(t) == 6:  # HHMMSS 格式
                hour_min = t[:4]
            elif ":" in t:
                hour_min = t.replace(":", "")[:4]
            else:
                continue
            try:
                hm = int(hour_min)
                if hm <= 935:
                    early_seal += 1
                elif hm >= 1400:
                    late_seal += 1
            except (ValueError, TypeError):
                continue

    lines = [
        f"## 涨停板概览（共 {total} 只）",
        f"- 最高连板：{max_board} 板",
        f"- 炸板率：{blown_rate:.1f}%",
        f"- 早盘封板（9:35前）：{early_seal} 只（{early_seal/total*100:.0f}%）— 越多说明资金越坚决",
        f"- 尾盘封板（14:00后）：{late_seal} 只（{late_seal/total*100:.0f}%）— 越多说明抢筹/虚假封板风险越大",
        "",
        "### 涨停行业分布（前10）",
    ]
    for industry, count in top_industries.items():
        lines.append(f"- {industry}：{count} 只")

    if not multi_board.empty:
        lines.append("")
        lines.append("### 连板股（2板及以上）")
        for _, row in multi_board.iterrows():
            lines.append(
                f"- {row['名称']}（{row['代码']}）{int(row['连板数'])}板 "
                f"封板资金{row.get('封板资金', 0)/1e8:.1f}亿"
            )

    return "\n".join(lines)


def summarize_limit_down(df: pd.DataFrame) -> str:
    """跌停板摘要（含连续跌停信号和开板次数）"""
    if df.empty:
        return "无跌停板数据（跌停板数据缺失，退潮/冰点判断缺少依据）"

    total = len(df)
    industry_counts = df["所属行业"].value_counts().head(5)

    lines = [
        f"## 跌停板概览（共 {total} 只）",
        "",
        "### 跌停行业分布（前5）",
    ]
    for industry, count in industry_counts.items():
        lines.append(f"- {industry}：{count} 只")

    # 连续跌停股（核按钮信号）
    if "连续跌停" in df.columns:
        multi_down = df[df["连续跌停"] > 1].sort_values("连续跌停", ascending=False)
        if not multi_down.empty:
            lines.append("")
            lines.append("### 连续跌停股（情绪恶化信号）")
            for _, row in multi_down.iterrows():
                name = row.get("名称", "?")
                code = row.get("代码", "?")
                streak = int(row["连续跌停"])
                industry = row.get("所属行业", "?")
                lines.append(f"- {name}（{code}）连续{streak}日跌停，{industry}")

    # 跌停但多次开板的（资金分歧/抄底信号）
    if "开板次数" in df.columns:
        high_open = df[df["开板次数"] >= 2].sort_values("开板次数", ascending=False)
        if not high_open.empty:
            lines.append("")
            lines.append("### 多次开板跌停股（资金博弈激烈）")
            for _, row in high_open.head(5).iterrows():
                name = row.get("名称", "?")
                opens = int(row["开板次数"])
                lines.append(f"- {name} 开板{opens}次")

    return "\n".join(lines)


def load_stock_pool(data_dir: str) -> str:
    """从 stocks.md 加载股票池辨识度信息，返回文本摘要

    只提取⭐标记的辨识度核心股，按板块分组输出。
    """
    stocks_path = os.path.join(data_dir, "stocks.md")
    if not os.path.exists(stocks_path):
        return ""

    with open(stocks_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 解析各板块的⭐股票
    sectors = {}
    current_sector = None
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("## ") and "（" in line:
            current_sector = line[3:].split("（")[0].strip()
        elif current_sector and "|" in line and "⭐" in line:
            parts = [p.strip() for p in line.split("|")]
            # 表格格式: | 股票 | 地位 | 备注 |
            if len(parts) >= 4:
                name = parts[1]
                note = parts[3] if parts[3] else ""
                if current_sector not in sectors:
                    sectors[current_sector] = []
                entry = name
                if note:
                    entry += f"（{note}）"
                sectors[current_sector].append(entry)

    if not sectors:
        return ""

    lines = ["## 股票池辨识度核心（⭐）"]
    for sector, stocks in sectors.items():
        lines.append(f"- **{sector}**：{'、'.join(stocks)}")
    lines.append("")
    lines.append("说明：⭐为经确认的板块龙头/中军，分析时应重点关注这些标的的动向和地位变化。")
    return "\n".join(lines)


def load_lessons(data_dir: str, max_lessons: int = 15) -> str:
    """加载持久化经验教训库

    从 config 中获取经验库路径，读取历史验证积累的教训。
    只保留最近 max_lessons 条，避免 prompt 过长。
    """
    from config import get_config
    lessons_path = get_config()["lessons_file"]
    if not os.path.exists(lessons_path):
        return ""

    try:
        with open(lessons_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return ""

    lessons = data.get("lessons", [])
    if not lessons:
        return ""

    # 取最近的 max_lessons 条
    recent = lessons[-max_lessons:]
    lines = ["## 历史经验教训（从过往预测验证中总结）",
             "以下是过往预测中被验证的经验教训，请在分析中注意：",
             ""]
    for item in recent:
        date = item.get("date", "?")
        lesson = item.get("lesson", "")
        lines.append(f"- [{date}] {lesson}")

    return "\n".join(lines)


def save_lessons(data_dir: str, date: str, new_lessons: list,
                 what_was_right: list = None, scores: dict = None):
    """保存新的经验教训到持久化库

    Args:
        data_dir: trading 数据根目录
        date: 验证对应的日期（Day D）
        new_lessons: 新的教训列表
        what_was_right: 正确判断列表
        scores: 各维度评分
    """
    from config import get_config
    lessons_path = get_config()["lessons_file"]

    # 加载已有数据
    if os.path.exists(lessons_path):
        try:
            with open(lessons_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            data = {"lessons": [], "history": []}
    else:
        data = {"lessons": [], "history": []}

    # 追加教训
    for lesson in new_lessons:
        data["lessons"].append({"date": date, "lesson": lesson})

    # 追加验证历史记录
    data["history"].append({
        "date": date,
        "scores": scores or {},
        "what_was_right": what_was_right or [],
        "what_was_wrong": new_lessons,
    })

    # 教训总量上限 50 条，超出则删除最旧的
    if len(data["lessons"]) > 50:
        data["lessons"] = data["lessons"][-50:]

    with open(lessons_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_index_data(data_dir: str, date: str) -> str:
    """加载当日指数数据，返回文本摘要"""
    daily_dir = os.path.join(data_dir, "daily", date)
    date_compact = date.replace("-", "")
    csv_path = os.path.join(daily_dir, f"指数_{date_compact}.csv")
    if not os.path.exists(csv_path):
        return ""

    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
    except (pd.errors.EmptyDataError, Exception):
        return ""

    if df.empty:
        return ""

    lines = ["## 指数行情"]
    lines.append("| 指数 | 收盘 | 涨跌幅 | 成交额(亿) |")
    lines.append("|------|------|--------|-----------|")
    for _, row in df.iterrows():
        name = row.get("名称", row.get("代码", "?"))
        close_val = row.get("收盘价", 0)
        pct = row.get("涨跌幅", 0)
        amount = row.get("成交额", 0)
        try:
            amount_yi = float(amount) / 1e8
        except (ValueError, TypeError):
            amount_yi = 0
        lines.append(f"| {name} | {close_val} | {float(pct):+.2f}% | {amount_yi:.0f} |")

    return "\n".join(lines)


def load_capital_flow(data_dir: str, date: str) -> str:
    """加载当日资金流数据（板块资金流+北向资金），返回文本摘要"""
    daily_dir = os.path.join(data_dir, "daily", date)
    date_compact = date.replace("-", "")
    lines = []

    # 板块资金流
    sector_path = os.path.join(daily_dir, f"板块资金流_{date_compact}.csv")
    if os.path.exists(sector_path):
        try:
            df = pd.read_csv(sector_path, encoding="utf-8-sig")
            if not df.empty:
                lines.append("## 板块资金流向（今日）")
                # 取净流入前5和后5
                if "净额" in df.columns or "主力净流入" in df.columns:
                    flow_col = "净额" if "净额" in df.columns else "主力净流入"
                    name_col = "名称" if "名称" in df.columns else df.columns[0]
                    df[flow_col] = pd.to_numeric(df[flow_col], errors="coerce")
                    top5 = df.nlargest(5, flow_col)
                    bot5 = df.nsmallest(5, flow_col)
                    lines.append("**净流入前5**：" + "、".join(
                        f"{r[name_col]}({r[flow_col]/1e8:+.1f}亿)" for _, r in top5.iterrows()
                    ))
                    lines.append("**净流出前5**：" + "、".join(
                        f"{r[name_col]}({r[flow_col]/1e8:+.1f}亿)" for _, r in bot5.iterrows()
                    ))
        except Exception:
            pass

    # 北向资金
    north_path = os.path.join(daily_dir, f"北向资金_{date_compact}.csv")
    if os.path.exists(north_path):
        try:
            df = pd.read_csv(north_path, encoding="utf-8-sig")
            if not df.empty:
                lines.append("\n## 北向资金")
                for _, row in df.iterrows():
                    channel = row.get("通道", row.get("名称", "?"))
                    # 尝试多个可能的列名
                    net = None
                    for col in ["当日成交净买额", "当日净买额", "净买入"]:
                        val = row.get(col)
                        if pd.notna(val) and val != "":
                            try:
                                net = float(val)
                                break
                            except (ValueError, TypeError):
                                pass
                    # 领涨股信息
                    leader = row.get("领涨股", "")
                    leader_pct = row.get("领涨股-涨跌幅", "")
                    if net is not None:
                        lines.append(f"- {channel}：净买入 {net/1e8:+.1f}亿")
                    elif leader:
                        lines.append(f"- {channel}：领涨股 {leader}({leader_pct}%)")
        except Exception:
            pass

    return "\n".join(lines) if lines else ""


def load_memory(memory_dir: str, date: str, max_days: int = 5) -> str:
    """加载跨周期记忆（日期记忆），严格只读取 <= date 的记忆文件

    Args:
        memory_dir: 记忆文件目录（如 data/memory/main/）
        date: 当前分析日期，只加载此日期之前（含）的记忆
        max_days: 最多读取最近几天的记忆
    """
    if not os.path.isdir(memory_dir):
        return ""

    # 列出所有 YYYY-MM-DD.md 文件，按日期排序
    memory_files = []
    for f in os.listdir(memory_dir):
        if f.endswith(".md") and len(f) == 13:  # YYYY-MM-DD.md
            mem_date = f[:-3]  # remove .md
            if mem_date <= date:  # 严格不读取未来数据
                memory_files.append((mem_date, f))

    memory_files.sort(key=lambda x: x[0])
    recent = memory_files[-max_days:]  # 只取最近几天

    if not recent:
        return ""

    lines = ["## 近期复盘记忆（跨周期上下文）",
             "以下是最近几个交易日的复盘总结，帮助你理解市场演变脉络：",
             ""]
    for mem_date, filename in recent:
        filepath = os.path.join(memory_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            # 截取前1500字符避免token爆炸
            if len(content) > 1500:
                content = content[:1500] + "\n...(截断)"
            lines.append(f"### {mem_date}")
            lines.append(content)
            lines.append("")
        except IOError:
            continue

    return "\n".join(lines)


def load_quantitative_rules(data_dir: str = "") -> str:
    """加载量化规律库"""
    from config import get_config
    knowledge_dir = get_config()["knowledge_dir"]
    rules_path = os.path.join(knowledge_dir, "quantitative_rules.json")
    if not os.path.exists(rules_path):
        return ""

    try:
        with open(rules_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return ""

    rules = data.get("rules", [])
    if not rules:
        return ""

    lines = ["## 量化规律参考",
             "以下是从历史数据和短线交易通识中总结的量化规律，请在分析中参考：",
             ""]
    for r in rules:
        lines.append(f"- **[{r['category']}]** {r['rule']}")
        lines.append(f"  → 操作指引：{r['action']}")
        lines.append("")

    return "\n".join(lines)


def summarize_stock_data(df: pd.DataFrame) -> str:
    """个股行情摘要（按板块分组统计涨跌）"""
    if df.empty:
        return DataResult(
            content="无个股行情数据",
            warnings=["个股行情数据为空，板块强弱分析不可靠"],
            data_sources_missing=["stock_csv"],
        )

    lines = ["## 跟踪股票池行情"]

    # 按板块分组
    # 一只股票可能属于多个板块（用 / 分隔）
    records = []
    for _, row in df.iterrows():
        sectors = str(row.get("板块", "未知")).split("/")
        for sector in sectors:
            records.append({
                "板块": sector.strip(),
                "名称": row["名称"],
                "代码": row["代码"],
                "涨跌幅": row.get("涨跌幅", 0),
                "成交额": row.get("成交额", 0),
            })

    expanded = pd.DataFrame(records)
    for sector, group in expanded.groupby("板块"):
        avg_change = group["涨跌幅"].mean()
        top = group.nlargest(3, "涨跌幅")
        bottom = group.nsmallest(2, "涨跌幅")

        lines.append(f"\n### {sector}（均涨跌幅 {avg_change:.2f}%）")
        lines.append("领涨：" + "、".join(
            f"{r['名称']}{r['涨跌幅']:+.1f}%" for _, r in top.iterrows()
        ))
        if len(group) > 3:
            lines.append("领跌：" + "、".join(
                f"{r['名称']}{r['涨跌幅']:+.1f}%" for _, r in bottom.iterrows()
            ))

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# SQLite 数据加载函数（从 retrieval.py 统一到此处）
# ════════════════════════════════════════════════════════════════


def _get_db_path(data_dir: str) -> str:
    """获取 intraday.db 路径。"""
    return os.path.join(data_dir, "intraday", "intraday.db")


def load_stock_detail(
    data_dir: str,
    name: Optional[str] = None,
    code: Optional[str] = None,
    date: Optional[str] = None,
    max_date: Optional[str] = None,
) -> str:
    """从 intraday 数据库查询个股详细行情（分时快照）。

    至少提供 name 或 code 之一。

    Args:
        max_date: 回测模式下的日期上界（只返回 date <= max_date 的数据，防止未来数据泄露）
    """
    if not name and not code:
        return "请提供 name 或 code 参数"

    target_date = date or datetime.now().strftime("%Y-%m-%d")
    date_fallback = False
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return DataResult(
            content="无数据",
            warnings=["intraday.db 不存在，无法查询个股详情"],
            data_sources_missing=["intraday_db"],
        )

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception:
        return DataResult(
            content="无数据",
            warnings=["数据库连接失败"],
            data_sources_missing=["intraday_db"],
        )

    try:
        # 先检查指定日期是否有数据，无数据且用户未显式指定日期时 fallback
        has_date_data = conn.execute(
            "SELECT 1 FROM snapshots WHERE date = ? LIMIT 1", (target_date,)
        ).fetchone()

        if not has_date_data and not date:
            # 回测模式下，fallback 不超过 max_date
            if max_date:
                fallback_row = conn.execute(
                    "SELECT date FROM snapshots WHERE date <= ? ORDER BY date DESC LIMIT 1",
                    (max_date,),
                ).fetchone()
            else:
                fallback_row = conn.execute(
                    "SELECT date FROM snapshots ORDER BY date DESC LIMIT 1"
                ).fetchone()
            if fallback_row:
                target_date = fallback_row[0]
                date_fallback = True

        conditions = ["date = ?"]
        params: list = [target_date]
        # 回测模式下，额外校验日期不超过 max_date
        if max_date and target_date > max_date:
            return DataResult(
                content="无数据（回测模式下日期超出范围）",
                warnings=[f"请求日期 {target_date} 超出回测截止日期 {max_date}"],
                data_sources_missing=["intraday_snapshot"],
            )
        if code:
            conditions.append("code LIKE ?")
            params.append(f"%{code}%")
        if name:
            conditions.append("name LIKE ?")
            params.append(f"%{name}%")

        where = " AND ".join(conditions)
        query = f"""
            SELECT date, ts, code, name, price, pctChg,
                   open, high, low, last_close,
                   volume, amount, amount_yi,
                   is_limit_up, is_limit_down, sector
            FROM snapshots WHERE {where} ORDER BY ts
        """
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    if not rows:
        return DataResult(
            content="无数据（未找到匹配的股票快照）",
            warnings=[f"未找到 {name or code} 在 {target_date} 的分时数据"],
            data_sources_missing=["intraday_snapshot"],
        )

    lines = []
    first = rows[0]
    stock_name = first["name"]
    stock_code = first["code"]
    sector = first["sector"] or ""
    if date_fallback:
        lines.append(f"> 注：今日为非交易日，已自动切换到最近交易日 {target_date}\n")
    header = f"## {stock_name}（{stock_code}）{f' - {sector}' if sector else ''}"
    header += f"\n日期: {target_date}，共 {len(rows)} 条快照\n"
    lines.append(header)

    lines.append("| 时间 | 价格 | 涨跌幅 | 成交额(亿) | 涨停 |")
    lines.append("|------|------|--------|-----------|------|")
    for row in rows:
        ts = row["ts"]
        price = row["price"] or 0
        pct = row["pctChg"] or 0
        amt = row["amount_yi"] or 0
        limit = "是" if row["is_limit_up"] else ""
        lines.append(f"| {ts} | {price:.2f} | {pct:+.2f}% | {amt:.2f} | {limit} |")

    prices = [r["price"] for r in rows if r["price"]]
    if prices:
        lines.append("")
        lines.append(
            f"开盘 {prices[0]:.2f}，最高 {max(prices):.2f}，"
            f"最低 {min(prices):.2f}，收盘 {prices[-1]:.2f}"
        )

    return "\n".join(lines)


def load_market_snapshot(
    data_dir: str,
    date: Optional[str] = None,
    time: Optional[str] = None,
    name: Optional[str] = None,
    code: Optional[str] = None,
    mode: Optional[str] = "overview",
    sort_by: Optional[str] = "pctChg",
    top_n: Optional[int] = None,
    max_date: Optional[str] = None,
) -> str:
    """获取行情快照数据（支持概览/个股/股票池模式）。

    数据源: SQLite 优先, mootdx 实时 fallback。

    Args:
        max_date: 回测模式下的日期上界（只返回 date <= max_date 的数据，防止未来数据泄露）
    """
    ds = date or datetime.now().strftime("%Y-%m-%d")
    db_path = _get_db_path(data_dir)
    rows = []
    actual_ts = None
    date_fallback = False

    if os.path.exists(db_path):
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            has_data = conn.execute(
                "SELECT 1 FROM snapshots WHERE date = ? LIMIT 1", (ds,)
            ).fetchone()

            # 用户未显式指定日期时，自动 fallback 到最近一个交易日
            if not has_data and not date:
                # 回测模式下 fallback 不超过 max_date
                if max_date:
                    fallback_row = conn.execute(
                        "SELECT date FROM snapshots WHERE date <= ? ORDER BY date DESC LIMIT 1",
                        (max_date,),
                    ).fetchone()
                else:
                    fallback_row = conn.execute(
                        "SELECT date FROM snapshots ORDER BY date DESC LIMIT 1"
                    ).fetchone()
                if fallback_row:
                    ds = fallback_row[0]
                    has_data = True
                    date_fallback = True

            if has_data:
                if time and time not in ("close", "latest"):
                    ts_row = conn.execute(
                        "SELECT ts FROM snapshots WHERE date = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                        (ds, time + ":59"),
                    ).fetchone()
                else:
                    ts_row = conn.execute(
                        "SELECT ts FROM snapshots WHERE date = ? ORDER BY ts DESC LIMIT 1",
                        (ds,),
                    ).fetchone()

                if ts_row:
                    actual_ts = ts_row[0]
                    conditions = ["date = ?", "ts = ?"]
                    params: list = [ds, actual_ts]
                    if code:
                        conditions.append("code LIKE ?")
                        params.append(f"%{code}%")
                    if name:
                        conditions.append("name LIKE ?")
                        params.append(f"%{name}%")
                    if mode == "pool":
                        conditions.append("in_pool = 1")

                    where = " AND ".join(conditions)
                    sort_col = (
                        "amount_yi" if sort_by == "amount"
                        else "volume" if sort_by == "volume"
                        else "pctChg"
                    )
                    query = f"SELECT * FROM snapshots WHERE {where} ORDER BY {sort_col} DESC"
                    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
        except Exception as e:
            logger.error("[load_market_snapshot] DB error: %s", e)
        finally:
            if conn:
                conn.close()

    if not rows:
        return DataResult(
            content=f"无行情数据（{ds} {time or ''}），本地数据库和通达信接口均无数据",
            warnings=[f"{ds} 无行情快照数据"],
            data_sources_missing=["intraday_snapshot"],
        )

    def _fmt_pct(v):
        if v is None: return "-"
        return f"{float(v):+.2f}%"

    def _fmt_price(v):
        if v is None: return "-"
        return f"{float(v):.2f}"

    def _fmt_amt(v):
        if v is None: return "-"
        return f"{float(v):.2f}"

    fallback_note = ""
    if date_fallback:
        fallback_note = f"\n> 注：今日为非交易日，已自动切换到最近交易日 {ds}\n"

    if mode == "stock":
        n = top_n or 5
        filtered = rows[:n]
        r = filtered[0]
        lines = [
            fallback_note,
            f"## {r.get('name','')}（{r.get('code','')}）",
            f"日期: {ds}  时间: {actual_ts}", "",
            "| 代码 | 名称 | 现价 | 涨跌幅 | 开盘 | 最高 | 最低 | 成交额(亿) |",
            "|------|------|------|--------|------|------|------|-----------|",
        ]
        for r in filtered:
            lines.append(
                f"| {r['code']} | {r['name']} | {_fmt_price(r.get('price'))} "
                f"| {_fmt_pct(r.get('pctChg'))} | {_fmt_price(r.get('open'))} "
                f"| {_fmt_price(r.get('high'))} | {_fmt_price(r.get('low'))} "
                f"| {_fmt_amt(r.get('amount_yi'))} |"
            )
        return "\n".join(lines)

    # overview / pool
    n = top_n or 10
    limit_ups = [r for r in rows if r.get("is_limit_up")]
    limit_downs = [r for r in rows if r.get("is_limit_down")]
    up_count = sum(1 for r in rows if (r.get("pctChg") or 0) > 0)
    down_count = sum(1 for r in rows if (r.get("pctChg") or 0) < 0)
    total_amount = sum(r.get("amount_yi", 0) or 0 for r in rows)

    sorted_rows = sorted(rows, key=lambda r: r.get("pctChg", 0) or 0, reverse=True)
    top_gainers = sorted_rows[:n]
    top_losers = sorted_rows[-n:][::-1]

    label = "股票池" if mode == "pool" else "全市场"
    lines = [
        fallback_note,
        f"## 行情概览（{label}）",
        f"日期: {ds}  时间: {actual_ts}  总数: {len(rows)}",
        f"涨: {up_count}  跌: {down_count}  涨停: {len(limit_ups)}  跌停: {len(limit_downs)}  总成交: {total_amount:.1f}亿",
        "", f"### 涨幅 TOP{n}",
        "| 代码 | 名称 | 现价 | 涨跌幅 | 成交额(亿) |",
        "|------|------|------|--------|-----------|",
    ]
    for r in top_gainers:
        lines.append(f"| {r['code']} | {r['name']} | {_fmt_price(r.get('price'))} | {_fmt_pct(r.get('pctChg'))} | {_fmt_amt(r.get('amount_yi'))} |")

    lines += ["", f"### 跌幅 TOP{n}"]
    for r in top_losers:
        lines.append(f"| {r['code']} | {r['name']} | {_fmt_price(r.get('price'))} | {_fmt_pct(r.get('pctChg'))} | {_fmt_amt(r.get('amount_yi'))} |")

    if limit_ups and mode != "pool":
        lines += ["", f"### 涨停（{len(limit_ups)}只）"]
        for r in limit_ups:
            amt = r.get("amount_yi", 0) or 0
            lines.append(f"- {r['name']}（{r['code']}）{amt:.1f}亿")

    return "\n".join(lines)


def scan_trend_stocks(
    data_dir: str,
    date: Optional[str] = None,
    min_pct: float = 3.0,
    max_pct: Optional[float] = None,
    sector: Optional[str] = None,
    ma_type: str = "both",
    top_n: int = 30,
    hot_only: bool = False,
    max_date: Optional[str] = None,
) -> str:
    """全市场趋势股扫描 — 寻找沿5日线或10日线上方运行的趋势股。

    从 intraday.db 读取日线收盘数据，计算 MA5/MA10。

    Args:
        max_date: 回测模式下的日期上界（只使用 date <= max_date 的数据，防止未来数据泄露）
    """
    ds = date or datetime.now().strftime("%Y-%m-%d")
    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return DataResult(
            content="无数据",
            warnings=["intraday.db 不存在，无法扫描趋势股"],
            data_sources_missing=["intraday_db"],
        )

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception:
        return DataResult(
            content="无数据",
            warnings=["数据库连接失败"],
            data_sources_missing=["intraday_db"],
        )

    try:
        # 回测模式下，只使用 <= max_date 的交易日
        date_filter = max_date or date or datetime.now().strftime("%Y-%m-%d")
        trading_days = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM snapshots "
                "WHERE ts = '15:00:00' AND date <= ? ORDER BY date DESC LIMIT 12",
                (date_filter,),
            ).fetchall()
        ]
        if len(trading_days) < 6:
            return DataResult(
                content=f"数据不足：仅 {len(trading_days)} 个交易日，至少需要6个",
                warnings=["日线数据不足，无法计算均线"],
                data_sources_missing=["daily_snapshots"],
            )

        today = trading_days[0]
        calc_days = trading_days[:11]

        placeholders = ",".join(["?"] * len(calc_days))
        rows = conn.execute(f"""
            SELECT date, code, name, price, pctChg, open, high, low,
                   amount_yi, volume, sector, star, in_pool
            FROM snapshots
            WHERE date IN ({placeholders}) AND ts = '15:00:00'
            ORDER BY code, date DESC
        """, calc_days).fetchall()

        if not rows:
            return DataResult(
                content="无收盘数据",
                warnings=["SQLite 中无收盘快照数据"],
                data_sources_missing=["daily_snapshots"],
            )

        stock_data = {}
        for r in rows:
            code = r["code"]
            if code not in stock_data:
                stock_data[code] = {
                    "prices": {}, "name": r["name"],
                    "sector": r["sector"] or "", "star": r["star"], "in_pool": r["in_pool"],
                }
            stock_data[code]["prices"][r["date"]] = r["price"]

        results = []
        need_ma5 = ma_type in ("5", "both")
        need_ma10 = ma_type in ("10", "both")

        for code, sd in stock_data.items():
            prices = sd["prices"]
            if today not in prices:
                continue
            today_price = prices[today]
            if not today_price or today_price <= 0:
                continue

            sorted_dates = sorted(prices.keys(), reverse=True)
            today_row = next(
                (r for r in rows if r["code"] == code and r["date"] == today), None
            )
            if not today_row:
                continue
            today_pct = today_row["pctChg"] or 0
            today_amount = today_row["amount_yi"] or 0

            if today_pct < min_pct:
                continue
            if max_pct is not None and today_pct > max_pct:
                continue

            ma5 = None
            if need_ma5 and len(sorted_dates) >= 5:
                ma5_dates = sorted_dates[:5]
                ma5_prices = [prices[d] for d in ma5_dates if prices.get(d)]
                if len(ma5_prices) >= 5:
                    ma5 = sum(ma5_prices) / len(ma5_prices)

            ma10 = None
            if need_ma10 and len(sorted_dates) >= 10:
                ma10_dates = sorted_dates[:10]
                ma10_prices = [prices[d] for d in ma10_dates if prices.get(d)]
                if len(ma10_prices) >= 10:
                    ma10 = sum(ma10_prices) / len(ma10_prices)

            above_ma5 = False
            above_ma10 = False

            if need_ma5 and ma5 and today_price >= ma5:
                days_above = sum(1 for d in ma5_dates if prices.get(d, 0) >= ma5 * 0.99)
                if days_above >= 3:
                    above_ma5 = True

            if need_ma10 and ma10 and today_price >= ma10:
                ma10_check = sorted_dates[:min(5, len(sorted_dates))]
                days_above = sum(1 for d in ma10_check if prices.get(d, 0) >= ma10 * 0.99)
                if days_above >= 3:
                    above_ma10 = True

            if not above_ma5 and not above_ma10:
                continue

            results.append({
                "code": code, "name": sd["name"], "price": today_price,
                "pctChg": today_pct, "amount_yi": today_amount,
                "sector": sd["sector"], "star": sd["star"], "in_pool": sd["in_pool"],
                "ma5": round(ma5, 2) if ma5 else None,
                "ma10": round(ma10, 2) if ma10 else None,
                "above_ma5": above_ma5, "above_ma10": above_ma10,
                "dist_ma5": round((today_price / ma5 - 1) * 100, 2) if ma5 else None,
                "dist_ma10": round((today_price / ma10 - 1) * 100, 2) if ma10 else None,
            })

        if hot_only:
            sector_avg = {}
            sector_counts = {}
            for r in results:
                s = r["sector"]
                if not s: continue
                sector_avg.setdefault(s, []).append(r["pctChg"])
                sector_counts[s] = sector_counts.get(s, 0) + 1
            hot_sectors = {
                s for s, pcts in sector_avg.items()
                if sum(pcts) / len(pcts) > 1.0 and sector_counts.get(s, 0) >= 2
            }
            results = [r for r in results if r["sector"] in hot_sectors]

        if sector:
            results = [r for r in results if sector in r["sector"]]

        results.sort(key=lambda x: (-int(x["in_pool"] or 0), -int(x["star"] or 0), -(x["pctChg"] or 0)))
        results = results[:top_n]

        if not results:
            return "未找到符合条件的趋势股"

        lines = [
            f"## 趋势股扫描结果（{ds}）",
            f"筛选条件：涨幅≥{min_pct}%" + (f"≤{max_pct}%" if max_pct else "")
            + f" | 均线类型={ma_type}"
            + (f" | 板块含「{sector}」" if sector else "")
            + (f" | 仅热门板块" if hot_only else ""),
            f"共找到 {len(results)} 只趋势股\n",
            "| 代码 | 名称 | 现价 | 涨幅 | 5日线 | 10日线 | 距5日线 | 距10日线 | 成交额(亿) | 板块 |",
            "|------|------|------|------|-------|--------|---------|----------|-----------|------|",
        ]

        for r in results:
            star_mark = "⭐" if r["star"] else ""
            pool_mark = "🏊" if r["in_pool"] else ""
            name_display = f"{star_mark}{pool_mark}{r['name']}"
            ma5_str = f"{r['ma5']:.2f}" if r["ma5"] else "-"
            ma10_str = f"{r['ma10']:.2f}" if r["ma10"] else "-"
            dist5 = f"{r['dist_ma5']:+.1f}%" if r["dist_ma5"] is not None else "-"
            dist10 = f"{r['dist_ma10']:+.1f}%" if r["dist_ma10"] is not None else "-"
            lines.append(
                f"| {r['code']} | {name_display} | {r['price']:.2f} "
                f"| {r['pctChg']:+.2f}% | {ma5_str} | {ma10_str} "
                f"| {dist5} | {dist10} | {r['amount_yi']:.1f} | {r['sector']} |"
            )

        sector_summary = {}
        for r in results:
            s = r["sector"] or "未知"
            sector_summary.setdefault(s, {"count": 0, "pcts": []})
            sector_summary[s]["count"] += 1
            sector_summary[s]["pcts"].append(r["pctChg"])

        lines.append("\n### 板块分布")
        for s, info in sorted(sector_summary.items(), key=lambda x: -x[1]["count"]):
            avg_pct = sum(info["pcts"]) / len(info["pcts"])
            lines.append(f"- **{s}**：{info['count']}只，平均涨幅 {avg_pct:+.2f}%")

        return "\n".join(lines)

    except Exception as e:
        logger.error("[scan_trend_stocks] error: %s", e)
        return DataResult(
            content=f"扫描失败: {e}",
            warnings=[f"趋势股扫描异常: {e}"],
            data_sources_missing=["intraday_db"],
        )
    finally:
        conn.close()


# ── 个股日线数据（CSV 优先，mootdx fallback）──────────────────────

def load_stock_daily_ohlcv(
    data_dir: str,
    date: str,
    stock_name: str,
) -> Optional[dict]:
    """加载个股日线 OHLCV 数据。

    优先从本地 CSV 读取，缺失时通过 mootdx 在线拉取。
    回测和实盘共用此接口。

    Args:
        data_dir: trading 数据根目录
        date: 日期 (YYYY-MM-DD)
        stock_name: 股票名称

    Returns:
        {"date", "code", "name", "open", "high", "low", "close",
         "pct_chg", "volume", "amount", "last_close"} 或 None
    """
    # Step 1: 尝试从本地 CSV 读取
    result = _load_stock_from_csv(data_dir, date, stock_name)
    if result:
        return result

    # Step 2: intraday.db fallback（全市场快照，可靠的历史数据源）
    result = _load_stock_from_intraday_db(data_dir, date, stock_name)
    if result:
        return result

    # Step 3: mootdx fallback
    return _load_stock_from_mootdx(data_dir, date, stock_name)


def load_stock_daily_ohlcv_by_code(
    data_dir: str,
    date: str,
    stock_code: str,
) -> Optional[dict]:
    """按股票代码加载日线 OHLCV 数据（CSV 优先，mootdx fallback）。

    Args:
        data_dir: trading 数据根目录
        date: 日期 (YYYY-MM-DD)
        stock_code: 股票代码（6位数字，如 "000788"）

    Returns:
        同 load_stock_daily_ohlcv 或 None
    """
    # Step 1: 尝试从本地 CSV 读取
    result = _load_stock_from_csv_by_code(data_dir, date, stock_code)
    if result:
        return result

    # Step 2: mootdx fallback（直接用代码查询，无需名称映射）
    return _load_stock_from_mootdx_by_code(data_dir, date, stock_code)


def _load_stock_from_csv_by_code(
    data_dir: str, date: str, stock_code: str,
) -> Optional[dict]:
    """从本地行情 CSV 按代码查找"""
    import csv as csv_mod
    import io as io_mod

    # 兼容 "000788" 和 "sz.000788" 格式
    normalized = stock_code.strip()
    if "." in normalized:
        normalized = normalized.split(".", 1)[1]

    d_dir = os.path.join(data_dir, "daily", date)
    csv_files = glob.glob(os.path.join(d_dir, "行情_*.csv"))

    for csv_file in csv_files:
        try:
            with open(csv_file, "r", encoding="utf-8-sig") as f:
                raw = f.read().replace("\x00", "")
            for row in csv_mod.DictReader(io_mod.StringIO(raw)):
                code = row.get("代码", "").strip()
                code_short = code.split(".", 1)[1] if "." in code else code
                if code_short == normalized:
                    return _csv_row_to_dict(row, date)
        except Exception:
            continue

    return None


def _load_stock_from_mootdx_by_code(
    data_dir: str, date: str, stock_code: str,
) -> Optional[dict]:
    """通过 mootdx 按代码直接拉取日线数据"""
    try:
        from mootdx.quotes import Quotes

        code = stock_code.strip()
        if "." in code:
            code = code.split(".", 1)[1]

        client = Quotes.factory(market="std")
        df = client.bars(symbol=code, frequency=9, offset=10)
        if df is None or df.empty:
            return None

        target = date.replace("-", "")
        df["date_str"] = df["datetime"].astype(str).str[:10].str.replace("-", "")
        match = df[df["date_str"] == target]
        if match.empty:
            df["date_str2"] = df["datetime"].astype(str).str[:10]
            match = df[df["date_str2"] == date]
        if match.empty:
            return None

        row = match.iloc[-1]
        return {
            "date": date,
            "code": code,
            "name": "",
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "pct_chg": float(row.get("pctChg", 0)),
            "volume": float(row.get("vol", row.get("volume", 0))),
            "amount": float(row.get("amount", 0)),
            "last_close": float(row.get("last_close", 0)),
            "_source": "mootdx",
        }
    except Exception as e:
        logger.debug("[mootdx] by_code fallback 失败 %s %s: %s", date, stock_code, e)
        return None


def _load_stock_from_csv(
    data_dir: str, date: str, stock_name: str,
) -> Optional[dict]:
    """从本地行情 CSV 加载个股数据（处理 NUL 字节）"""
    import csv as csv_mod
    import io as io_mod

    d_dir = os.path.join(data_dir, "daily", date)
    csv_files = glob.glob(os.path.join(d_dir, "行情_*.csv"))
    if not csv_files:
        return None

    for csv_file in csv_files:
        try:
            with open(csv_file, "r", encoding="utf-8-sig") as f:
                raw = f.read().replace("\x00", "")  # 清除 NUL 字节
            for row in csv_mod.DictReader(io_mod.StringIO(raw)):
                name = row.get("名称", "").strip()
                if name == stock_name:
                    return _csv_row_to_dict(row, date)
        except Exception:
            continue

    return None


def _load_stock_from_mootdx(
    data_dir: str, date: str, stock_name: str,
) -> Optional[dict]:
    """通过 mootdx 拉取个股日线数据"""
    try:
        from mootdx.quotes import Quotes

        # 名称→代码映射
        code = _resolve_stock_code(data_dir, stock_name)
        if not code:
            logger.debug("[mootdx] 无法解析 %s 的代码", stock_name)
            return None

        client = Quotes.factory(market="std")
        df = client.bars(symbol=code, frequency=9, offset=10)
        if df is None or df.empty:
            return None

        # 找到目标日期的行
        target = date.replace("-", "")
        df["date_str"] = df["datetime"].astype(str).str[:10].str.replace("-", "")
        match = df[df["date_str"] == target]
        if match.empty:
            # 也尝试 YYYY-MM-DD 格式
            df["date_str2"] = df["datetime"].astype(str).str[:10]
            match = df[df["date_str2"] == date]
        if match.empty:
            return None

        row = match.iloc[-1]
        return {
            "date": date,
            "code": code,
            "name": stock_name,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "pct_chg": float(row.get("pctChg", 0)),
            "volume": float(row.get("vol", row.get("volume", 0))),
            "amount": float(row.get("amount", 0)),
            "last_close": float(row.get("last_close", 0)),
            "_source": "mootdx",
        }
    except Exception as e:
        logger.debug("[mootdx] fallback 失败 %s %s: %s", date, stock_name, e)
        return None


def _load_stock_from_intraday_db(
    data_dir: str, date: str, stock_name: str,
) -> Optional[dict]:
    """从 intraday.db snapshots 表加载个股日线数据（全市场覆盖）"""
    code = _resolve_stock_code(data_dir, stock_name)
    if not code:
        return None

    db_path = _get_db_path(data_dir)
    if not os.path.exists(db_path):
        return None

    try:
        conn = sqlite3.connect(db_path)
        # 取当日最后一条快照作为日线数据
        row = conn.execute(
            "SELECT open, high, low, price, last_close, pctChg, volume, amount "
            "FROM snapshots WHERE code = ? AND date = ? ORDER BY ts DESC LIMIT 1",
            (code, date),
        ).fetchone()
        conn.close()

        if not row:
            return None

        open_price, high, low, close, last_close, pct_chg, volume, amount = row

        # volume 字段可能包含二进制数据，安全转换
        def _safe_float(v, default=0.0):
            try:
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        open_price = _safe_float(open_price)
        if open_price <= 0:
            return None

        return {
            "date": date,
            "code": code,
            "name": stock_name,
            "open": open_price,
            "high": _safe_float(high, open_price),
            "low": _safe_float(low, open_price),
            "close": _safe_float(close, open_price),
            "pct_chg": _safe_float(pct_chg),
            "volume": _safe_float(volume),
            "amount": _safe_float(amount),
            "last_close": _safe_float(last_close),
            "_source": "intraday_db",
        }
    except Exception as e:
        logger.debug("[intraday_db] fallback 失败 %s %s: %s", date, stock_name, e)

    return None


def _resolve_stock_code(data_dir: str, stock_name: str) -> Optional[str]:
    """解析股票名称→代码。优先行情 CSV，其次 intraday.db（全市场），再次涨跌停 CSV"""
    import re

    def _clean_code(raw: str) -> Optional[str]:
        """提取 6 位纯数字代码"""
        if not raw:
            return None
        m = re.search(r'(\d{6})', raw)
        return m.group(1) if m else None

    def _extract_code_from_row(row: dict) -> Optional[str]:
        """从 CSV 行提取代码（兼容新旧格式列名）"""
        return _clean_code(
            row.get("代码", "") or row.get("code", "")
        )

    # 1. 从最近的行情 CSV 查找（股票池）
    daily_root = os.path.join(data_dir, "daily")
    if os.path.isdir(daily_root):
        dirs = sorted(os.listdir(daily_root), reverse=True)
        for d in dirs[:10]:
            csv_files = glob.glob(os.path.join(daily_root, d, "行情_*.csv"))
            for csv_file in csv_files:
                try:
                    import csv as csv_mod
                    import io as io_mod
                    with open(csv_file, "r", encoding="utf-8-sig") as f:
                        raw = f.read().replace("\x00", "")
                    for row in csv_mod.DictReader(io_mod.StringIO(raw)):
                        if row.get("名称", "").strip() == stock_name:
                            code = _extract_code_from_row(row)
                            if code:
                                return code
                except Exception:
                    continue

    # 2. 从 intraday.db 查找（全市场 ~5200 只）
    db_path = _get_db_path(data_dir)
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT DISTINCT code FROM snapshots WHERE name = ? LIMIT 1",
                (stock_name,),
            ).fetchone()
            conn.close()
            if row:
                return row[0]
        except Exception:
            pass

    # 3. 从涨跌停 CSV 查找（全市场）
    if os.path.isdir(daily_root):
        dirs = sorted(os.listdir(daily_root), reverse=True)
        for d in dirs[:10]:
            for csv_pattern in ["涨停板_*.csv", "跌停板_*.csv"]:
                csv_files = glob.glob(os.path.join(daily_root, d, csv_pattern))
                for csv_file in csv_files:
                    try:
                        import csv as csv_mod
                        import io as io_mod
                        with open(csv_file, "r", encoding="utf-8-sig") as f:
                            raw = f.read().replace("\x00", "")
                        for row in csv_mod.DictReader(io_mod.StringIO(raw)):
                            if row.get("名称", "").strip() == stock_name:
                                code = _extract_code_from_row(row)
                                if code:
                                    return code
                    except Exception:
                        continue

    return None


def _csv_row_to_dict(row: dict, date: str) -> dict:
    """CSV 行 → 标准化字典"""
    def _float(val, default=0.0):
        try:
            return float(str(val).replace(",", ""))
        except (ValueError, TypeError):
            return default

    return {
        "date": date,
        "code": row.get("代码", "").strip() or row.get("code", "").strip(),
        "name": row.get("名称", "").strip(),
        "open": _float(row.get("开盘价") or row.get("open")),
        "high": _float(row.get("最高价") or row.get("high")),
        "low": _float(row.get("最低价") or row.get("low")),
        "close": _float(row.get("收盘价") or row.get("close")),
        "pct_chg": _float(row.get("涨跌幅") or row.get("pctChg")),
        "volume": _float(row.get("成交量") or row.get("volume")),
        "amount": _float(row.get("成交额") or row.get("amount")),
        "last_close": _float(row.get("昨收", row.get("前收盘"))),
        "_source": "csv",
    }
