"""Layer 2: 量化选股模块

根据 Layer 1 (LLM) 输出的板块方向，从 intraday.db 中筛选并评分涨停标的。
全部逻辑为确定性代码，同输入必同输出。
"""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class ScoredStock:
    """评分后的候选标的"""
    code: str
    name: str
    industry: str
    score: float
    board_count: int  # 连板数
    first_limit_time: str  # 首封时间 HHMMSS
    blown_count: int  # 炸板次数
    amount: float  # 成交额
    price: float  # 涨停价
    score_breakdown: dict = field(default_factory=dict)


# ── 评分参数（可调优） ──────────────────────────────

# 首封时间评分
SEAL_TIME_SCORES = {
    "S": 5,  # 9:25-9:35 开盘5分钟内
    "A": 4,  # 9:35-10:00
    "B": 3,  # 10:00-10:30
    "C": 2,  # 10:30-13:00
    "D": 1,  # 13:00-14:30
    "F": 0,  # 14:30-15:00
}

# 炸板次数评分
BLOWN_SCORES = {
    0: 3,   # 无炸板
    1: 1,   # 炸板1次
}
BLOWN_DEFAULT = -2  # 炸板2次及以上

# 量能门槛（元）
VOLUME_THRESHOLDS = {
    "small": 3e8,    # 小盘 < 50亿市值：成交额 ≥ 3亿
    "mid": 5e8,      # 中盘 50-200亿：成交额 ≥ 5亿
    "large": 10e8,   # 大盘 > 200亿：成交额 ≥ 10亿
}
VOLUME_PASS_SCORE = 2
VOLUME_FAIL_SCORE = -3

# 连板高度评分
BOARD_TOP_SCORE = 3    # 板块内连板最高
BOARD_SECOND_SCORE = 1  # 板块内连板次高
BOARD_DEFAULT_SCORE = 0  # 首板或低位


def _classify_seal_time(first_limit_time: str) -> str:
    """将首封时间(HHMMSS)分级为 S/A/B/C/D/F"""
    if not first_limit_time:
        return "F"
    try:
        t = int(first_limit_time)
    except (ValueError, TypeError):
        return "F"

    if t <= 93500:
        return "S"
    elif t <= 100000:
        return "A"
    elif t <= 103000:
        return "B"
    elif t <= 130000:
        return "C"
    elif t <= 143000:
        return "D"
    else:
        return "F"


def _estimate_market_cap(price: float, amount: float) -> str:
    """粗略估算市值档位（基于成交额/换手率近似）

    由于 stock_meta 没有市值字段，用成交额粗略判断：
    - 成交额 > 10亿 → 大盘
    - 成交额 3-10亿 → 中盘
    - 成交额 < 3亿 → 小盘
    """
    if amount > 10e8:
        return "large"
    elif amount > 3e8:
        return "mid"
    else:
        return "small"


def _score_stock(stock: dict, max_board_in_sector: int, second_board_in_sector: int) -> ScoredStock:
    """对单只涨停股评分"""
    breakdown = {}

    # 1. 首封时间评分
    seal_grade = _classify_seal_time(stock["first_limit_time"])
    seal_score = SEAL_TIME_SCORES[seal_grade]
    breakdown["seal_time"] = f"{seal_grade}级={seal_score}"

    # 2. 炸板次数评分
    blown = stock.get("blown_count", 0) or 0
    blown_score = BLOWN_SCORES.get(blown, BLOWN_DEFAULT)
    breakdown["blown"] = f"{blown}次={blown_score}"

    # 3. 量能评分
    amount = stock.get("amount", 0) or 0
    cap_tier = _estimate_market_cap(stock.get("price", 0), amount)
    threshold = VOLUME_THRESHOLDS[cap_tier]
    vol_score = VOLUME_PASS_SCORE if amount >= threshold else VOLUME_FAIL_SCORE
    breakdown["volume"] = f"{amount/1e8:.1f}亿({cap_tier})={vol_score}"

    # 4. 连板高度评分
    board = stock.get("board_count", 1) or 1
    if board >= max_board_in_sector and max_board_in_sector > 1:
        board_score = BOARD_TOP_SCORE
        breakdown["board"] = f"{board}板(最高)={board_score}"
    elif board >= second_board_in_sector and second_board_in_sector > 1:
        board_score = BOARD_SECOND_SCORE
        breakdown["board"] = f"{board}板(次高)={board_score}"
    else:
        board_score = BOARD_DEFAULT_SCORE
        breakdown["board"] = f"{board}板={board_score}"

    total = seal_score + blown_score + vol_score + board_score

    return ScoredStock(
        code=stock["code"],
        name=stock["name"],
        industry=stock.get("industry", ""),
        score=total,
        board_count=board,
        first_limit_time=stock.get("first_limit_time", ""),
        blown_count=blown,
        amount=amount,
        price=stock.get("price", 0),
        score_breakdown=breakdown,
    )


