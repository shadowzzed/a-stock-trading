"""日常验证：对比 Agent 预测与次日实际行情，提取经验教训并持久化"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import ChatOpenAI

from .data.loader import (
    load_daily_data,
    summarize_limit_up,
    summarize_limit_down,
    save_lessons,
)
from .graph import DEFAULT_CONFIG
import csv
import re
import glob as glob_mod

logger = logging.getLogger(__name__)


VERIFIER_PROMPT = """你是一位短线交易回测验证官。

你需要对比 Agent 系统在 Day D 的预测与 Day D+1 的实际行情，评估预测准确性。

## 评分维度（每项 1-5 分，满分 20 分）

1. **情绪周期判断**（5分）：情绪阶段是否判断正确？情绪转换风险的识别是否有价值（列出的转换条件次日是否触发）？
2. **主线板块判断**（5分）：主线板块次日是否延续强势？支线/退潮的判断是否准确？
3. **龙头判断**（5分）：龙头次日表现如何？生命周期阶段判断是否准确？
4. **策略实效**（5分）：这是最重要的维度，以次日实际盈亏为准评估：
   - **关注标的次日涨跌幅**：推荐的股票次日实际涨了还是跌了？涨跌幅多少？
   - **操作逻辑是否可执行**：推荐"打板"的次日是否有打板机会？推荐"低吸"的是否有低吸位？
   - **仓位建议是否合理**：建议重仓时次日是否大涨？建议空仓时次日是否大跌？反过来就扣分
   - **风险提示是否有效**：提示的风险次日是否兑现？
   - 评分标准：推荐标的次日平均涨幅>3%=5分，1-3%=4分，0-1%=3分，-1~0%=2分，<-1%=1分（仅作参考，需结合操作逻辑综合判断）

注意：不再单独评估"方向判断"（涨/跌/震荡），因为方向本身不可预测。重点看情绪定位是否准确、策略是否产生实际盈利。

## 输出格式（严格 JSON）

```json
{
  "scores": {
    "sentiment": {"score": 3, "reason": "..."},
    "sector": {"score": 4, "reason": "..."},
    "leader": {"score": 3, "reason": "..."},
    "strategy": {"score": 3, "reason": "...（含推荐标的次日实际表现）"}
  },
  "total_score": 13,
  "key_lessons": [
    "教训1：...",
    "教训2：..."
  ],
  "what_was_right": [
    "正确判断1：...",
  ],
  "what_was_wrong": [
    "错误判断1：...",
  ]
}
```

