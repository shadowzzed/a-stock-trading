"""场景分类器：从市场数据中提取场景标签，用于教训匹配

场景标签是一组离散化的市场状态描述，使得相似的市场状态能匹配到相同的教训。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ScenarioTags:
    """市场场景标签（离散化，用于匹配）"""
    sentiment_phase: str = ""           # 冰点/修复/升温/高潮/分歧/退潮
    limit_up_range: str = ""            # 涨停数区间
    limit_down_range: str = ""          # 跌停数区间
    blown_rate_range: str = ""          # 炸板率区间
    max_board_range: str = ""           # 最高连板区间
    sector_concentration: str = ""      # 板块集中度
    volume_trend: str = ""              # 成交量趋势

    def to_dict(self) -> dict:
        return {
            "sentiment_phase": self.sentiment_phase,
            "limit_up_range": self.limit_up_range,
            "limit_down_range": self.limit_down_range,
            "blown_rate_range": self.blown_rate_range,
            "max_board_range": self.max_board_range,
            "sector_concentration": self.sector_concentration,
            "volume_trend": self.volume_trend,
        }

    def to_description(self) -> str:
        """生成人类可读的场景描述"""
        parts = []
        if self.sentiment_phase:
            parts.append(f"情绪阶段={self.sentiment_phase}")
        if self.limit_up_range:
            parts.append(f"涨停数={self.limit_up_range}")
        if self.limit_down_range:
            parts.append(f"跌停数={self.limit_down_range}")
        if self.blown_rate_range:
            parts.append(f"炸板率={self.blown_rate_range}")
        if self.max_board_range:
            parts.append(f"最高连板={self.max_board_range}")
        if self.sector_concentration:
            parts.append(f"板块集中度={self.sector_concentration}")
        if self.volume_trend:
            parts.append(f"成交量={self.volume_trend}")
        return ", ".join(parts) if parts else "未知场景"


class ScenarioClassifier:
    """从市场原始数据提取场景标签"""

    @staticmethod
    def classify(
        limit_up_count: int = 0,
        limit_down_count: int = 0,
        blown_rate: float = 0.0,
        max_board: int = 0,
        sector_top1_count: int = 0,
        sector_top1_total: int = 0,
        prev_limit_up_count: Optional[int] = None,
        sentiment_phase: str = "",
        volume_change_pct: Optional[float] = None,
    ) -> ScenarioTags:
        """根据市场数据生成场景标签

        Args:
            limit_up_count: 涨停家数
            limit_down_count: 跌停家数
            blown_rate: 炸板率 (0-100)
            max_board: 最高连板数
            sector_top1_count: 涨停最多板块的涨停数
            sector_top1_total: 总涨停数
            prev_limit_up_count: 前一日涨停数（用于计算趋势）
            sentiment_phase: 已识别的情绪阶段（可选）
            volume_change_pct: 成交量环比变化百分比
        """
        tags = ScenarioTags()

        # 情绪阶段（如果已提供，直接使用）
        tags.sentiment_phase = sentiment_phase or ScenarioClassifier._infer_phase(
            limit_up_count, limit_down_count, blown_rate, max_board
        )

        # 涨停数区间
        tags.limit_up_range = ScenarioClassifier._range(limit_up_count, [
            (0, "0"), (30, "1-30"), (50, "31-50"), (70, "51-70"),
            (100, "71-100"), (float("inf"), ">100"),
        ])

        # 跌停数区间
        tags.limit_down_range = ScenarioClassifier._range(limit_down_count, [
            (0, "0"), (5, "1-5"), (10, "6-10"), (20, "11-20"), (float("inf"), ">20"),
        ])

        # 炸板率区间
        tags.blown_rate_range = ScenarioClassifier._range(blown_rate, [
            (20, "<20%"), (35, "20-35%"), (50, "35-50%"), (float("inf"), ">50%"),
        ])

        # 最高连板区间
        tags.max_board_range = ScenarioClassifier._range(max_board, [
            (2, "1-2板"), (4, "3-4板"), (7, "5-7板"), (float("inf"), ">7板"),
        ])

        # 板块集中度
        if sector_top1_total > 0:
            ratio = sector_top1_count / sector_top1_total
            if ratio >= 0.3:
                tags.sector_concentration = "集中"
            elif ratio >= 0.15:
                tags.sector_concentration = "一般"
            else:
                tags.sector_concentration = "分散"
        else:
            tags.sector_concentration = "未知"

        # 成交量趋势
        if volume_change_pct is not None:
            if volume_change_pct > 15:
                tags.volume_trend = "放量"
            elif volume_change_pct < -15:
                tags.volume_trend = "缩量"
            else:
                tags.volume_trend = "持平"
        elif prev_limit_up_count is not None:
            # 没有成交量数据时，用涨停数变化近似
            change = limit_up_count - prev_limit_up_count
            pct = change / max(prev_limit_up_count, 1) * 100
            if pct > 30:
                tags.volume_trend = "放量"
            elif pct < -30:
                tags.volume_trend = "缩量"
            else:
                tags.volume_trend = "持平"

        return tags

    @staticmethod
    def classify_from_report(report_text: str, market_data: dict) -> ScenarioTags:
        """从 Agent 报告文本 + 市场数据字典中提取场景标签

        Args:
            report_text: Agent 裁决报告文本
            market_data: 包含 limit_up_count, limit_down_count 等的字典
        """
        # 尝试从报告文本中提取情绪阶段
        phase = ""
        phase_keywords = {
            "冰点": "冰点", "修复": "修复", "升温": "升温",
            "高潮": "高潮", "分歧": "分歧", "退潮": "退潮",
        }
        for keyword, phase_name in phase_keywords.items():
            if keyword in report_text:
                # 取最常出现的阶段作为当前阶段
                phase = phase_name
                break

        return ScenarioClassifier.classify(
            limit_up_count=market_data.get("limit_up_count", 0),
            limit_down_count=market_data.get("limit_down_count", 0),
            blown_rate=market_data.get("blown_rate", 0.0),
            max_board=market_data.get("max_board", 0),
            sector_top1_count=market_data.get("sector_top1_count", 0),
            sector_top1_total=market_data.get("limit_up_count", 0),
            prev_limit_up_count=market_data.get("prev_limit_up_count"),
            sentiment_phase=phase,
            volume_change_pct=market_data.get("volume_change_pct"),
        )

    @staticmethod
    def _infer_phase(
        limit_up: int, limit_down: int, blown_rate: float, max_board: int
    ) -> str:
        """根据硬数据推断情绪阶段（简化版，作为 fallback）"""
        if limit_down >= 20 or (limit_down >= 15 and blown_rate >= 50):
            return "冰点"
        if limit_down >= 10 and limit_up < 40:
            return "退潮"
        if limit_up > 80 and max_board >= 5:
            return "高潮"
        if limit_up > 50 and blown_rate > 40:
            return "分歧"
        if limit_up > 50 and max_board >= 3:
            return "升温"
        if limit_up > 30:
            return "修复"
        return "冰点"

    @staticmethod
    def _range(value: float, boundaries: list[tuple]) -> str:
        """将连续值映射到离散区间"""
        for threshold, label in boundaries:
            if value <= threshold:
                return label
        return boundaries[-1][1]  # fallback


def classify_error_type(scores: dict) -> str:
    """根据各维度评分，判断主要错误类型

    返回得分最低的维度作为主要错误类型。
    如果多个维度并列最低，返回 strategy（策略最重要）。
    """
    if not scores:
        return "unknown"

    dims = {}
    for dim in ["sentiment", "sector", "leader", "strategy"]:
        s = scores.get(dim, {})
        if isinstance(s, dict) and "score" in s:
            dims[dim] = s["score"]
        elif isinstance(s, (int, float)):
            dims[dim] = s

    if not dims:
        return "unknown"

    # 策略维度权重最高（最难改善），优先关注
    min_score = min(dims.values())
    if dims.get("strategy", 5) == min_score:
        return "strategy"

    for dim, score in dims.items():
        if score == min_score:
            return dim

    return "strategy"
