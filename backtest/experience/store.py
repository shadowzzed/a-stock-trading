"""结构化经验库：场景化教训的存储、检索、去重与合并

经验库以 JSON 文件持久化，每条经验绑定一个场景标签，支持按场景精确检索，
自动去重合并同类教训，并根据效果追踪数据排序。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from .classifier import ScenarioTags


@dataclass
class ScoreBreakdown:
    """各维度评分"""
    sentiment: int = 0
    sector: int = 0
    leader: int = 0
    strategy: int = 0

    @property
    def total(self) -> int:
        return self.sentiment + self.sector + self.leader + self.strategy


@dataclass
class Experience:
    """一条结构化经验教训"""
    id: str = ""
    date: str = ""                          # 回测日期 (Day D)
    scenario: dict = field(default_factory=dict)  # ScenarioTags 的字典形式
    prediction: str = ""                    # Agent 原始判断摘要
    reality: str = ""                       # 实际结果摘要
    scores: dict = field(default_factory=dict)     # ScoreBreakdown 字典
    error_type: str = ""                    # 错误分类 (sentiment/sector/leader/strategy)
    lesson: str = ""                        # 提炼出的教训
    correction_rule: str = ""               # 可执行的修正规则
    confidence: float = 0.5                 # 置信度 (出现次数 / 总匹配次数)
    occurrence_count: int = 1               # 该教训出现的次数（同类合并后累加）
    effectiveness: float = 0.0              # 注入后的平均改善率
    injection_count: int = 0                # 被注入 prompt 的次数
    improvement_samples: list = field(default_factory=list)  # 改善记录 [{date, before, after}]
    created_at: str = ""
    last_validated: str = ""
    last_merged_from: str = ""              # 最近合并来源日期

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.last_validated:
            self.last_validated = self.created_at


class ExperienceStore:
    """结构化经验库

    存储路径: {data_dir}/experience_store.json

    文件结构:
    {
      "version": 1,
      "experiences": [Experience, ...],
      "metadata": {
        "total_count": 42,
        "last_updated": "2026-04-03T..."
      }
    }
    """

    MAX_EXPERIENCES = 200  # 经验库容量上限

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.store_path = os.path.join(data_dir, "experience_store.json")
        self._experiences: list[Experience] = []
        self._load()

    # ── 读写 ─────────────────────────────────────────────

    def _load(self):
        if not os.path.exists(self.store_path):
            self._experiences = []
            return
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("experiences", []):
                self._experiences.append(Experience(**item))
        except (json.JSONDecodeError, IOError, TypeError):
            self._experiences = []

    def save(self):
        """持久化到磁盘"""
        data = {
            "version": 1,
            "experiences": [asdict(e) for e in self._experiences],
            "metadata": {
                "total_count": len(self._experiences),
                "last_updated": datetime.now().isoformat(),
            },
        }
        os.makedirs(os.path.dirname(self.store_path) or ".", exist_ok=True)
        with open(self.store_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ── 增删改查 ──────────────────────────────────────────

    def add(self, exp: Experience) -> Experience:
        """添加新经验，自动去重合并

        如果新经验的 scenario + error_type 与已有经验相似，则合并：
        - 保留 correction_rule 更完善的版本
        - 累加 occurrence_count
        - 更新 confidence
        - 保留更高 confidence 的教训
        """
        # 尝试合并
        existing = self._find_similar(exp)
        if existing:
            self._merge(existing, exp)
            self.save()
            return existing

        # 新增
        self._experiences.append(exp)

        # 容量控制：按 confidence * effectiveness 排序，移除最差
        if len(self._experiences) > self.MAX_EXPERIENCES:
            self._experiences.sort(
                key=lambda e: e.confidence * max(e.effectiveness, 0.1),
                reverse=True,
            )
            removed = self._experiences[self.MAX_EXPERIENCES:]
            self._experiences = self._experiences[: self.MAX_EXPERIENCES]
            # 通知合并来源
            for r in removed:
                if r.last_merged_from:
                    exp.last_merged_from = r.id

        self.save()
        return exp

    def get(self, exp_id: str) -> Optional[Experience]:
        for e in self._experiences:
            if e.id == exp_id:
                return e
        return None

    def update(self, exp_id: str, **kwargs) -> Optional[Experience]:
        exp = self.get(exp_id)
        if not exp:
            return None
        for k, v in kwargs.items():
            if hasattr(exp, k):
                setattr(exp, k, v)
        exp.last_validated = datetime.now().isoformat()
        self.save()
        return exp

    def search(
        self,
        scenario: Optional[ScenarioTags] = None,
        error_type: Optional[str] = None,
        min_confidence: float = 0.0,
        min_effectiveness: float = -1.0,
        limit: int = 10,
    ) -> list[Experience]:
        """按条件检索经验

        Args:
            scenario: 目标场景，会计算与每条经验的场景匹配度
            error_type: 错误类型过滤
            min_confidence: 最低置信度
            min_effectiveness: 最低效果值
            limit: 最大返回条数
        """
        candidates = []
        for e in self._experiences:
            if e.confidence < min_confidence:
                continue
            if e.effectiveness < min_effectiveness:
                continue
            if error_type and e.error_type != error_type:
                continue

            # 计算场景匹配度
            match_score = 1.0
            if scenario:
                match_score = self._calc_scenario_match(scenario, e)

            if match_score > 0:
                candidates.append((match_score, e))

        # 排序：场景匹配度 × (置信度 × max(效果值, 0.1))
        candidates.sort(
            key=lambda x: x[0] * (x[1].confidence * max(x[1].effectiveness, 0.1)),
            reverse=True,
        )
        return [e for _, e in candidates[:limit]]

    def search_by_text(
        self,
        query: str,
        limit: int = 5,
    ) -> list[Experience]:
        """简单文本匹配检索（用于没有明确场景标签时的 fallback）"""
        query_lower = query.lower()
        scored = []
        for e in self._experiences:
            text = f"{e.lesson} {e.correction_rule} {e.error_type}".lower()
            # 简单关键词匹配
            keywords = [w for w in query_lower.split() if len(w) > 1]
            hits = sum(1 for k in keywords if k in text)
            if hits > 0:
                scored.append((hits, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:limit]]

    @property
    def all_experiences(self) -> list[Experience]:
        return list(self._experiences)

    @property
    def stats(self) -> dict:
        """经验库统计信息"""
        if not self._experiences:
            return {"total": 0, "by_error_type": {}, "avg_confidence": 0, "avg_effectiveness": 0}
        by_type = {}
        for e in self._experiences:
            by_type[e.error_type] = by_type.get(e.error_type, 0) + 1
        avg_conf = sum(e.confidence for e in self._experiences) / len(self._experiences)
        avg_eff = sum(e.effectiveness for e in self._experiences) / len(self._experiences)
        return {
            "total": len(self._experiences),
            "by_error_type": by_type,
            "avg_confidence": round(avg_conf, 2),
            "avg_effectiveness": round(avg_eff, 2),
        }

    # ── 内部方法 ──────────────────────────────────────────

    def _find_similar(self, exp: Experience) -> Optional[Experience]:
        """查找与新经验相似的已有经验

        相似条件: error_type 相同 + 场景标签至少 3 个维度匹配
        """
        exp_scenario = exp.scenario
        best_match = None
        best_score = 0

        for existing in self._experiences:
            if existing.error_type != exp.error_type:
                continue
            # 计算场景匹配度
            match_count = 0
            for key in ["sentiment_phase", "limit_up_range", "limit_down_range",
                        "blown_rate_range", "max_board_range"]:
                if (exp_scenario.get(key) and existing.scenario.get(key)
                        and exp_scenario[key] == existing.scenario[key]):
                    match_count += 1

            if match_count >= 3 and match_count > best_score:
                best_score = match_count
                best_match = existing

        return best_match

    def _merge(self, existing: Experience, new: Experience):
        """合并两条相似经验"""
        # 累加出现次数
        existing.occurrence_count += new.occurrence_count

        # 更新置信度：出现次数越多越可信
        existing.confidence = min(0.95, existing.confidence + 0.1)

        # 保留更好的修正规则
        if len(new.correction_rule) > len(existing.correction_rule):
            existing.correction_rule = new.correction_rule

        # 保留更详细的教训
        if len(new.lesson) > len(existing.lesson):
            existing.lesson = new.lesson

        # 更新验证时间
        existing.last_validated = new.created_at
        existing.last_merged_from = new.date

    def _calc_scenario_match(
        self, target: ScenarioTags, exp: Experience
    ) -> float:
        """计算目标场景与经验场景的匹配度 (0~1)"""
        exp_tags = exp.scenario
        if not exp_tags:
            return 0.3  # 无标签时给基础分

        weights = {
            "sentiment_phase": 3,      # 情绪阶段权重最高
            "limit_up_range": 2,
            "limit_down_range": 2,
            "blown_rate_range": 1.5,
            "max_board_range": 1.5,
            "sector_concentration": 1,
            "volume_trend": 1,
        }

        target_dict = asdict(target) if hasattr(target, "__dataclass_fields__") else target
        weighted_total = 0
        weighted_matched = 0

        for key, weight in weights.items():
            t_val = target_dict.get(key, "")
            e_val = exp_tags.get(key, "")
            if t_val and e_val:
                weighted_total += weight
                if t_val == e_val:
                    weighted_matched += weight

        if weighted_total == 0:
            return 0.3

        return weighted_matched / weighted_total
