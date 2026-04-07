"""Trade Agent 版本号管理

每次修改 prompt、规则、架构时更新此文件。
版本号会记录在回测输出、日常报告归档、经验库中，用于追踪能力变化。
"""

AGENT_VERSION = "v1.0.0"

CHANGELOG = {
    "v1.0.0": {
        "date": "2026-04-07",
        "changes": [
            "引入版本号机制",
            "新增数据纪律规则（防幻觉）— 所有 prompt 加入红线约束",
            "新增 validate_output 数据审查节点（LLM 审查员）",
            "新增 reflect 反思节点（对照历史教训修正策略错误）",
            "新增日常报告自动归档",
            "新增经验自动导入 CLI",
            "新增 ExpeL 批量经验蒸馏",
        ],
        "benchmark": {
            "test_period": None,
            "avg_pnl_pct": None,
            "hit_rate": None,
            "max_drawdown_pct": None,
        },
    },
}


def get_version() -> str:
    return AGENT_VERSION


def get_changelog() -> dict:
    return CHANGELOG
