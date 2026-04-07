"""日常分析报告归档器

在 Agent 输出包含操盘计划/推荐标的时，自动归档为结构化文件，
纳入进化闭环（可被回测系统和蒸馏模块消费）。

归档目录结构：
  ~/shared/trading/daily_reports/
    2026-04-07/
      report.md              # 完整分析报告
      focus_stocks.json      # 结构化推荐标的
      metadata.json          # 元数据（版本号、时间戳）
      verify.json            # D+1 验证结果（次日自动生成）
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# 默认归档目录
DEFAULT_ARCHIVE_DIR = os.path.expanduser("~/shared/trading/daily_reports")


class ReportArchiver:
    """日常分析报告归档器。"""

    def __init__(self, archive_dir: str = DEFAULT_ARCHIVE_DIR):
        self.archive_dir = archive_dir

    def archive(self, content: str, version: str = "") -> Optional[str]:
        """检测并归档分析报告。

        Args:
            content: Agent 最终输出内容
            version: Agent 版本号

        Returns:
            归档目录路径，非报告类内容返回 None
        """
        # 检测是否包含操盘计划/推荐标的
        if not self._is_report(content):
            return None

        today = datetime.now().strftime("%Y-%m-%d")
        day_dir = os.path.join(self.archive_dir, today)
        os.makedirs(day_dir, exist_ok=True)

        # 1. 保存完整报告
        report_path = os.path.join(day_dir, "report.md")
        # 如果今天已有报告，追加序号
        if os.path.exists(report_path):
            idx = 2
            while os.path.exists(os.path.join(day_dir, f"report_{idx}.md")):
                idx += 1
            report_path = os.path.join(day_dir, f"report_{idx}.md")

        with open(report_path, "w", encoding="utf-8") as f:
            f.write(content)

        # 2. 提取并保存 focus_stocks
        focus_stocks = self._extract_focus_stocks(content)
        if focus_stocks:
            stocks_path = os.path.join(day_dir, "focus_stocks.json")
            with open(stocks_path, "w", encoding="utf-8") as f:
                json.dump(focus_stocks, f, ensure_ascii=False, indent=2)

        # 3. 保存元数据
        metadata = {
            "version": version,
            "archived_at": datetime.now().isoformat(),
            "report_file": os.path.basename(report_path),
            "has_focus_stocks": bool(focus_stocks),
            "focus_stock_count": len(focus_stocks) if focus_stocks else 0,
        }
        meta_path = os.path.join(day_dir, "metadata.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        logger.info("报告已归档: %s (标的 %d 只, 版本 %s)",
                     day_dir, len(focus_stocks) if focus_stocks else 0, version)
        return day_dir

    def verify_previous(self, date: str, data_dir: str) -> Optional[dict]:
        """验证前日推荐标的的实际表现。

        Args:
            date: 要验证的日期（即前日 focus_stocks 的 D+1）
            data_dir: trading 数据根目录

        Returns:
            验证结果 dict，无数据时返回 None
        """
        day_dir = os.path.join(self.archive_dir, date)
        stocks_path = os.path.join(day_dir, "focus_stocks.json")

        if not os.path.exists(stocks_path):
            return None

        with open(stocks_path, "r", encoding="utf-8") as f:
            focus_stocks = json.load(f)

        if not focus_stocks:
            return None

        # 加载实际行情
        verify_results = []
        try:
            from backtest.adapter import CSVStockDataProvider
            loader = CSVStockDataProvider()

            for stock in focus_stocks:
                name = stock.get("name", "") if isinstance(stock, dict) else stock
                if not name:
                    continue

                actual = loader.load_stock_daily(data_dir, date, name)
                if not actual:
                    verify_results.append({
                        "name": name,
                        "status": "no_data",
                    })
                    continue

                open_price = actual.get("open", 0)
                close_price = actual.get("close", 0)
                pnl_pct = (close_price - open_price) / open_price * 100 if open_price > 0 else 0

                verify_results.append({
                    "name": name,
                    "open": round(open_price, 2),
                    "close": round(close_price, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "is_limit_up": actual.get("is_limit_up", False),
                    "is_limit_down": actual.get("is_limit_down", False),
                    "status": "verified",
                })

        except Exception as e:
            logger.warning("验证前日报告失败: %s", e)
            return None

        # 读取元数据获取版本号
        meta_path = os.path.join(day_dir, "metadata.json")
        version = ""
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
                version = meta.get("version", "")

        result = {
            "signal_date": date,
            "verify_date": datetime.now().strftime("%Y-%m-%d"),
            "agent_version": version,
            "results": verify_results,
            "summary": {
                "total": len(verify_results),
                "verified": sum(1 for r in verify_results if r["status"] == "verified"),
                "avg_pnl_pct": round(
                    sum(r.get("pnl_pct", 0) for r in verify_results if r["status"] == "verified")
                    / max(1, sum(1 for r in verify_results if r["status"] == "verified")),
                    2,
                ),
                "hit_rate": round(
                    sum(1 for r in verify_results if r.get("pnl_pct", 0) > 0)
                    / max(1, sum(1 for r in verify_results if r["status"] == "verified"))
                    * 100,
                    1,
                ),
            },
        }

        # 保存 verify.json
        verify_path = os.path.join(day_dir, "verify.json")
        with open(verify_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        logger.info("前日报告验证完成: %s (平均收益 %+.2f%%, 胜率 %.1f%%)",
                     date, result["summary"]["avg_pnl_pct"], result["summary"]["hit_rate"])
        return result

    def _is_report(self, content: str) -> bool:
        """检测内容是否为分析报告（包含操作建议）。"""
        if len(content) < 200:
            return False

        keywords = re.compile(
            r"(focus_stocks|操盘计划|明日策略|买入标的|推荐标的|position_actions)"
        )
        return bool(keywords.search(content))

    def _extract_focus_stocks(self, content: str) -> list[dict]:
        """从报告中提取 focus_stocks 结构化数据。"""
        # Pattern 1: JSON ```json``` block
        json_match = re.search(r'```json\s*\n(.*?)\n```', content, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                stocks = data.get("focus_stocks", [])
                if stocks:
                    return [s if isinstance(s, dict) else {"name": s} for s in stocks]
            except json.JSONDecodeError:
                pass

        # Pattern 2: Bare JSON "focus_stocks": [...]
        json_match = re.search(r'"focus_stocks"\s*:\s*\[(.*?)\]', content, re.DOTALL)
        if json_match:
            try:
                stocks = json.loads("[" + json_match.group(1) + "]")
                return [s if isinstance(s, dict) else {"name": s} for s in stocks]
            except json.JSONDecodeError:
                pass

        # Pattern 3: 从文本中提取 中文名（代码）
        result = []
        seen = set()
        for m in re.finditer(r'([\u4e00-\u9fff]{2,6})\s*[（(]\s*(\d{6})\s*[）)]', content):
            name = m.group(1)
            code = m.group(2)
            if name not in seen:
                seen.add(name)
                result.append({"name": name, "code": code})

        return result
