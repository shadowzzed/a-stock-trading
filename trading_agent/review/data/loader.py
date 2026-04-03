"""加载每日行情数据（涨停板、跌停板、个股行情CSV）"""

from __future__ import annotations

import json
import os
import glob
from datetime import datetime, timedelta
import pandas as pd
from dataclasses import dataclass, field


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


def load_daily_data(data_dir: str, date: str, history_days: int = 7) -> DailyData:
    """从 trading/daily/YYYY-MM-DD/ 目录加载当日数据

    Args:
        data_dir: trading 根目录（如 /path/to/trading）
        date: 日期字符串，如 "2026-03-24"
    """
    daily_dir = os.path.join(data_dir, "daily", date)
    if not os.path.isdir(daily_dir):
        raise FileNotFoundError(f"找不到 {date} 的数据目录: {daily_dir}")

    # 涨停板
    limit_up = _load_csv(daily_dir, f"涨停板_{date.replace('-', '')}.csv")

    # 跌停板
    limit_down = _load_csv(daily_dir, f"跌停板_{date.replace('-', '')}.csv")

    # 个股行情
    stock_data = _load_csv(daily_dir, f"行情_{date.replace('-', '')}.csv")

    # 复盘文档（从 review_docs/ 子目录加载所有 .md 文件）
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
        ld = _load_csv(hist_dir, "跌停板_{}.csv".format(date_compact))

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
        return "（无历史数据）"

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


def _load_csv(directory: str, filename: str) -> pd.DataFrame:
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
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


def summarize_limit_up(df: pd.DataFrame) -> str:
    """将涨停板 DataFrame 转为分析师可读的文本摘要"""
    if df.empty:
        return "无涨停板数据"

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
        return "无跌停板数据"

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
        return "无个股行情数据"

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
