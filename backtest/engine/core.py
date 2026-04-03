"""回测引擎核心 — 经验驱动的 D→D+1 回测

通过依赖注入接收 DataProvider / AgentRunner / LLMCaller 实现，
引擎本身零耦合于具体数据源和 Agent 框架。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from .protocols import DataProvider, AgentRunner, LLMCaller, MarketData
from .prompts import VERIFIER_PROMPT, EXPERIENCE_EXTRACTOR_PROMPT
from .report import generate_summary
from ..experience.store import ExperienceStore, Experience
from ..experience.classifier import ScenarioClassifier, classify_error_type
from ..experience.tracker import LessonTracker
from ..experience.prompt_engine import PromptEngine


@dataclass
class BacktestResult:
    """单日回测结果"""
    day_d: str
    day_d1: str
    status: str = "pending"           # completed / analysis_failed / d1_data_failed
    scenario: dict = field(default_factory=dict)
    injected_lessons: int = 0
    scores: dict = field(default_factory=dict)
    total_score: float = 0.0
    key_lessons: list = field(default_factory=list)
    what_was_right: list = field(default_factory=list)
    what_was_wrong: list = field(default_factory=list)
    error: str = ""


class BacktestEngine:
    """经验驱动的回测引擎

    使用方式:
        engine = BacktestEngine(
            data_provider=ReviewDataProvider(),
            agent_runner=ReviewAgentRunner(),
            llm_caller=LangChainLLMCaller(llm),
        )
        summary = engine.run(data_dir="...", dates=[...])
    """

    def __init__(
        self,
        data_provider: DataProvider,
        agent_runner: AgentRunner,
        llm_caller: LLMCaller,
    ):
        self.data_provider = data_provider
        self.agent_runner = agent_runner
        self.llm_caller = llm_caller

    def run(
        self,
        data_dir: str,
        dates: list[str],
        output_dir: Optional[str] = None,
        on_progress=None,
    ) -> dict:
        """运行经验驱动的回测

        Args:
            data_dir: 数据根目录
            dates: 已排序的交易日列表
            output_dir: 输出目录
            on_progress: 进度回调 fn(idx, total, date, stage)
        """
        if not output_dir:
            output_dir = os.path.join(data_dir, "backtest_v6")
        os.makedirs(output_dir, exist_ok=True)

        # 初始化经验系统
        exp_store = ExperienceStore(data_dir)
        lesson_tracker = LessonTracker(data_dir)
        prompt_engine = PromptEngine(data_dir)
        classifier = ScenarioClassifier()

        results: list[BacktestResult] = []
        pairs = [(dates[i], dates[i + 1]) for i in range(len(dates) - 1)]
        prev_report = ""

        for idx, (day_d, day_d1) in enumerate(pairs):
            if on_progress:
                on_progress(idx + 1, len(pairs), day_d, "analyzing")

            print("=" * 60)
            print("回测 {}/{}: {} → {} [v6 经验驱动]".format(
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

                # 检索注入了哪些教训 ID
                relevant = exp_store.search(
                    scenario=scenario, min_confidence=0.3, limit=10,
                )
                active_ids = set(lesson_tracker.get_active_lessons()) or {e.id for e in relevant}
                injected_ids = [e.id for e in relevant if e.id in active_ids][:9]

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

            # ── Step 4: 验证打分 ──
            if on_progress:
                on_progress(idx + 1, len(pairs), day_d, "verifying")

            verify_path = os.path.join(output_dir, "{}_verify.json".format(day_d))
            if os.path.exists(verify_path):
                with open(verify_path, "r", encoding="utf-8") as f:
                    verify_result = json.load(f)
                print("  [跳过] {} 验证已存在".format(day_d))
            else:
                verify_msg = (
                    "## Day D ({day_d}) 的 Agent 预测报告\n\n"
                    "{report}\n\n"
                    "---\n\n"
                    "## Day D+1 ({day_d1}) 的实际行情数据\n\n"
                    "{d1_summary}\n\n"
                    "请对比预测与实际，给出评分和教训。"
                ).format(day_d=day_d, day_d1=day_d1, report=report, d1_summary=d1_summary)

                try:
                    raw = self.llm_caller.invoke(VERIFIER_PROMPT, verify_msg)
                    content = raw
                    if "```json" in content:
                        content = content.split("```json")[1].split("```")[0]
                    elif "```" in content:
                        content = content.split("```")[1].split("```")[0]

                    verify_result = json.loads(content.strip())
                    with open(verify_path, "w", encoding="utf-8") as f:
                        json.dump(verify_result, f, ensure_ascii=False, indent=2)
                    print("  [完成] {} 验证: 总分 {}/20".format(
                        day_d, verify_result.get("total_score", "?")))
                except Exception as e:
                    print("  [失败] {} 验证失败: {}".format(day_d, e))
                    verify_result = {"error": str(e)}

            total_score = verify_result.get("total_score", 0)
            result.total_score = total_score
            result.scores = verify_result.get("scores", {})
            result.key_lessons = verify_result.get("key_lessons", [])
            result.what_was_right = verify_result.get("what_was_right", [])
            result.what_was_wrong = verify_result.get("what_was_wrong", [])

            # ── Step 4.5: 后置校验 — 磁核事实声明与 DB 对比 ──
            corrections = self._post_verify_check(
                data_dir, day_d1, verify_result, report,
            )
            if corrections:
                # 按涉及维度去重（同一股票多次出现只扣一次）
                affected_dims = set()
                print("  [校验] 发现 {} 处事实修正:".format(len(corrections)))
                for c in corrections:
                    print("    - {}: {} → {}".format(c["stock"], c["claimed"], c["actual"]))
                    # 修正分数中涉及该股票的错误判断
                    for dim in ["sentiment", "sector", "leader", "strategy"]:
                        dim_score = result.scores.get(dim, {})
                        reason = dim_score.get("reason", "")
                        if c["stock"] in reason:
                            dim_score["reason"] = reason.replace(
                                c["claimed"], c["actual"]
                            )
                            affected_dims.add(dim)

                # 事实错误扣分：每个受影响维度扣 1 分
                for dim in affected_dims:
                    dim_info = result.scores.get(dim, {})
                    old_score = dim_info.get("score", 0)
                    new_score = max(1, old_score - 1)
                    dim_info["score"] = new_score
                    dim_info["reason"] = (
                        "[事实修正: {}→{}] ".format(
                            c["claimed"], c["actual"]
                        ) + dim_info.get("reason", "")
                    )
                    print("    - 扣分: {} 维度 {} → {}".format(
                        dim, old_score, new_score))

                # 重新计算总分
                total_score = sum(
                    result.scores.get(d, {}).get("score", 0)
                    for d in ["sentiment", "sector", "leader", "strategy"]
                )
                result.total_score = total_score
                verify_result["total_score"] = total_score
                # 更新保存的验证结果
                verify_path = os.path.join(output_dir, "{}_verify.json".format(day_d))
                with open(verify_path, "w", encoding="utf-8") as f:
                    json.dump(verify_result, f, ensure_ascii=False, indent=2)
                print("  [校验] 修正后总分: {}/20".format(total_score))

            # ── Step 5: 提取结构化经验 ──
            if on_progress:
                on_progress(idx + 1, len(pairs), day_d, "extracting_experience")

            extract_path = os.path.join(output_dir, "{}_experience.json".format(day_d))
            if not os.path.exists(extract_path) and "error" not in verify_result:
                try:
                    experience = self._extract_experience(
                        day_d=day_d, day_d1=day_d1,
                        report=report, verify_result=verify_result,
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

            # ── Step 6: 记录教训效果 ──
            if injected_ids:
                lesson_tracker.record_injection(
                    date=day_d, lesson_ids=injected_ids, score=total_score,
                )
                lesson_tracker.feedback_to_store(exp_store)

            result.status = "completed"
            results.append(result)
            time.sleep(1)

        # ── 生成汇总报告 ──
        summary = generate_summary(
            results, output_dir, exp_store, lesson_tracker,
        )
        return summary

    def _post_verify_check(
        self,
        data_dir: str,
        day_d1: str,
        verify_result: dict,
        report: str,
    ) -> list[dict]:
        """后置校验：对比验证 LLM 的事实声明与 intraday DB 实际数据

        扫描 verify_result 中的 reason 文本，检测对具体股票的涨跌判断，
        与 DB 中的实际行情对比，发现矛盾则返回修正列表。
        """
        import re as re_mod
        import sqlite3 as sqlite3_mod

        db_path = os.path.join(data_dir, "intraday", "intraday.db")
        if not os.path.exists(db_path):
            return []

        # 收集所有 reason 中提到的股票名 + 判断
        all_reasons = ""
        scores = verify_result.get("scores", {})
        for dim_scores in scores.values():
            if isinstance(dim_scores, dict):
                all_reasons += dim_scores.get("reason", "") + " "

        # 提取 "XXX跌停" 或 "XXX涨停" 的声明
        corrections = []
        for m in re_mod.finditer(r"([\u4e00-\u9fa5]{2,4})(涨停|跌停)", all_reasons):
            stock_name = m.group(1)
            claim = m.group(2)  # "涨停" or "跌停"

            # 查询 DB
            try:
                conn = sqlite3_mod.connect(f"file:{db_path}?mode=ro")
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT is_limit_up, is_limit_down, pctChg
                    FROM snapshots
                    WHERE date = ? AND name = ?
                    AND ts = (SELECT MAX(ts) FROM snapshots WHERE date = ?)
                    LIMIT 1
                """, (day_d1, stock_name, day_d1))
                row = cursor.fetchone()
                conn.close()

                if row:
                    is_up, is_down, pct = row[0], row[1], row[2]
                    # 检查矛盾
                    if claim == "跌停" and is_up:
                        corrections.append({
                            "stock": stock_name,
                            "claimed": "跌停",
                            "actual": "涨停（{:+.2f}%）".format(pct),
                        })
                    elif claim == "涨停" and is_down:
                        corrections.append({
                            "stock": stock_name,
                            "claimed": "涨停",
                            "actual": "跌停（{:+.2f}%）".format(pct),
                        })
            except Exception:
                continue

        return corrections

    def _extract_experience(
        self,
        day_d: str,
        day_d1: str,
        report: str,
        verify_result: dict,
        scenario,
    ) -> Optional[Experience]:
        """从验证结果中提取结构化经验"""
        scores_text = json.dumps(verify_result.get("scores", {}), ensure_ascii=False)
        wrong_items = "\n".join(
            "- {}".format(w) for w in verify_result.get("what_was_wrong", [])
        )
        lessons_items = "\n".join(
            "- {}".format(l) for l in verify_result.get("key_lessons", [])
        )

        extract_msg = (
            "## 回测验证结果\n\n"
            "**分析日期**: {day_d}\n"
            "**验证日期**: {day_d1}\n"
            "**市场场景**: {scenario}\n"
            "**总分**: {total}/20\n\n"
            "### 各维度评分\n{scores}\n\n"
            "### 错误判断\n{wrong}\n\n"
            "### 已有教训摘要\n{lessons}\n\n"
            "请提炼一条最关键的结构化经验教训。"
        ).format(
            day_d=day_d, day_d1=day_d1,
            scenario=scenario.to_description(),
            total=verify_result.get("total_score", 0),
            scores=scores_text,
            wrong=wrong_items or "无",
            lessons=lessons_items or "无",
        )

        raw = self.llm_caller.invoke(EXPERIENCE_EXTRACTOR_PROMPT, extract_msg)
        content = raw
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        try:
            extracted = json.loads(content.strip())
        except json.JSONDecodeError:
            return None

        error_type = extracted.get("error_type") or classify_error_type(
            verify_result.get("scores", {})
        )

        return Experience(
            date=day_d,
            scenario=scenario.to_dict(),
            prediction=extracted.get("prediction_summary", ""),
            reality=extracted.get("reality_summary", ""),
            scores=verify_result.get("scores", {}),
            error_type=error_type,
            lesson=extracted.get("lesson", ""),
            correction_rule=extracted.get("correction_rule", ""),
        )
