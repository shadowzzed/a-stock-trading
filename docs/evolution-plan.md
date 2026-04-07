# Trade Agent 进化方案 — Phase 1 & 2

> 版本：v1.0 | 日期：2026-04-07

## 目标

让 tradeAgent 从"手动注入经验"进化为"自动闭环进化"，持续提高交易能力。

---

## 总览

```
Phase 1A: Reflexion 反思节点          → graph.py 新增 reflect 节点
Phase 1B: 经验自动导入                → 新建 auto_import.py + CLI
Phase 1C: 日常报告归档                → 新建 report_archiver.py
Phase 1D: 版本号机制                  → 新建 version.py + 回测对比
Phase 2:  ExpeL 批量经验蒸馏          → 新建 distill.py
```

---

## Phase 1A：Reflexion 反思节点

### 目的

Agent 输出分析报告后，对照历史失败模式自我审视，发现重复犯错时自动修正。

### 改动文件

`trading_agent/chat/graph.py`

### 图拓扑变化

```
Before:
  synthesize → validate_output → END
  direct_reply → validate_output → END

After:
  synthesize → reflect → validate_output → END
  direct_reply → reflect → validate_output → END
```

### reflect 节点逻辑

```python
def reflect(state: TradingState) -> dict:
    """反思节点：对照历史失败教训审视输出，修正策略性错误。"""

    messages = state.get("messages", [])
    content = messages[-1].content

    # 1. 短回复/纯数据查询 → 跳过反思
    if len(content) < 100 or not _contains_recommendation(content):
        return {}

    # 2. 获取当前市场场景
    scenario = _get_current_scenario()  # 复用 ScenarioClassifier

    # 3. 从 ExperienceStore 检索匹配的失败教训（Top 5）
    lessons = experience_store.search(
        scenario=scenario,
        min_confidence=0.3,
        max_results=5
    )

    if not lessons:
        return {}  # 无相关教训，跳过

    # 4. 构建反思 prompt
    reflect_prompt = f"""你是交易策略审查员。请对照历史失败教训，审查以下分析报告。

## 历史失败教训（在类似市场场景中验证过的）
{_format_lessons(lessons)}

## 待审查的分析报告
{content}

## 审查要求
1. 逐条对照教训，检查报告中的推荐是否重复了已知的失败模式
2. 如果发现问题：
   - 直接在原文基础上修正，标注 [反思修正] 和修正理由
   - 修正可以是：删除有问题的推荐、调整仓位建议、补充风险提示
3. 如果没有问题：原样输出报告内容，不加任何标注

直接输出最终版本的报告（不要输出分析过程）："""

    # 5. LLM 审查 + 修正
    llm = _get_llm()
    resp = llm.invoke([HumanMessage(content=reflect_prompt)])
    revised = resp.content

    return {"messages": [AIMessage(content=revised)]}
```

### 关键设计决策

- **只审查含操作建议的回复**：纯数据查询（"XX多少钱"）不触发反思
- **教训动态获取**：从 ExperienceStore 按场景匹配，不硬编码
- **直接修正而非追加警告**：用户看到的是修正后的最终版本
- **修正处标注 [反思修正]**：用户可以看出哪些结论被修正了

---

## Phase 1B：经验自动导入

### 目的

回测完成后一键导入经验到 ExperienceStore，取代手动审阅+导入。

### 新建文件

`backtest/experience/auto_import.py`

### 核心逻辑

```python
class ExperienceAutoImporter:
    """回测经验自动导入器。"""

    def __init__(self, store: ExperienceStore, tracker: LessonTracker):
        self.store = store
        self.tracker = tracker

    def import_from_review(self, review_json_path: str,
                           min_confidence: float = 0.3,
                           auto_approve: bool = False) -> ImportStats:
        """从回测经验审阅 JSON 导入。

        过滤规则：
        1. correction_rule 非空（必须有可执行的修正规则）
        2. confidence >= min_confidence
        3. 失败经验：pnl < -2% 才导入（轻微亏损不提取规则）
        4. 成功经验：pnl > 3% 才导入

        Returns:
            ImportStats(added, merged, skipped, rejected)
        """

    def import_from_backtest_dir(self, backtest_dir: str, **kwargs) -> ImportStats:
        """从回测输出目录自动发现 经验总结.json 并导入。"""

    def dry_run(self, review_json_path: str) -> list[Experience]:
        """预览模式：返回将要导入的经验列表，不实际写入。"""
```

