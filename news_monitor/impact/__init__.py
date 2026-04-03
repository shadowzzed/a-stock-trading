"""
新闻历史影响分析系统

模块：
- db.py    数据库操作（news_embeddings + news_impacts 表）
- embed.py Embedding 编码与向量检索（bge-m3）
- calc.py  历史影响计算引擎（各时间窗口涨跌幅）
- search.py 相似新闻检索与影响聚合
- hooks.py 与 news_monitor 的集成钩子
- prompts.py 影响分析报告的 AI 润色提示词
- bootstrap.py 冷启动：回填 + 批量计算
"""
