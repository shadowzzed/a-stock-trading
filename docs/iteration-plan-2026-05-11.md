# 9 小时自主迭代 Plan — 2026-05-11

**执行窗口**：2026-05-11 00:30 → 09:30（含早报必须 9:00 准时发）
**执行者**：Claude (Opus 4.7)，自主推进
**两大方向**：News Monitor 改造 + Trade Agent 回测验证 + 迭代方案

---

## 用户已确认的关键决策

| 决策 | 选择 |
|------|------|
| A 股新闻精选策略 | **两层精排**（粗筛 30 → LLM 精排 Top 12 + 一句话开盘启示）|
| 美股数据源 | **yfinance**（Top 11 SPDR Sector ETF + 科技七巨头 + 中概 + 半导体）|
| 早报发送 | **复用现有 webhook**（`config.yaml` 中的 `feishu_webhook_url`）|
| commit 策略 | **自由 commit + push 到 origin/main** |
| Trade Agent 收益目标 | **月度 20%**（按现有 `monitor_backtest_v2` 含 T+1 + 30% 仓位 + 10w 起步的实盘风格回测衡量）|

## 已知现状

- **News Monitor 进程**：当前**未运行**（ps aux 空）
- **News Monitor 现有调度**：30s 轮询；交易时间高优实时 + 低优 20 分钟聚合；非交易时间 60 分钟聚合
- **minute_bars 数据**：覆盖 2026-04-13 ~ 2026-05-08（约 18 个交易日活跃数据；daily_bars 覆盖 03-09 ~ 05-08）
- **当前最佳策略**：`backtest/monitor_backtest_v2.py --recommended` 配置已验证 19 天 +22.61% / 胜率 78.6% / 14 笔（2026-05-07 数据）
- **8 个新闻数据源**：TrendRadar / 财联社 / 华尔街见闻 / 金十 / BlockBeats / TechFlow / PANews / 东财研报

## 总体时间盒

| Phase | 内容 | 时长 | 时段 |
|-------|------|------|------|
| 0 | 工作区切片提交 + push（基础工作） | 30 min | 00:30-01:00 |
| 1 | News Monitor 改造（盘后入池 + 早报模块 + 美股拉取 + 调度） | 5h | 01:00-06:00 |
| 2 | Trade Agent 回测因子有效性 | 2h | 06:00-08:00 |
| 3 | Trade Agent 迭代方案文档 + 收尾 | 1h | 08:00-09:00 |
| 4 | 8:55 早报触发验证 + 最终 push | 30 min | 09:00-09:30 |

---

## Phase 0：工作区切片提交（30 min）

按之前给用户的 4-commit 切片，用户已批准 push 到 origin/main：