### CLI 入口

新建 `backtest/import_experience.py`：

```bash
# 预览将导入哪些经验
python -m backtest.import_experience ~/shared/backtest/20260405_120000/ --dry-run

# 交互式导入（逐条确认）
python -m backtest.import_experience ~/shared/backtest/20260405_120000/

# 全自动导入（流水线中使用）
python -m backtest.import_experience ~/shared/backtest/20260405_120000/ --auto

# 在 run.py 中集成
python -m backtest.run --data-dir ~/trading-data --start 2026-04-01 --auto-import
```

### 改动文件

- 新建 `backtest/experience/auto_import.py`（~120行）
- 新建 `backtest/import_experience.py`（~40行 CLI 入口）
- 修改 `backtest/run.py`：加 `--auto-import` 参数（~15行）
- 修改 `backtest/engine/core.py`：run() 末尾可选调用 auto_import（~10行）

---

## Phase 1C：日常报告归档

### 目的

实盘对话中产生的分析报告自动保存为结构化文件，纳入进化闭环。

### 归档触发时机

在 `graph.py` 的 `validate_output` 之后（最终回复确定后），检测回复中是否包含 `focus_stocks` JSON 或操盘建议，如果有则自动归档。

### 归档内容

```
~/shared/trading/daily_reports/
  2026-04-07/
    report.md              # 完整分析报告（Agent 原始输出）
    focus_stocks.json      # 结构化推荐标的（从报告中提取）
    metadata.json          # 元数据（版本号、场景标签、时间戳）
    verify.json            # D+1 验证结果（次日自动生成）
```

### 新建文件

`trading_agent/chat/report_archiver.py`

```python
class ReportArchiver:
    """日常分析报告归档器。"""

    ARCHIVE_DIR = "~/shared/trading/daily_reports"

    def archive(self, content: str, version: str) -> Optional[str]:
        """检测并归档分析报告。

        1. 检测内容是否包含 focus_stocks JSON 或操盘计划
        2. 提取 focus_stocks 结构化数据
        3. 获取当前市场场景标签
        4. 写入归档目录

        Returns:
            归档路径，非报告类内容返回 None
        """

    def verify_previous(self, date: str) -> Optional[dict]:
        """验证前日推荐标的的实际表现。

        1. 读取前日 focus_stocks.json
        2. 加载今日实际行情
        3. 计算 pnl_pct
        4. 写入 verify.json
        """
```

### 自动验证机制

通过定时任务（或每天首次对话时触发）：

```
每个交易日 15:30 后：
  1. 读取前日 focus_stocks.json
  2. 调用行情工具获取今日 OHLCV
  3. 计算按开盘买入的 pnl
  4. 写入 verify.json
  5. 这些 verify.json 可以直接被 Phase 2 蒸馏模块消费
```

### 在 graph.py 中的集成

```python
# validate_output 节点末尾追加归档逻辑
def validate_output(state: TradingState) -> dict:
    # ... 现有审查逻辑 ...

    # 归档（异步，不阻塞回复）
    try:
        archiver = ReportArchiver()
        archiver.archive(final_content, version=AGENT_VERSION)
    except Exception as e:
        logger.warning("报告归档失败: %s", e)

    return result
```

---

## Phase 1D：版本号机制

### 目的

1. 追踪 tradeAgent 的每次能力变更
2. 版本变化后可用同一批历史数据重新回测，量化对比能力提升
3. 日常报告带版本号标记，方便回溯

### 版本号规则

```
格式：v{major}.{minor}.{patch}

major: 架构变更（如加 reflect 节点、换模型）
minor: 策略变更（如 prompt 修改、新增规则、经验库大更新）
patch: 微调（如阈值调整、bug 修复）
```

### 新建文件

`trading_agent/version.py`

