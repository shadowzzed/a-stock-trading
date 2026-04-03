"""动态 Prompt 注入引擎：根据当前市场场景，精确匹配最相关的教训注入

核心思路：
- 不再无差别注入所有教训
- 根据当前市场场景（情绪阶段、涨停数区间、跌停数区间等）匹配相关教训
- 按效果值排序，只注入高置信度 + 高改善率的教训
- 生成结构化的注入文本，帮助 Agent 避免特定场景下的已知错误
"""

from __future__ import annotations

from typing import Optional

from .store import ExperienceStore, Experience
from .classifier import ScenarioClassifier, ScenarioTags
from .tracker import LessonTracker


# 注入模板：不同错误类型对应的关注提醒
ERROR_TYPE_LABELS = {
    "sentiment": "情绪周期判断",
    "sector": "主线板块判断",
    "leader": "龙头辨识",
    "strategy": "策略实效",
    "unknown": "综合",
}

# 每个 Agent 应该接收的错误类型过滤
AGENT_ERROR_FILTERS = {
    "sentiment_analyst": {"sentiment", "unknown"},
    "sector_analyst": {"sector", "unknown"},
    "leader_analyst": {"leader", "unknown"},
    "bull": {"strategy", "sector", "leader", "unknown"},
    "bear": {"strategy", "sector", "leader", "unknown"},
    "judge": {"sentiment", "sector", "leader", "strategy", "unknown"},
}


class PromptEngine:
    """动态 Prompt 注入引擎

    使用方式:
        engine = PromptEngine(data_dir)
        injection = engine.build_injection(current_market_data)
        # injection 是一个 dict: {agent_name: extra_prompt_text}
    """

    def __init__(self, data_dir: str):
        self.store = ExperienceStore(data_dir)
        self.tracker = LessonTracker(data_dir)
        self.classifier = ScenarioClassifier()

    def build_injection(
        self,
        market_data: dict,
        agents: Optional[list[str]] = None,
        max_lessons_per_agent: int = 3,
        min_confidence: float = 0.3,
        min_effectiveness: float = -0.5,
    ) -> dict[str, str]:
        """构建场景感知的动态 Prompt 注入

        Args:
            market_data: 当前市场数据字典，包含:
                - limit_up_count, limit_down_count, blown_rate, max_board 等
                - sentiment_phase (可选)
                - prev_limit_up_count (可选)
                - volume_change_pct (可选)
            agents: 需要注入的 agent 列表（默认全部）
            max_lessons_per_agent: 每个 agent 最多注入几条教训
            min_confidence: 最低置信度阈值
            min_effectiveness: 最低效果值阈值

        Returns:
            {agent_name: injection_text} 字典
        """
        # 1. 分类当前场景
        scenario = self.classifier.classify(
            limit_up_count=market_data.get("limit_up_count", 0),
            limit_down_count=market_data.get("limit_down_count", 0),
            blown_rate=market_data.get("blown_rate", 0.0),
            max_board=market_data.get("max_board", 0),
            sector_top1_count=market_data.get("sector_top1_count", 0),
            sector_top1_total=market_data.get("limit_up_count", 0),
            prev_limit_up_count=market_data.get("prev_limit_up_count"),
            sentiment_phase=market_data.get("sentiment_phase", ""),
            volume_change_pct=market_data.get("volume_change_pct"),
        )

        # 2. 检索相关教训（多取一些，后面按 agent 分配）
        relevant = self.store.search(
            scenario=scenario,
            min_confidence=min_confidence,
            min_effectiveness=min_effectiveness,
            limit=20,
        )

        # 排除已废弃的教训
        active_ids = set(self.tracker.get_active_lessons())
        if not active_ids:
            # 首次使用，没有追踪数据，全部视为活跃
            active_ids = {e.id for e in relevant}
        relevant = [e for e in relevant if e.id in active_ids]

        if not relevant:
            return {}

        # 3. 为每个 agent 生成注入文本
        if agents is None:
            agents = list(AGENT_ERROR_FILTERS.keys())

        result = {}
        for agent in agents:
            allowed_errors = AGENT_ERROR_FILTERS.get(agent, {"unknown"})
            # 筛选该 agent 应关注的教训
            agent_lessons = [
                e for e in relevant
                if e.error_type in allowed_errors
            ][:max_lessons_per_agent]

            if agent_lessons:
                result[agent] = self._format_injection(
                    agent_lessons, scenario
                )

        return result

    def build_injection_from_report(
        self,
        report_text: str,
        market_data: dict,
        agents: Optional[list[str]] = None,
    ) -> dict[str, str]:
        """从 Agent 报告文本 + 市场数据构建注入（更精确的场景识别）"""
        scenario = self.classifier.classify_from_report(report_text, market_data)
        # 注入到 search
        relevant = self.store.search(
            scenario=scenario,
            min_confidence=0.3,
            min_effectiveness=-0.5,
            limit=20,
        )

        active_ids = set(self.tracker.get_active_lessons()) or {e.id for e in relevant}
        relevant = [e for e in relevant if e.id in active_ids]

        if not relevant:
            return {}

        if agents is None:
            agents = list(AGENT_ERROR_FILTERS.keys())

        result = {}
        for agent in agents:
            allowed_errors = AGENT_ERROR_FILTERS.get(agent, {"unknown"})
            agent_lessons = [e for e in relevant if e.error_type in allowed_errors][:3]
            if agent_lessons:
                result[agent] = self._format_injection(agent_lessons, scenario)

        return result

    def record_result(
        self,
        date: str,
        injected_ids: list[str],
        score: float,
        baseline_score: Optional[float] = None,
    ):
        """记录一次注入的结果（供回测后调用）

        Args:
            date: 被分析日期
            injected_ids: 本次注入的教训 ID 列表
            score: 实际得分
            baseline_score: 基准得分（可选）
        """
        self.tracker.record_injection(
            date=date,
            lesson_ids=injected_ids,
            score=score,
            baseline_score=baseline_score,
        )
        # 反馈到经验库
        self.tracker.feedback_to_store(self.store)

    def _format_injection(
        self, lessons: list[Experience], scenario: ScenarioTags
    ) -> str:
        """格式化教训注入文本

        结构：
        1. 场景说明（为什么这些教训与你当前的分析相关）
        2. 每条教训的错误类型 + 具体教训 + 修正规则
        """
        lines = [
            "## 场景化经验教训（与当前市场状态高度相关）",
            "",
            f"**当前场景特征**：{scenario.to_description()}",
            "",
            "以下是在类似市场场景中，过往预测被验证为错误的教训，请重点关注：",
            "",
        ]

        for i, exp in enumerate(lessons, 1):
            label = ERROR_TYPE_LABELS.get(exp.error_type, "综合")
            lines.append(f"### 教训 {i}：{label}（置信度 {exp.confidence:.0%}）")
            lines.append(f"- **错误场景**：{exp.lesson}")
            if exp.correction_rule:
                lines.append(f"- **修正规则**：{exp.correction_rule}")
            if exp.occurrence_count > 1:
                lines.append(f"- **出现次数**：{exp.occurrence_count} 次同类错误")
            if exp.effectiveness > 0:
                lines.append(f"- **改善效果**：注入后平均提升 {exp.effectiveness:.1f} 分")
            lines.append("")

        return "\n".join(lines)
