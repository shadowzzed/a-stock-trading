"""数据迁移：将旧的 agent_lessons.json 转换为结构化经验库

旧格式（扁平文本教训）:
{
  "lessons": [{"date": "2026-03-10", "lesson": "教训文本"}],
  "history": [{"date": "...", "scores": {...}, ...}]
}

新格式（结构化经验库）:
{
  "experiences": [Experience, ...]
}

迁移逻辑:
- 旧教训缺少场景标签和结构化字段，设为默认值
- 保留原有教训文本
- 标记为"已迁移"，confidence 设为 0.5（中等，待验证）
"""

from __future__ import annotations

import json
import os

from .store import ExperienceStore, Experience


def migrate_legacy_lessons(data_dir: str, dry_run: bool = False) -> int:
    """将旧 agent_lessons.json 迁移到结构化经验库

    Args:
        data_dir: trading 数据根目录
        dry_run: 只打印不实际写入

    Returns:
        迁移的教训条数
    """
    legacy_path = os.path.join(data_dir, "agent_lessons.json")
    if not os.path.exists(legacy_path):
        print("[迁移] 未找到 agent_lessons.json，跳过")
        return 0

    with open(legacy_path, "r", encoding="utf-8") as f:
        legacy = json.load(f)

    lessons = legacy.get("lessons", [])
    history = legacy.get("history", [])

    if not lessons:
        print("[迁移] agent_lessons.json 无教训数据，跳过")
        return 0

    # 建立 history 查找表
    history_map = {}
    for h in history:
        history_map[h.get("date", "")] = h

    store = ExperienceStore(data_dir)
    migrated = 0

    for item in lessons:
        date = item.get("date", "")
        lesson_text = item.get("lesson", "")

        if not lesson_text:
            continue

        # 从 history 中补充 scores
        hist = history_map.get(date, {})
        scores = hist.get("scores", {})

        # 简单分类错误类型
        error_type = _infer_error_type(lesson_text)

        exp = Experience(
            date=date,
            scenario={"migrated": True},  # 标记为迁移，无场景标签
            prediction="(迁移自旧格式，无结构化摘要)",
            reality="(迁移自旧格式，无结构化摘要)",
            scores=scores,
            error_type=error_type,
            lesson=lesson_text,
            correction_rule=_infer_correction(lesson_text),
            confidence=0.5,  # 迁移数据置信度中等
            occurrence_count=1,
        )

        if dry_run:
            print("  [预览] {}: {}... → error_type={}".format(
                date, lesson_text[:40], error_type))
        else:
            store.add(exp)

        migrated += 1

    if not dry_run and migrated > 0:
        store.save()
        # 备份旧文件
        backup_path = legacy_path + ".bak"
        os.rename(legacy_path, backup_path)
        print("[迁移] 完成，旧文件已备份为 {}".format(backup_path))

    print("[迁移] 共迁移 {} 条教训".format(migrated))
    return migrated


def _infer_error_type(lesson_text: str) -> str:
    """从教训文本推断错误类型"""
    text = lesson_text.lower()
    if any(k in text for k in ["情绪", "冰点", "退潮", "升温", "高潮", "分歧"]):
        return "sentiment"
    if any(k in text for k in ["板块", "主线", "支线", "轮动", "发酵"]):
        return "sector"
    if any(k in text for k in ["龙头", "连板", "封板", "破局", "补涨"]):
        return "leader"
    if any(k in text for k in ["策略", "仓位", "追高", "低吸", "操作", "标的"]):
        return "strategy"
    return "unknown"


def _infer_correction(lesson_text: str) -> str:
    """从教训文本提取修正规则（简单启发式）"""
    for keyword in ["应该", "必须", "不要", "不宜", "需要"]:
        if keyword in lesson_text:
            idx = lesson_text.index(keyword)
            start = max(0, lesson_text.rfind("。", 0, idx) + 1)
            end = lesson_text.find("。", idx)
            if end == -1:
                end = len(lesson_text)
            return lesson_text[start:end].strip()

    return ""


if __name__ == "__main__":
    import sys
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    dry_run = "--dry-run" in sys.argv
    migrate_legacy_lessons(data_dir, dry_run=dry_run)
