"""回测引擎提示词模板

v6 收益率驱动模式下，不再使用 VERIFIER_PROMPT（LLM 打分）。
经验提取改为基于实际交易结果，不再依赖 LLM。
"""

# 保留文件以维持模块结构
# VERIFIER_PROMPT 和 EXPERIENCE_EXTRACTOR_PROMPT 已删除
# 经验提取逻辑已移至 core.py 的 _extract_experience_from_outcome()，纯数据驱动
