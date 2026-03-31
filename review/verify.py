"""日常验证：对比 Agent 预测与次日实际行情，提取经验教训并持久化"""

from __future__ import annotations

import json
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
    """从报告的策略部分提取推荐标的名称"""
    names = set()
    # 匹配 **股票名称** 或 股票名称（ 格式
    for m in re.finditer(r'\*\*([^*]{2,8})\*\*', report):
        name = m.group(1).strip()
        # 过滤掉非股票名的常见关键词
        skip = {'主线', '支线', '退潮', '总龙头', '板块龙头', '操作方向', '仓位建议',
                '风险提示', '关注标的', '情绪阶段', '一致点', '分歧点', '情绪转换预判',
                '情绪阶段对应策略', '核心数据', '与前日对比'}
        if name not in skip and not any(k in name for k in ['板块', '策略', '建议', '方向', '逻辑']):
            names.add(name)
    return list(names)


def _load_stock_pnl(data_dir: str, day_d1: str, report: str) -> str:
    """从 D+1 行情CSV中查找报告推荐标的的实际涨跌幅"""
    # 找行情CSV
    d1_dir = os.path.join(data_dir, "daily", day_d1)
    csv_files = glob_mod.glob(os.path.join(d1_dir, "行情_*.csv"))
    if not csv_files:
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
        except Exception:
            continue

    if not stock_data:
        return ""

    # 从报告中提取推荐标的
    names = _extract_stock_names(report)
    if not names:
        return ""

    # 查找匹配
    lines = ["## 推荐标的次日实际表现（关键！策略评分依据）", ""]
    lines.append("| 股票 | 开盘价 | 收盘价 | 涨跌幅 | 最高价 | 最低价 |")
    lines.append("|------|--------|--------|--------|--------|--------|")
    found = 0
    for name in names:
        if name in stock_data:
            row = stock_data[name]
            lines.append(
                f"| {name} | {row.get('开盘价', '-')} | {row.get('收盘价', '-')} "
                f"| {row.get('涨跌幅', '-')}% | {row.get('最高价', '-')} | {row.get('最低价', '-')} |"
            )
            found += 1

    if found == 0:
        return ""

    lines.append("")
    lines.append(f"（共匹配到 {found}/{len(names)} 只推荐标的的次日行情）")
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

    # 调用 LLM 验证
    llm = ChatOpenAI(
        model=cfg["model"],
        base_url=cfg["base_url"],
        temperature=0.1,
    )

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