def screen_stocks(
    date: str,
    top_sectors: List[str],
    action_gate: str,
    intraday_db: str,
    concept_db: str,
    max_picks: Optional[int] = None,
) -> List[ScoredStock]:
    """Layer 2 主入口：根据板块方向筛选并评分涨停标的

    Args:
        date: 交易日 YYYY-MM-DD
        top_sectors: Layer 1 输出的板块方向列表
        action_gate: Layer 1 输出的买入门控 ("可买入"/"谨慎"/"空仓")
        intraday_db: intraday.db 路径
        concept_db: stock_concept.db 路径
        max_picks: 最大选股数量（None 则根据 action_gate 决定）

    Returns:
        评分排序后的候选标的列表
    """
    if action_gate == "空仓":
        return []

    if max_picks is None:
        max_picks = 2 if action_gate == "可买入" else 1

    # 1. 获取当日全部涨停股
    conn = sqlite3.connect(intraday_db)
    conn.row_factory = sqlite3.Row
    try:
        limit_up_stocks = conn.execute(
            "SELECT code, name, industry, pct_chg, price, amount, "
            "first_limit_time, last_limit_time, blown_count, board_count "
            "FROM limit_up WHERE date = ?",
            (date,),
        ).fetchall()
    finally:
        conn.close()

    if not limit_up_stocks:
        return []

    limit_up_dicts = [dict(row) for row in limit_up_stocks]

    # 2. 板块匹配：按 industry 直接匹配 + 按 concept 扩展匹配
    matched_codes = set()

    # 2a. industry 直接匹配（limit_up 表的 industry 字段）
    for stock in limit_up_dicts:
        industry = stock.get("industry", "") or ""
        for sector in top_sectors:
            if sector in industry or industry in sector:
                matched_codes.add(stock["code"])
                break

    # 2b. concept 扩展匹配（stock_concept.db）
    concept_conn = sqlite3.connect(concept_db)
    try:
        for sector in top_sectors:
            # 查找包含该板块关键词的概念
            rows = concept_conn.execute(
                "SELECT stock_codes FROM concept_stocks WHERE concept_name LIKE ?",
                (f"%{sector}%",),
            ).fetchall()
            for row in rows:
                try:
                    codes = json.loads(row[0])
                    matched_codes.update(codes)
                except (json.JSONDecodeError, TypeError):
                    pass
    finally:
        concept_conn.close()

    # 3. 取交集：涨停 ∩ 板块匹配
    candidates = [s for s in limit_up_dicts if s["code"] in matched_codes]

    if not candidates:
        # 如果板块匹配后无涨停标的，fallback 到全市场涨停股
        candidates = limit_up_dicts

    # 4. 计算板块内连板高度分布
    board_counts = sorted([s.get("board_count", 1) or 1 for s in candidates], reverse=True)
    max_board = board_counts[0] if board_counts else 1
    second_board = board_counts[1] if len(board_counts) > 1 else 0

    # 5. 逐只评分
    scored = [_score_stock(s, max_board, second_board) for s in candidates]

    # 6. 排序（分数降序，同分按连板高度降序）
    scored.sort(key=lambda x: (x.score, x.board_count), reverse=True)

    # 7. 取 Top N
    return scored[:max_picks]


def format_screening_result(stocks: List[ScoredStock]) -> str:
    """格式化选股结果为可读文本"""
    if not stocks:
        return "Layer 2 选股结果：空仓，无候选标的"

    lines = [f"Layer 2 选股结果：{len(stocks)} 只候选标的\n"]
    for i, s in enumerate(stocks, 1):
        lines.append(f"  {i}. {s.name}({s.code}) 总分={s.score}")
        lines.append(f"     {s.board_count}连板 | 首封{s.first_limit_time} | "
                     f"炸板{s.blown_count}次 | 成交{s.amount/1e8:.1f}亿")
        lines.append(f"     评分明细: {s.score_breakdown}")
    return "\n".join(lines)
