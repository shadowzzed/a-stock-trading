# Trade Agent 迭代路线图（2026-05-11）

> 目标：月化收益 ≥20%，回撤 ≤15%，胜率 ≥55%
> 当前生产：v8_tight（已于 2026-05-11 切换上线）

## 0. TL;DR

**关键决策**：生产从 v8_base（-7/+15）切换到 v8_tight（-5/+10）。

shadow_runner 18 个交易日（2026-04-13 ~ 2026-05-08）8 策略对比（**串跑结果，shadow_runner 有跨策略污染 bug**）：

> ⚠️ **重要更正**：v8_tight 隔离跑实际是 **+18.26%（24 笔）**，shadow_runner 串跑只有 +15.32%（21 笔），差 3pp。原因是 shadow_runner 跨策略调用时存在状态污染（已记 P1 todo）。下表保留串跑数据用于策略间相对排名，但**绝对回报以隔离跑为准**。

| 排名 | 策略 | 收益（串跑） | 收益（隔离） | 笔数 | 胜率 | 平均/笔 | 回撤 | 状态 |
|------|------|------------|------------|------|------|--------|------|------|
| 🥇 | **v8_tight** | +15.32% | **+18.26%** | 21~24 | 50-52% | 2.13% | 10.1% | ⚠️ warning |
| 🥈 | v8_auction_only | +7.66% | 4 | 75.0% | 6.73% | - | ✅ healthy |
| 🥉 | v8_base（旧生产） | +6.89% | 21 | 52.4% | 0.77% | 15.5% | 🚫 stop |
| 4 | v8_no_trend | +6.89% | 21 | 52.4% | 0.77% | 15.5% | 🚫 stop |
| 5 | v8_heat_gate | +6.89% | 21 | 52.4% | 0.77% | 15.5% | 🚫 stop |
| 6 | v8_sealed_only | +0.13% | 21 | 47.6% | -0.30% | 21.4% | 🚫 stop |
| 7 | v8_conservative | -0.56% | 17 | 41.2% | 0.01% | 17.4% | 🚫 stop |
| 8 | v8_wide | -2.32% | 21 | 52.4% | -0.71% | 19.4% | 🚫 stop |

**最重要的发现**：v8_tight 与 v8_base 的笔数完全相同（21 笔）、胜率完全相同（52.4%），但回报差 **8.4pp**。差异 100% 来自止损止盈点位 —— 同样的胜负个数下，紧止盈（10% vs 15%）避免了"赚到 12% 又回吐到 7%"的典型场景。

**月化测算**：v8_tight 18 个交易日 +15.32% → 月化 ~20-22%，**首次触及 20%/月目标**。但回撤 10%、连亏 5 笔的特征意味着实盘体验会很颠簸。

## 1. 当前因子系统盘点

```
Layer 1: 情绪门控（GLM）
  ├─ 大盘方向判断：上涨/震荡/下跌/分歧
  ├─ 市场情绪：激进/谨慎/恐慌
  └─ 热点板块：动态识别 1-3 个

Layer 2: 选股评分（screener.py）
  ├─ 涨停股路径（limit_up）
  │  ├─ seal_time:    封板时间分（S/A/B/C 级，0-5 分）
  │  ├─ blown:        炸板次数（0-3 分）
  │  ├─ volume:       成交额（mid/large，-2 到 +2 分）
  │  ├─ board:        连板数（含板块最高加分，1-6 分）
  │  ├─ continuity:   板块持续天数（0-3 分）
  │  ├─ leader:       是否有龙头（0-2 分）
  │  └─ prev_perf:    昨涨停今表现（0-2 分）
  └─ 趋势股路径（trend）
     ├─ trend_3d:     3 日涨幅（强 6/中 4/弱 2）
     ├─ trend_today:  今日涨幅（0-3 分）
     ├─ trend_volume: 量能放大（0-2 分）
     ├─ trend_sector: 板块匹配（0-3 分）
     ├─ trend_consecutive: 连阳天数（0-4 分）
     └─ trend_7d:     7 日涨幅（0-3 分）

Layer 3: 买卖信号（monitor.py）
  ├─ 入场信号
  │  ├─ sealed:        封板入场（次日开盘）
  │  ├─ auction_strong: 竞价高开 >3%
  │  └─ trend_breakout: 趋势股盘中突破 5%
  └─ 出场信号
     ├─ stop_loss:     浮亏 ≤ STOP_LOSS_PCT（-5%）
     ├─ take_profit:   浮盈 ≥ TAKE_PROFIT_PCT（+10%）
     ├─ max_hold:      超过 MAX_HOLD_DAYS（5 天）
     └─ trailing:      移动止盈（仅 --recommended 启用）
```

