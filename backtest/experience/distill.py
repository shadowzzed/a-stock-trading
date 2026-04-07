"""ExpeL 式经验蒸馏器：对比成功 vs 失败交易，自动归纳系统性规则

核心思路（来自 ExpeL 论文）：
- 不从单次失败提取教训，而是对比同一场景下的成功组和失败组
- LLM 找出"赢在哪里""输在哪里"的共同模式
- 归纳出可执行的交易规则

数据来源：
- 回测输出的 verify.json（每天的推荐标的 + 实际表现）
- 日常报告归档的 verify.json（实盘数据）
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .store import Experience, ExperienceStore

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    """一笔交易记录（用于蒸馏分析）"""
    date: str = ""
    stock_name: str = ""
    stock_code: str = ""
    action: str = ""
    pnl_pct: float = 0.0
    is_limit_up: bool = False
    is_limit_down: bool = False
    report_summary: str = ""  # Agent 分析摘要
    scenario: dict = field(default_factory=dict)
    error_type: str = ""
    agent_version: str = ""

    @property
    def is_success(self) -> bool:
        return self.pnl_pct > 0

    @property
    def is_failure(self) -> bool:
        return self.pnl_pct < -1.0  # 小幅亏损不算失败


@dataclass
class TradeGroup:
    """一组交易（按某个维度分组）"""
    key: str = ""
    dimension: str = ""
    trades: list = field(default_factory=list)

    @property
    def successes(self) -> list[Trade]:
        return [t for t in self.trades if t.is_success]

    @property
    def failures(self) -> list[Trade]:
        return [t for t in self.trades if t.is_failure]

    @property
    def has_enough_data(self) -> bool:
        """至少 3 笔成功 + 3 笔失败才触发蒸馏"""
        return len(self.successes) >= 3 and len(self.failures) >= 3


@dataclass
class DistilledRule:
    """蒸馏出的规则"""
    rule: str = ""
    evidence: str = ""
    scenario_scope: str = ""
    confidence: float = 0.5
    source_group: str = ""  # 来源分组


@dataclass
class DistillReport:
    """蒸馏报告"""
    new_rules: list = field(default_factory=list)
    reinforced: list = field(default_factory=list)  # 被强化的已有规则
    contradicted: list = field(default_factory=list)  # 被否定的已有规则
    total_trades: int = 0
    groups_analyzed: int = 0
    agent_version: str = ""


# 蒸馏分组维度
DISTILL_DIMENSIONS = [
    {"name": "情绪阶段", "key": "sentiment_phase"},
    {"name": "错误类型", "key": "error_type"},
    {"name": "情绪×炸板率", "keys": ("sentiment_phase", "blown_rate_range")},
    {"name": "情绪×连板高度", "keys": ("sentiment_phase", "max_board_range")},
]


class ExperienceDistiller:
    """ExpeL 式经验蒸馏器。"""

    def __init__(self, store: ExperienceStore):
        self.store = store

    def distill(
        self,
        data_dirs: list[str],
        min_group_size: int = 6,
        agent_version: str = "",
        auto_import: bool = False,
    ) -> DistillReport:
        """批量蒸馏。

        Args:
            data_dirs: 回测输出目录 或 日常报告目录列表
            min_group_size: 每组最少交易笔数（成功+失败 >= 此值）
            agent_version: Agent 版本号
            auto_import: 自动将新规则导入 ExperienceStore

        Returns:
            DistillReport
        """
        # 1. 汇总所有交易记录
        all_trades = self._load_trades(data_dirs)
        if not all_trades:
            logger.warning("无交易记录可蒸馏")
            return DistillReport()

        logger.info("加载 %d 笔交易记录", len(all_trades))

        report = DistillReport(
            total_trades=len(all_trades),
            agent_version=agent_version,
        )

        # 2. 按多个维度分组
        all_rules = []
        for dim in DISTILL_DIMENSIONS:
            groups = self._group_trades(all_trades, dim)
            for group in groups:
                if not group.has_enough_data:
                    continue

                logger.info("蒸馏分组 [%s=%s]: %d 成功, %d 失败",
                            dim.get("name", ""), group.key,
                            len(group.successes), len(group.failures))

                rules = self._distill_group(group)
                all_rules.extend(rules)
                report.groups_analyzed += 1

        if not all_rules:
            logger.info("无新规则产生")
            return report

        # 3. 规则去重（语义级）
        unique_rules = self._deduplicate_rules(all_rules)
        logger.info("蒸馏出 %d 条规则（去重前 %d 条）", len(unique_rules), len(all_rules))

        # 4. 与已有经验对比
        for rule in unique_rules:
            match = self._find_existing_match(rule)
            if match:
                # 强化已有规则
                report.reinforced.append({
                    "rule": rule.rule,
                    "existing_lesson": match.lesson,
                    "new_evidence": rule.evidence,
                })
                # 更新已有经验的置信度
                match.confidence = min(0.95, match.confidence + 0.1)
                match.occurrence_count += 1
                match.last_validated = datetime.now().isoformat()
            else:
                report.new_rules.append(rule)

        # 5. 导入新规则
        if auto_import and report.new_rules:
            for rule in report.new_rules:
                exp = Experience(
                    error_type="strategy",
                    lesson=rule.rule,
                    correction_rule=rule.rule,
                    confidence=rule.confidence,
                    scenario={"distill_scope": rule.scenario_scope},
                )
                if agent_version:
                    exp.lesson = f"[{agent_version}蒸馏] {exp.lesson}"
                self.store.add(exp)
            self.store.save()
            logger.info("已导入 %d 条新规则", len(report.new_rules))

        return report

    def _load_trades(self, data_dirs: list[str]) -> list[Trade]:
        """从多个目录加载交易记录。"""
        trades = []

        for dir_path in data_dirs:
            dir_path = os.path.expanduser(dir_path)
            if not os.path.isdir(dir_path):
                continue

            # 方式1：回测输出目录（{date}_verify.json）
            for fname in sorted(os.listdir(dir_path)):
                if fname.endswith("_verify.json"):
                    fpath = os.path.join(dir_path, fname)
                    trades.extend(self._parse_verify_json(fpath))

            # 方式2：日常报告目录（子目录/verify.json）
            for subdir in sorted(os.listdir(dir_path)):
                verify_path = os.path.join(dir_path, subdir, "verify.json")
                if os.path.exists(verify_path):
                    trades.extend(self._parse_daily_verify(verify_path))

        return trades

    def _parse_verify_json(self, path: str) -> list[Trade]:
        """解析回测 verify.json。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            trades = []
            recs = data.get("recommendations", [])
            scenario = data.get("scenario", {})
            date = data.get("day_d", "")
            version = data.get("agent_version", "")

            for rec in recs:
                trade = Trade(
                    date=date,
                    stock_name=rec.get("stock", rec.get("name", "")),
                    stock_code=rec.get("code", ""),
                    action=rec.get("action", "买入"),
                    pnl_pct=rec.get("pnl_pct", 0),
                    is_limit_up=rec.get("is_limit_up", False),
                    is_limit_down=rec.get("is_limit_down", False),
                    report_summary=rec.get("reason", ""),
                    scenario=scenario,
                    error_type=rec.get("error_type", ""),
                    agent_version=version,
                )
                trades.append(trade)

            return trades
        except Exception as e:
            logger.warning("解析 %s 失败: %s", path, e)
            return []

    def _parse_daily_verify(self, path: str) -> list[Trade]:
        """解析日常报告的 verify.json。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            trades = []
            results = data.get("results", [])
            date = data.get("signal_date", "")
            version = data.get("agent_version", "")

            for r in results:
                if r.get("status") != "verified":
                    continue
                trade = Trade(
                    date=date,
                    stock_name=r.get("name", ""),
                    pnl_pct=r.get("pnl_pct", 0),
                    is_limit_up=r.get("is_limit_up", False),
                    is_limit_down=r.get("is_limit_down", False),
                    agent_version=version,
                )
                trades.append(trade)

            return trades
        except Exception as e:
            logger.warning("解析 %s 失败: %s", path, e)
            return []

    def _group_trades(self, trades: list[Trade], dimension: dict) -> list[TradeGroup]:
        """按指定维度分组。"""
        groups = {}

        if "keys" in dimension:
            # 多维度交叉分组
            key1, key2 = dimension["keys"]
            for trade in trades:
                v1 = trade.scenario.get(key1, "") or getattr(trade, key1, "")
                v2 = trade.scenario.get(key2, "") or getattr(trade, key2, "")
                if not v1 or not v2:
                    continue
                group_key = f"{v1}×{v2}"
                if group_key not in groups:
                    groups[group_key] = TradeGroup(
                        key=group_key,
                        dimension=dimension.get("name", ""),
                    )
                groups[group_key].trades.append(trade)
        else:
            # 单维度分组
            key = dimension["key"]
            for trade in trades:
                value = trade.scenario.get(key, "") or getattr(trade, key, "")
                if not value:
                    continue
                if value not in groups:
                    groups[value] = TradeGroup(
                        key=value,
                        dimension=dimension.get("name", ""),
                    )
                groups[value].trades.append(trade)

        return list(groups.values())

    def _distill_group(self, group: TradeGroup) -> list[DistilledRule]:
        """单组蒸馏：LLM 对比成功 vs 失败。"""
        successes = group.successes[:10]  # 限制数量避免 token 爆炸
        failures = group.failures[:10]

        success_text = "\n".join(
            f"- {t.date} {t.stock_name}: 收益 {t.pnl_pct:+.2f}%"
            f"{' (涨停)' if t.is_limit_up else ''}"
            f"{'，分析：' + t.report_summary[:100] if t.report_summary else ''}"
            for t in successes
        )

        failure_text = "\n".join(
            f"- {t.date} {t.stock_name}: 亏损 {t.pnl_pct:+.2f}%"
            f"{' (跌停)' if t.is_limit_down else ''}"
            f"{'，分析：' + t.report_summary[:100] if t.report_summary else ''}"
            for t in failures
        )

        prompt = f"""你是一位资深交易教练。以下是 Trade Agent 在 [{group.dimension}={group.key}] 场景下的交易记录。

