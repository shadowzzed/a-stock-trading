"""教训效果追踪器：追踪每条教训注入后的实际改善效果

核心逻辑：
1. 在注入教训前记录"基准分数"（不注入教训时的回测得分）
2. 注入教训后，记录实际得分
3. 计算改善率 = (after - before) / max(before, 1)
4. 根据改善率自动升降权
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


@dataclass
class InjectionRecord:
    """一次教训注入的记录"""
    date: str                       # 注入日期（被分析的日期）
    lesson_ids: list[str]           # 注入的教训 ID 列表
    score_before: float             # 该日期不注入教训时的基准分（如有）
    score_after: float              # 注入后的实际得分
    improvement: float              # score_after - score_before


@dataclass
class LessonEffectiveness:
    """单条教训的效果追踪"""
    lesson_id: str
    total_injections: int = 0       # 总注入次数
    avg_score_before: float = 0.0   # 注入前平均分
    avg_score_after: float = 0.0    # 注入后平均分
    improvement: float = 0.0        # 平均改善幅度
    sample_size: int = 0            # 有效样本数（有 before/after 的）
    last_injected: str = ""         # 最近注入日期
    status: str = "active"          # active/deprecated/promoted

    def update(self, record: InjectionRecord):
        """更新效果统计"""
        self.total_injections += 1
        self.last_injected = record.date

        if record.score_before > 0:
            # 有基准分，可计算改善
            self.sample_size += 1
            n = self.sample_size
            # 增量更新均值
            self.avg_score_before = (
                self.avg_score_before * (n - 1) + record.score_before
            ) / n
            self.avg_score_after = (
                self.avg_score_after * (n - 1) + record.score_after
            ) / n
            self.improvement = self.avg_score_after - self.avg_score_before

        # 自动状态管理
        if self.sample_size >= 3 and self.improvement < -1:
            # 持续负效果，标记为废弃
            self.status = "deprecated"
        elif self.sample_size >= 5 and self.improvement > 2:
            # 持续正效果，可升级为规则
            self.status = "promoted"


class LessonTracker:
    """教训效果追踪器

    持久化路径: {data_dir}/lesson_tracker.json

    用于：
    1. 记录每次回测中注入了哪些教训、得分如何
    2. 追踪每条教训的累计改善效果
    3. 自动降权无效教训、升级高效教训
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.tracker_path = os.path.join(data_dir, "lesson_tracker.json")
        self.effectiveness: dict[str, LessonEffectiveness] = {}
        self.injection_history: list[dict] = []
        self._load()

    def _load(self):
        if not os.path.exists(self.tracker_path):
            return
        try:
            with open(self.tracker_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("effectiveness", []):
                le = LessonEffectiveness(**item)
                self.effectiveness[le.lesson_id] = le
            self.injection_history = data.get("injection_history", [])
        except (json.JSONDecodeError, IOError, TypeError):
            pass

    def save(self):
        data = {
            "version": 1,
            "effectiveness": [asdict(le) for le in self.effectiveness.values()],
            "injection_history": self.injection_history[-200:],  # 保留最近200条
            "metadata": {
                "total_tracked": len(self.effectiveness),
                "last_updated": datetime.now().isoformat(),
            },
        }
        os.makedirs(os.path.dirname(self.tracker_path) or ".", exist_ok=True)
        with open(self.tracker_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def record_injection(
        self,
        date: str,
        lesson_ids: list[str],
        score: float,
        baseline_score: Optional[float] = None,
    ):
        """记录一次教训注入

        Args:
            date: 被分析日期
            lesson_ids: 注入的教训 ID
            score: 实际得分
            baseline_score: 不注入教训时的基准分（可为 None）
        """
        record = InjectionRecord(
            date=date,
            lesson_ids=lesson_ids,
            score_before=baseline_score or 0,
            score_after=score,
            improvement=(score - baseline_score) if baseline_score else 0,
        )

        # 记录历史
        self.injection_history.append({
            "date": date,
            "lesson_ids": lesson_ids,
            "score": score,
            "baseline_score": baseline_score,
        })

        # 更新每条教训的效果
        for lid in lesson_ids:
            if lid not in self.effectiveness:
                self.effectiveness[lid] = LessonEffectiveness(lesson_id=lid)
            self.effectiveness[lid].update(record)

        self.save()

    def get_effectiveness(self, lesson_id: str) -> Optional[LessonEffectiveness]:
        return self.effectiveness.get(lesson_id)

    def get_active_lessons(self) -> list[str]:
        """获取所有活跃状态的教训 ID"""
        return [
            lid for lid, le in self.effectiveness.items()
            if le.status == "active"
        ]

    def get_deprecated_lessons(self) -> list[str]:
        """获取已废弃的教训 ID"""
        return [
            lid for lid, le in self.effectiveness.items()
            if le.status == "deprecated"
        ]

    def get_promotable_lessons(self) -> list[str]:
        """获取可升级为量化规则的教训 ID"""
        return [
            lid for lid, le in self.effectiveness.items()
            if le.status == "promoted"
        ]

    def get_effectiveness_ranking(self, limit: int = 20) -> list[tuple[str, float]]:
        """按效果值排序，返回 (lesson_id, improvement) 列表"""
        ranked = [
            (lid, le.improvement)
            for lid, le in self.effectiveness.items()
            if le.status == "active"
        ]
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked[:limit]

    def feedback_to_store(self, store):
        """将效果追踪结果反馈回经验库，更新各条经验的 effectiveness

        Args:
            store: ExperienceStore 实例
        """
        for lid, le in self.effectiveness.items():
            exp = store.get(lid)
            if exp:
                store.update(
                    lid,
                    effectiveness=le.improvement,
                    injection_count=le.total_injections,
                )

        # 同时降权废弃的教训
        for lid in self.get_deprecated_lessons():
            exp = store.get(lid)
            if exp:
                store.update(lid, confidence=0.1)  # 大幅降权但不删除

        store.save()
