#!/usr/bin/env python3
"""经验驱动回测 v6 — CLI 入口

用法:
    # 完整回测（分析+验证+经验提取）
    python -m backtest.run --data-dir ~/trading-data --start 2026-03-01 --end 2026-03-31

    # 回测 + 交易模拟
    python -m backtest.run --data-dir ~/trading-data --start 2026-03-01 --trade-sim

    # 仅运行交易模拟（复用已有报告，零 LLM 消耗）
    python -m backtest.run --data-dir ~/trading-data --start 2026-03-01 --trade-sim-only

    # 后台运行
    nohup python -m backtest.run --data-dir ~/trading-data --start 2026-03-01 > backtest.log 2>&1 &
"""

from __future__ import annotations

import argparse
import json
import os

from .engine.core import BacktestEngine
from .adapter import ReviewDataProvider, ChatAgentRunner
from .trade.executor import TradeSimulator
from .trade.evaluator import evaluate, save_evaluation


def main():
    parser = argparse.ArgumentParser(description="经验驱动的回测 v6")
    parser.add_argument("--data-dir", required=True, help="trading 数据根目录")
    parser.add_argument("--start", help="开始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", help="结束日期 (YYYY-MM-DD)")
    parser.add_argument("--output", help="输出目录")
    parser.add_argument("--trade-sim", action="store_true",
                        help="启用交易模拟（在回测基础上模拟实际买卖）")
    parser.add_argument("--trade-sim-only", action="store_true",
                        help="仅运行交易模拟（复用已有报告，零 LLM 消耗）")
    parser.add_argument("--capital", type=float, default=1_000_000.0,
                        help="模拟初始资金（默认100万）")
    parser.add_argument("--simple-pnl", action="store_true",
                        help="简化盈亏模式：提到即买入（D+1开盘买，D+2开盘卖），纯测选股")
    parser.add_argument("--workers", type=int, default=1,
                        help="并行 worker 数（加速 LLM 调用，默认1=顺序）")
    args = parser.parse_args()

    data_provider = ReviewDataProvider()

    # 发现日期范围
    dates = data_provider.discover_dates(args.data_dir, args.start, args.end)
    if not dates:
        print("未找到符合条件的交易日")
        return

    print("发现 {} 个交易日: {} ~ {}".format(len(dates), dates[0], dates[-1]))

    output_dir = args.output or os.path.join(args.data_dir, "backtest_v6")
    os.makedirs(output_dir, exist_ok=True)

    # ── 简化盈亏模式 ──
    if args.simple_pnl:
        _run_simple_pnl(dates, args.data_dir, output_dir, args.capital)
        return

    # ── 交易模拟（仅模拟模式） ──
    if args.trade_sim_only:
        _run_trade_sim_only(dates, args.data_dir, output_dir, args.capital)
        return

    # ── 完整回测 ──
    agent_runner = ChatAgentRunner()

    engine = BacktestEngine(
        data_provider=data_provider,
        agent_runner=agent_runner,
    )

    engine.run(
        data_dir=args.data_dir,
        dates=dates,
        output_dir=output_dir,
        workers=args.workers,
    )

    # ── 回测后追加交易模拟 ──
    if args.trade_sim:
        _run_trade_sim_only(dates, args.data_dir, output_dir, args.capital)


