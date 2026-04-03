"""信号解析器 — 从 Agent Markdown 报告中提取结构化交易信号

解析策略：
1. 优先解析 JSON 前置块（focus_stocks, do_actions, position_advice）
2. 正则匹配 Markdown "明日策略" 节，提取每只标的的操作类型和条件
3. 交叉验证两层结果
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .models import TradeSignal


# 操作类型关键词 → 匹配优先级从高到低
ACTION_PATTERNS = [
    ("打板", re.compile(r'打板|封板|板上买|排板|涨停[介入买]|板价买')),
    ("竞价买入", re.compile(r'竞价[买入参与]|集合竞价|竞价介入')),
    ("低吸", re.compile(r'低吸|回调买|水下买|绿盘买|低点买|回踩[买接]|逢低')),
    ("观望", re.compile(r'观望|关注|不参与|不追|等确认|看戏')),
]

# 竞价/条件关键词
CONDITION_PATTERNS = [
    re.compile(r'需[要]?一字板'),
    re.compile(r'高开([\d.]+)%?以上'),
    re.compile(r'竞价.*?([\d.]+)%'),
    re.compile(r'缩量|放量|换手'),
    re.compile(r'封单.*?亿'),
]

# 仓位建议 → 仓位比例映射
POSITION_MAP = [
    (re.compile(r'空仓'), 0.0),
    (re.compile(r'重仓|满仓|5成以上|半仓以上'), 0.5),
    (re.compile(r'双标的各3成|两只各3成'), 0.3),
    (re.compile(r'单只3成|3成'), 0.3),
    (re.compile(r'轻仓|试探|2成|1成'), 0.2),
]

# 报告中的非标的关键词（过滤噪声）
STOCK_NAME_STOPWORDS = {
    '主线', '支线', '退潮', '总龙头', '板块龙头', '操作方向', '仓位建议',
    '风险提示', '关注标的', '情绪阶段', '一致点', '分歧点', '情绪转换预判',
    '情绪阶段对应策略', '核心数据', '与前日对比', '策略方向', '明日策略',
    '进攻', '防守', '观望', '试探', '情绪周期', '龙头生态', 'AI',
}


def parse_trade_signals(
    report: str,
    signal_date: str,
    target_date: str,
) -> list[TradeSignal]:
    """从 Agent 报告中解析交易信号

    Args:
        report: Agent 裁决报告全文（Markdown）
        signal_date: 报告日期 (Day D)
        target_date: 计划执行日期 (Day D+1)

    Returns:
        TradeSignal 列表
    """
    signals = []

    # Layer 1: 解析 JSON 前置块
    json_data = _extract_json_block(report)

    # Layer 2: 解析 Markdown 明日策略节
    strategy_section = _extract_strategy_section(report)

    if strategy_section:
        # 从 Markdown 解析详细信号
        md_signals = _parse_strategy_section(
            strategy_section, signal_date, target_date
        )

        # 如果有 JSON 数据，补充信息
        if json_data:
            _enrich_from_json(md_signals, json_data)

        signals.extend(md_signals)

    elif json_data:
        # 只有 JSON，从 JSON 构建信号
        signals.extend(_build_from_json(json_data, signal_date, target_date))

    return signals


def _extract_json_block(report: str) -> Optional[dict]:
    """提取报告开头的 JSON 前置块"""
    # 匹配 ```json ... ``` 或行首 { ... }
    json_match = re.search(r'```json\s*\n(.*?)\n```', report, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试匹配裸 JSON
    json_match = re.search(r'^\s*(\{[^{}]*"focus_stocks"[^{}]*\})', report, re.MULTILINE)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    return None


def _extract_strategy_section(report: str) -> str:
    """提取 '五、明日策略' 或 '明日策略' 节"""
    # 尝试匹配 "## 五、明日策略" 或 "## 明日策略"
    match = re.search(
        r'(?:^|\n)(?:##\s*(?:五[、.]?)?\s*明日策略|四、明日策略)\s*\n(.*?)(?=\n##\s|\Z)',
        report, re.DOTALL,
    )
    if match:
        return match.group(1)

    # fallback: 匹配"关注标的"到文末
    match = re.search(r'关注标的[：:]\s*\n(.*?)(?=\n##\s|\Z)', report, re.DOTALL)
    if match:
        return match.group(0)

    return ""


def _parse_strategy_section(
    section: str, signal_date: str, target_date: str,
) -> list[TradeSignal]:
    """解析策略节中的每只标的"""
    signals = []

    # 全局仓位建议
    position_pct = _parse_position_advice(section)

    # 按标的分段：匹配 **股票名** 或 - **股票名** 或编号列表开头的段落
    # 每个段落描述一只标的（注意缩进空格）
    stock_blocks = re.split(r'\n\s*(?=-\s*\*\*|→\s*\*\*|\d[、.]\s*\*\*)', section)

    for block in stock_blocks:
        # 提取股票名称
        name_match = re.search(r'\*\*([^*]{2,8})\*\*', block)
        if not name_match:
            continue

        name = name_match.group(1).strip()
        if name in STOCK_NAME_STOPWORDS:
            continue
        if any(k in name for k in ['板块', '策略', '建议', '方向', '逻辑', '阶段', '仓位']):
            continue

        # 识别操作类型
        action_type = _detect_action_type(block)

        # 识别条件
        conditions = _detect_conditions(block)

        # 确定仓位
        local_pos = _parse_position_advice(block)
        final_pos = local_pos if local_pos > 0 else position_pct

        # 确定优先级
        priority = 1
        if any(k in block for k in ['备选', '次选', '二选', '备一']):
            priority = 2

        signals.append(TradeSignal(
            signal_date=signal_date,
            target_date=target_date,
            stock_name=name,
            action_type=action_type,
            conditions=conditions,
            position_pct=final_pos,
            priority=priority,
            raw_text=block.strip()[:200],
            source="markdown",
        ))

    return signals


def _detect_action_type(text: str) -> str:
    """从文本中检测操作类型"""
    for action_type, pattern in ACTION_PATTERNS:
        if pattern.search(text):
            return action_type
    return "观望"


def _detect_conditions(text: str) -> list[str]:
    """从文本中提取竞价/盘中条件"""
    conditions = []
    for pattern in CONDITION_PATTERNS:
        match = pattern.search(text)
        if match:
            conditions.append(match.group(0))
    return conditions


def _parse_position_advice(text: str) -> float:
    """从文本中解析仓位建议"""
    for pattern, pct in POSITION_MAP:
        if pattern.search(text):
            return pct
    return 0.3  # 默认 3 成


def _enrich_from_json(signals: list[TradeSignal], json_data: dict):
    """用 JSON 数据补充 Markdown 解析结果"""
    focus_stocks = json_data.get("focus_stocks", [])
    do_actions = json_data.get("do_actions", [])
    pos_advice = json_data.get("position_advice", "")

    # 如果 JSON 中有 focus_stocks 但 Markdown 没解析到，补充
    signal_names = {s.stock_name for s in signals}
    for stock in focus_stocks:
        if stock and stock not in signal_names and stock not in STOCK_NAME_STOPWORDS:
            # JSON 有但 Markdown 没解析到的标的
            pass  # 暂不自动补充，避免误匹配

    # 用 JSON 的 do_actions 补充操作类型
    for action_text in do_actions:
        for signal in signals:
            if signal.stock_name in action_text and signal.action_type == "观望":
                signal.action_type = _detect_action_type(action_text)
                signal.source = "json+markdown"

    # 用 JSON 的 position_advice 补充仓位
    if pos_advice:
        json_pct = _parse_position_advice(pos_advice)
        if json_pct > 0:
            for signal in signals:
                if signal.position_pct == 0.3:  # 还是默认值的
                    signal.position_pct = json_pct


def _build_from_json(
    json_data: dict, signal_date: str, target_date: str,
) -> list[TradeSignal]:
    """仅从 JSON 构建信号（fallback）"""
    signals = []
    focus_stocks = json_data.get("focus_stocks", [])
    do_actions = json_data.get("do_actions", [])
    pos_advice = json_data.get("position_advice", "")

    position_pct = _parse_position_advice(pos_advice) if pos_advice else 0.3

    for i, stock in enumerate(focus_stocks):
        if not stock or stock in STOCK_NAME_STOPWORDS:
            continue

        # 从 do_actions 中找对应的操作类型
        action_type = "观望"
        for action_text in do_actions:
            if stock in action_text:
                action_type = _detect_action_type(action_text)
                break

        signals.append(TradeSignal(
            signal_date=signal_date,
            target_date=target_date,
            stock_name=stock,
            action_type=action_type,
            position_pct=position_pct,
            priority=i + 1,
            raw_text=json.dumps(json_data, ensure_ascii=False)[:200],
            source="json",
        ))

    return signals