仓位：3 仓 × 30%（满仓 90%，留 10% 现金缓冲）。

## 2. 短期（本周，已上线）

### ✅ 2.1 v8_tight 切换 - 已完成

修改了：
- `trading_agent/intraday/monitor.py`: `STOP_LOSS_PCT=-5.0, TAKE_PROFIT_PCT=10.0`
- `trading_agent/intraday/layered_analysis.py`: 同步

预期效果：每笔平均盈利从 0.77% 提升到 2.13%（×2.8）。

### 🔥 2.2 立即跟进项（明天上班前）

**P0 - 池子 priority 字段为空 bug**：
- 池表 `priority` 列大量为空字符串（216/216 条中只有 61 条有值）
- 影响：morning_brief 粗排时 priority 维度失效，但其他维度（recency/股票数/板块数/解读长度）兜底，早报仍可工作
- 位置：`news_monitor/news_monitor.py:1204` save_to_morning_pool 函数
- 修复：核对 `is_critical_news()` 返回值与池 priority 字段映射

**P1 - 早报 markdown 链接渲染**：
- 当前用 `[原文](url)` 但飞书富文本卡片需要测试是否点击有效
- 测试：明早实盘验证 8:55 推送

### ⚠️ 2.3 不上线项

**v8_auction_only 不上**：虽然胜率 75%、+7.66%，但只有 4 笔，统计噪音太大。建议作为 shadow 持续跟踪 3 周（再积累 30+ 笔）后再决策。

**v8_sealed_only 不上**：胜率反而下降到 47.6%，且回撤 21%。说明竞价高开（auction_strong）路径对总体贡献为正，不能砍。

## 3. 中期（2-4 周）

### 3.1 因子精修方向（按预期 ROI 排序）

| 优先级 | 改动 | 预期 | 实测 | 工作量 | 风险 |
|--------|------|------|------|------|------|
| **P0** | **Layer 1 大盘门控（layer1_gate=True）** | +2-4pp | **熊市 +2.78pp ✅ / 震荡 无副作用**（见 §3.1.0） | 0h | 低 |
| ~~P0~~ | ~~移动止盈（trailing_activate=5/drawdown=3）~~ | ~~+3pp~~ | **实测 -1pp ❌，不推荐**（见 §3.1.1） | 2h | 低 |
| P0 | shadow_runner 跨策略污染 fix | 数据可靠性 | - | 4h | 低，已记 task #26 |
| P1 | 入场后第 1 分钟试错止损（-3% 立即出） | +2pp | 待测 | 4h | 中，可能误杀真信号 |
| P2 | 板块龙头加成提高（leader 2→3） | +1-2pp | 待测 | 1h | 低 |
| P2 | sealed_min_prev_board 收紧（1→2） | +1pp | 待测 | 1h | 减少噪音封板 |
| P3 | 多空过滤（连续 3 天下跌则空仓） | +2pp | 待测 | 6h | 高，需新数据流 |

### 3.1.0 Layer 1 大盘门控实测结果（2026-05-11，**已上线**）

backtest 增加了两种 Layer 1 实现，与生产逻辑统一：
- **deterministic（推荐 ✅）**：纯规则代码（`backtest/layered_engine.py:_code_sentiment_fallback`），与生产 `layered_analysis.py` 完全一致，0 LLM 成本
- LLM（DeepSeek）：仅作对比，实测不如 deterministic 激进

