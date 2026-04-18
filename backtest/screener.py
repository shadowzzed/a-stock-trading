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

# 首封时间评分（恢复接近原权重）
SEAL_TIME_SCORES = {
    "S": 5,  # 9:25-9:35
    "A": 4,  # 9:35-10:00
    "B": 3,  # 10:00-10:30
    "C": 2,  # 10:30-13:00
    "D": 1,  # 13:00-14:30
    "F": 0,  # 14:30-15:00
}

# 炸板次数评分（放宽惩罚：最多扣 0 分，避免误杀高位分歧龙头）
BLOWN_SCORES = {
    0: 3,   # 无炸板
    1: 1,   # 炸板1次
    2: 0,   # 炸板2次
    3: 0,   # 炸板3次
}
BLOWN_DEFAULT = -1  # 炸板4次及以上

# 量能门槛（元）
VOLUME_THRESHOLDS = {
    "small": 3e8,
    "mid": 5e8,
    "large": 10e8,
}
VOLUME_PASS_SCORE = 2
VOLUME_FAIL_SCORE = -2   # 略放宽（原 -3）

# 连板高度评分（加分更线性但不会过度主导）
BOARD_PER_LEVEL = 1       # 每板 +1 分
BOARD_LEADER_BONUS = 3    # 板块最高板额外 +3 分（龙头地位）
BOARD_SECOND_BONUS = 1    # 板块次高板额外 +1 分

# ── 新因子（Phase 2） ──────────────────────────

# 板块连续性加分（该板块连续 N 天有涨停）
SECTOR_CONTINUITY_SCORES = {
    1: 0,   # 仅今天有涨停
    2: 1,   # 连续2天
    3: 2,   # 连续3天
}
SECTOR_CONTINUITY_MAX = 3  # 连续4天+

# 龙头带路加分（板块内最高连板数 >= 3）
LEADER_PRESENT_SCORE = 2    # 板块有3板+龙头带路
LEADER_ABSENT_SCORE = 0     # 无明显龙头

# 前日涨停表现（该板块昨日涨停股今日平均涨跌幅）
PREV_PERFORMANCE_GOOD = 2   # 昨日涨停股今日平均 > 3%
PREV_PERFORMANCE_OK = 0     # 昨日涨停股今日平均 0~3%
PREV_PERFORMANCE_BAD = -2   # 昨日涨停股今日平均 < 0%

# ── 反包加分（昨日大跌或炸板多次，今日强反封）──
REVERSAL_PREV_DROP = -3.0     # 昨日跌幅阈值
REVERSAL_PREV_BLOWN = 2        # 昨日炸板次数阈值
REVERSAL_BONUS = 3             # 反包加分

# ── 趋势股筛选参数 ──
TREND_LOOKBACK_DAYS = 3        # 考察近 N 日累计涨幅
TREND_3D_STRONG = 20.0         # 3日累计涨幅 ≥ 20% → 强趋势（下调）
TREND_3D_MEDIUM = 10.0         # 3日累计涨幅 ≥ 10% → 中趋势（下调）
TREND_TODAY_MIN = 3.0          # 当日涨幅至少 3%
TREND_VOLUME_RATIO = 1.15      # 近5日均量 / 近10日均量阈值（下调）
TREND_MIN_DAYS = 5             # 前置需要至少 N 天历史数据

# 趋势股评分
TREND_3D_STRONG_SCORE = 6      # 强趋势加分
TREND_3D_MEDIUM_SCORE = 4      # 中趋势加分
TREND_TODAY_STRONG_SCORE = 3   # 当日 ≥ 5%
TREND_TODAY_NORMAL_SCORE = 2   # 当日 3-5%（原 1，太低）
TREND_VOLUME_UP_SCORE = 2
TREND_SECTOR_MATCH_SCORE = 3   # 板块匹配 top_sectors
TREND_CONSECUTIVE_UP_SCORE = 2 # 近 3 日连续阳线 +2


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


