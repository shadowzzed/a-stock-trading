"""经验自动导入器：从回测经验审阅 JSON 自动导入到 ExperienceStore

取代手动审阅 + 导入流程，支持过滤、预览、批量导入。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from .store import Experience, ExperienceStore
from .tracker import LessonTracker

logger = logging.getLogger(__name__)


@dataclass
class ImportStats:
    """导入统计"""
    added: int = 0
    merged: int = 0
    skipped: int = 0
    rejected: int = 0

    @property
    def total(self) -> int:
        return self.added + self.merged + self.skipped + self.rejected

    def __str__(self) -> str:
        return (
            f"导入完成: 新增 {self.added}, 合并 {self.merged}, "
            f"跳过 {self.skipped}, 拒绝 {self.rejected} (共 {self.total} 条)"
        )


class ExperienceAutoImporter:
    """回测经验自动导入器。"""

    def __init__(self, store: ExperienceStore,
                 tracker: Optional[LessonTracker] = None):
        self.store = store
        self.tracker = tracker

    def import_from_review(
        self,
        review_json_path: str,
        min_confidence: float = 0.3,
        min_pnl_loss: float = -2.0,
        min_pnl_win: float = 3.0,
        auto_approve: bool = False,
        agent_version: str = "",
    ) -> ImportStats:
        """从回测经验审阅 JSON 导入。

        Args:
            review_json_path: 经验总结.json 路径
            min_confidence: 最低置信度阈值
            min_pnl_loss: 失败经验的最低亏损百分比（如 -2.0 表示亏损>2%才导入）
            min_pnl_win: 成功经验的最低盈利百分比
            auto_approve: True=直接导入，False=打印预览等用户确认
            agent_version: Agent 版本号（记录在经验中）

        Returns:
            ImportStats
        """
        if not os.path.exists(review_json_path):
            logger.error("文件不存在: %s", review_json_path)
            return ImportStats()

        with open(review_json_path, "r", encoding="utf-8") as f:
            raw_experiences = json.load(f)

        if not isinstance(raw_experiences, list):
            logger.error("JSON 格式错误，期望数组: %s", review_json_path)
            return ImportStats()

        # 过滤
        candidates = []
        stats = ImportStats()

        for raw in raw_experiences:
            exp = self._parse_experience(raw)
            if not exp:
                stats.rejected += 1
                continue

            # 标记版本号
            if agent_version:
                exp.lesson = f"[{agent_version}] {exp.lesson}"

            # 过滤条件
            reason = self._should_reject(exp, min_confidence, min_pnl_loss, min_pnl_win)
            if reason:
                logger.debug("拒绝: %s — %s", exp.lesson[:50], reason)
                stats.rejected += 1
                continue

            candidates.append(exp)

        if not candidates:
            logger.info("无可导入的经验（%d 条被过滤）", stats.rejected)
            return stats

        # 预览模式
        if not auto_approve:
            print(f"\n待导入经验（{len(candidates)} 条）：")
            print("-" * 60)
            for i, exp in enumerate(candidates, 1):
                print(f"{i}. [{exp.error_type}] {exp.lesson[:80]}")
                print(f"   修正规则: {exp.correction_rule[:80]}")
                print(f"   置信度: {exp.confidence:.2f}")
                print()

            confirm = input("确认导入？(y/N) ").strip().lower()
            if confirm != "y":
                print("已取消")
                return stats

        # 执行导入
        for exp in candidates:
            before_count = len(self.store.experiences)
            self.store.add(exp)
            after_count = len(self.store.experiences)

            if after_count > before_count:
                stats.added += 1
            else:
                stats.merged += 1

        self.store.save()
        logger.info("%s", stats)
        return stats

    def import_from_backtest_dir(self, backtest_dir: str, **kwargs) -> ImportStats:
        """从回测输出目录自动发现 经验总结.json 并导入。"""
        review_path = os.path.join(backtest_dir, "经验总结.json")
        if not os.path.exists(review_path):
            # 尝试其他可能的文件名
            for name in ["experience_review.json", "experiences.json"]:
                alt = os.path.join(backtest_dir, name)
                if os.path.exists(alt):
                    review_path = alt
                    break
            else:
                logger.error("未在 %s 中找到经验审阅文件", backtest_dir)
                return ImportStats()

        return self.import_from_review(review_path, **kwargs)

    def dry_run(self, review_json_path: str,
                min_confidence: float = 0.3) -> list[Experience]:
        """预览模式：返回将要导入的经验列表，不实际写入。"""
        if not os.path.exists(review_json_path):
            return []

        with open(review_json_path, "r", encoding="utf-8") as f:
            raw_experiences = json.load(f)

        results = []
        for raw in raw_experiences:
            exp = self._parse_experience(raw)
            if not exp:
                continue
            reason = self._should_reject(exp, min_confidence)
            if not reason:
                results.append(exp)

        return results

    def _parse_experience(self, raw: dict) -> Optional[Experience]:
        """从 JSON dict 解析为 Experience 对象。"""
        try:
            exp = Experience(
                date=raw.get("date", ""),
                scenario=raw.get("scenario", {}),
                prediction=raw.get("prediction", ""),
                reality=raw.get("reality", ""),
                scores=raw.get("scores", {}),
                error_type=raw.get("error_type", "unknown"),
                lesson=raw.get("lesson", ""),
                correction_rule=raw.get("correction_rule", ""),
                confidence=raw.get("confidence", 0.5),
                occurrence_count=raw.get("occurrence_count", 1),
            )
            return exp
        except Exception as e:
            logger.warning("解析经验失败: %s", e)
            return None

    def _should_reject(
        self,
        exp: Experience,
        min_confidence: float = 0.3,
        min_pnl_loss: float = -2.0,
        min_pnl_win: float = 3.0,
    ) -> Optional[str]:
        """判断是否应拒绝导入，返回拒绝原因或 None。"""
        # 必须有修正规则
        if not exp.correction_rule or not exp.correction_rule.strip():
            return "无修正规则"

        # 必须有教训内容
        if not exp.lesson or not exp.lesson.strip():
            return "无教训内容"

        # 置信度过低
        if exp.confidence < min_confidence:
            return f"置信度 {exp.confidence:.2f} < {min_confidence}"

        return None