def _run_trade_sim_only(
    dates: list[str],
    data_dir: str,
    output_dir: str,
    capital: float,
):
    """仅运行交易模拟（复用已有的回测报告，不消耗 LLM）"""
    from .adapter import CSVStockDataProvider

    print("\n" + "=" * 60)
    print("交易模拟（零 LLM 消耗模式）")
    print("=" * 60)

    loader = CSVStockDataProvider()
    sim = TradeSimulator(initial_capital=capital)
    sim.set_data_loader(loader)

    # 需要三元组：(Day D, Day D+1, Day D+2)
    # D+1 用于买入执行，D+2 用于卖出
    pairs = [(dates[i], dates[i + 1]) for i in range(len(dates) - 1)]

    for idx, (day_d, day_d1) in enumerate(pairs):
        # D+2 用于卖出
        day_d2 = pairs[idx + 1][1] if idx + 1 < len(pairs) else None

        # 读取已有报告
        report = _load_report(data_dir, output_dir, day_d)
        if not report:
            print("  [跳过] {} 无报告".format(day_d))
            continue

        print("模拟 {}/{}: {} → {} (卖出日: {})".format(
            idx + 1, len(pairs), day_d, day_d1, day_d2 or "无"))

        sim.process_day(
            signal_date=day_d,
            target_date=day_d1,
            sell_date=day_d2,
            report=report,
            data_dir=data_dir,
        )

    # 评估并保存
    results = sim.get_results()
    snapshots = sim.get_snapshots()

    if results:
        ev = evaluate(results, snapshots, capital)
        save_evaluation(ev, output_dir, prefix="trade_sim")
    else:
        print("\n无交易记录，跳过评估")

    print("\n交易模拟完成。结果保存在 {}".format(output_dir))


def _load_report(data_dir: str, output_dir: str, date: str) -> str:
    """加载回测报告（优先从 output_dir，再从 daily 目录）"""
    # 优先从回测输出目录
    report_path = os.path.join(output_dir, "{}_report.md".format(date))
    if os.path.exists(report_path):
        with open(report_path, "r", encoding="utf-8") as f:
            return f.read()

    # 从 daily 目录找裁决报告
    daily_dir = os.path.join(data_dir, "daily", date)
    if os.path.isdir(daily_dir):
        for fname in os.listdir(daily_dir):
            if "裁决" in fname and fname.endswith(".md"):
                with open(os.path.join(daily_dir, fname), "r", encoding="utf-8") as f:
                    return f.read()

    return ""


