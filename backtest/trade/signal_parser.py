"""信号解析器 — 从 Agent Markdown 报告中提取结构化交易信号

解析策略：
1. 优先解析 JSON 前置块（focus_stocks, do_actions, position_advice）
2. 正则匹配 Markdown "明日策略"/"关注标的" 节，提取每只标的的操作类型和条件
3. 交叉验证两层结果

v2 改进：
- 新增操作类型：竞价低吸、追涨
- 改进"关注标的"节的股票提取：支持带(代码)格式、支持 N. **股票名** 列表
- 改进操作类型检测：段落级语义分析（观望优先判断）
- 改进条件提取：更多竞价/盘中条件模式
- 改进"观望"优先级：段落明确说"不建议参与"时，不生成信号
- 支持"关注标的"节中的股票名(代码)格式提取 stock_code
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .models import TradeSignal


# 操作类型关键词 -> 匹配优先级从高到低
ACTION_PATTERNS = [
    ("打板", re.compile(r'打板|封板|板上买|排板|涨停[介入买]|板价买')),
    ("竞价买入", re.compile(r'竞价[买入参与]|集合竞价|竞价介入')),
    ("竞价低吸", re.compile(r'竞价.*?低吸|低吸.*?竞价')),
    ("低吸", re.compile(r'低吸|回调买|水下买|绿盘买|低点买|回踩[买接]|逢低[买接]|分时承接.*?低吸|开盘.*?低吸')),
    ("追涨", re.compile(r'追涨|追[入买]|接力|打高')),
    ("卖出", re.compile(r'卖出|清仓|离场|止损|止盈|板砸|竞价卖出')),
    ("观望", re.compile(r'观望|不参与|不追|等确认|看戏|空仓|不建议.*?参与|放弃参与|直接回避|直接放弃|直接规避')),
]

# 竞价/条件关键词
CONDITION_PATTERNS = [
    re.compile(r'需[要]?一字板'),
    re.compile(r'高开([\d.]+)%?以上'),
    re.compile(r'竞价.*?([\d.]+)%'),
    re.compile(r'缩量|放量|换手'),
    re.compile(r'封单.*?亿'),
    re.compile(r'平开或高开'),
    re.compile(r'低开[^，。]*?([\d.]+)%'),
    re.compile(r'竞价.*?红盘'),
    re.compile(r'集合竞价.*?量能'),
    re.compile(r'分时承接[强弱]|分时均线'),
    re.compile(r'封[板单][意愿].*?强'),
    re.compile(r'9:\d{2}.*?封板'),
]

# 仓位建议 -> 仓位比例映射
POSITION_MAP = [
    (re.compile(r'空仓'), 0.0),
    (re.compile(r'重仓|满仓|5成以上|半仓以上'), 0.5),
    (re.compile(r'双标的各3成|两只各3成'), 0.3),
    (re.compile(r'单只3成|3成'), 0.3),
    (re.compile(r'轻仓|试探|2成|1[成]|0\.5成'), 0.2),
    (re.compile(r'禁止满仓|禁止重仓|总仓位不超(\d)成'), 0.0),
]

# 报告中的非标的关键词（过滤噪声）
STOCK_NAME_STOPWORDS = {
    '主线', '支线', '退潮', '总龙头', '板块龙头', '操作方向', '仓位建议',
    '风险提示', '关注标的', '情绪阶段', '一致点', '分歧点', '情绪转换预判',
    '情绪阶段对应策略', '核心数据', '与前日对比', '策略方向', '明日策略',
    '进攻', '防守', '观望', '试探', '情绪周期', '龙头生态', 'AI',
    # 新格式中的节标题
    '买入条件', '放弃条件', '操作方式', '仓位', '持有条件', '卖出条件',
    '卖出时间', '买入计划', '持仓卖出条件', '空仓判定', '风险点',
    '买入标的', '关注信号', '总体仓位建议', '次日操盘', '关注标的',
    '条件1', '条件2', '条件3', '条件4', '条件5',
    # 操作指令词（易被误识别为股票名）
    '注意', '空仓', '理由', '回避', '谨慎', '重点', '等待', '回避',
    '避雷', '止损', '止盈', '减仓', '加仓', '清仓', '持股', '打板',
    # 通用描述词
    '最高连板', '连板梯队', '亏钱效应', '赚钱效应',
}

# 段落级"放弃/观望"判定模式 — 如果段落整体是观望，不生成信号
_WATCH_PATTERNS = [
    re.compile(r'不建议.*?(?:参与|买入|操作|追)'),
    re.compile(r'放弃参与'),
    re.compile(r'直接回避'),
    re.compile(r'直接放弃'),
    re.compile(r'直接规避'),
    re.compile(r'仅.*?观察'),
    re.compile(r'不建议直接参与'),
    re.compile(r'观望逻辑'),
    re.compile(r'作为.*?观察'),
]


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

    # 尝试匹配裸 JSON（在文件开头的 JSON 块）
    # 支持 ```json 包裹和裸 JSON 两种
    json_match = re.search(r'^\s*(\{[^{}]*"focus_stocks"[^{}]*\})', report, re.MULTILINE)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试匹配报告中间的 JSON 块（有些报告在正文中间插入 JSON）
    json_match = re.search(r'\{\s*"market_bias".*?"focus_stocks".*?\}', report, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def _extract_strategy_section(report: str) -> str:
    """提取策略/交易信号节

    支持多种标题格式:
    - ## 五、明日策略
    - ### 买入计划
    - ## 关注标的
    - 五、明日策略 (非标题形式)

    返回标题之后到下一个同级标题之间的内容。
    """
    # 方案1: 搜索 markdown 标题，找包含策略关键词的标题
    strategy_keywords = [
        '买入计划', '买入标的与操作规则', '次日操盘计划', '明日策略',
        '操盘计划', '买入标的', '关注信号',
    ]
    for kw in strategy_keywords:
        pattern = r'(?:^|\n)#{1,6}\s.*?' + re.escape(kw)
        match = re.search(pattern, report)
        if match:
            start = match.start()
            rest = report[start:]
            nl_pos = rest.find('\n')
            if nl_pos >= 0:
                after_nl = rest[nl_pos + 1:]
                end_match = re.search(r'\n#{1,3}\s[^#\n]', after_nl)
                if end_match:
                    section = rest[:nl_pos + 1 + end_match.start()]
                else:
                    section = rest
            else:
                section = rest
            lines = section.split('\n', 1)
            return lines[1] if len(lines) > 1 else ""

    # 方案2: 搜索"关注标的"节（可能在"五、明日策略"下的子节）
    # 匹配 "- **关注标的**[：:]" 或 "关注标的[：:]" 行
    match = re.search(r'(?:^|\n)[-\s]*\*{0,2}关注标的\*{0,2}[\s：:]*\n', report)
    if match:
        start = match.start()
        rest = report[start:]
        end_match = re.search(r'\n#{1,3}\s', rest)
        if end_match:
            section = rest[:end_match.start()]
        else:
            section = rest
        return section

    # 方案3: 搜索"五、明日策略"或"四.明日策略"等中文编号标题
    match = re.search(r'(?:^|\n)[四五六七][、.．]\s*明日策略', report)
    if match:
        start = match.start()
        rest = report[start:]
        nl_pos = rest.find('\n')
        if nl_pos >= 0:
            after_nl = rest[nl_pos + 1:]
            end_match = re.search(r'\n#{1,3}\s[^#\n]', after_nl)
            if end_match:
                section = rest[:nl_pos + 1 + end_match.start()]
            else:
                section = rest
        else:
            section = rest
        lines = section.split('\n', 1)
        return lines[1] if len(lines) > 1 else ""

    return ""


def _parse_strategy_section(
    section: str, signal_date: str, target_date: str,
) -> list[TradeSignal]:
    """解析策略节中的每只标的"""
    signals = []

    # 全局仓位建议
    position_pct = _parse_position_advice(section)

    # 按标的分段 — 支持多种格式
    stock_blocks = _split_by_stock_blocks(section)

    for block in stock_blocks:
        # 提取股票名称和代码
        name, code = _extract_stock_name_and_code(block)

        if not name:
            continue
        if name in STOCK_NAME_STOPWORDS:
            continue
        if any(k in name for k in ['板块', '策略', '建议', '方向', '逻辑', '阶段', '仓位',
                                     '风险', '独狼', '炸板', '封板', '负反馈', '压制',
                                     '跌停', '涨停', '溢价', '早盘', '高度']):
            continue
        # 过滤含数字的名称（如"炸板率40.9%"、"早盘封板0只"）
        if re.search(r'\d', name):
            continue
        # 必须是纯中文2-4字
        if not re.match(r'^[\u4e00-\u9fa5]{2,4}$', name):
            continue

        # 段落级"观望"判定 — 如果整体是观望逻辑，不生成信号
        if _is_watch_only(block):
            continue

        # 识别操作类型 — 只看买入条件文本，排除卖出预案等干扰段落
        buy_text = _extract_buy_condition_text(block)
        action_type = _detect_action_type(buy_text)
        # "卖出"/"清仓" 是卖出预案，不是买入信号
        if action_type in ("卖出", "清仓"):
            continue

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
            stock_code=code,
            action_type=action_type,
            conditions=conditions,
            position_pct=final_pos,
            priority=priority,
            raw_text=block.strip()[:200],
            source="markdown",
        ))

    return signals


def _split_by_stock_blocks(section: str) -> list[str]:
    """将策略节按标的分段，支持多种格式

    支持格式：
    1. #### 标的X：股票名
    2. #### N. 股票名（代码，说明）
    3. 缩进编号列表：  1. **广生堂（300436，...）**
    4. **股票名** 或 - **股票名** 或编号列表
    5. N. **股票名（代码）**：说明
    6. N. **股票名**（板块说明）：说明
    """
    # 格式1: #### 标的X：股票名
    stock_blocks = re.split(r'\n\s*(?=#{2,4}\s*标的)', section)
    if len(stock_blocks) > 1:
        return stock_blocks

    # 格式2: #### N. 股票名（代码）
    stock_blocks = re.split(r'\n\s*(?=#{2,4}\s*\d+\.\s*[\u4e00-\u9fa5])', section)
    if len(stock_blocks) > 1:
        return stock_blocks

    # 格式5/6: N. **股票名（代码）** 或 N. **股票名**（说明）
    # 先检查是否存在这种格式
    if re.search(r'\n\s*\d+\.\s*\*\*[\u4e00-\u9fa5]', section):
        stock_blocks = re.split(r'\n\s*(?=\d+\.\s*\*\*[\u4e00-\u9fa5])', section)
        if len(stock_blocks) > 1:
            return stock_blocks

    # 格式3: 缩进编号列表
    stock_blocks = re.split(r'\n\s*(?=\s*\d+\.\s*\*\*[\u4e00-\u9fa5])', section)
    if len(stock_blocks) > 1:
        return stock_blocks

    # 格式4: 旧的 ** 格式 或 - ** 格式
    stock_blocks = re.split(r'\n\s*(?=-\s*\*\*|→\s*\*\*|\d[、.]\s*\*\*)', section)
    if len(stock_blocks) > 1:
        return stock_blocks

    return [section]


def _extract_stock_name_and_code(block: str) -> tuple[Optional[str], str]:
    """从文本块中提取股票名称和代码

    Returns:
        (name, code) — name 可能为 None, code 可能为空字符串
    """
    name = None
    code = ""

    # 格式1: #### 标的X：股票名（说明）
    heading_match = re.search(r'#{2,4}\s*标的[一二三四五六七八九十\d]*[：:]\s*([\u4e00-\u9fa5]{2,4})', block)
    if heading_match:
        name = heading_match.group(1).strip()

    # 格式2: #### N. 股票名（代码，说明）
    if not name:
        heading_match = re.search(r'#{2,4}\s*\d+\.\s*([\u4e00-\u9fa5]{2,6})\s*[（(]\s*(\d{6})', block)
        if heading_match:
            name = heading_match.group(1).strip()
            code = heading_match.group(2).strip()

    # 格式3: N. **股票名（代码）** 或 N. **股票名（代码，说明）**
    if not name:
        indent_match = re.search(r'\d+\.\s*\*{1,2}\s*([\u4e00-\u9fa5]{2,6})\s*[（(]\s*(\d{6})', block)
        if indent_match:
            name = indent_match.group(1).strip()
            code = indent_match.group(2).strip()

    # 格式4: N. **股票名**（板块/说明）：操作描述
    if not name:
        indent_match = re.search(r'\d+\.\s*\*{1,2}\s*([\u4e00-\u9fa5]{2,4})\s*\*{1,2}\s*[（(]', block)
        if indent_match:
            name = indent_match.group(1).strip()
            # 尝试在括号内找代码
            code_match = re.search(r'\*{1,2}\s*' + re.escape(name) + r'\s*\*{1,2}\s*[（(][^）)]*?(\d{6})', block)
            if code_match:
                code = code_match.group(1).strip()

    # 格式5: **股票名（代码）** 或 **股票名（代码，说明）**
    if not name:
        bold_match = re.search(r'\*\*([\u4e00-\u9fa5]{2,6})\s*[（(]\s*(\d{6})', block)
        if bold_match:
            name = bold_match.group(1).strip()
            code = bold_match.group(2).strip()

    # 格式6: **股票名**（板块说明）— 无代码
    if not name:
        bold_match = re.search(r'\*\*([\u4e00-\u9fa5]{2,4})\*\*', block)
        if bold_match:
            candidate = bold_match.group(1).strip()
            # 确认后面紧跟(板块)格式，排除非股票名
            if re.search(r'\*\*' + re.escape(candidate) + r'\*\*\s*[（(]', block):
                name = candidate
            elif candidate not in STOCK_NAME_STOPWORDS:
                # 最后的 fallback：只要是2-4字纯中文就考虑
                if re.match(r'^[\u4e00-\u9fa5]{2,4}$', candidate):
                    name = candidate

    # 格式7: 股票名（代码）— 无加粗
    if not name:
        plain_match = re.search(r'(?<!\*) ([\u4e00-\u9fa5]{2,6})\s*[（(]\s*(\d{6})', block)
        if plain_match:
            name = plain_match.group(1).strip()
            code = plain_match.group(2).strip()

    # 格式8: Markdown TABLE 格式 — | 股票名 | 代码 | ...
    if not name:
        table_match = re.search(
            r'\|\s*([\u4e00-\u9fa5A-Za-z]{2,6}[A-Za-z]?)\s*\|\s*(\d{6})\s*\|',
            block,
        )
        if table_match:
            candidate = table_match.group(1).strip()
            if candidate not in STOCK_NAME_STOPWORDS:
                name = candidate
                code = table_match.group(2).strip()

    return name, code


def _extract_buy_condition_text(block: str) -> str:
    """从标的段落中提取仅包含买入条件的文本，排除卖出预案等干扰段落

    避免卖出预案中的"卖出"、"止损"等关键词被误判为操作类型。
    """
    lines = block.split('\n')
    buy_lines = []
    in_sell_section = False
    sell_keywords = ['卖出预案', '卖出条件', '持仓卖出', 'T+1卖出', '卖出时间']

    for line in lines:
        stripped = line.strip()
        # 检测是否进入卖出/止损相关段落
        if any(kw in stripped for kw in sell_keywords):
            in_sell_section = True
            continue
        # 新的子段落（以 - 或 ** 开头）重置卖出段落状态
        if re.match(r'^\s*[-*]\s*\*{0,2}(买入|条件|操作|仓位|逻辑|预期)', stripped):
            in_sell_section = False
        if not in_sell_section:
            buy_lines.append(line)

    return '\n'.join(buy_lines)


def _is_watch_only(text: str) -> bool:
    """判断段落是否是纯观望逻辑（不建议参与）"""
    for pat in _WATCH_PATTERNS:
        if pat.search(text):
            return True
    return False


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


def _normalize_focus_stocks(focus_stocks: list) -> list[dict]:
    """统一 focus_stocks 格式：支持旧版 ["名称"] 和新版 [{"name":"X","code":"Y"}]"""
    result = []
    for item in focus_stocks:
        if isinstance(item, dict):
            name = item.get("name", "")
            code = item.get("code", "")
            if name:
                result.append({"name": name, "code": code})
        elif isinstance(item, str) and item:
            result.append({"name": item, "code": ""})
    return result


def _enrich_from_json(signals: list[TradeSignal], json_data: dict):
    """用 JSON 数据补充 Markdown 解析结果"""
    focus_stocks = _normalize_focus_stocks(json_data.get("focus_stocks", []))
    do_actions = json_data.get("do_actions", [])
    pos_advice = json_data.get("position_advice", "")

    # 用 JSON 的 focus_stocks 补充 stock_code 和修正 stock_name
    for stock_info in focus_stocks:
        json_name = stock_info["name"]
        json_code = stock_info["code"]
        if not json_name or json_name in STOCK_NAME_STOPWORDS:
            continue
        for signal in signals:
            # 名称匹配（包含关系，处理简称）
            if json_name in signal.stock_name or signal.stock_name in json_name:
                if json_code and not signal.stock_code:
                    signal.stock_code = json_code
                # 用 JSON 中的完整名称覆盖（如"粤电力A"覆盖"粤电力"）
                if len(json_name) > len(signal.stock_name):
                    signal.stock_name = json_name
                break

    # 用 JSON 的 do_actions 补充操作类型
    for action_text in do_actions:
        for signal in signals:
            if signal.stock_name in action_text and signal.action_type == "观望":
                signal.action_type = _detect_action_type(action_text)
                signal.source = "json+markdown"

    # 用 JSON 的 direction + buy_condition 补充操作类型（无 do_actions 时生效）
    for stock_info in focus_stocks:
        json_name = stock_info.get("name", "")
        direction = stock_info.get("direction", "")
        buy_condition = stock_info.get("buy_condition", "")
        if direction != "买入" or not json_name:
            continue
        for signal in signals:
            if (json_name in signal.stock_name or signal.stock_name in json_name) \
                    and signal.action_type == "观望":
                if buy_condition:
                    detected = _detect_action_type(buy_condition)
                    signal.action_type = detected if detected != "观望" else "低吸"
                else:
                    signal.action_type = "低吸"
                signal.source = "json+markdown"
                break

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
    focus_stocks = _normalize_focus_stocks(json_data.get("focus_stocks", []))
    do_actions = json_data.get("do_actions", [])
    pos_advice = json_data.get("position_advice", "")

    position_pct = _parse_position_advice(pos_advice) if pos_advice else 0.3

    for i, stock_info in enumerate(focus_stocks):
        stock_name = stock_info["name"]
        stock_code = stock_info["code"]
        if not stock_name or stock_name in STOCK_NAME_STOPWORDS:
            continue

        # 从 direction 字段推断操作类型
        direction = stock_info.get("direction", "")
        action_type = "观望"
        if direction == "买入":
            # 从 buy_condition 中检测具体操作类型
            buy_condition = stock_info.get("buy_condition", "")
            if buy_condition:
                detected = _detect_action_type(buy_condition)
                action_type = detected if detected != "观望" else "低吸"
            else:
                action_type = "低吸"  # 有买入方向但无条件，默认低吸

        # 从 do_actions 中找对应的操作类型（覆盖上面）
        for action_text in do_actions:
            if stock_name in action_text:
                action_type = _detect_action_type(action_text)
                break

        signals.append(TradeSignal(
            signal_date=signal_date,
            target_date=target_date,
            stock_name=stock_name,
            stock_code=stock_code,
            action_type=action_type,
            position_pct=position_pct,
            priority=i + 1,
            raw_text=json.dumps(json_data, ensure_ascii=False)[:200],
            source="json",
        ))

    return signals