def _query_sector_context(
    intraday_db: str, date: str, industries: List[str],
) -> dict:
    """查询板块上下文信息（连续性、前日表现、龙头高度）

    Returns:
        {industry: {continuity_days, prev_avg_pct, max_board}}
    """
    if not industries:
        return {}

    conn = sqlite3.connect(intraday_db, timeout=10)
    result = {}

    try:
        for industry in industries:
            ctx = {"continuity_days": 1, "prev_avg_pct": 0.0, "max_board": 0}

            # 1. 板块连续性：往前数连续有涨停的天数
            dates_with_lu = conn.execute(
                "SELECT DISTINCT date FROM limit_up "
                "WHERE industry = ? AND date <= ? ORDER BY date DESC LIMIT 10",
                (industry, date),
            ).fetchall()
            if dates_with_lu:
                # 从今天往前数连续天数
                all_trading_dates = [r[0] for r in conn.execute(
                    "SELECT DISTINCT date FROM limit_up WHERE date <= ? ORDER BY date DESC LIMIT 15",
                    (date,),
                ).fetchall()]
                lu_dates = {r[0] for r in dates_with_lu}
                cont = 0
                for d in all_trading_dates:
                    if d in lu_dates:
                        cont += 1
                    else:
                        break
                ctx["continuity_days"] = cont

            # 2. 前日涨停表现：昨日该板块涨停股今日平均涨跌幅
            prev_date_row = conn.execute(
                "SELECT MAX(date) FROM limit_up WHERE date < ?", (date,)
            ).fetchone()
            if prev_date_row and prev_date_row[0]:
                prev_date = prev_date_row[0]
                rows = conn.execute(
                    "SELECT l.code, d.pct_chg "
                    "FROM limit_up l "
                    "JOIN daily_bars d ON l.code = d.code AND d.date = ? "
                    "WHERE l.date = ? AND l.industry = ?",
                    (date, prev_date, industry),
                ).fetchall()
                if rows:
                    avg_pct = sum(r[1] for r in rows if r[1] is not None) / len(rows)
                    ctx["prev_avg_pct"] = avg_pct

            # 3. 板块当日最高连板
            max_board_row = conn.execute(
                "SELECT MAX(board_count) FROM limit_up WHERE date = ? AND industry = ?",
                (date, industry),
            ).fetchone()
            ctx["max_board"] = (max_board_row[0] or 0) if max_board_row else 0

            result[industry] = ctx
    finally:
        conn.close()

    return result


def _score_stock(stock: dict, max_board_in_sector: int, second_board_in_sector: int,
                 sector_ctx: Optional[dict] = None) -> ScoredStock:
    """对单只涨停股评分（含 Phase 2 新因子）"""
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

    # 4. 连板高度评分（核心因子：线性加分 + 龙头奖励）
    board = stock.get("board_count", 1) or 1
    base_board = board * BOARD_PER_LEVEL
    leader_bonus = 0
    if board >= max_board_in_sector and max_board_in_sector >= 2:
        leader_bonus = BOARD_LEADER_BONUS
        breakdown["board"] = f"{board}板(板块最高)={base_board}+{leader_bonus}"
    elif board >= second_board_in_sector and second_board_in_sector >= 2:
        leader_bonus = BOARD_SECOND_BONUS
        breakdown["board"] = f"{board}板(板块次高)={base_board}+{leader_bonus}"
    else:
        breakdown["board"] = f"{board}板={base_board}"
    board_score = base_board + leader_bonus

    total = seal_score + blown_score + vol_score + board_score

    # ── Phase 2 新因子 ──

    industry = stock.get("industry", "")
    ctx = (sector_ctx or {}).get(industry, {})

    # 5. 板块连续性（连续 N 天有涨停）
    cont_days = ctx.get("continuity_days", 1)
    cont_score = SECTOR_CONTINUITY_SCORES.get(
        cont_days, SECTOR_CONTINUITY_MAX
    )
    breakdown["continuity"] = f"{cont_days}天连续={cont_score}"
    total += cont_score

    # 6. 龙头带路（板块内有 3板+ 龙头）
    sector_max_board = ctx.get("max_board", 0)
    if sector_max_board >= 3:
        leader_score = LEADER_PRESENT_SCORE
        breakdown["leader"] = f"有{sector_max_board}板龙头={leader_score}"
    else:
        leader_score = LEADER_ABSENT_SCORE
        breakdown["leader"] = f"无龙头={leader_score}"
    total += leader_score

    # 7. 前日涨停表现（昨日该板块涨停股今日平均涨幅）
    prev_pct = ctx.get("prev_avg_pct", 0)
    if prev_pct > 3:
        prev_score = PREV_PERFORMANCE_GOOD
        breakdown["prev_perf"] = f"昨涨停今+{prev_pct:.1f}%={prev_score}"
    elif prev_pct >= 0:
        prev_score = PREV_PERFORMANCE_OK
        breakdown["prev_perf"] = f"昨涨停今+{prev_pct:.1f}%={prev_score}"
    else:
        prev_score = PREV_PERFORMANCE_BAD
        breakdown["prev_perf"] = f"昨涨停今{prev_pct:.1f}%={prev_score}"
    total += prev_score

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


