"""回测引擎核心 — 收益率驱动的 D→D+1 回测

通过依赖注入接收 DataProvider / AgentRunner 实现，
引擎本身零耦合于具体数据源和 Agent 框架。

验证方式：不使用 LLM 打分，而是基于 Agent 推荐标的的实际涨跌幅计算收益。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .protocols import DataProvider, AgentRunner, MarketData
from .report import generate_summary
from ..experience.store import ExperienceStore, Experience
from ..experience.classifier import ScenarioClassifier
from ..experience.prompt_engine import PromptEngine


@dataclass
class Recommendation:
    """单只推荐标的的实际表现"""
    stock: str
    action: str              # 买入/卖出/观望
    buy_condition: str       # 买入条件
    position: str            # 仓位建议
    # D+1 实际表现
    next_open: float = 0.0
    next_close: float = 0.0
    next_high: float = 0.0
    next_low: float = 0.0
    next_pct_chg: float = 0.0
    is_limit_up: bool = False
    is_limit_down: bool = False
    pnl_pct: float = 0.0     # 按开盘买入计算的收益率


@dataclass
class BacktestResult:
    """单日回测结果"""
    day_d: str
    day_d1: str
    status: str = "pending"           # completed / analysis_failed / d1_data_failed
    scenario: dict = field(default_factory=dict)
    injected_lessons: int = 0
    # 收益率验证（替代评分）
    recommendations: list = field(default_factory=list)   # list[Recommendation]
    avg_pnl_pct: float = 0.0
    hit_rate: float = 0.0            # 推荐标的中上涨的比例
    key_lessons: list = field(default_factory=list)
    error: str = ""


class BacktestPortfolioTracker:
    """回测持仓追踪器 — 跟踪 Agent 推荐标的的模拟持仓状态。

    规则：
    - Agent 在 Day D 推荐买入的标的，假定在 Day D+1 开盘价买入
    - Agent 每天对持仓做出持有/卖出决策（position_actions）
    - 卖出在次日开盘价执行；持有则保留到下一轮决策
    - 超过 MAX_HOLD_DAYS 天未被 Agent 主动卖出的持仓强制卖出（防护性兜底）
    """

    MAX_HOLD_DAYS = 30  # 最大持仓天数兜底

    def __init__(self, initial_capital: float = 1_000_000.0):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: list[dict] = []  # [{name, code, buy_date, buy_price, shares, cost, sell_condition, buy_reason}]
        self.closed_trades: list[dict] = []  # 已平仓交易记录

    @property
    def total_value(self) -> float:
        return self.cash + sum(p["cost"] for p in self.positions)

    def apply_position_actions(
        self, actions: list[dict], sell_date: str, data_dir: str,
    ) -> list[dict]:
        """根据 Agent 的 position_actions 执行卖出/持有决策。

        Args:
            actions: Agent 输出的 position_actions 列表
                [{"name": "xxx", "action": "卖出"/"持有", "sell_condition": "..."}]
            sell_date: 卖出执行日期（用该日开盘价）
            data_dir: 数据目录

        Returns:
            卖出记录列表
        """
        from ..adapter import CSVStockDataProvider
        loader = CSVStockDataProvider()

        # 构建 Agent 决策映射
        action_map = {}
        for a in actions:
            name = a.get("name", "")
            if name:
                action_map[name] = a

        sold_records = []
        remaining = []

        for p in self.positions:
            # T+0 当天买入的不能卖（T+1 约束）
            if p["buy_date"] >= sell_date:
                remaining.append(p)
                continue

            agent_action = action_map.get(p["name"], {})
            action = agent_action.get("action", "")

            # 计算持仓天数（用于兜底）
            hold_days = self._count_trading_days(p["buy_date"], sell_date, data_dir)

            should_sell = False
            sell_reason = ""

            if action == "卖出":
                should_sell = True
                sell_reason = agent_action.get("reason", "Agent 决策卖出")
            elif action == "持有":
                # 更新持仓的卖出条件
                p["sell_condition"] = agent_action.get("sell_condition", "")
                if hold_days >= self.MAX_HOLD_DAYS:
                    should_sell = True
                    sell_reason = "持仓超过{}天强制卖出".format(self.MAX_HOLD_DAYS)
            else:
                # Agent 没有对此持仓做出决策（可能是遗漏）
                if hold_days >= self.MAX_HOLD_DAYS:
                    should_sell = True
                    sell_reason = "Agent 未决策 + 持仓超过{}天".format(self.MAX_HOLD_DAYS)
                # 否则默认持有

            if should_sell:
                daily = loader.load_stock_daily(data_dir, sell_date, p["name"])
                if daily and daily.get("open", 0) > 0:
                    sell_price = daily["open"]
                    proceeds = p["shares"] * sell_price
                    pnl_pct = (sell_price - p["buy_price"]) / p["buy_price"] * 100
                    pnl_amount = proceeds - p["cost"]
                    self.cash += proceeds
                    trade = {
                        "name": p["name"],
                        "code": p.get("code", ""),
                        "buy_date": p["buy_date"],
                        "buy_price": round(p["buy_price"], 2),
                        "shares": p["shares"],
                        "cost": round(p["cost"], 2),
                        "sell_date": sell_date,
                        "sell_price": round(sell_price, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "pnl_amount": round(pnl_amount, 2),
                        "hold_days": hold_days,
                        "reason": sell_reason,
                        "buy_reason": p.get("buy_reason", ""),
                    }
                    sold_records.append(trade)
                    self.closed_trades.append(trade)
                else:
                    self.cash += p["cost"]
                    trade = {
                        "name": p["name"],
                        "code": p.get("code", ""),
                        "buy_date": p["buy_date"],
                        "buy_price": round(p["buy_price"], 2),
                        "shares": p["shares"],
                        "cost": round(p["cost"], 2),
                        "sell_date": sell_date,
                        "sell_price": p["buy_price"],
                        "pnl_pct": 0,
                        "pnl_amount": 0,
                        "hold_days": hold_days,
                        "reason": sell_reason + "（无行情，按成本价）",
                        "buy_reason": p.get("buy_reason", ""),
                    }
                    sold_records.append(trade)
                    self.closed_trades.append(trade)
            else:
                remaining.append(p)

        self.positions = remaining
        return sold_records

    def _count_trading_days(self, from_date: str, to_date: str, data_dir: str) -> int:
        """简化的交易日计数（基于 daily 目录）。"""
        daily_root = os.path.join(data_dir, "daily")
        if not os.path.isdir(daily_root):
            return 0
        count = 0
        for d in os.listdir(daily_root):
            if from_date < d <= to_date and os.path.isdir(os.path.join(daily_root, d)):
                count += 1
        return count

    def buy_from_recommendations(
        self, recs: list, buy_date: str, data_dir: str, buy_reasons: dict = None,
    ):
        """根据推荐标的在 buy_date 开盘价买入。

        Args:
            buy_reasons: {股票名: 参与逻辑} 从报告提取的买入原因
        """
        from ..adapter import CSVStockDataProvider
        loader = CSVStockDataProvider()

        buy_recs = [r for r in recs if r.action == "买入" and r.next_open > 0]
        if not buy_recs:
            return

        position_pct = 0.3  # 固定 3 成
        for rec in buy_recs:
            # 跳过已持仓标的
            if any(p["name"] == rec.stock for p in self.positions):
                continue
            target_amount = self.total_value * position_pct
            available = self.cash
            amount = min(target_amount, available)
            if amount < 1000:
                continue

            buy_price = rec.next_open
            shares = int(amount / (buy_price * 100)) * 100
            if shares <= 0:
                continue

            cost = shares * buy_price
            self.cash -= cost
            # 买入原因：优先用从报告提取的参与逻辑，其次用 Recommendation 的字段
            reason = ""
            if buy_reasons and rec.stock in buy_reasons:
                reason = buy_reasons[rec.stock]
            if not reason:
                reason = getattr(rec, 'buy_condition', '') or getattr(rec, 'reason', '')
            self.positions.append({
                "name": rec.stock,
                "code": "",
                "buy_date": buy_date,
                "buy_price": buy_price,
                "shares": shares,
                "cost": cost,
                "sell_condition": "",
                "buy_reason": reason,
            })

    def get_state(self, current_date: str, data_dir: str) -> dict:
        """生成当前持仓状态快照，供传递给 Agent。"""
        from ..adapter import CSVStockDataProvider
        loader = CSVStockDataProvider()

        positions_info = []
        for p in self.positions:
            # 尝试获取当日收盘价作为"当前价"
            daily = loader.load_stock_daily(data_dir, current_date, p["name"])
            current_price = daily["close"] if daily and daily.get("close", 0) > 0 else p["buy_price"]
            pnl_pct = (current_price - p["buy_price"]) / p["buy_price"] * 100

            positions_info.append({
                "name": p["name"],
                "code": p.get("code", ""),
                "shares": p["shares"],
                "buy_price": p["buy_price"],
                "current_price": round(current_price, 2),
                "pnl_pct": round(pnl_pct, 2),
                "buy_date": p["buy_date"],
            })

        total = self.total_value
        return {
            "total_value": round(total, 2),
            "cash": round(self.cash, 2),
            "cash_pct": round(self.cash / total * 100, 1) if total > 0 else 100,
            "positions": positions_info,
        }


class BacktestEngine:
    """收益率驱动的回测引擎

    使用方式:
        engine = BacktestEngine(
            data_provider=ReviewDataProvider(),
            agent_runner=ChatAgentRunner(),
        )
        summary = engine.run(data_dir="...", dates=[...])
    """

    def __init__(
        self,
        data_provider: DataProvider,
        agent_runner: AgentRunner,
    ):
        self.data_provider = data_provider
        self.agent_runner = agent_runner

    def run(
        self,
        data_dir: str,
        dates: list[str],
        output_dir: Optional[str] = None,
        on_progress=None,
        workers: int = 1,
    ) -> dict:
        """运行收益率驱动的回测

        Args:
            data_dir: 数据根目录
            dates: 已排序的交易日列表
            output_dir: 输出目录
            on_progress: 进度回调 fn(idx, total, date, stage)
            workers: 并行 worker 数（加速 LLM 调用）
        """
        if not output_dir:
            output_dir = os.path.join(data_dir, "backtest_v6")
        os.makedirs(output_dir, exist_ok=True)

        # 初始化经验系统
        exp_store = ExperienceStore(data_dir)
        prompt_engine = PromptEngine(data_dir)
        classifier = ScenarioClassifier()

        results: list[BacktestResult] = []
        pairs = [(dates[i], dates[i + 1]) for i in range(len(dates) - 1)]

        # ── 并行模式：先并行跑所有 agent 调用，再顺序验证 ──
        if workers > 1:
            return self._run_parallel(
                data_dir, output_dir, pairs, results,
                exp_store, prompt_engine, classifier, workers,
            )

        prev_report = ""
        portfolio = BacktestPortfolioTracker()
        all_experiences: list[Experience] = []  # 收集所有经验，不自动保存

        for idx, (day_d, day_d1) in enumerate(pairs):
            if on_progress:
                on_progress(idx + 1, len(pairs), day_d, "analyzing")

            print("=" * 60)
            print("回测 {}/{}: {} → {} [v6 收益率驱动]".format(
                idx + 1, len(pairs), day_d, day_d1))
            print("=" * 60)

            result = BacktestResult(day_d=day_d, day_d1=day_d1)

            # ── Step 0: 加载当日数据，提取场景标签 ──
            try:
                market_data = self.data_provider.load_market_data(data_dir, day_d)
                scenario = classifier.classify(
                    limit_up_count=market_data.limit_up_count,
                    limit_down_count=market_data.limit_down_count,
                    blown_rate=market_data.blown_rate,
                    max_board=market_data.max_board,
                    sector_top1_count=market_data.sector_top1_count,
                    sector_top1_total=market_data.sector_top1_total,
                    prev_limit_up_count=market_data.prev_limit_up_count,
                    sentiment_phase=market_data.sentiment_phase,
                    volume_change_pct=market_data.volume_change_pct,
                )
                print("  [场景] {}".format(scenario.to_description()))
                result.scenario = scenario.to_dict()
            except Exception as e:
                print("  [场景识别失败] {}".format(e))
                scenario = classifier.classify()
                market_data = MarketData(date=day_d)

            # ── Step 1: 构建场景感知的 Prompt 注入 ──
            market_dict = {
                "limit_up_count": market_data.limit_up_count,
                "limit_down_count": market_data.limit_down_count,
                "blown_rate": market_data.blown_rate,
                "max_board": market_data.max_board,
                "sector_top1_count": market_data.sector_top1_count,
                "prev_limit_up_count": market_data.prev_limit_up_count,
                "sentiment_phase": market_data.sentiment_phase,
                "volume_change_pct": market_data.volume_change_pct,
            }

            injection = prompt_engine.build_injection(
                market_dict,
                agents=["sentiment_analyst", "sector_analyst", "judge"],
                max_lessons_per_agent=3,
            )
            injected_ids = []

            run_config: dict = {"backtest_mode": True}
            if injection:
                overrides = {}
                for agent, inject_text in injection.items():
                    overrides[agent] = inject_text
                run_config["prompt_overrides"] = overrides

                relevant = exp_store.search(
                    scenario=scenario, min_confidence=0.3, limit=10,
                )
                injected_ids = [e.id for e in relevant][:9]

                print("  [教训注入] {} 条，涉及 {}".format(
                    sum(len(v) for v in injection.values()),
                    ", ".join(injection.keys()),
                ))
            else:
                print("  [教训注入] 无匹配教训")

            result.injected_lessons = len(injected_ids)

            # ── Step 1.5: 根据前一天报告的 position_actions 执行卖出 ──
            if portfolio.positions and prev_report:
                position_actions = self._extract_position_actions(prev_report)
                sold = portfolio.apply_position_actions(position_actions, day_d, data_dir)
                if sold:
                    for s in sold:
                        print("  [卖出] {} @ {:.2f} ({:+.2f}%) — {}".format(
                            s["name"], s["sell_price"], s["pnl_pct"], s["reason"]))
            elif portfolio.positions and not prev_report:
                # 首日无前日报告，不卖出（保持持仓到下一轮 Agent 决策）
                pass

            portfolio_state = portfolio.get_state(day_d, data_dir)
            if portfolio.positions:
                print("  [持仓] {} 只: {}  现金 {:.0f} ({:.0f}%)".format(
                    len(portfolio.positions),
                    ", ".join(p["name"] for p in portfolio.positions),
                    portfolio_state["cash"],
                    portfolio_state["cash_pct"]))
            else:
                print("  [持仓] 空仓, 总资产 {:.0f}".format(portfolio_state["total_value"]))

            # ── Step 2: 用 Day D 跑 Agent（带教训注入 + 持仓状态）──
            if on_progress:
                on_progress(idx + 1, len(pairs), day_d, "analyzing")

            report_path = os.path.join(output_dir, "{}_report.md".format(day_d))
            if os.path.exists(report_path):
                with open(report_path, "r", encoding="utf-8") as f:
                    report = f.read()
                print("  [跳过] {} 报告已存在".format(day_d))
            else:
                try:
                    report = self.agent_runner.run(
                        data_dir=data_dir,
                        date=day_d,
                        config=run_config,
                        prev_report=prev_report,
                        portfolio_state=portfolio_state,
                    )
                    with open(report_path, "w", encoding="utf-8") as f:
                        f.write(report)
                    print("  [完成] {} 报告已生成".format(day_d))
                except Exception as e:
                    print("  [失败] {} 分析失败: {}".format(day_d, e))
                    result.status = "analysis_failed"
                    result.error = str(e)
                    results.append(result)
                    continue

            prev_report = report

            # ── Step 3: 加载 Day D+1 实际数据 ──
            try:
                next_date, d1_summary = self.data_provider.load_next_day_summary(
                    data_dir, day_d1, report,
                )
            except Exception as e:
                print("  [失败] {} 数据加载失败: {}".format(day_d1, e))
                result.status = "d1_data_failed"
                result.error = str(e)
                results.append(result)
                continue

            # ── Step 4: 收益率验证（数据驱动，无 LLM）──
            if on_progress:
                on_progress(idx + 1, len(pairs), day_d, "verifying")

            try:
                recs = self._verify_recommendations(data_dir, day_d1, report)
            except Exception as e:
                print(f"  [验证错误] {e}")
                recs = []
            result.recommendations = recs

            if recs:
                # P1: 只统计有实际行情数据的推荐（排除无行情的垃圾解析）
                valid_recs = [r for r in recs if r.action == "买入" and (r.next_pct_chg != 0 or r.pnl_pct != 0)]
                if valid_recs:
                    pnl_list = [r.pnl_pct for r in valid_recs]
                    result.avg_pnl_pct = round(sum(pnl_list) / len(pnl_list), 2)
                    result.hit_rate = round(
                        sum(1 for p in pnl_list if p > 0) / len(pnl_list) * 100, 1
                    )
                    print("  [验证] {} 只有效标的（过滤 {} 条无数据）, 平均收益 {:+.2f}%, 命中率 {:.0f}%".format(
                        len(valid_recs), len(recs) - len(valid_recs), result.avg_pnl_pct, result.hit_rate))
                else:
                    print("  [验证] {} 只推荐标的, 但无有效行情数据".format(len(recs)))
            else:
                print("  [验证] 未发现推荐标的")

            # ── Step 4.5: 模拟 Day D+1 买入（更新持仓追踪）──
            if recs:
                # 从报告提取买入原因
                buy_plans = self._extract_buy_plans(report)
                plan_reasons = {name: p.get("reason", "") for name, p in buy_plans.items()}
                portfolio.buy_from_recommendations(recs, day_d1, data_dir, buy_reasons=plan_reasons)
                bought = [p["name"] for p in portfolio.positions if p["buy_date"] == day_d1]
                if bought:
                    print("  [模拟买入] {}".format(", ".join(bought)))

            # 保存验证结果
            audit = getattr(self.agent_runner, '_last_audit', None)
            verify_path = os.path.join(output_dir, "{}_verify.json".format(day_d))
            verify_data = {
                "day_d": day_d,
                "day_d1": day_d1,
                "recommendations": [
                    {
                        "stock": r.stock,
                        "action": r.action,
                        "buy_condition": r.buy_condition,
                        "position": r.position,
                        "next_pct_chg": r.next_pct_chg,
                        "pnl_pct": r.pnl_pct,
                        "is_limit_up": r.is_limit_up,
                        "is_limit_down": r.is_limit_down,
                    }
                    for r in recs
                ],
                "avg_pnl_pct": result.avg_pnl_pct,
                "hit_rate": result.hit_rate,
                "data_leak_audit": {
                    "clean": audit["clean"] if audit else True,
                    "blocked_count": audit["blocked_count"] if audit else 0,
                    "blocked_details": audit["blocked_details"] if audit else [],
                },
            }
            with open(verify_path, "w", encoding="utf-8") as f:
                json.dump(verify_data, f, ensure_ascii=False, indent=2)

            # ── Step 5: 基于实盘结果提取经验（不自动保存）──
            if on_progress:
                on_progress(idx + 1, len(pairs), day_d, "extracting_experience")

            if recs:
                has_signal = (
                    any(r.action == "买入" and r.pnl_pct < -2 for r in recs)
                    or any(r.action == "买入" and r.pnl_pct > 3 for r in recs)
                )
                if has_signal:
                    try:
                        day_experiences = self._extract_experience_from_outcome(
                            day_d=day_d, day_d1=day_d1,
                            report=report, recs=recs,
                            scenario=scenario,
                        )
                        if day_experiences:
                            all_experiences.extend(day_experiences)
                            print("  [经验提取] {} 条（累计 {} 条）".format(
                                len(day_experiences), len(all_experiences)))
                    except Exception as e:
                        print("  [经验提取失败] {}".format(e))

            result.status = "completed"
            results.append(result)
            time.sleep(1)

        # ── 生成汇总报告 ──
        summary = generate_summary(results, output_dir, exp_store)

        # ── 生成交割单 ──
        from .report import generate_settlement_report
        generate_settlement_report(
            tracker=portfolio,
            output_dir=output_dir,
            initial_capital=portfolio.initial_capital,
        )

        # ── 生成经验总结审阅文件 ──
        if all_experiences:
            review_path = self._generate_experience_review_file(all_experiences, output_dir)
            if review_path:
                print("\n经验总结已保存到 {}（共 {} 条，请审阅后决定是否沉淀）".format(
                    review_path, len(all_experiences)))
            # 保存 JSON 格式的经验列表（方便后续批量导入）
            exp_json_path = os.path.join(output_dir, "经验总结.json")
            with open(exp_json_path, "w", encoding="utf-8") as f:
                json.dump([{
                    "id": e.id,
                    "date": e.date,
                    "scenario": e.scenario,
                    "prediction": e.prediction,
                    "reality": e.reality,
                    "error_type": e.error_type,
                    "lesson": e.lesson,
                    "correction_rule": e.correction_rule,
                } for e in all_experiences], f, ensure_ascii=False, indent=2)

        return summary

    def _run_parallel(
        self,
        data_dir: str,
        output_dir: str,
        pairs: list[tuple[str, str]],
        results: list,
        exp_store,
        prompt_engine,
        classifier,
        workers: int,
    ) -> dict:
        """并行模式：先并行生成所有报告，再顺序验证+经验提取"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        print("\n[并行模式] {} workers, {} 天待处理".format(workers, len(pairs)))
        all_experiences: list[Experience] = []  # 收集所有经验，不自动保存

        # Phase 1: 并行生成报告（LLM 调用）
        def generate_report(idx_pair):
            idx, (day_d, day_d1) = idx_pair
            report_path = os.path.join(output_dir, "{}_report.md".format(day_d))
            if os.path.exists(report_path):
                with open(report_path, "r", encoding="utf-8") as f:
                    report = f.read()
                return idx, day_d, day_d1, report, True  # skipped
            try:
                report = self.agent_runner.run(
                    data_dir=data_dir,
                    date=day_d,
                    config={"backtest_mode": True},
                    prev_report="",
                )
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(report)
                return idx, day_d, day_d1, report, False
            except Exception as e:
                print("  [失败] {}: {}".format(day_d, e))
                return idx, day_d, day_d1, "", False

        print("[Phase 1] 并行生成报告...")
        reports = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(generate_report, (i, p)): i for i, p in enumerate(pairs)}
            for future in as_completed(futures):
                idx, day_d, day_d1, report, skipped = future.result()
                reports[idx] = (day_d, day_d1, report, skipped)
                status = "跳过" if skipped else ("完成" if report else "失败")
                print("  [{}/{}] {} {}".format(
                    len(reports), len(pairs), day_d, status))

        # Phase 2: 顺序验证 + 经验提取
        print("\n[Phase 2] 顺序验证 + 经验提取...")
        for idx in sorted(reports.keys()):
            day_d, day_d1, report, skipped = reports[idx]
            if not report:
                continue

            result = BacktestResult(day_d=day_d, day_d1=day_d1)

            # 场景分类
            try:
                market_data = self.data_provider.load_market_data(data_dir, day_d)
                scenario = classifier.classify(
                    limit_up_count=market_data.limit_up_count,
                    limit_down_count=market_data.limit_down_count,
                    blown_rate=market_data.blown_rate,
                    max_board=market_data.max_board,
                    sector_top1_count=market_data.sector_top1_count,
                    sector_top1_total=market_data.sector_top1_total,
                    prev_limit_up_count=market_data.prev_limit_up_count,
                    sentiment_phase=market_data.sentiment_phase,
                    volume_change_pct=market_data.volume_change_pct,
                )
                result.scenario = scenario.to_dict()
            except Exception:
                pass

            # 验证推荐
            try:
                recs = self._verify_recommendations(data_dir, day_d1, report)
            except Exception as e:
                print("  [验证错误] {}: {}".format(day_d, e))
                recs = []
            result.recommendations = recs

            if recs:
                valid_recs = [r for r in recs if r.action == "买入" and (r.next_pct_chg != 0 or r.pnl_pct != 0)]
                if valid_recs:
                    pnl_list = [r.pnl_pct for r in valid_recs]
                    result.avg_pnl_pct = round(sum(pnl_list) / len(pnl_list), 2)
                    result.hit_rate = round(
                        sum(1 for p in pnl_list if p > 0) / len(pnl_list) * 100, 1)
                    print("  [验证] {} 只有效标的, 命中率 {:.0f}%, 均收益 {:+.2f}%".format(
                        len(valid_recs), result.hit_rate, result.avg_pnl_pct))

            # 保存验证结果
            verify_path = os.path.join(output_dir, "{}_verify.json".format(day_d))
            verify_data = {
                "day_d": day_d, "day_d1": day_d1,
                "recommendations": [
                    {"stock": r.stock, "action": r.action, "buy_condition": r.buy_condition,
                     "position": r.position, "next_pct_chg": r.next_pct_chg, "pnl_pct": r.pnl_pct,
                     "is_limit_up": r.is_limit_up, "is_limit_down": r.is_limit_down}
                    for r in recs
                ],
                "avg_pnl_pct": result.avg_pnl_pct, "hit_rate": result.hit_rate,
            }
            with open(verify_path, "w", encoding="utf-8") as f:
                json.dump(verify_data, f, ensure_ascii=False, indent=2)

            # 经验提取（不自动保存）
            if recs:
                has_signal = (
                    any(r.action == "买入" and r.pnl_pct < -2 for r in recs)
                    or any(r.action == "买入" and r.pnl_pct > 3 for r in recs)
                )
                if has_signal:
                    try:
                        day_experiences = self._extract_experience_from_outcome(
                            day_d=day_d, day_d1=day_d1, report=report,
                            recs=recs, scenario=scenario,
                        )
                        if day_experiences:
                            all_experiences.extend(day_experiences)
                    except Exception as e:
                        print("  [经验提取失败] {}".format(e))

            result.status = "completed"
            results.append(result)

        # 生成汇总
        summary = generate_summary(results, output_dir, exp_store)

        # 生成经验总结审阅文件
        if all_experiences:
            review_path = self._generate_experience_review_file(all_experiences, output_dir)
            if review_path:
                print("\n经验总结已保存到 {}（共 {} 条，请审阅后决定是否沉淀）".format(
                    review_path, len(all_experiences)))
            exp_json_path = os.path.join(output_dir, "经验总结.json")
            with open(exp_json_path, "w", encoding="utf-8") as f:
                json.dump([{
                    "id": e.id,
                    "date": e.date,
                    "scenario": e.scenario,
                    "prediction": e.prediction,
                    "reality": e.reality,
                    "error_type": e.error_type,
                    "lesson": e.lesson,
                    "correction_rule": e.correction_rule,
                } for e in all_experiences], f, ensure_ascii=False, indent=2)

        return summary

    def _verify_recommendations(
        self,
        data_dir: str,
        day_d1: str,
        report: str,
    ) -> list[Recommendation]:
        """从报告中提取推荐标的，验证 D+1 实际表现（纯数据驱动）"""
        from ..adapter import CSVStockDataProvider

        stock_provider = CSVStockDataProvider()
        recs = []

        # 从报告 JSON 前置块提取 focus_stocks
        stocks_info = self._extract_focus_stocks(report)

        # 从"买入计划"章节提取更详细的操作信息
        buy_plans = self._extract_buy_plans(report)

        for info in stocks_info:
            stock_name = info.get("name", "")
            if not stock_name or len(stock_name) < 2:
                continue

            plan = buy_plans.get(stock_name, {})

            # 加载 D+1 实际行情
            daily = stock_provider.load_stock_daily(data_dir, day_d1, stock_name)
            if not daily or daily.get("open", 0) <= 0:
                recs.append(Recommendation(
                    stock=stock_name,
                    action=plan.get("action", "买入"),
                    buy_condition=plan.get("condition", ""),
                    position=plan.get("position", ""),
                ))
                continue

            open_price = daily["open"]
            close_price = daily.get("close", open_price)
            pct_chg = daily.get("pct_chg", 0)
            is_up = daily.get("is_limit_up", False)
            is_down = daily.get("is_limit_down", False)

            # 按开盘买入计算收益率（回测默认 D+1 开盘执行）
            pnl_pct = round((close_price - open_price) / open_price * 100, 2) if open_price > 0 else 0

            recs.append(Recommendation(
                stock=stock_name,
                action=plan.get("action", "买入"),
                buy_condition=plan.get("condition", ""),
                position=plan.get("position", ""),
                next_open=open_price,
                next_close=close_price,
                next_high=daily.get("high", close_price),
                next_low=daily.get("low", close_price),
                next_pct_chg=round(pct_chg, 2) if pct_chg else 0,
                is_limit_up=is_up,
                is_limit_down=is_down,
                pnl_pct=pnl_pct,
            ))

        return recs

    @staticmethod
    def _extract_position_actions(report: str) -> list[dict]:
        """从报告 JSON 中提取 position_actions（持仓决策）。

        Returns:
            [{"name": "xxx", "action": "卖出"/"持有", "reason": "...", "sell_condition": "..."}]
        """
        # 优先从 ```json``` 块解析
        json_match = re.search(r'```json\s*\n(.*?)\n```', report, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                actions = data.get("position_actions", [])
                if isinstance(actions, list):
                    return [a for a in actions if isinstance(a, dict) and a.get("name")]
            except json.JSONDecodeError:
                pass

        # Fallback: 裸 JSON
        json_match = re.search(r'"position_actions"\s*:\s*\[(.*?)\]', report, re.DOTALL)
        if json_match:
            try:
                actions = json.loads("[" + json_match.group(1) + "]")
                return [a for a in actions if isinstance(a, dict) and a.get("name")]
            except json.JSONDecodeError:
                pass

        return []

    def _extract_focus_stocks(self, report: str) -> list[dict]:
        """从报告中提取推荐标的（优先 JSON 结构化输出，fallback 到 Markdown 解析）"""
        stocks_found: list[dict] = []
        seen_names: set[str] = set()

        # ── Priority: JSON structured output ──
        # Method 1: JSON ```json``` block with focus_stocks（闭合的代码块）
        json_match = re.search(r'```json\s*\n(.*?)\n```', report, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                stocks = data.get("focus_stocks", [])
                if stocks:
                    valid = [s for s in stocks if isinstance(s, dict) and s.get("name") and len(s["name"]) >= 2]
                    if valid:
                        return valid
            except json.JSONDecodeError:
                pass

        # Method 1.5: 不闭合的 JSON 代码块（LLM 输出被截断，如 max_tokens 不够）
        # 从 ```json 开始提取，尝试逐字符找到有效 JSON
        json_start = re.search(r'```json\s*\n', report)
        if json_start and not json_match:
            json_text = report[json_start.end():]
            valid = self._try_parse_truncated_json(json_text)
            if valid:
                return valid

        # Method 2: Bare JSON "focus_stocks": [...]
        json_match = re.search(r'"focus_stocks"\s*:\s*\[(.*?)\]', report)
        if json_match:
            try:
                stocks = json.loads("[" + json_match.group(1) + "]")
                valid = [s for s in stocks if isinstance(s, dict) and s.get("name") and len(s["name"]) >= 2]
                if valid:
                    return valid
            except json.JSONDecodeError:
                pass

        # ── Method 2.5: Markdown TABLE 格式解析 ──
        # 匹配 "| 股票名称 | 代码 | ..." 表格行
        table_rows = re.findall(
            r'\|\s*([\u4e00-\u9fa5A-Za-z]{2,6}[A-Za-z]?)\s*\|\s*(\d{6})\s*\|',
            report,
        )
        for name, code in table_rows:
            name = name.strip()
            if name in ('股票名称', '----------', '------') or len(name) < 2:
                continue
            if name not in seen_names:
                seen_names.add(name)
                stocks_found.append({"name": name, "code": code})
        if stocks_found:
            return stocks_found

        # ── Fallback: Markdown 正则解析（仅当无 JSON 时使用）──
        # Method 3: Markdown 操盘计划 / 买入标的 section
        section_patterns = [
            r'(?:买入标的|次日操盘计划|操盘计划|买入标的).*?\n(.*?)(?=\n####|\n---|\Z)',
            r'(?:核心标的|补涨标的).*?\n(.*?)(?=\n####|\n---|\n- \*\*核心|\Z)',
        ]
        for pat in section_patterns:
            section_match = re.search(pat, report, re.DOTALL)
            if not section_match:
                continue
            section = section_match.group(1)
            stock_pattern = re.findall(
                r'\*{0,2}([^\n*（(]+?)\s*[（(]\s*(\d{6})\s*[，,）)]',
                section,
            )
            for name, code in stock_pattern:
                name = name.strip().lstrip("*").strip()
                name = re.sub(r'^[-、\s]+', '', name)
                name = re.sub(r'^[\u4e00-\u9fa5]{2,4}[：:]\s*', '', name)
                name = name.strip()
                if len(name) >= 2 and name not in seen_names:
                    seen_names.add(name)
                    stocks_found.append({"name": name, "code": code})

        # Method 4: Fallback — 全文搜索 股票名（6位代码） 模式
        if not stocks_found:
            buy_sections = re.findall(
                r'(?:买入|标的|操盘|推荐|关注)(.*?)(?=\n\n|\Z)',
                report, re.DOTALL,
            )
            text = "\n".join(buy_sections) if buy_sections else report
            stock_pattern = re.findall(
                r'([^\n*（(]{2,10}?)\s*[（(]\s*(\d{6})\s*[）)]',
                text,
            )
            for name, code in stock_pattern:
                name = name.strip().lstrip("*").strip()
                # 清除前缀噪声："- 板块："、"、"、"-"等
                name = re.sub(r'^[-、\s]+', '', name)
                name = re.sub(r'^[\u4e00-\u9fa5]{2,4}[：:]\s*', '', name)
                name = name.strip()
                if len(name) >= 2 and name not in seen_names:
                    seen_names.add(name)
                    stocks_found.append({"name": name, "code": code})

        return stocks_found

    def _try_parse_truncated_json(self, json_text: str) -> list[dict]:
        """尝试从被截断的 JSON 文本中提取 focus_stocks。

        策略：从文本中逐个提取完整的 {"name": ..., "code": ...} 对象，
        不依赖整体 JSON 合法性。
        """
        # 尝试找到 focus_stocks 数组中的每个完整对象
        # 匹配 {"name": "股票名", "code": "代码", ...} 格式
        stock_objects = re.findall(
            r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*"code"\s*:\s*"(\d{6})"',
            json_text,
        )
        if not stock_objects:
            # 尝试宽松匹配：name 在前或 code 在前
            stock_objects = re.findall(
                r'\{[^{}]*?"name"\s*:\s*"([^"]+)"[^{}]*?"code"\s*:\s*"(\d{6})"[^{}]*?\}',
                json_text,
            )

        valid = []
        for name, code in stock_objects:
            name = name.strip()
            if len(name) >= 2 and re.match(r'^[\u4e00-\u9fa5A-Za-z]{2,8}$', name):
                valid.append({"name": name, "code": code})

        return valid

    def _extract_buy_plans(self, report: str) -> dict[str, dict]:
        """从"买入计划"章节提取各标的的详细操作信息"""
        plans = {}
        # 匹配 "买入计划" 后的各标的段落
        # 典型格式：
        # **标的**：兰石重装
        # **买入条件**：竞价高开3%以上
        # **仓位**：3成
        buy_section = re.search(
            r'###?\s*买入计划(.*?)(?=###?\s*(?:卖出|空仓|持仓)|$)',
            report, re.DOTALL,
        )
        if not buy_section:
            return plans

        section_text = buy_section.group(1)
        # 按标的分段（**标的**：xxx 或 | 标的 | ...）
        stock_blocks = re.split(r'(?=\*\*标的\*\*[：:]|^\|\s*.*?\s*\|)', section_text, flags=re.MULTILINE)

        for block in stock_blocks:
            name_match = re.search(r'\*\*标的\*\*[：:]\s*(.+?)(?:\n|$)', block)
            if not name_match:
                continue
            stock_name = name_match.group(1).strip()
            if len(stock_name) < 2:
                continue

            condition = ""
            cond_match = re.search(r'\*\*买入条件\*\*[：:]\s*(.+?)(?:\n|$)', block)
            if cond_match:
                condition = cond_match.group(1).strip()

            position = ""
            pos_match = re.search(r'\*\*仓位\*\*[：:]\s*(.+?)(?:\n|$)', block)
            if pos_match:
                position = pos_match.group(1).strip()

            # 提取参与逻辑（Markdown 表格行）
            logic = ""
            logic_match = re.search(r'\|\s*参与逻辑\s*\|\s*(.+?)\s*\|', block)
            if logic_match:
                logic = logic_match.group(1).strip()

            plans[stock_name] = {
                "action": "买入",
                "condition": condition,
                "position": position,
                "reason": logic,
            }

        return plans

    def _extract_experience_from_outcome(
        self,
        day_d: str,
        day_d1: str,
        report: str,
        recs: list[Recommendation],
        scenario,
    ) -> list[Experience]:
        """从实际交易结果中提取结构化经验（含成功和失败）

        Returns:
            经验列表（可能包含多条，分别对应不同标的的表现）
        """
        experiences = []
        phase = scenario.sentiment_phase if hasattr(scenario, "sentiment_phase") else ""
        phase_desc = scenario.to_description() if hasattr(scenario, "to_description") else str(scenario)

        # ── 失败经验（亏损 > 2%）──
        losses = [r for r in recs if r.action == "买入" and r.pnl_pct < -2]
        if losses:
            for r in losses:
                error_type, correction = self._infer_error_and_correction(
                    r, phase, phase_desc,
                )
                detail = "{}: D+1实际{:+.2f}%{}".format(
                    r.stock, r.next_pct_chg,
                    "（涨停）" if r.is_limit_up else "（跌停）" if r.is_limit_down else "",
                )
                lesson = "在[{}]场景下推荐{}，次日{:+.2f}%。{}".format(
                    phase_desc, r.stock, r.next_pct_chg, correction,
                )
                experiences.append(Experience(
                    date=day_d,
                    scenario=scenario.to_dict(),
                    prediction="推荐买入: {}".format(r.stock),
                    reality=detail,
                    scores={},
                    error_type=error_type,
                    lesson=lesson,
                    correction_rule=correction,
                ))

        # ── 成功经验（盈利 > 3%）──
        wins = [r for r in recs if r.action == "买入" and r.pnl_pct > 3]
        for r in wins:
            detail = "{}: D+1实际{:+.2f}%{}".format(
                r.stock, r.next_pct_chg,
                "（涨停）" if r.is_limit_up else "",
            )
            # 根据场景判断成功原因
            if phase in ("冰点", "退潮") and r.next_pct_chg > 5:
                success_reason = "逆势大涨，可能在冰点/退潮期选中了辨识度龙头的超跌反弹"
                correction = "冰点/退潮期仍有结构性机会，聚焦辨识度龙头超跌反弹"
            elif phase == "高潮" and r.is_limit_up:
                success_reason = "高潮期涨停，可能抓住了情绪主升浪的核心标的"
                correction = "高潮期果断上核心辨识度龙头，情绪溢价确定性高"
            elif phase in ("升温", "修复"):
                success_reason = "升温/修复期顺势盈利，可能抓住了情绪回暖的节奏"
                correction = "升温/修复期积极做多，关注情绪共振方向"
            else:
                success_reason = "选股方向正确，次日表现符合预期"
                correction = "当前场景下选股逻辑有效，保持类似筛选标准"

            lesson = "在[{}]场景下推荐{}，次日{:+.2f}%。{}".format(
                phase_desc, r.stock, r.next_pct_chg, success_reason,
            )
            experiences.append(Experience(
                date=day_d,
                scenario=scenario.to_dict(),
                prediction="推荐买入: {}".format(r.stock),
                reality=detail,
                scores={},
                error_type="success",
                lesson=lesson,
                correction_rule=correction,
            ))

        return experiences

    def _infer_error_and_correction(
        self, r: Recommendation, phase: str, phase_desc: str,
    ) -> tuple[str, str]:
        """根据标的实际表现 + 情绪阶段推断错误类型和修正规则"""
        # 跌停 — 最严重
        if r.is_limit_down:
            if phase in ("分歧", "退潮", "冰点"):
                return (
                    "sentiment",
                    "{}阶段推荐标的次日跌停，在{}阶段应空仓或极致保守，禁止开新仓".format(phase, phase),
                )
            return (
                "strategy",
                "推荐标的次日跌停，选股逻辑存在重大缺陷（可能追高后排/非辨识度标的）",
            )

        # 大跌 > 5%
        if r.next_pct_chg < -5:
            if phase in ("分歧", "退潮"):
                return (
                    "sentiment",
                    "{}阶段推荐标的次日大跌{:.1f}%，分歧/退潮期应优先防守，控制仓位或空仓".format(
                        phase, r.next_pct_chg),
                )
            if phase == "高潮":
                return (
                    "sentiment",
                    "高潮期次日大跌{:.1f}%，可能踩中高潮→分歧的拐点，高潮末期需警惕亏钱效应".format(
                        r.next_pct_chg),
                )
            return (
                "strategy",
                "在[{}]场景下推荐{}次日大跌{:.1f}%，选股可能偏向非核心标的，应聚焦辨识度龙头".format(
                    phase_desc, r.stock, r.next_pct_chg),
            )

        # 中度亏损 -2% ~ -5%
        if phase in ("冰点", "退潮"):
            return (
                "sentiment",
                "{}阶段强行操作导致亏损，冰点/退潮期应减少出手频率，等待情绪修复信号".format(phase),
            )
        if phase == "分歧":
            return (
                "sentiment",
                "分歧期选股失误，分歧期应只做辨识度龙头的低吸，不追高不买后排",
            )
        return (
            "strategy",
            "在[{}]场景下推荐{}次日亏损{:.1f}%，选股精度不足或买入时机偏差".format(
                phase_desc, r.stock, r.next_pct_chg),
        )

    def _generate_experience_review_file(
        self, all_experiences: list[Experience], output_dir: str,
    ) -> str:
        """生成经验总结 Markdown 文件供用户审阅

        Returns:
            生成的文件路径
        """
        if not all_experiences:
            return ""

        review_path = os.path.join(output_dir, "经验总结.md")
        lines = [
            "# 回测经验总结（待审阅）",
            "",
            "> 以下经验由回测引擎自动提取，包含成功和失败案例。",
            "> **请审阅后决定哪些经验值得沉淀到经验库。**",
            "",
        ]

        # 按日期分组
        by_date: dict[str, list[Experience]] = {}
        for exp in all_experiences:
            by_date.setdefault(exp.date, []).append(exp)

        # 统计
        failures = [e for e in all_experiences if e.error_type != "success"]
        successes = [e for e in all_experiences if e.error_type == "success"]
        lines.append("## 概览")
        lines.append("")
        lines.append("- 提取日期数：{}".format(len(by_date)))
        lines.append("- 失败教训：{} 条".format(len(failures)))
        lines.append("- 成功经验：{} 条".format(len(successes)))
        lines.append("")

        # 按日期输出
        for date in sorted(by_date.keys()):
            exps = by_date[date]
            lines.append("## {}".format(date))
            lines.append("")

            for i, exp in enumerate(exps, 1):
                tag = "✅ 成功" if exp.error_type == "success" else "❌ 失败"
                error_label = exp.error_type if exp.error_type != "success" else ""
                lines.append("### {}. {} {} {}".format(i, exp.prediction, tag, error_label))
                lines.append("")
                lines.append("- **场景**: {}".format(
                    ", ".join("{}={}".format(k, v) for k, v in exp.scenario.items() if v)
                    if exp.scenario else "未知",
                ))
                lines.append("- **实际结果**: {}".format(exp.reality))
                lines.append("- **教训**: {}".format(exp.lesson))
                lines.append("- **修正规则**: {}".format(exp.correction_rule))
                lines.append("")

        with open(review_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return review_path