def _run_simple_pnl(
    dates: list[str],
    data_dir: str,
    output_dir: str,
    capital: float,
):
    """简化盈亏模式：提取 focus_stocks → D+1 开盘买 → D+2 开盘卖，纯测选股能力"""
    import json
    import re
    from .adapter import CSVStockDataProvider

    print("\n" + "=" * 60)
    print("简化盈亏模式（提到即买，纯测选股）")
    print("=" * 60)

    loader = CSVStockDataProvider()
    pairs = [(dates[i], dates[i + 1]) for i in range(len(dates) - 1)]

    all_trades = []
    equity = capital
    equity_curve = []

    for idx, (day_d, day_d1) in enumerate(pairs):
        day_d2 = pairs[idx + 1][1] if idx + 1 < len(pairs) else None
        report = _load_report(data_dir, output_dir, day_d)

        if not report:
            print("  [跳过] {} 无报告".format(day_d))
            continue

        # 提取 focus_stocks
        stocks = _extract_focus_stocks(report)
        if not stocks:
            print("  [跳过] {} 无 focus_stocks".format(day_d))
            continue

        print("\n模拟 {}/{}: {} → {} | 标的: {}".format(
            idx + 1, len(pairs), day_d, day_d1, ", ".join(stocks)))

        # 等权买入
        per_stock_capital = equity / len(stocks) if stocks else 0
        day_pnl = 0.0
        day_trades = 0

        for stock_name_raw in stocks:
            # 清理名称前缀和标记
            stock_name = re.sub(
                r'^[\d\.\s]*'
                r'|'
                r'^(核心|补涨|备选|观察|试探|补涨)\s*标的\s*[：:**]*\s*'
                r'|'
                r'^\*+'
                r'|'
                r'^[（(）)\s]'
                r'|'
                r'^[-–—\s]+',
                '', stock_name_raw,
            ).strip()
            stock_name = re.sub(r'[，,].*$', '', stock_name).strip()
            if not stock_name or len(stock_name) < 2:
                continue
            # D+1 开盘价买入
            buy_data = loader.load_stock_daily(data_dir, day_d1, stock_name)
            if not buy_data or buy_data.get("open", 0) <= 0:
                print("    {} : 无{}行情数据".format(stock_name, day_d1))
                continue

            buy_price = buy_data["open"]
            shares = int(per_stock_capital / (buy_price * 100)) * 100
            if shares <= 0:
                print("    {} : 资金不足（需{:.0f}，分配{:.0f}）".format(
                    stock_name, buy_price * 100, per_stock_capital))
                continue

            # D+2 开盘价卖出
            if day_d2:
                sell_data = loader.load_stock_daily(data_dir, day_d2, stock_name)
                if sell_data and sell_data.get("open", 0) > 0:
                    sell_price = sell_data["open"]
                    pnl_pct = (sell_price - buy_price) / buy_price * 100
                    pnl_amount = shares * (sell_price - buy_price)
                else:
                    # 无 D+2 数据，用 D+1 收盘价
                    sell_price = buy_data.get("close", buy_price)
                    pnl_pct = (sell_price - buy_price) / buy_price * 100
                    pnl_amount = shares * (sell_price - buy_price)
                    print("    {} : 无{}卖出数据，用收盘价{:.2f}".format(
                        stock_name, day_d2, sell_price))
            else:
                # 最后一天无法卖出
                sell_price = buy_data.get("close", buy_price)
                pnl_pct = (sell_price - buy_price) / buy_price * 100
                pnl_amount = shares * (sell_price - buy_price)

            day_pnl += pnl_amount
            day_trades += 1

            trade_record = {
                "signal_date": day_d,
                "buy_date": day_d1,
                "sell_date": day_d2 or "未卖出",
                "stock_name": stock_name,
                "buy_price": round(buy_price, 2),
                "sell_price": round(sell_price, 2),
                "shares": shares,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_amount": round(pnl_amount, 2),
            }
            all_trades.append(trade_record)
            print("    {} : 买{:.2f} → 卖{:.2f} | {:+.2f}% ({:+.0f}元)".format(
                stock_name, buy_price, sell_price, pnl_pct, pnl_amount))

        equity += day_pnl
        daily_return = day_pnl / capital * 100
        equity_curve.append({
            "date": day_d1,
            "equity": round(equity, 2),
            "daily_return": round(daily_return, 2),
            "trades": day_trades,
        })
        print("  日收益: {:+.2f}% | 净值: {:.0f}".format(daily_return, equity))

    # 汇总报告
    _save_simple_pnl_report(all_trades, equity_curve, capital, output_dir)