def _apply_reversal_bonus(stocks: List[dict], date: str, intraday_db: str) -> dict:
    """计算反包加分：昨日大跌或多次炸板，今日涨停 → +3 分

    Returns: {code: bonus_score}
    """
    if not stocks:
        return {}
    conn = sqlite3.connect(intraday_db, timeout=10)
    try:
        prev_row = conn.execute(
            "SELECT MAX(date) FROM daily_bars WHERE date < ?", (date,)
        ).fetchone()
        if not prev_row or not prev_row[0]:
            return {}
        prev_date = prev_row[0]
        codes = [s["code"] for s in stocks]
        placeholders = ",".join("?" * len(codes))
        # 昨日涨跌幅
        prev_pct_map = {}
        rows = conn.execute(
            f"SELECT code, pct_chg FROM daily_bars WHERE date=? AND code IN ({placeholders})",
            (prev_date, *codes),
        ).fetchall()
        for c, p in rows:
            prev_pct_map[c] = p if p is not None else 0
        # 昨日炸板次数（若昨日有涨停记录）
        prev_blown_map = {}
        rows = conn.execute(
            f"SELECT code, blown_count FROM limit_up WHERE date=? AND code IN ({placeholders})",
            (prev_date, *codes),
        ).fetchall()
        for c, b in rows:
            prev_blown_map[c] = b or 0
    finally:
        conn.close()

    bonus = {}
    for s in stocks:
        code = s["code"]
        prev_pct = prev_pct_map.get(code, 0)
        prev_blown = prev_blown_map.get(code, 0)
        if prev_pct <= REVERSAL_PREV_DROP or prev_blown >= REVERSAL_PREV_BLOWN:
            bonus[code] = REVERSAL_BONUS
    return bonus