| 窗口 | 策略 | 收益 | 笔数 | 胜率 | Layer1 gates |
|------|------|------|------|------|-------------|
| 3-12 ~ 4-10（熊市） | v8_tight_naked（无 Layer1） | **-7.96%** | 12 | 16.7% | - |
| 3-12 ~ 4-10（熊市） | v8_tight_layer1（DeepSeek） | -5.18% | 10 | 20.0% | 6 空仓 / 10 谨慎 / 4 可买入 |
| 3-12 ~ 4-10（熊市） | **v8_tight（生产默认，deterministic）** | **-1.49%** | 12 | **33.3%** | **10 空仓** / 2 谨慎 / 8 可买入 |
| 4-13 ~ 5-08（震荡） | v8_tight_naked / v8_tight | +18.26% | 24 | 50.0% | deterministic: 6 谨慎 / 10 可买入 |

**机制**：deterministic Layer1 通过涨停数 / 跌停数 / 炸板率 / 边际变化判断情绪阶段。"退潮"/"冰点" → "空仓" → 强平所有仓位 + 拒绝新买入。震荡市无"空仓"输出，无副作用。

**为什么 deterministic 比 LLM 好**：
1. 0 调用成本（生产盘后已实时运行，可重现）
2. 在熊市更激进（10 空仓 vs LLM 的 6 空仓），实际止血更彻底
3. 回测可重现，不受 LLM 配额/语言抖动影响

**关键意义**：Layer1 是**熊市保险**，让 v8_tight 的最差月化从 -8% 收窄到 -1.5%。**已合并到 v8_tight 默认配置**（`backtest/strategies/__init__.py:50-56`）。生产 `layered_analysis.py:114-121` 早已实现，所以实际生产从 v8_tight 上线那一刻起就自带 Layer1 保护。

**已修 bug**：
- `monitor_backtest_v2.py:415` 调用 `MarketJudgmentRunner.run()` 传错参数名 `provider_name`（应为 `provider_index`）
- 添加 deterministic 路径（与生产 `_code_sentiment_fallback` 完全一致）
- Strategy dataclass 新增 `layer1_gate`/`layer1_provider` 字段
- `v8_tight` 默认 `layer1_gate=True, layer1_provider="deterministic"`

### 3.1.1 Trailing Stop Loss 实测结果（2026-05-11）

新增 2 个变体到 `backtest/strategies/__init__.py`：

| 变体 | 配置 | 回报 | 笔数 | 胜率 |
|------|------|------|------|------|
| v8_tight（baseline） | -5/+10, 无 trailing | **+18.26%** | 24 | 50.0% |
| v8_tight_trail | -5/+10, trailing +5%/回撤 3% | +17.30% | 24 | 50.0% |
| v8_tight_trail_wide | -5/+10, trailing +5%/回撤 5% | +18.26% | 24 | 50.0% |

**结论**：在 v8_tight 紧止盈（10%）配置下，trailing 几乎没机会触发：要么股票直接到 10% 止盈，要么回撤 5% 止损。中间被 trailing 拦截的样本极少，且拦截后反而切走潜在 winner（v8_tight_trail -1pp）。

**应用决策**：**不上线 trailing**。如果未来切宽止盈（比如 v8_wide 配置），trailing 可能有意义，但目前不需要。

### 3.2 单步实验设计（一周一个）

每周只验证一个改动，原则：

1. **基线锁定**：v8_tight 为对照组
2. **AB 跑 shadow_runner**：实验组在新规则下，对比同期 baseline
3. **stop 条件**：实验组连续 5 个交易日跑输 baseline → 立即回滚
4. **promote 条件**：实验组超过 baseline > 3pp 持续 2 周 → 升级为新生产

**Week 1 (5-11 ~ 5-15)**: trailing stop loss 验证
**Week 2 (5-18 ~ 5-22)**: Layer 1 GLM 门控验证
**Week 3 (5-25 ~ 5-29)**: 入场试错止损验证
**Week 4 (6-01 ~ 6-05)**: 综合 BEST-OF 配置回测

### 3.3 数据质量改进

- **5-09 数据缺失** 已验证是因为周六非交易日（OK）
- **ABB→ROK ticker 修复** 已在 morning_brief_us 完成
- **yfinance $KO/$TSLA/$BABA 临时 delisted 报错**：建议加重试逻辑（已观察 stderr 显示，未影响主结果）

## 4. 长期（1-3 个月）

### 4.1 因子库扩展方向