def _extract_focus_stocks(report: str) -> list[str]:
    """从报告中提取推荐标的（支持 JSON 和 Markdown 多种格式）

    v2 改进：
    - 支持报告中间的 JSON 块（如 {market_bias, focus_stocks, ...}）
    - 支持"关注标的"节中的 N. **股票名（板块）** 格式
    - 支持"五、明日策略"中的编号列表格式
    - 更精确的板块区域定位
    """
    import json
    import re

    seen = set()

    # Pattern 1: JSON ```json``` block
    json_match = re.search(r'```json\s*\n(.*?)\n```', report, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            stocks = data.get("focus_stocks", [])
            if stocks:
                result = []
                for s in stocks:
                    name = s if isinstance(s, str) else s.get("name", "")
                    if name and len(name) >= 2 and name not in seen:
                        seen.add(name)
                        result.append(name)
                if result:
                    return result
        except json.JSONDecodeError:
            pass

    # Pattern 2: Bare JSON "focus_stocks": [...] (string list)
    json_match = re.search(r'"focus_stocks"\s*:\s*\[(.*?)\]', report)
    if json_match:
        try:
            stocks = json.loads("[" + json_match.group(1) + "]")
            result = []
            for s in stocks:
                name = s if isinstance(s, str) else s.get("name", "")
                if name and len(name) >= 2 and name not in seen:
                    seen.add(name)
                    result.append(name)
            if result:
                return result
        except json.JSONDecodeError:
            pass

    # Pattern 2b: Full JSON block in report body (with market_bias etc.)
    json_match = re.search(r'\{\s*"market_bias".*?"focus_stocks".*?\}', report, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            stocks = data.get("focus_stocks", [])
            if stocks:
                result = []
                for s in stocks:
                    name = s if isinstance(s, str) else s.get("name", "")
                    if name and len(name) >= 2 and name not in seen:
                        seen.add(name)
                        result.append(name)
                if result:
                    return result
        except json.JSONDecodeError:
            pass

    # Pattern 3: 定位策略/操盘计划/关注标的 节
    section = _extract_strategy_section_for_focus(report)
    if section:
        _extract_stocks_from_section(section, seen)
        if seen:
            return list(seen)

    # Pattern 4: Full text fallback — "关注标的" 节
    # 匹配 "- **关注标的**：" 后面的内容
    focus_match = re.search(
        r'关注标的[\s：:]*\n(.*?)(?=\n#{1,3}\s|\n- \*\*仓位|\n- \*\*风险|\Z)',
        report, re.DOTALL,
    )
    if focus_match:
        _extract_stocks_from_section(focus_match.group(1), seen)
        if seen:
            return list(seen)

    # Pattern 5: Full text fallback — pure Chinese name before (code) in buy-related sections
    buy_sections = re.findall(
        r'(?:买入|标的|操盘|推荐|关注|策略)(.*?)(?=\n\n|\Z)',
        report, re.DOTALL,
    )
    text = "\n".join(buy_sections) if buy_sections else report
    for m in re.finditer(r'([\u4e00-\u9fff]{2,6})\s*[（(]\s*(\d{6})', text):
        name = m.group(1).strip()
        if name not in seen:
            seen.add(name)

    return list(seen) if seen else []


def _extract_strategy_section_for_focus(report: str) -> str:
    """定位操盘计划/买入标的节的文本内容"""
    import re

    section_patterns = [
        # 结构化标题
        r'(?:买入标的及操作策略|买入标的及条件|买入标的与操作规则|操盘计划|买入计划)(.*?)(?=\n####[^#]|\n---|\n- \*\*风险|\Z)',
        # 次日操盘计划
        r'(?:次日操盘计划)(.*?)(?=\n---|\n- \*\*风险|\Z)',
        # ## 五、明日策略 后面的内容（到下一个 ## 或文末）
        r'(?:^|\n)#{1,6}\s*(?:五.{0,5}|四.{0,3})?明日策略\s*\n(.*?)(?=\n#{1,3}\s|\Z)',
    ]

    for pat in section_patterns:
        match = re.search(pat, report, re.DOTALL)
        if match:
            return match.group(1)
    return ""


def _extract_stocks_from_section(section: str, seen: set):
    """从节文本中提取股票名（多种格式）"""
    import re

    # 3a: Numbered structured items with 标的 keyword
    for m in re.finditer(
        r'\d+\.\s*\*{1,2}\s*(?:核心|补涨|备选|观察|试探)?\s*标的\s*[：:*]+\s*([\u4e00-\u9fffA-Za-z]{2,8})\s*[（(]\s*(\d{6})',
        section,
    ):
        name = m.group(1).strip()
        if name not in seen:
            seen.add(name)
    if seen:
        return

    # 3b: Sub-heading format "#### N. 股票名（代码）"
    for m in re.finditer(
        r'#{2,4}\s*\d+\.\s*([\u4e00-\u9fff]{2,6})\s*[（(]\s*(\d{6})',
        section,
    ):
        name = m.group(1).strip()
        if name not in seen:
            seen.add(name)
    if seen:
        return

    # 3c: N. **股票名（板块说明）**：操作描述 — 股票名(代码) 在描述中
    # 典型格式：1. **华电辽能**：竞价高开... 或 1. **美诺华（5板，创新药）**：
    for m in re.finditer(
        r'\d+\.\s*\*{1,2}\s*([\u4e00-\u9fff]{2,6})\s*(?:[（(][^）)]*[）)])?\s*\*{1,2}\s*[：:]',
        section,
    ):
        name = m.group(1).strip()
        if name not in seen and re.match(r'^[\u4e00-\u9fff]{2,6}$', name):
            seen.add(name)
    if seen:
        return

    # 3d: Fallback — pure Chinese name (2-6 chars) before (6-digit code)
    for m in re.finditer(r'([\u4e00-\u9fff]{2,6})\s*[（(]\s*(\d{6})', section):
        name = m.group(1).strip()
        if name not in seen:
            seen.add(name)


def _save_simple_pnl_report(
    trades: list[dict],
    equity_curve: list[dict],
    initial_capital: float,
    output_dir: str,
):
    """保存简化盈亏报告"""
    import json

    # 统计
    total = len(trades)
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    win_rate = len(wins) / total * 100 if total > 0 else 0
    avg_pnl = sum(t["pnl_pct"] for t in trades) / total if total > 0 else 0
    final_equity = equity_curve[-1]["equity"] if equity_curve else initial_capital
    total_return = (final_equity - initial_capital) / initial_capital * 100

    # 最大回撤
    peak = initial_capital
    max_dd = 0.0
    for s in equity_curve:
        if s["equity"] > peak:
            peak = s["equity"]
        dd = (peak - s["equity"]) / peak * 100
        if dd > max_dd:
            max_dd = dd

    summary = {
        "initial_capital": initial_capital,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return, 2),
        "total_trades": total,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate, 1),
        "avg_pnl_pct": round(avg_pnl, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss_pct": round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0,
    }

    # 保存 JSON
    result = {"summary": summary, "trades": trades, "equity_curve": equity_curve}
    json_path = os.path.join(output_dir, "simple_pnl.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 保存 Markdown
    md_lines = [
        "# 简化盈亏报告（提到即买模式）",
        "",
        "## 总体表现",
        "",
        "- 初始资金：{:.0f}".format(initial_capital),
        "- 期末净值：{:.2f}".format(final_equity),
        "- 累计收益率：{:+.2f}%".format(total_return),
        "- 最大回撤：{:.2f}%".format(max_dd),
        "",
        "## 交易统计",
        "",
        "| 指标 | 值 |",
        "|------|-----|",
        "| 总交易次数 | {} |".format(total),
        "| 胜率 | {:.1f}%（{}胜{}负）|".format(win_rate, len(wins), len(losses)),
        "| 平均盈亏 | {:+.2f}% |".format(avg_pnl),
        "| 平均盈利 | {:+.2f}% |".format(summary["avg_win_pct"]),
        "| 平均亏损 | {:+.2f}% |".format(summary["avg_loss_pct"]),
        "",
        "## 逐笔明细",
        "",
        "| 信号日 | 买入日 | 卖出日 | 标的 | 买价 | 卖价 | 盈亏% | 盈亏额 |",
        "|--------|--------|--------|------|------|------|-------|--------|",
    ]
    for t in trades:
        md_lines.append("| {} | {} | {} | {} | {:.2f} | {:.2f} | {:+.2f}% | {:+.0f} |".format(
            t["signal_date"], t["buy_date"], t["sell_date"],
            t["stock_name"], t["buy_price"], t["sell_price"],
            t["pnl_pct"], t["pnl_amount"]))

    md_lines.extend([
        "",
        "## 净值曲线",
        "",
        "| 日期 | 净值 | 当日收益 | 交易数 |",
        "|------|------|---------|--------|",
    ])
    for s in equity_curve:
        md_lines.append("| {} | {:.2f} | {:+.2f}% | {} |".format(
            s["date"], s["equity"], s["daily_return"], s["trades"]))

    md_path = os.path.join(output_dir, "simple_pnl_报告.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    print("\n" + "=" * 60)
    print("简化盈亏结果:")
    print("  总交易: {}笔 | 胜率: {:.1f}% | 平均盈亏: {:+.2f}%".format(
        total, win_rate, avg_pnl))
    print("  累计收益: {:+.2f}% | 最大回撤: {:.2f}%".format(total_return, max_dd))
    print("  报告已保存到: {} 和 {}".format(json_path, md_path))


if __name__ == "__main__":
    main()
