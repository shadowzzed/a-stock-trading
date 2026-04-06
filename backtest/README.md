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
│   ├── core.py          # 回测主流程 + BacktestPortfolioTracker（持仓追踪）
│   └── report.py        # 汇总报告生成（JSON + Markdown，含数据泄露审计统计）
├── trade/               # 交易模拟
│   ├── models.py        # 数据模型（Signal、Record、Portfolio、Position）
│   ├── executor.py      # 交易执行模拟器（TradeSimulator）
│   ├── signal_parser.py # 报告 → 交易信号解析
│   └── evaluator.py     # 交易模拟结果评估
├── adapter.py           # 唯一桥接 review/ 的适配器（持仓状态注入 + 审计）
└── run.py               # CLI 入口

tests/
└── test_backtest_no_future_leak.py  # 数据泄露防护测试（17 个用例）
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

每天回测跑以下步骤：

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

Step 1.5: 持仓管理
    BacktestPortfolioTracker.sell_all(Day D)
    → 卖出前日持仓（T+1，Day D 开盘价）
    BacktestPortfolioTracker.get_state(Day D)
    → 生成持仓快照（总资产/现金/持仓标的/浮盈）

Step 2: 跑 Agent 分析（带教训注入 + 持仓状态）
    AgentRunner.run(Day D, config={...}, portfolio_state=持仓快照)
    → Agent 感知当前持仓，避免重复推荐、合理分配仓位
    → 当日分析报告

Step 3: 加载 Day D+1 实际行情
    DataProvider.load_next_day_summary(Day D+1)
    → 涨停/跌停汇总 + 推荐标的实际涨跌幅

Step 4: 收益率验证（数据驱动，无 LLM）
    从报告中提取推荐标的 → 查 Day D+1 OHLCV → 计算开盘买入收益率
    → avg_pnl_pct, hit_rate

Step 4.5: 模拟买入
    BacktestPortfolioTracker.buy_from_recommendations(recs, Day D+1)
    → 按推荐标的在 Day D+1 开盘价买入（固定 3 成仓位）

Step 5: 提取经验（亏损标的规则化，无需 LLM）
    亏损 > 2% 或次日跌幅 > 5% 的标的 → Experience 对象
    → ExperienceStore.add()（自动去重合并同类经验）
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

1. **数据依赖**：回测需要 `~/shared/trading/daily/YYYY-MM-DD/` 下有完整的 CSV 数据（涨停板、跌停板、个股行情），由数据采集模块生成
2. **LLM 消耗**：每跑一天回测需要多次 LLM 调用（4 个 Sub-Agent 并行分析 + Coordinator 综合），20 个交易日消耗较大
3. **经验库冷启动**：首次运行没有历史教训可注入，需要积累 5~10 个交易日的回测数据后才能看到改善效果
4. **场景标签粒度**：当前场景分类基于硬编码的阈值区间（如涨停数 0/1-30/31-50/...），不会自动适应市场格局变化
5. **回测 ≠ 实盘**：回测验证的是"Agent 的分析逻辑是否自洽"，不代表实盘收益
6. **并行模式限制**：`--workers > 1` 时无持仓状态传递和前日报告传递，回测质量低于顺序模式
7. **持仓模型简化**：持仓追踪器采用固定 3 成仓位、T+1 开盘买收盘卖的简化模型，不完全模拟真实交易
8. **报告缓存**：已存在的 `_report.md` 会被跳过。重新回测（例如启用持仓状态后）需先清理旧报告

## 数据隔离：防止未来数据泄露

回测时 Agent 必须只能看到 Day D 及之前的数据，否则验证结果无效。系统实现了**四层防护**。

### 第一层：工具层拦截

`RetrievalToolFactory._check_date_boundary()` 统一拦截所有工具的越界日期请求。所有接受日期参数的工具在执行前都会校验，超出 `backtest_max_date` 的请求直接返回错误信息。