```python
# Trade Agent 版本号
# 每次修改 prompt、规则、架构时更新此文件

AGENT_VERSION = "v1.0.0"

CHANGELOG = {
    "v1.0.0": {
        "date": "2026-04-07",
        "changes": [
            "初始版本号",
            "新增数据纪律规则（防幻觉）",
            "新增 validate_output 数据审查节点",
            "新增 reflect 反思节点",
        ],
        # 回测基准（同一批数据的表现）
        "benchmark": {
            "test_period": None,     # 首次回测后填入
            "avg_pnl_pct": None,
            "hit_rate": None,
            "sharpe": None,
        }
    }
}

def get_version() -> str:
    return AGENT_VERSION

def get_changelog() -> dict:
    return CHANGELOG
```

### 版本对比回测

修改 `backtest/run.py`，增加 `--compare` 模式：

```bash
# 正常回测（自动记录版本号）
python -m backtest.run --data-dir ~/trading-data --start 2026-03-01 --end 2026-03-31
# 输出目录：~/shared/backtest/20260407_v1.0.0/

# 版本对比：用同一批数据对比两个版本
python -m backtest.compare v0.9.0 v1.0.0 --period 2026-03-01:2026-03-31
```

对比报告输出：

```markdown
# 版本对比报告：v0.9.0 → v1.0.0

| 指标 | v0.9.0 | v1.0.0 | 变化 |
|------|--------|--------|------|
| 平均日收益 | +0.3% | +0.8% | +0.5% ↑ |
| 胜率 | 52% | 61% | +9% ↑ |
| 最大回撤 | -5.48% | -3.2% | +2.28% ↑ |
| 空仓正确率 | 60% | 78% | +18% ↑ |

## 关键改善
- v1.0.0 在退潮期空仓率从 40% 提升到 85%（reflect 节点修正了3次追高后排的建议）
- 数据幻觉从 12 次降至 0 次（validate_output 拦截）

## 仍需改善
- 冰点期的买入时机仍不够精准
```

### 版本号的使用位置

| 位置 | 用途 |
|------|------|
| 回测输出目录名 | `~/shared/backtest/{日期}_v1.0.0/` |
| 日常报告 metadata.json | `{"version": "v1.0.0", ...}` |
| verify.json | 标记生成该报告的 Agent 版本 |
| ExperienceStore | 每条经验标记提取时的版本 |
| CHANGELOG | 记录每个版本的变更和基准数据 |

---

## Phase 2：ExpeL 批量经验蒸馏

### 目的

从多天的回测/实盘数据中，自动对比成功 vs 失败交易，归纳系统性规则。

### 新建文件

`backtest/experience/distill.py`

### 核心流程

```python
class ExperienceDistiller:
    """ExpeL 式经验蒸馏器。"""

    def distill(self, data_dirs: list[str],
                min_group_size: int = 6) -> DistillReport:
        """批量蒸馏。

        Args:
            data_dirs: 回测输出目录 或 日常报告目录
            min_group_size: 每组最少交易笔数（成功+失败 >= 此值）

        流程：
        1. 汇总所有交易记录（从 verify.json 加载）
        2. 按维度分组（见下方分组策略）
        3. 每组内分成功/失败
        4. LLM 对比分析，归纳规则
        5. 规则去重 & 写入 ExperienceStore
        """

    def _group_trades(self, trades: list[Trade]) -> dict[str, TradeGroup]:
        """多维度分组。"""

    def _distill_group(self, group: TradeGroup) -> list[DistilledRule]:
        """单组蒸馏：LLM 对比成功 vs 失败。"""

    def _deduplicate_rules(self, new_rules: list[DistilledRule],
                           existing: ExperienceStore) -> list[DistilledRule]:
        """语义去重：LLM 判断新规则是否与已有教训重复。"""
```

### 分组策略

```python
DISTILL_DIMENSIONS = [
    # 单维度分组
    {"key": "sentiment_phase"},           # 按情绪阶段
    {"key": "error_type"},                # 按错误类型（跨场景）

    # 双维度交叉（发现更细粒度的规律）
    {"key": ("sentiment_phase", "blown_rate_range")},   # 情绪 × 炸板率
    {"key": ("sentiment_phase", "max_board_range")},    # 情绪 × 连板高度

    # 特殊分组
    {"key": "time_of_recommendation"},    # 按推荐时段（竞价/早盘/午后）
    {"key": "stock_type"},                # 按标的类型（龙头/后排/趋势）
]
```