1. **Commit 1** "功能: Schema 迁移 + Chat 集成 News Impact"
   - intraday/{graph,intraday_tick}.py（snapshots → minute_bars + stock_meta）
   - chat/agents/*.py（加 get_news / search_similar_news / get_news_impact）
   - chat/coordinator.py（MiniMax 控制字符清理 + JSON 解析鲁棒化）
   - chat/graph.py
   - review/{data/loader, tools/retrieval}.py
   - tools/build_stock_concept_map.py（snapshots → daily_bars）

2. **Commit 2** "功能: News Monitor Phase 3/4 + Embedding 升级"
   - news_monitor/news_monitor.py 用户改动（DeepSeek 优先 + 事件分类 + 情绪指数 + 飞书消息卡片重构 + AI 精排）
   - news_monitor/impact/{db,embed}.py（transformers + safetensors）
   - news_monitor/prompts/news_interpret.md（事件类型字段）
   - tools/opening_analysis.py（_ensure_stock_meta + 重构）

3. **Commit 3** "功能: Backtest 主力套件入 git + 数据填充修复"
   - 新增（git add）: backtest/{monitor_backtest_v2, shadow_runner, param_sweep_v4}.py
   - 新增: backtest/strategies/__init__.py
   - 新增: data_tools/{data_quality_check, synthesize_minute_bars, fix_stock_meta}.py
   - 修改: backtest/{engine/report, screener, trade/signal_parser}.py
   - 修改: data/{backfill_intraday, backfill_minute_bars_sina, import_history}.py

4. **Commit 4** "重构: 清理废弃代码 + 文档对账 + 路径修正"（这次会话的）
   - 删除 trading_agent/chat/{*.mjs, node_modules, package*.json}
   - 删除 trading/ 目录（chat checkpoint 迁移到 data_root）
   - config.py: 加 chat_checkpoint_dir
   - chat/__main__.py: checkpoint 路径走 config
   - data/mootdx_tool.py: L226 相对路径 + L19 STOCKS_MD bug
   - README.md / CLAUDE.md: 全面文档对账（删失效引用、补 8 数据源、写 trading/ 角色、密钥说明）
   - 各 docstring 修复

**待你拍板的死代码**（先不删，留着 commit 5 等你回来确认）：
- backtest/backfill_daily_bars.py + _v2.py（0 引用）
- backtest/desensitize_lessons.py（一次性脱敏）
- data_tools/synthesize_0416_minute.py（4-16 应急）

---

## Phase 1：News Monitor 改造（5h，01:00-06:00）

### 1.1 新增"盘后候选池"机制（45 min）

**问题**：当前非交易时间 60 分钟聚合一次推送，效率低 + 噪音多。
**改造**：盘后所有新闻进 SQLite 候选池，不再发推送（除超紧急 critical 级别可选实时）。

新增 SQLite 表 `morning_brief_pool`：
```sql
CREATE TABLE morning_brief_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    news_key TEXT UNIQUE,        -- make_key() 哈希
    source TEXT,
    title TEXT,
    brief TEXT,                  -- 原文摘要
    interpretation TEXT,         -- AI 解读
    priority TEXT,               -- supply_demand/earnings/research/geopolitics/null
    event_type TEXT,             -- AI 提取的事件类型
    plates TEXT,                 -- JSON list
    stocks TEXT,                 -- JSON list
    url TEXT,
    news_time TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    used_in_brief INTEGER DEFAULT 0   -- 1 表示已用于早报
);
```

修改 `run_once()`：在 `flush_aggregate_buffer` 调用之外，对**非交易时间**的所有新闻做 `save_to_morning_pool(item, interpretation, priority)`。

砍掉 `AGGREGATE_INTERVAL_OFF_HOURS=60min` 的非交易时间聚合发送。保留交易时间高优实时 + 低优 20 分钟聚合。

**critical 兜底**：如果新闻 priority='geopolitics' AND 标题命中 ['核', '战争', '宣战', '台海', '美联储紧急', '熔断']（白名单），即使非交易时间也实时推送。

### 1.2 新增 morning_brief 模块（90 min）

新建 `news_monitor/morning_brief.py`：

```
generate_a_share_section() :
    1. 读 morning_brief_pool: created_at >= 昨日 15:00 AND used_in_brief=0
    2. 粗筛: 用 _rank_importance + 优先级 + 是否有 event_type 排序，取 Top 30
    3. AI 精排（call_ai with deepseek/ark）:
       - prompt: "你是 A 股短线交易员。这 30 条是昨夜+今晨新闻，请按【对开盘可能影响】打分排序，输出 Top 12。
         附加要求: 输出 JSON {ranked: [news_id], opening_hint: 'one-sentence summary'}"
       - 解析 JSON
    4. 格式化为飞书 Markdown:
       【🌅 A 股早报 · 重点新闻】
       💡 开盘启示：{opening_hint}
       
       1. [财报] [半导体] [中芯国际] {title}
          {AI 解读}
       2. ...
    5. UPDATE used_in_brief=1
```

### 1.3 美股部分 morning_brief_us.py（90 min）

新建 `news_monitor/morning_brief_us.py`：

```python
US_SECTOR_ETFS = {
    "XLK": "科技", "XLF": "金融", "XLE": "能源", "XLV": "医疗",
    "XLI": "工业", "XLY": "可选消费", "XLP": "必选消费",
    "XLU": "公用事业", "XLB": "原材料", "XLRE": "房地产", "XLC": "通讯",
}

US_STAR_STOCKS = {
    # 科技七巨头
    "NVDA": ("英伟达", "AI 芯片"),
    "TSLA": ("特斯拉", "电动车"),
    "AAPL": ("苹果", "消费电子"),
    "MSFT": ("微软", "云/AI"),
    "GOOG": ("Google", "云/AI"),
    "META": ("Meta", "社交/AI"),
    "AMZN": ("亚马逊", "电商/云"),
    # 中概股
    "BABA": ("阿里巴巴", "电商"),
    "PDD":  ("拼多多", "电商"),
    "JD":   ("京东", "电商"),
    "BIDU": ("百度", "AI"),
    # 半导体（A 股映射）
    "TSM":  ("台积电", "半导体代工"),
    "ASML": ("阿斯麦", "光刻机"),
    "AMD":  ("AMD", "AI 芯片"),
}

def fetch_us_data():
    # yfinance.download 批量拉昨日收盘价 + 涨跌幅
    # 返回 dict {symbol: {pct, close, volume, prev_close}}

def llm_summarize_us(sector_pcts, star_stocks_data, news_pool):
    # 把美股板块涨跌幅 + 明星股表现 + 昨夜美股相关新闻喂给 LLM
    # prompt: "你是中美股票联动分析师。基于美股 11 板块和明星股表现，给 A 股投资者 5 条要点：
    #   1) 哪几个板块大涨/大跌？
    #   2) 中概表现如何？
    #   3) 半导体/AI 链如何？映射 A 股哪些板块？
    #   4) 给一个'今日 A 股关注方向'的判断（不是预测，仅是值得关注的方向）"

def generate_us_section():
    # 整合上面，输出 Markdown
```

### 1.4 主调度入口 + 整合（45 min）

新增 `news_monitor/__main__.py` 增加 `morning_brief` 子命令：

```bash
python -m news_monitor morning_brief         # 生成并发送
python -m news_monitor morning_brief --dry   # 仅打印不发
```

整合 morning_brief.py + morning_brief_us.py 输出到一条飞书消息。失败重试 3 次 + fallback 到无 LLM 简化版。

### 1.5 News Monitor 启动 + 早报调度（30 min）

启动方案：
- 优先用 `tools/daily_maintenance.sh` 同样的 LaunchAgent 模式
- 创建 `~/Library/LaunchAgents/com.luoxin.newsmonitor.plist`（持续运行）
- 创建 `~/Library/LaunchAgents/com.luoxin.morningbrief.plist`（每天 8:55 触发）
- 日志写到 `~/shared/trading/logs/news_monitor.log` 和 `morning_brief.log`

启动并验证 30 秒内能正常拉到第一批新闻。

### 1.6 测试（30 min）
- dry-run 早报，看输出格式正确
- 实际跑一次发到飞书 webhook（人工确认能收到）
- 跑一段时间确认 morning_brief_pool 在写入

---

## Phase 2：Trade Agent 回测因子有效性（2h，06:00-08:00）

### 2.1 摸清当前因子集合（30 min）

读以下代码，提取所有因子：
- `backtest/screener.py`：Layer 2 选股因子（涨停股评分、反包加分、连续阳线 3/5/7 日、趋势股路径）
- `backtest/strategies/__init__.py`：8 个预配置变体的差异参数
- `backtest/monitor_backtest_v2.py`：方向二盘中监控规则（封板/炸板/止损/止盈/T+1/超时强平）
- `backtest/layered_engine.py`：Layer 1（情绪判断 - 代码规则版本）+ Layer 2（量化筛选）+ Layer 3（AI 综合）

输出：因子清单 markdown 表，每行 = 因子名 + 来源 + 当前是否启用 + 阈值

### 2.2 跑滚动回测（45 min）

用 `shadow_runner` 跑：
- 时间窗：2026-04-13 ~ 2026-05-08（约 18 个交易日，覆盖最新数据）
- 8 个策略变体全部跑
- 输出每个策略的：总收益率 / 胜率 / 平均盈亏比 / 最大回撤 / 交易笔数

```bash
python -m backtest.shadow_runner --start 2026-04-13 --end 2026-05-08 \
    --output ~/shared/backtest/shadow_2026-05-11.json
```

### 2.3 因子贡献度分析（45 min）

A/B 测试方法（每个因子独立切换）：
- baseline: monitor_backtest_v2 默认
- 关闭"反包加分" → 看胜率/收益变化
- 关闭"连续阳线分级" → 看变化
- 关闭"sealed_min_prev_board=2" 门控 → 看变化
- 关闭"max_hold_days=3" → 看变化
- 关闭 trailing stop → 看变化

输出：因子贡献度表（因子 / Δ 收益 / Δ 胜率 / Δ 回撤）

---

## Phase 3：Trade Agent 迭代方案（1h，08:00-09:00）

### 3.1 写 `docs/trade-agent-roadmap-2026-05-11.md`

基于 Phase 2 结论，给出：

**短期（1-2 周）**：
- 调整哪些因子阈值（基于 A/B 测试结果）
- 修哪些已发现的 bug（如 strategy_health 仍跑老引擎）
- 加哪些数据源（如 Layer 1 情绪判断的"涨停跌停净额"信号）

**中期（1 个月）**：
- 增加新因子候选（如 "MACD 金叉前 N 日"、"龙虎榜净买入"、"北向资金流入"、"分时量比"）
- 引入 News Monitor 的事件影响打分（连接 impact 模块）
- 多策略动态切换（按情绪周期切策略变体）

**长期（达成 20%/月）**：
- 引入板块联动信号（同板块分时同步）
- 加入开盘 30 分钟集合竞价分析
- 考虑日内 T+0 套利（当前 T+1 限制）
- 风控：单日最大亏损 < 3% 强制平仓 / 单笔最大亏损 < 1.5%

**风险/约束**：
- 月度 20% 在历史上只有少数月份出现，需要看连续性
- 当前盘中数据覆盖仅 18 个交易日，回测置信度需要往前补
- 实盘摩擦（佣金 0.025% × 2 + 印花税 0.05% 卖出 + 滑点 0.1%）

### 3.2 给一个"先做这 3 件事"的优先级清单
（用户回来后能立刻看到要不要继续做）

---

## Phase 4：早报实战 + 最终交付（30 min，09:00-09:30）

- 9:00 早报应已自动触发（LaunchAgent），人工到飞书确认收到
- 如果 9:00 早报有问题，紧急修复 + 手动重发
- 跑 `git status` 确认所有改动已 commit + push
- 在飞书发一条"迭代完成总结"，含 commit 列表 + 关键产出文档链接 + 待你拍板事项

---

## 风险 + Fallback

1. **9:00 早报失败**：tail logs 看错误；常见问题：yfinance 限流、LLM 超时、webhook 401。fallback：morning_brief --dry 重新跑一次手动发 webhook。
2. **News Monitor 进程死掉**：LaunchAgent 自动重启 + 心跳监控（已有 `_write_heartbeat`）
3. **回测时间不够**：Phase 2 优先跑 1 个完整 shadow_runner 拿到 8 策略的对比，A/B 因子贡献分析可以缩减到核心 3 个因子
4. **commit 冲突**：用户在 happyclaw 工作时不会动 a-stock-trading 仓库；origin/main push 失败时 git pull --rebase
5. **凌晨开发节奏**：每 30 分钟检查一次 plan 进度，如严重落后则砍 Phase 3 的"长期路径"细节，优先保证 Phase 1 + 4 完成

## 决策点（中途遇到时怎么办）

- 紧急/重大新闻定义不明 → 用关键词白名单兜底（核/战争/宣战/熔断/紧急加息），有疑问倾向"实时推 + 早报里再来一次"
- 美股明星股 yfinance 拉不到 → 切到 AKShare 备用
- 回测因子贡献度不显著 → 不强求结论，直接给出"现有因子组合已是较优"的结论 + 建议方向
- 任何 commit 时不确定是否该入 → 默认 commit + push（用户已授权）
