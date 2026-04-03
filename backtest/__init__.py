"""回测与经验反馈系统 — 独立于 Trading Agent 本体

目录结构:
    experience/    结构化经验库（场景化教训存储、检索、效果追踪）
    engine/        回测引擎（接口驱动，依赖注入）
    adapter.py     数据适配层（桥接 Trading Agent 数据源）
    run.py         CLI 入口
"""