def _screen_trending_stocks(
    date: str, top_sectors: List[str], intraday_db: str,
) -> List[ScoredStock]:
    """筛选非涨停但强趋势股：3日大涨 + 当日放量

    候选条件（AND）：
    - 有至少 TREND_MIN_DAYS 天历史数据
    - 3日累计涨幅 ≥ TREND_3D_MEDIUM
    - 当日涨幅 ≥ TREND_TODAY_MIN
    - 当日非涨停（避免与主流程重复）
    - 当日非跌停
    """
    conn = sqlite3.connect(intraday_db, timeout=10)
    try:
        # 取当日行情 + 前若干日历史
        trading_dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_bars WHERE date <= ? ORDER BY date DESC LIMIT ?",
            (date, TREND_MIN_DAYS + TREND_LOOKBACK_DAYS),
        ).fetchall()]
        if len(trading_dates) < TREND_LOOKBACK_DAYS + 1:
            return []
        # today + lookback 累计涨幅计算
        base_date = trading_dates[TREND_LOOKBACK_DAYS]  # N 日前的日期
        # 取当日 vs base_date 的 close
        rows = conn.execute(
            "SELECT t.code, sm.name, t.open, t.high, t.low, t.close, "
            "t.pct_chg, t.amount, b.close AS base_close, t.volume "
            "FROM daily_bars t "
            "JOIN daily_bars b ON t.code=b.code AND b.date=? "
            "LEFT JOIN stock_meta sm ON t.code=sm.code AND sm.date=? "
            "WHERE t.date=? AND t.close > 0 AND b.close > 0",
            (base_date, date, date),
        ).fetchall()
        # 行业从 limit_up 最近记录补（stock_meta 无 industry 字段）
        ind_rows = conn.execute(
            "SELECT code, industry FROM limit_up "
            "WHERE date <= ? AND date >= date(?, '-30 days') "
            "GROUP BY code",
            (date, date),
        ).fetchall()
        industry_map = {c: i for c, i in ind_rows if i}
        # 取近 5 日和近 10 日量能
        vol_5d = {}
        vol_10d = {}
        if len(trading_dates) >= 10:
            date_5d = trading_dates[4]  # 5日前
            date_10d = trading_dates[min(9, len(trading_dates)-1)]
            v5_rows = conn.execute(
                "SELECT code, AVG(volume) FROM daily_bars "
                "WHERE date BETWEEN ? AND ? GROUP BY code",
                (date_5d, date),
            ).fetchall()
            vol_5d = {c: v or 0 for c, v in v5_rows}
            v10_rows = conn.execute(
                "SELECT code, AVG(volume) FROM daily_bars "
                "WHERE date BETWEEN ? AND ? GROUP BY code",
                (date_10d, date),
            ).fetchall()
            vol_10d = {c: v or 0 for c, v in v10_rows}
        # 近 3 日连续阳线标记（用于加分）
        consecutive_up_map = {}
        if len(trading_dates) >= 3:
            d_today, d_1, d_2 = trading_dates[0], trading_dates[1], trading_dates[2]
            up_rows = conn.execute(
                "SELECT code FROM daily_bars WHERE date=? AND pct_chg > 0 "
                "INTERSECT SELECT code FROM daily_bars WHERE date=? AND pct_chg > 0 "
                "INTERSECT SELECT code FROM daily_bars WHERE date=? AND pct_chg > 0",
                (d_today, d_1, d_2),
            ).fetchall()
            consecutive_up_map = {r[0]: True for r in up_rows}
    finally:
        conn.close()

    results = []
    for row in rows:
        code, name, o, h, l, c, pct, amount, base_close, vol = row
        industry = industry_map.get(code, "")
        # 过滤 ST/退市
        if name and any(m in name for m in ("ST", "*ST", "退")):
            continue
        if c is None or base_close is None or base_close <= 0:
            continue
        cum_pct = (c - base_close) / base_close * 100
        if cum_pct < TREND_3D_MEDIUM:
            continue
        today_pct = pct or 0
        if today_pct < TREND_TODAY_MIN:
            continue
        # 排除涨停/跌停（20cm: 创业板/科创板，其余 10cm）
        is_20cm = code.startswith(("300", "301", "688"))
        limit_pct = 20 if is_20cm else 10
        if today_pct >= limit_pct - 0.2 or today_pct <= -(limit_pct - 0.2):
            continue

        # 评分
        breakdown = {}
        total = 0
        if cum_pct >= TREND_3D_STRONG:
            total += TREND_3D_STRONG_SCORE
            breakdown["trend_3d"] = f"3日+{cum_pct:.1f}%(强)={TREND_3D_STRONG_SCORE}"
        else:
            total += TREND_3D_MEDIUM_SCORE
            breakdown["trend_3d"] = f"3日+{cum_pct:.1f}%(中)={TREND_3D_MEDIUM_SCORE}"
        if today_pct >= 5:
            total += TREND_TODAY_STRONG_SCORE
            breakdown["trend_today"] = f"今日+{today_pct:.1f}%={TREND_TODAY_STRONG_SCORE}"
        else:
            total += TREND_TODAY_NORMAL_SCORE
            breakdown["trend_today"] = f"今日+{today_pct:.1f}%={TREND_TODAY_NORMAL_SCORE}"
        # 量能
        v5 = vol_5d.get(code, 0)
        v10 = vol_10d.get(code, 0)
        if v10 > 0 and v5 / v10 >= TREND_VOLUME_RATIO:
            total += TREND_VOLUME_UP_SCORE
            breakdown["trend_volume"] = f"5日量/10日量={v5/v10:.2f}={TREND_VOLUME_UP_SCORE}"
        else:
            breakdown["trend_volume"] = "量能未放大=0"
        # 板块匹配
        sector_match = industry and any(s in industry or industry in s for s in top_sectors)
        if sector_match:
            total += TREND_SECTOR_MATCH_SCORE
            breakdown["trend_sector"] = f"板块{industry}匹配={TREND_SECTOR_MATCH_SCORE}"
        else:
            breakdown["trend_sector"] = f"板块{industry}不匹配=0"
        # 连续阳线
        if consecutive_up_map.get(code):
            total += TREND_CONSECUTIVE_UP_SCORE
            breakdown["trend_consecutive"] = f"3日连阳+{TREND_CONSECUTIVE_UP_SCORE}"
        breakdown["kind"] = "趋势"

        results.append(ScoredStock(
            code=code,
            name=name or "",
            industry=industry or "",
            score=total,
            board_count=0,
            first_limit_time="",
            blown_count=0,
            amount=amount or 0,
            price=c,
            score_breakdown=breakdown,
        ))
    # 排序
    results.sort(key=lambda x: x.score, reverse=True)
    return results


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
        max_picks = 4 if action_gate == "可买入" else 2

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

    # 过滤 ST/*ST/退市 股票（规则不同 + 风险高）
    limit_up_dicts = [
        s for s in limit_up_dicts
        if not any(m in (s.get("name") or "") for m in ("ST", "*ST", "退"))
    ]
    if not limit_up_dicts:
        return []

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
    # 注意：只对精确等同的 concept_name 匹配，避免 LIKE 误匹配
    # （比如 '电池' 会匹配 '锂电池概念'，把非电池行业股票拉入）
    concept_conn = sqlite3.connect(concept_db)
    try:
        for sector in top_sectors:
            rows = concept_conn.execute(
                "SELECT stock_codes FROM concept_stocks WHERE concept_name = ?",
                (sector,),
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

    # 4.5. 查询板块上下文（连续性、前日表现、龙头高度）
    industries = list({s.get("industry", "") for s in candidates if s.get("industry")})
    sector_ctx = _query_sector_context(intraday_db, date, industries)

    # 5. 逐只评分（含 Phase 2 新因子）
    scored = [_score_stock(s, max_board, second_board, sector_ctx) for s in candidates]

    # 5.5. 反包加分（昨日大跌或多次炸板）
    reversal_bonus = _apply_reversal_bonus(candidates, date, intraday_db)
    for s in scored:
        if s.code in reversal_bonus:
            s.score += reversal_bonus[s.code]
            s.score_breakdown["reversal"] = f"反包+{reversal_bonus[s.code]}"

    # 5.6. 合并趋势股候选池（非涨停但强趋势）
    trending = _screen_trending_stocks(date, top_sectors, intraday_db)
    # 去重（如果某股既在 scored 又在 trending，保留高分）
    existing = {s.code for s in scored}
    for t in trending:
        if t.code not in existing:
            scored.append(t)

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