| 工具 | 防护方式 |
|------|---------|
| `get_review_docs` | `_check_date_boundary()` |
| `get_memory` | `_check_date_boundary()`（显式日期参数）+ loader `mem_date <= date` |
| `get_index_data` | `_check_date_boundary()` |
| `get_capital_flow` | `_check_date_boundary()` |
| `get_stock_detail` | `_check_date_boundary()` + loader `max_date` |
| `get_market_data` | `_check_date_boundary()` + loader `max_date` |
| `get_past_report` | `date >= factory.date` 检查 + `_check_date_boundary()` |
| `scan_trend_stocks` | loader `max_date`（无用户日期参数） |
| `get_history_data` | loader `d < current_date`（无用户日期参数） |
| `get_prev_report` | 自动取 `< factory.date`（无用户日期参数） |
| `get_lessons` | 静态文件，无日期参数 |
| `get_quant_rules` | 静态文件，无日期参数 |

### 第二层：Prompt 层约束

回测模式下，`BaseAgent.__init__()` 自动在 system prompt 末尾注入：

```
## 回测模式约束
当前处于回测模式，模拟日期为 {date}。
你只能使用 {date} 及之前的数据进行分析。
所有工具调用中的日期参数不得超过 {date}。
不要尝试获取该日期之后的任何信息。
```

### 第三层：审计层日志

每次工具的日期检查都会记录审计条目（`RetrievalToolFactory._record_audit()`）：
- 被拦截的越界请求输出 WARNING 日志
- `TradingChatAgent.get_audit_summary()` 汇总所有 Agent（含 Sub-Agent）的审计数据
- 回测结果 `{date}_verify.json` 中包含 `data_leak_audit` 字段
- 汇总报告 `summary_v6.json` 和 `回测报告_v6.md` 中包含整体泄露审计统计

### 第四层：测试层保护

`tests/test_backtest_no_future_leak.py`（17 个测试用例）：

| 测试组 | 说明 |
|--------|------|
| `TestFutureDataBlocked`（7 个） | 验证每个工具都会拦截未来日期 |
| `TestPastDataAllowed`（6 个） | 验证合法日期正常返回数据 |
| `TestBoundaryConditions`（3 个） | 边界日期和非回测模式测试 |
| `TestToolCompleteness`（1 个） | 自动检测所有工具是否都在测试覆盖中注册 |

**关键设计**：`TestToolCompleteness` 维护了 `DATE_BOUND_TOOLS` 和 `DATE_FREE_TOOLS` 两个集合。新增工具时如果没注册到任何一个集合，此测试会**自动失败**，强制开发者补充泄露测试。

### 参数传递链

```
backtest/adapter.py
  ChatAgentRunner.run(date=D)
    ↓  backtest_max_date=D
trading_agent/chat/agent.py
  TradingChatAgent(backtest_max_date=D)
    ↓
trading_agent/chat/agents/base.py
  BaseAgent(backtest_max_date=D)
    ├── system_prompt += 回测模式约束（Prompt 层）
    └── RetrievalToolFactory(backtest_max_date=D)
        ├── _check_date_boundary()（工具层）
        ├── _record_audit()（审计层）
        └── max_date 传递到 loader（数据层）
```

实盘模式下 `backtest_max_date=None`，`_check_date_boundary()` 以 `factory.date` 为上界，所有工具行为不受影响。

## 如何测试

### 数据泄露防护测试（必跑）

```bash
# 17 个测试用例，验证所有工具的日期边界拦截
python -m pytest tests/test_backtest_no_future_leak.py -v
```

**新增检索工具时必须先跑此测试**。`TestToolCompleteness` 会自动检测未注册的新工具并报错。

### 集成测试（需要数据 + LLM）

```bash
# 完整回测
python -m backtest.run --data-dir ~/shared/trading --start 2026-03-24 --end 2026-03-31

# 并行模式（注意：无持仓状态传递）
python -m backtest.run --data-dir ~/shared/trading --start 2026-03-24 --workers 4

# 仅交易模拟（复用已有报告，零 LLM 消耗）
python -m backtest.run --data-dir ~/shared/trading --start 2026-03-24 --trade-sim-only

# 干跑（检查数据加载和场景分类，不消耗 LLM）
python -c "
from backtest.adapter import ReviewDataProvider
dp = ReviewDataProvider()
dates = dp.discover_dates('~/shared/trading', '2026-03-24', '2026-03-31')
for d in dates:
    md = dp.load_market_data('~/shared/trading', d)
    print(d, md.limit_up_count, md.limit_down_count, md.blown_rate)
"
```