### 蒸馏 Prompt

```
你是一位资深交易教练。以下是 Trade Agent 在 [{场景}] 场景下的交易记录。

## 成功交易（{N}笔，平均收益 +{X}%）
{逐笔列出：日期、标的、Agent分析摘要、实际收益}

## 失败交易（{M}笔，平均亏损 -{Y}%）
{逐笔列出：日期、标的、Agent分析摘要、实际亏损}

请对比成功和失败交易，归纳出 1-3 条可执行规则。

输出 JSON 数组：
[
  {
    "rule": "具体可执行的规则描述",
    "evidence": "成功X次/失败Y次的统计证据",
    "scenario_scope": "适用的场景范围",
    "confidence": 0.0-1.0（基于样本量和一致性）
  }
]
```

### 蒸馏报告

每次蒸馏后输出 `distill_report.md`：

```markdown
# 蒸馏报告 — v1.0.0 | 2026-04-07

## 数据范围
- 来源：回测 2026-03-01 ~ 2026-03-31（19个交易日）
- 总交易笔数：47笔（成功 22 笔，失败 25 笔）

## 新发现的规则（3条）
1. **升温期午后封板标的次日亏损率 78%**
   - 证据：成功 2/9，失败 7/9
   - 建议：升温期只做 10:30 前封板的标的

2. ...

## 被强化的已有规则（2条）
1. "退潮+炸板>50% → 空仓" — 新增 5 个样本支持，confidence 0.7→0.85

## 被否定的已有规则（0条）
（无）
```

### CLI

```bash
# 对单次回测蒸馏
python -m backtest.distill ~/shared/backtest/20260405_v1.0.0/

# 合并多次回测 + 日常报告蒸馏
python -m backtest.distill ~/shared/backtest/202604*/ ~/shared/trading/daily_reports/2026-04-*/

# 在回测流水线中集成
python -m backtest.run --data-dir ~/trading-data --start 2026-03-01 --end 2026-03-31 \
    --auto-import --distill
```

---

## 改动清单

| 阶段 | 文件 | 操作 | 改动量 |
|------|------|------|-------|
| 1A | `trading_agent/chat/graph.py` | 新增 reflect 节点 | ~80行 |
| 1B | `backtest/experience/auto_import.py` | 新建 | ~120行 |
| 1B | `backtest/import_experience.py` | 新建 CLI | ~40行 |
| 1B | `backtest/run.py` | 加 --auto-import | ~15行 |
| 1B | `backtest/engine/core.py` | run() 末尾集成 | ~10行 |
| 1C | `trading_agent/chat/report_archiver.py` | 新建 | ~150行 |
| 1C | `trading_agent/chat/graph.py` | validate_output 中调用归档 | ~10行 |
| 1D | `trading_agent/version.py` | 新建 | ~50行 |
| 1D | `backtest/run.py` | 输出目录带版本号 | ~10行 |
| 1D | `backtest/compare.py` | 新建版本对比 | ~150行 |
| 2 | `backtest/experience/distill.py` | 新建 | ~250行 |
| 2 | `backtest/run.py` | 加 --distill | ~15行 |

**总计**：~900行新代码，~60行改动

---

## 完整进化闭环（实现后）

```
日常对话                              回测
  │                                     │
  ├─ Agent 分析 (v1.2.0)               ├─ Agent 分析 (v1.2.0)
  ├─ reflect 反思修正                   ├─ D+1 实际验证
  ├─ validate_output 数据审查           ├─ 经验提取
  ├─ 报告归档 → daily_reports/         ├─ 自动导入 ExperienceStore
  │                                     ├─ ExpeL 蒸馏 → 新规则
  │                                     │
  └──────── verify.json ───────────────→ 合并蒸馏
                                         │
                                         ↓
                                    ExperienceStore
                                         │
                     ┌───────────────────┼────────────────────┐
                     ↓                   ↓                    ↓
              PromptEngine          reflect 节点         版本对比
              (动态注入教训)      (实时策略审查)      (量化能力变化)
                     │                   │                    │
                     └───────────────────┼────────────────────┘
                                         ↓
                                  tradeAgent v1.3.0
                                   (自动进化)
```