只输出 JSON，不要其他内容。
"""


def _extract_stock_names(report: str) -> list:
    """从报告的策略部分提取推荐标的名称

    支持多种格式：
    - **股票名称**
    - 股票名称（代码）
    - 关注标的：股票名称
    """
    names = set()
    # 过滤掉非股票名的常见关键词（扩展版）
    skip = {
        # 操作相关
        '主线', '支线', '退潮', '总龙头', '板块龙头', '操作方向', '仓位建议',
        '风险提示', '关注标的', '情绪阶段', '一致点', '分歧点', '情绪转换预判',
        '情绪阶段对应策略', '核心数据', '与前日对比', '重点关注', '建议关注',
        # 市场术语
        '涨停板', '跌停板', '连板', '首板', '二板', '三板', '四板', '五板',
        '龙头', '中军', '补涨', '跟风', '分化', '一致', '分歧', '加速',
        '核心', '主线', '支线', '热点', '题材', '概念', '板块', '方向', '关注',
        # 操作动作
        '低吸', '打板', '追高', '抄底', '止损', '止盈', '持有', '观望',
        '加仓', '减仓', '空仓', '重仓', '轻仓', '满仓', '清仓',
        # 常见板块名后缀（需要过滤）
        '光伏', '锂电', '半导体', '医药', '消费', '新能源', '汽车', '芯片',
    }

    # 跳过词模式（包含这些词的短语跳过）
    skip_patterns = [
        '板块', '策略', '建议', '方向', '逻辑', '分析', '观点', '判断',
        '重点', '推荐', '标的', '操作', '仓位', '风险',
    ]

    # 模式1: 从列表项中提取 **股票名称** (更严格的上下文)
    # 匹配 "1. **股票名**：" 或 "- **股票名**：" 格式
    for m in re.finditer(r'[-\d.]+\s*\*\*([^*]{2,8}?)\*\*[:：]', report):
        name = m.group(1).strip()
        if name not in skip:
            names.add(name)

    # 模式2: 从 "关注标的" 部分提取（带括号代码或纯列表）
    # 匹配 "关注标的：股票名（代码）、股票名（代码）" 或 "**关注**：股票名、股票名"
    for m in re.finditer(r'(?:\*\*关注\*\*|关注标的|重点关注|推荐标的)[:：]\s*([^。\n]{10,500})', report):
        section = m.group(1)
        # 提取 "股票名（代码）"
        for name_match in re.finditer(r'([\u4e00-\u9fa5]{2,4})[（(]\d{6}[）)]', section):
            name = name_match.group(1).strip()
            if name not in skip and name != '关注':
                names.add(name)
        # 提取纯中文股票名（在分隔符之间或行尾）
        for name_match in re.finditer(r'([\u4e00-\u9fa5]{2,4})[、，,;\s]|([\u4e00-\u9fa5]{2,4})$', section):
            name = (name_match.group(1) or name_match.group(2)).strip()
            if name and name not in skip and name != '关注':
                names.add(name)

    # 模式3: 一般的 **股票名称** 格式（但必须不在跳过词模式中）
    for m in re.finditer(r'\*\*([^*]{2,8}?)\*\*', report):
        name = m.group(1).strip()
        # 检查是否在跳过列表中，或者包含跳过模式
        if name in skip:
            continue
        if any(p in name for p in skip_patterns):
            continue
        # 进一步过滤：必须是2-4个汉字，或包含"股份"、"集团"等后缀
        if re.match(r'^[\u4e00-\u9fa5]{2,4}$', name):
            names.add(name)

    return sorted(names)


def _query_intraday_db(data_dir: str, date: str, stock_name: str) -> Optional[dict]:
    """从 intraday SQLite DB 查询指定股票的行情数据（CSV fallback）

    Returns:
        dict with keys: code, name, open, high, low, close, pct_chg, volume, amount, is_limit_up, is_limit_down
        or None if not found
    """
    import sqlite3 as sqlite3_mod

    db_path = os.path.join(data_dir, "intraday", "intraday.db")
    if not os.path.exists(db_path):
        return None

    try:
        conn = sqlite3_mod.connect(f"file:{db_path}?mode=ro")
        conn.row_factory = sqlite3_mod.Row
        cursor = conn.cursor()
        # 取当天最后一条快照
        cursor.execute("""
            SELECT code, name, open, high, low, price as close,
                   pctChg as pct_chg, volume, amount, amount_yi,
                   is_limit_up, is_limit_down
            FROM snapshots
            WHERE date = ? AND name = ?
            ORDER BY ts DESC LIMIT 1
        """, (date, stock_name))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "code": row["code"],
                "name": row["name"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "pct_chg": row["pct_chg"],
                "volume": row["volume"],
                "amount": row["amount"],
                "amount_yi": row["amount_yi"],
                "is_limit_up": row["is_limit_up"],
                "is_limit_down": row["is_limit_down"],
            }
    except Exception as e:
        logger.debug(f"[验证] DB查询失败: {e}")
    return None


def _enrich_from_db(data_dir: str, date: str, daily_data) -> str:
    """用 intraday DB 补充 CSV 中缺失的涨停/跌停股票数据

    对比 CSV 加载的涨停/跌停列表 vs DB 完整列表，找出 CSV 遗漏的股票并生成补充文本。
    """
    import sqlite3 as sqlite3_mod

    db_path = os.path.join(data_dir, "intraday", "intraday.db")
    if not os.path.exists(db_path):
        return ""

    try:
        conn = sqlite3_mod.connect(f"file:{db_path}?mode=ro")
        cursor = conn.cursor()

        # DB 中的涨停股
        cursor.execute("""
            SELECT code, name, pctChg, price, is_limit_up
            FROM snapshots
            WHERE date = ? AND is_limit_up = 1
            AND ts = (SELECT MAX(ts) FROM snapshots WHERE date = ?)
            ORDER BY pctChg DESC
        """, (date, date))
        db_limit_up = {row[1]: {"code": row[0], "pct": row[2], "price": row[3]} for row in cursor.fetchall()}

        # DB 中的跌停股
        cursor.execute("""
            SELECT code, name, pctChg, price, is_limit_down
            FROM snapshots
            WHERE date = ? AND is_limit_down = 1
            AND ts = (SELECT MAX(ts) FROM snapshots WHERE date = ?)
            ORDER BY pctChg ASC
        """, (date, date))
        db_limit_down = {row[1]: {"code": row[0], "pct": row[2], "price": row[3]} for row in cursor.fetchall()}

        conn.close()
    except Exception as e:
        logger.debug(f"[DB补充] 查询失败: {e}")
        return ""

    # CSV 中已有的涨停/跌停股名
    csv_limit_up_names = set()
    csv_limit_down_names = set()
    if not daily_data.limit_up.empty and "名称" in daily_data.limit_up.columns:
        csv_limit_up_names = set(daily_data.limit_up["名称"].str.strip().tolist())
    if not daily_data.limit_down.empty and "名称" in daily_data.limit_down.columns:
        csv_limit_down_names = set(daily_data.limit_down["名称"].str.strip().tolist())

    # 找出 DB 有但 CSV 没有的股票
    missing_up = {k: v for k, v in db_limit_up.items() if k not in csv_limit_up_names}
    missing_down = {k: v for k, v in db_limit_down.items() if k not in csv_limit_down_names}

    if not missing_up and not missing_down:
        return ""

    lines = ["### ⚠️ CSV 数据不完整，以下为 DB 补充数据（已验证真实行情）", ""]
    if missing_up:
        lines.append(f"**DB 补充涨停**（CSV 中遗漏 {len(missing_up)} 只）：")
        for name, info in sorted(missing_up.items(), key=lambda x: -x[1]["pct"]):
            lines.append(f"- {name}（{info['code']}）涨跌幅 {info['pct']:+.2f}%，收盘 {info['price']:.2f}")
    if missing_down:
        lines.append(f"**DB 补充跌停**（CSV 中遗漏 {len(missing_down)} 只）：")
        for name, info in sorted(missing_down.items(), key=lambda x: x[1]["pct"]):
            lines.append(f"- {name}（{info['code']}）涨跌幅 {info['pct']:+.2f}%，收盘 {info['price']:.2f}")

    logger.info(f"[DB补充] {date}: 补充涨停 {len(missing_up)} 只, 跌停 {len(missing_down)} 只")
    return "\n".join(lines)


def _load_stock_pnl(data_dir: str, day_d1: str, report: str) -> str:
    """从 D+1 行情CSV中查找报告推荐标的的实际涨跌幅

    Returns:
        Markdown 格式的表格，包含推荐标的的次日表现
    """
    # 找行情CSV
    d1_dir = os.path.join(data_dir, "daily", day_d1)
    csv_files = glob_mod.glob(os.path.join(d1_dir, "行情_*.csv"))
    if not csv_files:
        logger.warning(f"[验证] {day_d1} 无行情CSV文件")
        return ""

    # 读取行情数据，建立名称→行情映射
    stock_data = {}
    for csv_file in csv_files:
        try:
            with open(csv_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name = row.get("名称", "").strip()
                    if name:
                        stock_data[name] = row
        except Exception as e:
            logger.debug(f"[验证] 读取CSV失败: {e}")
            continue

    if not stock_data:
        logger.warning(f"[验证] {day_d1} 行情数据为空")
        return ""

    # 从报告中提取推荐标的
    names = _extract_stock_names(report)
    if not names:
        logger.info("[验证] 未从报告中提取到推荐标的")
        return ""

    # 查找匹配
    lines = ["## 推荐标的次日实际表现（关键！策略评分依据）", ""]
    lines.append("| 股票 | 代码 | 开盘价 | 收盘价 | 涨跌幅 | 最高价 | 最低价 | 成交额(亿) |")
    lines.append("|------|------|--------|--------|--------|--------|--------|-----------|")
    found = 0
    total_pct = 0.0
    up_count = 0
    down_count = 0

    for name in names:
        if name in stock_data:
            row = stock_data[name]
            pct_str = row.get('涨跌幅', '0').replace('%', '').replace('+', '').strip()
            try:
                pct = float(pct_str)
                total_pct += pct
                if pct > 0:
                    up_count += 1
                elif pct < 0:
                    down_count += 1
            except ValueError:
                pct = 0.0

            # 成交额转换
            amount = row.get('成交额', '0')
            try:
                amount_yi = float(amount) / 1e8 if amount else 0.0
            except (ValueError, TypeError):
                amount_yi = 0.0

            lines.append(
                f"| {name} | {row.get('代码', '-')} "
                f"| {row.get('开盘价', '-')} | {row.get('收盘价', '-')} "
                f"| {pct:+.2f}% "
                f"| {row.get('最高价', '-')} | {row.get('最低价', '-')} "
                f"| {amount_yi:.2f} |"
            )
            found += 1
        else:
            # CSV 未找到 → fallback 到 intraday DB（方案2）
            db_row = _query_intraday_db(data_dir, day_d1, name)
            if db_row:
                pct = db_row.get("pct_chg", 0.0)
                total_pct += pct
                if pct > 0:
                    up_count += 1
                elif pct < 0:
                    down_count += 1
                lines.append(
                    f"| {name} | {db_row.get('code', '-')} "
                    f"| {db_row.get('open', '-'):.2f} | {db_row.get('close', '-'):.2f} "
                    f"| {pct:+.2f}% "
                    f"| {db_row.get('high', '-'):.2f} | {db_row.get('low', '-'):.2f} "
                    f"| {db_row.get('amount_yi', 0.0):.2f} |"
                )
                found += 1
            else:
                # DB 也找不到，明确标记"无数据，请勿臆测"
                lines.append(f"| {name} | - | - | - | ⚠️ 无数据，请勿臆测涨跌 | - | - | - |")

    if found == 0:
        logger.warning(f"[验证] 未匹配到任何推荐标的的行情数据")
        return ""

    # 添加统计摘要
    avg_pct = total_pct / found if found > 0 else 0.0
    lines.append("")
    lines.append(f"### 统计摘要")
    lines.append(f"- 匹配成功：{found}/{len(names)} 只")
    lines.append(f"- 平均涨跌幅：{avg_pct:+.2f}%")
    lines.append(f"- 上涨：{up_count} 只，下跌：{down_count} 只")

    return "\n".join(lines)


def verify_prediction(
    data_dir: str,
    day_d: str,
    day_d1: str,
    report: Optional[str] = None,
    config: Optional[dict] = None,
) -> dict:
    """验证 Day D 的 Agent 预测，与 Day D+1 实际行情对比

    Args:
        data_dir: trading 数据根目录
        day_d: 预测日期（如 "2026-03-25"）
        day_d1: 验证日期（如 "2026-03-26"）
        report: Day D 的 Agent 报告文本。如果未提供，从文件中读取。
        config: LLM 配置

    Returns:
        验证结果 dict（含 scores、lessons 等）
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}

    # 读取 Day D 的 Agent 报告
    if not report:
        report_path = os.path.join(
            data_dir, "daily", day_d, "agent_05_裁决报告.md"
        )
        if not os.path.exists(report_path):
            # fallback: agent_report_MMDD.md
            date_compact = day_d.replace("-", "")[4:]  # "0325"
            report_path = os.path.join(
                data_dir, "daily", day_d,
                f"agent_report_{date_compact}.md"
            )
        if not os.path.exists(report_path):
            raise FileNotFoundError(
                f"找不到 {day_d} 的 Agent 报告，请先运行当日分析"
            )
        with open(report_path, "r", encoding="utf-8") as f:
            report = f.read()

    # 加载 Day D+1 实际数据
    data_d1 = load_daily_data(data_dir, day_d1)
    d1_summary = f"## {day_d1} 实际行情\n\n"
    d1_summary += summarize_limit_up(data_d1.limit_up) + "\n\n"
    d1_summary += summarize_limit_down(data_d1.limit_down)

    # 加载 D+1 行情CSV，提取推荐标的实际表现
    stock_pnl = _load_stock_pnl(data_dir, day_d1, report)
    if stock_pnl:
        d1_summary += f"\n\n{stock_pnl}"

    # 调用 LLM 验证（Grok 优先，DeepSeek fallback）
    from .graph import _create_llm
    llm = _create_llm({**cfg, "temperature": 0.1})

    verify_msg = f"""## Day D ({day_d}) 的 Agent 预测报告

{report}

---

## Day D+1 ({day_d1}) 的实际行情数据

{d1_summary}

请对比预测与实际，给出评分和教训。"""

    response = llm.invoke([
        SystemMessage(content=VERIFIER_PROMPT),
        HumanMessage(content=verify_msg),
    ])

    # 解析 JSON
    content = response.content
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        content = content.split("```")[1].split("```")[0]

    result = json.loads(content.strip())

    # 保存验证结果到文件
    verify_dir = os.path.join(data_dir, "daily", day_d)
    os.makedirs(verify_dir, exist_ok=True)
    verify_path = os.path.join(verify_dir, "agent_verify.json")
    with open(verify_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 持久化教训到经验库
    new_lessons = result.get("key_lessons", [])
    if new_lessons:
        save_lessons(
            data_dir,
            date=day_d,
            new_lessons=new_lessons,
            what_was_right=result.get("what_was_right", []),
            scores=result.get("scores", {}),
        )
        print(f"[经验库] 新增 {len(new_lessons)} 条教训（来自 {day_d} 验证）")

    return result


def verify_yesterday(data_dir: str, today: str, config: Optional[dict] = None) -> Optional[dict]:
    """便捷函数：验证昨天的预测（如果昨天有报告且今天有数据）

    Args:
        data_dir: trading 数据根目录
        today: 今天的日期
        config: LLM 配置

    Returns:
        验证结果，或 None（如果条件不满足）
    """
    # 找昨天（前一个交易日）
    daily_root = os.path.join(data_dir, "daily")
    if not os.path.isdir(daily_root):
        return None

    all_dates = sorted([
        d for d in os.listdir(daily_root)
        if os.path.isdir(os.path.join(daily_root, d)) and d < today
    ])

    if not all_dates:
        return None

    yesterday = all_dates[-1]

    # 检查是否有昨天的报告
    report_path = os.path.join(daily_root, yesterday, "agent_05_裁决报告.md")
    if not os.path.exists(report_path):
        date_compact = yesterday.replace("-", "")[4:]
        report_path = os.path.join(
            daily_root, yesterday, f"agent_report_{date_compact}.md"
        )
    if not os.path.exists(report_path):
        print(f"[验证跳过] {yesterday} 无 Agent 报告")
        return None

    # 检查是否已验证过
    verify_path = os.path.join(daily_root, yesterday, "agent_verify.json")
    if os.path.exists(verify_path):
        print(f"[验证跳过] {yesterday} 已验证过")
        with open(verify_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # 检查今天是否有数据
    today_dir = os.path.join(daily_root, today)
    if not os.path.isdir(today_dir):
        print(f"[验证跳过] {today} 无行情数据")
        return None

    print(f"[验证] 开始验证 {yesterday} 的预测（对比 {today} 实际行情）...")
    try:
        return verify_prediction(data_dir, yesterday, today, config=config)
    except Exception as e:
        print(f"[验证失败] {e}")
        return None
