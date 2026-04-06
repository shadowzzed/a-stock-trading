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

            # ── Step 2: 用 Day D 跑 Agent（带教训注入）──
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

            # 保存验证结果
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
            }
            with open(verify_path, "w", encoding="utf-8") as f:
                json.dump(verify_data, f, ensure_ascii=False, indent=2)

            # ── Step 5: 基于实盘结果提取经验 ──
            if on_progress:
                on_progress(idx + 1, len(pairs), day_d, "extracting_experience")

            extract_path = os.path.join(output_dir, "{}_experience.json".format(day_d))
            if not os.path.exists(extract_path) and recs:
                # 只在亏损或误判时提取教训
                losses = [r for r in recs if r.action == "买入" and r.pnl_pct < -2]
                wrong_calls = [r for r in recs if r.action == "买入" and r.next_pct_chg < -5]

                if losses or wrong_calls:
                    try:
                        experience = self._extract_experience_from_outcome(
                            day_d=day_d, day_d1=day_d1,
                            report=report, recs=recs,
                            scenario=scenario,
                        )
                        if experience:
                            exp_store.add(experience)
                            with open(extract_path, "w", encoding="utf-8") as f:
                                json.dump({
                                    "experience_id": experience.id,
                                    "scenario": experience.scenario,
                                    "lesson": experience.lesson,
                                    "correction_rule": experience.correction_rule,
                                    "error_type": experience.error_type,
                                }, f, ensure_ascii=False, indent=2)
                            print("  [经验提取] 新增教训: {}".format(
                                experience.lesson[:50]))
                    except Exception as e:
                        print("  [经验提取失败] {}".format(e))

            result.status = "completed"
            results.append(result)
            time.sleep(1)

        # ── 生成汇总报告 ──
        summary = generate_summary(results, output_dir, exp_store)
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

            # 经验提取
            extract_path = os.path.join(output_dir, "{}_experience.json".format(day_d))
            if not os.path.exists(extract_path) and recs:
                losses = [r for r in recs if r.action == "买入" and r.pnl_pct < -2]
                if losses:
                    try:
                        experience = self._extract_experience_from_outcome(
                            day_d=day_d, day_d1=day_d1, report=report,
                            recs=recs, scenario=scenario,
                        )
                        if experience:
                            exp_store.add(experience)
                            with open(extract_path, "w", encoding="utf-8") as f:
                                json.dump({
                                    "experience_id": experience.id,
                                    "scenario": experience.scenario,
                                    "lesson": experience.lesson,
                                    "correction_rule": experience.correction_rule,
                                    "error_type": experience.error_type,
                                }, f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        print("  [经验提取失败] {}".format(e))

            result.status = "completed"
            results.append(result)

        # 生成汇总
        summary = generate_summary(results, output_dir, exp_store)
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

    def _extract_focus_stocks(self, report: str) -> list[dict]:
        """从报告中提取推荐标的（优先 JSON 结构化输出，fallback 到 Markdown 解析）"""
        stocks_found: list[dict] = []
        seen_names: set[str] = set()

        # ── Priority: JSON structured output ──
        # Method 1: JSON ```json``` block with focus_stocks
        json_match = re.search(r'```json\s*\n(.*?)\n```', report, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                stocks = data.get("focus_stocks", [])
                if stocks:
                    valid = [s for s in stocks if isinstance(s, dict) and s.get("name") and len(s["name"]) >= 2]
                    if valid:
                        # JSON 模式下直接带完整信息，不再需要 _extract_buy_plans
                        return valid
            except json.JSONDecodeError:
                pass

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

            plans[stock_name] = {
                "action": "买入",
                "condition": condition,
                "position": position,
            }

        return plans

    def _extract_experience_from_outcome(
        self,
        day_d: str,
        day_d1: str,
        report: str,
        recs: list[Recommendation],
        scenario,
    ) -> Optional[Experience]:
        """从实际交易结果中提取结构化经验（不再依赖 LLM）"""
        # 收集亏损标的信息
        loss_details = []
        for r in recs:
            if r.action == "买入" and r.pnl_pct < -2:
                loss_details.append(
                    "{}: 推荐买入, D+1实际{:+.2f}%{}".format(
                        r.stock,
                        r.next_pct_chg,
                        "（涨停）" if r.is_limit_up else "（跌停）" if r.is_limit_down else "",
                    )
                )

        if not loss_details:
            return None

        # 构建教训（规则化，不需要 LLM）
        lesson = "在{}场景下，推荐{}等标的实际亏损。市场场景: {}".format(
            scenario.to_description(),
            "、".join(r.stock for r in recs if r.action == "买入" and r.pnl_pct < -2),
            scenario.to_description(),
        )

        # 分析错误类型
        worst = min(recs, key=lambda r: r.pnl_pct) if recs else None
        if worst:
            if worst.is_limit_down:
                error_type = "strategy"
                correction = "推荐标的次日跌停时，应在前日分析中识别退潮风险，避免推荐"
            elif worst.next_pct_chg < -5:
                error_type = "strategy"
                correction = "推荐标的次日大跌超5%，需加强选股过滤，避免在分歧/退潮期推荐非核心标的"
            else:
                error_type = "strategy"
                correction = "推荐标的次日表现不佳，需结合情绪阶段严格控制仓位或放弃操作"
        else:
            error_type = "unknown"
            correction = "选股失误"

        return Experience(
            date=day_d,
            scenario=scenario.to_dict(),
            prediction="推荐: " + "、".join(r.stock for r in recs if r.action == "买入"),
            reality="; ".join(loss_details),
            scores={},
            error_type=error_type,
            lesson=lesson,
            correction_rule=correction,
        )
