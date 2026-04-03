# backtest/ — 回测与经验反馈系统

## 业务上下文

这是一个 A 股短线交易 Agent 的**回测与经验反馈闭环系统**。

整个项目的数据流是这样的：

```
数据采集 → 盘中分析 → 盘后复盘（review/）→ 回测验证（backtest/）
                                                    ↓
                                              经验提炼 & 存储
                                                    ↓
                                         注入到下一轮复盘的 Prompt
```

**review/** 是 Trading Agent 本体，每天盘后跑多 Agent 复盘（情绪分析师、板块分析师、龙头辨识师、多空辩手、裁决官），产出当日分析报告。

**backtest/** 的职责是：拿历史数据回放这个流程，验证 Agent 的预测是否准确，并把失败经验结构化地反馈回去，让下一轮分析少犯同类错误。

## 目标

### 当前已实现
- **结构化经验库**：按市场场景（情绪阶段/涨停数区间/炸板率/连板高度等）分类存储教训
- **场景化注入**：不无差别注入所有教训，而是根据当前市场状态精确匹配
- **效果追踪**：追踪每条教训注入后的实际改善，自动降权无效教训、升级高效教训
- **D→D+1 回测引擎**：用 Day D 跑 Agent → 用 Day D+1 实际行情验证 → 四维打分（情绪/板块/龙头/策略，满分 20）

### 建设方向
1. **自动化流水线**：定时拉取最新数据，自动跑回测，无需人工触发
2. **Prompt 自优化**：根据效果追踪数据自动调整 Prompt 模板（而非手工迭代 v1→v2→v3）
3. **量化规则提炼**：将反复验证有效的教训自动转化为可执行的量化规则（写入 knowledge/quantitative_rules.json）
4. **趋势看板**：回测得分的时序趋势、各维度雷达图、教训覆盖率等可视化

## 架构设计

```
backtest/
├── experience/          # 经验库（完全自包含，零外部依赖）
│   ├── store.py         # 经验存储、去重合并、场景匹配检索
│   ├── classifier.py    # 市场数据 → 离散场景标签（7 个维度）
│   ├── tracker.py       # 教训效果追踪 + 自动升降权（active/deprecated/promoted）
│   ├── prompt_engine.py # 场景感知的动态 Prompt 注入（按 Agent 分配不同类型教训）
│   └── migrate.py       # 从旧 agent_lessons.json 格式迁移
├── engine/              # 回测引擎（接口驱动，依赖注入）
│   ├── protocols.py     # 数据/Agent/LLM 三个接口协议
│   ├── core.py          # 回测主流程（不 import review/）
│   ├── prompts.py       # 验证 & 经验提取的 Prompt 模板
│   └── report.py        # 汇总报告生成（JSON + Markdown）
├── adapter.py           # 唯一桥接 review/ 的适配器
└── run.py               # CLI 入口
```

**关键原则：反向依赖**

```
review/ (Trading Agent 本体)  ←──只此一处──→  backtest/adapter.py
                                           ↗
       backtest/engine/  ←──protocols───
       backtest/experience/ （零外部 import）
```

- `backtest/` 的代码**永远不 import `review/`**
- 引擎通过 Protocol 定义接口（DataProvider、AgentRunner、LLMCaller）
- `adapter.py` 是唯一知道 `review/` 存在的文件，负责把具体实现注入引擎
- 如果未来数据源换了（比如接入 AKShare、东方财富直连），只需写新 adapter，引擎和经验库不用改

## 限制

1. **数据依赖**：回测需要 `~/trading-data/daily/YYYY-MM-DD/` 下有完整的 CSV 数据（涨停板、跌停板、个股行情），由 `data/` 模块采集
2. **LLM 消耗**：每跑一天回测需要 3~5 次 LLM 调用（Agent 分析 + 验证打分 + 经验提取），20 个交易日约 60~100 次 API 调用
3. **经验库冷启动**：首次运行没有历史教训可注入，需要积累 5~10 个交易日的回测数据后才能看到改善效果
4. **场景标签粒度**：当前场景分类基于硬编码的阈值区间（如涨停数 0/1-30/31-50/...），不会自动适应市场格局变化
5. **回测 ≠ 实盘**：回测验证的是"Agent 的分析逻辑是否自洽"，不代表实盘收益。Day D 报告中的具体标的推荐，受盘中时效性影响，回测无法完全还原

## 如何测试

### 单元测试

经验库模块可以独立测试，不需要 LLM 或真实市场数据：

```bash
# 测试经验存储、检索、去重
pytest tests/test_experience_store.py

# 测试场景分类器
pytest tests/test_scenario_classifier.py

# 测试效果追踪
pytest tests/test_lesson_tracker.py

# 测试 Prompt 引擎
pytest tests/test_prompt_engine.py
```

单元测试写法示例：

```python
from backtest.experience import ExperienceStore, Experience, ScenarioClassifier

def test_store_search_by_scenario():
    store = ExperienceStore("/tmp/test_data")

    # 添加一条经验
    exp = Experience(
        date="2026-03-10",
        scenario={"sentiment_phase": "冰点", "limit_up_range": "1-30"},
        error_type="sentiment",
        lesson="冰点期不应追高",
        correction_rule="当跌停>20时，所有追高操作需额外确认",
    )
    store.add(exp)

    # 按场景检索
    tags = ScenarioClassifier.classify(limit_up_count=15, limit_down_count=25)
    results = store.search(scenario=tags, error_type="sentiment")
    assert len(results) == 1
    assert "冰点" in results[0].lesson
```

### 集成测试（需要数据 + LLM）

```bash
# 回测最近 5 个交易日（需要 data_dir 有对应数据 + ARK_API_KEY）
python -m backtest --data-dir ~/trading-data --start 2026-03-24 --end 2026-03-31

# 干跑（检查数据加载和场景分类，不消耗 LLM）
python -c "
from backtest.adapter import ReviewDataProvider
dp = ReviewDataProvider()
dates = dp.discover_dates('~/trading-data', '2026-03-24', '2026-03-31')
for d in dates:
    md = dp.load_market_data('~/trading-data', d)
    print(d, md.limit_up_count, md.limit_down_count, md.blown_rate)
"
```

### 迁移旧数据测试

```bash
# 预览迁移（不写入）
python -c "
from backtest.experience.migrate import migrate_legacy_lessons
migrate_legacy_lessons('~/trading-data', dry_run=True)
"
```
