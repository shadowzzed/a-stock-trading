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

### 依赖关系：反向依赖

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

### 回测引擎内部流程

每天回测跑 6 步：

```
Step 0: 场景识别
    DataProvider.load_market_data(Day D)
    → MarketData（涨停数/跌停数/炸板率/连板高度/...）
    → ScenarioClassifier.classify()
    → ScenarioTags（7维离散标签）

Step 1: 教训匹配 & Prompt 注入
    PromptEngine.build_injection(market_data)
    → ExperienceStore.search(scenario=当前场景)
    → 按效果值排序，取 Top N
    → 按 Agent 角色分配（情绪分析师只看 sentiment 类教训等）
    → 生成注入文本

Step 2: 跑 Agent 分析（带教训注入）
    AgentRunner.run(Day D, config={prompt_overrides: 注入文本})
    → LangGraph 多 Agent 复盘流程
    → 当日分析报告

Step 3: 加载 Day D+1 实际行情
    DataProvider.load_next_day_summary(Day D+1)
    → 涨停/跌停汇总 + 推荐标的实际涨跌幅

Step 4: 验证打分
    LLMCaller.invoke(VERIFIER_PROMPT, 报告+实际行情)
    → 四维打分（情绪/板块/龙头/策略，各 1-5 分，满分 20）
    → key_lessons / what_was_right / what_was_wrong

Step 5: 提取结构化经验
    LLMCaller.invoke(EXPERIENCE_EXTRACTOR_PROMPT, 验证结果)
    → Experience 对象（场景+错误类型+教训+修正规则）
    → ExperienceStore.add()（自动去重合并同类经验）

Step 6: 记录教训效果
    LessonTracker.record_injection(注入的教训ID, 实际得分)
    → 更新每条教训的累计改善率
    → 自动升降权（active → deprecated → promoted）
```

### 经验库数据模型

```
Experience（一条结构化经验）
├── id: 唯一标识（uuid hex 12位）
├── date: 回测日期
├── scenario: ScenarioTags 字典
│   ├── sentiment_phase: 冰点/修复/升温/高潮/分歧/退潮
│   ├── limit_up_range: 涨停数区间
│   ├── limit_down_range: 跌停数区间
│   ├── blown_rate_range: 炸板率区间
│   ├── max_board_range: 最高连板区间
│   ├── sector_concentration: 板块集中度
│   └── volume_trend: 成交量趋势
├── prediction: Agent 原始判断摘要
├── reality: 实际结果摘要
├── scores: 四维评分
├── error_type: sentiment / sector / leader / strategy
├── lesson: 提炼出的教训
├── correction_rule: 可执行的修正规则
├── confidence: 置信度（重复出现 → 递增，上限 0.95）
├── effectiveness: 注入后平均改善分数（负值=有害 → deprecated）
├── occurrence_count: 同类合并次数
└── injection_count: 被注入 Prompt 的次数

去重合并策略：
  新经验的 (error_type + scenario 3个维度匹配) == 已有经验
  → 合并（累加次数、保留更详细的 lesson、更新 confidence）
  → 不合并则新增

容量控制：MAX_EXPERIENCES = 200
  超出时按 confidence × effectiveness 排序，淘汰最差
```

### 效果追踪状态机

```
                    ┌─────────────────────────────┐
                    │                             │
                    ▼                             │
  [active] ──→ 注入后追踪 ──→ 3次以上改善<−1  ──→ [deprecated]（降权至 0.1，不再注入）
                    │
                    │ 5次以上改善>+2
                    ▼
              [promoted]（候选升级为量化规则）
```

## 限制

1. **数据依赖**：回测需要 `~/trading-data/daily/YYYY-MM-DD/` 下有完整的 CSV 数据（涨停板、跌停板、个股行情），由 `data/` 模块采集
2. **LLM 消耗**：每跑一天回测需要 3~5 次 LLM 调用（Agent 分析 + 验证打分 + 经验提取），20 个交易日约 60~100 次 API 调用
3. **经验库冷启动**：首次运行没有历史教训可注入，需要积累 5~10 个交易日的回测数据后才能看到改善效果
4. **场景标签粒度**：当前场景分类基于硬编码的阈值区间（如涨停数 0/1-30/31-50/...），不会自动适应市场格局变化
5. **回测 ≠ 实盘**：回测验证的是"Agent 的分析逻辑是否自洽"，不代表实盘收益。Day D 报告中的具体标的推荐，受盘中时效性影响，回测无法完全还原

## 数据隔离：防止未来数据泄露

回测时 Agent 必须只能看到 Day D 及之前的数据，否则验证结果无效。

### 实现方案：工具层日期边界过滤

LLM 只能通过 LangChain Tool 访问数据。所有涉及 `intraday.db` 查询的工具都支持 `max_date` 参数，在 SQL 层面添加 `WHERE date <= ?` 过滤，物理上阻止未来数据泄露。

### 受影响的工具（3个 DB 查询工具）

| 工具 | loader 函数 | 过滤方式 |
|------|------------|---------|
| `get_market_data` | `load_market_snapshot` | fallback 不超过 max_date；SQL `WHERE date <= ?` |
| `get_stock_detail` | `load_stock_detail` | fallback 不超过 max_date；请求日期超 max_date 时返回空 |
| `scan_trend_stocks` | `scan_trend_stocks` | trading_days 查询加 `WHERE date <= ?` |

不受影响的工具（无 DB 查询，无泄露风险）：

| 工具 | 数据来源 | 为何安全 |
|------|---------|---------|
| `get_history_data` | CSV 文件（`daily/` 目录） | `_load_history` 按日期目录回溯，天然只看到 <= D |
| `get_review_docs` | 文件系统（`daily/D/review_docs/`） | 按具体日期路径读取 |
| `get_memory` | memory 文件 | `load_memory` 有日期过滤 |
| `get_lessons` | 经验库 JSON | 静态文件 |
| `get_prev_report` | 文件系统（`daily/<D/reports/`） | 明确限定 `d < factory.date` |
| `get_past_report` | 文件系统 | 明确限定 `date >= factory.date` 时拒绝 |
| `get_index_data` | CSV / mootdx | 按指定日期查询 |
| `get_capital_flow` | CSV / mootdx | 按指定日期查询 |
| `get_quant_rules` | 静态 JSON | 无时间维度 |

### 参数传递链

```
backtest/adapter.py
  ChatAgentRunner.run(date=D)
    ↓  backtest_max_date=D
trading_agent/chat/agent.py
  TradingChatAgent(backtest_max_date=D)
    ↓
trading_agent/chat/coordinator.py
  CoordinatorAgent(backtest_max_date=D)
    ↓
trading_agent/chat/agents/base.py
  BaseAgent(backtest_max_date=D)
    ↓
trading_agent/review/tools/retrieval.py
  RetrievalToolFactory(backtest_max_date=D)
    ↓  max_date=factory.backtest_max_date
trading_agent/review/data/loader.py
  load_market_snapshot(..., max_date=D)
  load_stock_detail(..., max_date=D)
  scan_trend_stocks(..., max_date=D)
    ↓
SQL: WHERE date <= 'D'   ← 物理过滤
```

实盘模式下 `backtest_max_date=None`，所有工具行为完全不变。

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