```
新增 Layer 2 维度：
  ├─ 资金流：北上资金、龙虎榜净买、主力净流入
  ├─ 估值：PE/PB 分位（避免高位陷阱）
  ├─ 情绪：股吧 / 同花顺评论情感分（NLP）
  ├─ 配对：行业 ETF 相对强度（XLK 大涨 → 半导体板块）
  └─ 季节性：财报季 / 政策窗口
```

### 4.2 模型化改造路线

阶段 1（当前）：规则评分 + LLM 解读
阶段 2（1 个月）：LLM 评分加权（次要因子由 LLM 判断给分）
阶段 3（3 个月）：训练专用打分模型（XGBoost / LightGBM），用于因子组合优化

### 4.3 多策略组合

- 池中并行运行 v8_tight + v8_auction_only + 实验组
- 按 7 日滚动 sharpe ratio 动态分配仓位（满仓 30%/30%/30%）
- 月度复盘 + 策略增删

## 5. 20%/月目标可达性评估

### 5.1 当前基准（2026-05-11 全量回测更新）

跨 38 个连续交易日（2026-03-12 ~ 2026-05-08，覆盖熊市+震荡两种行情）3 策略对比：

| 策略 | 总收益 | 月化 | 笔数 | 胜率 |
|------|------|-----|------|------|
| **v8_tight（生产，含 Layer1）** | **+24.86%** | **+13.05%** | 38 | 47.4% |
| v8_tight_naked（无 Layer1） | +18.92% | +10.05% | 38 | 44.7% |
| v8_base（旧生产 -7/+15） | +9.24% | +5.01% | 35 | 45.7% |

**边际价值拆解**：
- 紧止损 (-5/+10) vs (-7/+15): +9.68pp（v8_base → v8_tight_naked）
- Layer1 deterministic 门控: +5.94pp（v8_tight_naked → v8_tight）
- **合计：从 v8_base 9.24% 升到 v8_tight 24.86%，2.7x 倍提升**

| 情景 | 假设 | 月化预期 |
|------|------|---------|
| 乐观 | 升温/高潮市 + 趋势股放量（4-13~5-08 类） | **+18-22%** |
| 中性 | 当前混合（熊 + 震荡） | **+13%（实测）** |
| 悲观 | 极端熊市连续 1 个月 | **-2 ~ +3%**（Layer1 保护下） |

距 20% 月化目标还差 7pp，需要叠加 P1 因子改进（试错止损 +2pp、Layer 2 龙头加成 +1-2pp、sealed_min_prev_board 收紧 +1pp）。预计 2-3 周内能达到 18-20% 月化。

### 5.2 风险路径

**核心风险**：
1. **样本不足**：21 笔 18 天，回测窗口太短，过拟合风险
2. **市场风格依赖**：v8_tight 在震荡市最优，趋势市 v8_wide 才合理
3. **滑点未充分建模**：T+1 + 30% 仓位，封板买入时实际成交价可能偏离

**对冲措施**：
- 实盘前 1 周用 shadow 跑通 trailing + GLM 门控
- 实盘后每周一次 health check（用 strategy_health.py，需先修 v2 兼容）
- 月度切片切回 v8_base 做对照

### 5.3 健康度门槛

实盘运行中触发以下任一条件 → **暂停 1 天复盘**：

- 连续 7 个交易日累计 < -8%
- 单笔亏损 > -10%（远超 -5% 止损 → 系统问题）
- 连亏 ≥ 6 笔
- 15 日滚动回撤 > 15%

## 6. 待办列表

```
[已完成] v8_tight 切到生产
[已完成] News Monitor 池路由 + 早报上线
[已完成] 8 策略 shadow_runner 体检

[本周内] morning_brief priority 字段 bug 修复
[本周内] 早报 markdown 飞书渲染验证
[本周内] trailing_activate 默认开启的回测验证

[本月内] Layer 1 GLM 门控 shadow 验证 → 决策
[本月内] strategy_health.py 升级到 v2 兼容
[本月内] 实盘 health monitor（飞书报警）

[下月] 因子库扩展：资金流 + 估值
[下月] 多策略组合框架
```

---

`基于 shadow_runner 2026-05-11 报告生成` · `参数实盘上线于 2026-05-11`