## 成功交易（{len(successes)}笔，平均收益 {sum(t.pnl_pct for t in successes)/len(successes):+.2f}%）
{success_text}

## 失败交易（{len(failures)}笔，平均亏损 {sum(t.pnl_pct for t in failures)/len(failures):+.2f}%）
{failure_text}

请对比成功和失败交易，归纳出 1-3 条具体可执行的规则。

输出 JSON 数组，每条规则包含：
- rule: 具体可执行的规则描述（一句话）
- evidence: 成功X次/失败Y次的统计证据
- scenario_scope: 适用的场景范围
- confidence: 0.0-1.0（基于样本量和一致性判断）

只输出 JSON 数组，不要其他内容："""

        try:
            from trading_agent.chat.graph import _get_llm
            from langchain_core.messages import HumanMessage

            llm = _get_llm()
            resp = llm.invoke([HumanMessage(content=prompt)])
            text = resp.content if hasattr(resp, "content") else ""

            if isinstance(text, list):
                text = "\n".join(
                    block.text if hasattr(block, "text") else str(block)
                    for block in text
                )

            # 解析 JSON
            import re
            json_match = re.search(r'\[.*\]', text, re.DOTALL)
            if json_match:
                rules_data = json.loads(json_match.group(0))
                rules = []
                for rd in rules_data:
                    rules.append(DistilledRule(
                        rule=rd.get("rule", ""),
                        evidence=rd.get("evidence", ""),
                        scenario_scope=rd.get("scenario_scope", group.key),
                        confidence=rd.get("confidence", 0.5),
                        source_group=f"{group.dimension}={group.key}",
                    ))
                return rules

        except Exception as e:
            logger.warning("蒸馏分组 [%s] 失败: %s", group.key, e)

        return []

    def _deduplicate_rules(self, rules: list[DistilledRule]) -> list[DistilledRule]:
        """简单去重：基于规则文本的相似度。"""
        if not rules:
            return []

        unique = [rules[0]]
        for rule in rules[1:]:
            is_dup = False
            for existing in unique:
                # 简单文本相似度：超过 60% 的字符重叠视为重复
                overlap = len(set(rule.rule) & set(existing.rule))
                max_len = max(len(set(rule.rule)), len(set(existing.rule)), 1)
                if overlap / max_len > 0.6:
                    # 保留置信度更高的
                    if rule.confidence > existing.confidence:
                        unique.remove(existing)
                        unique.append(rule)
                    is_dup = True
                    break
            if not is_dup:
                unique.append(rule)

        return unique

    def _find_existing_match(self, rule: DistilledRule) -> Optional[Experience]:
        """在已有经验库中查找语义匹配的教训。"""
        for exp in self.store.experiences:
            # 简单文本匹配
            overlap = len(set(rule.rule) & set(exp.correction_rule))
            max_len = max(len(set(rule.rule)), len(set(exp.correction_rule)), 1)
            if overlap / max_len > 0.5:
                return exp
        return None

    def save_report(self, report: DistillReport, output_dir: str) -> str:
        """保存蒸馏报告。"""
        os.makedirs(output_dir, exist_ok=True)

        # Markdown 报告
        lines = [
            f"# 蒸馏报告 — {report.agent_version} | {datetime.now().strftime('%Y-%m-%d')}",
            "",
            "## 数据范围",
            f"- 总交易笔数：{report.total_trades}",
            f"- 分析分组数：{report.groups_analyzed}",
            "",
        ]

        if report.new_rules:
            lines.append(f"## 新发现的规则（{len(report.new_rules)}条）")
            lines.append("")
            for i, rule in enumerate(report.new_rules, 1):
                lines.append(f"{i}. **{rule.rule}**")
                lines.append(f"   - 证据：{rule.evidence}")
                lines.append(f"   - 适用范围：{rule.scenario_scope}")
                lines.append(f"   - 置信度：{rule.confidence:.0%}")
                lines.append(f"   - 来源分组：{rule.source_group}")
                lines.append("")

        if report.reinforced:
            lines.append(f"## 被强化的已有规则（{len(report.reinforced)}条）")
            lines.append("")
            for r in report.reinforced:
                lines.append(f"- 已有：{r['existing_lesson'][:80]}")
                lines.append(f"  新证据：{r['new_evidence']}")
                lines.append("")

        if report.contradicted:
            lines.append(f"## 被否定的已有规则（{len(report.contradicted)}条）")
            lines.append("")
            for r in report.contradicted:
                lines.append(f"- {r}")
                lines.append("")

        if not report.new_rules and not report.reinforced:
            lines.append("（无新发现）")

        md_path = os.path.join(output_dir, "distill_report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        # JSON 报告
        json_data = {
            "version": report.agent_version,
            "date": datetime.now().isoformat(),
            "total_trades": report.total_trades,
            "groups_analyzed": report.groups_analyzed,
            "new_rules": [
                {"rule": r.rule, "evidence": r.evidence,
                 "scope": r.scenario_scope, "confidence": r.confidence}
                for r in report.new_rules
            ],
            "reinforced": report.reinforced,
            "contradicted": report.contradicted,
        }
        json_path = os.path.join(output_dir, "distill_report.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)

        logger.info("蒸馏报告已保存: %s", output_dir)
        return md_path
