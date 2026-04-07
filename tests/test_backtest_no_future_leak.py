"""回测数据泄露防护测试

确保所有检索工具在回测模式下不能访问超出 backtest_max_date 的数据。
新增工具时必须在此文件添加对应的测试用例，CI 中通过 pytest 执行。
"""

import os
import tempfile
import shutil

import pytest

from trading_agent.review.tools.retrieval import RetrievalToolFactory


# ── 测试数据准备 ──────────────────────────────────────────────

BACKTEST_DATE = "2026-03-20"
FUTURE_DATE = "2026-04-05"
PAST_DATE = "2026-03-19"


@pytest.fixture()
def sandbox():
    """创建一个包含过去和未来日期数据的临时目录，模拟真实数据结构。"""
    root = tempfile.mkdtemp(prefix="backtest_leak_test_")
    data_dir = os.path.join(root, "data")
    memory_dir = os.path.join(root, "memory")

    # 创建过去日期的数据
    for date in [PAST_DATE, BACKTEST_DATE, FUTURE_DATE]:
        daily = os.path.join(data_dir, "daily", date)
        review = os.path.join(daily, "review_docs")
        os.makedirs(review, exist_ok=True)

        # 复盘文档
        with open(os.path.join(review, "测试博主.md"), "w") as f:
            f.write(f"# {date} 复盘\n这是 {date} 的复盘内容")

        # 裁决报告
        with open(os.path.join(daily, "agent_05_裁决报告.md"), "w") as f:
            f.write(f"# {date} 裁决报告\n推荐标的：测试股票")

        # 指数数据（CSV）
        with open(os.path.join(daily, "index_data.csv"), "w") as f:
            f.write("code,name,close,pctChg\n000001,上证指数,3200,0.5")

        # 资金流数据
        with open(os.path.join(daily, "capital_flow.csv"), "w") as f:
            f.write("sector,net_inflow\n科技,1000000")

        # 记忆文件
        os.makedirs(memory_dir, exist_ok=True)
        with open(os.path.join(memory_dir, f"{date}.md"), "w") as f:
            f.write(f"# {date} 记忆\n今日情绪：乐观")

    yield {"data_dir": data_dir, "memory_dir": memory_dir}

    shutil.rmtree(root)


def _make_factory(sandbox, backtest_max_date=BACKTEST_DATE):
    """创建回测模式的 factory。"""
    return RetrievalToolFactory(
        data_dir=sandbox["data_dir"],
        date=BACKTEST_DATE,
        memory_dir=sandbox["memory_dir"],
        backtest_max_date=backtest_max_date,
    )


def _invoke_tool(tools, tool_name, **kwargs):
    """按名称查找并调用工具。"""
    for t in tools:
        if t.name == tool_name:
            return t.invoke(kwargs)
    raise ValueError(f"工具 {tool_name} 不存在")


# ── 核心测试：未来日期必须被拦截 ───────────────────────────────

class TestFutureDataBlocked:
    """验证每个接受日期参数的工具都会拦截未来日期。"""

    def test_get_review_docs_blocks_future(self, sandbox):
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_review_docs", date=FUTURE_DATE)
        assert "超出可查询范围" in result
        assert FUTURE_DATE in result

    def test_get_memory_blocks_future(self, sandbox):
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_memory", date=FUTURE_DATE)
        assert "超出可查询范围" in result

    def test_get_index_data_blocks_future(self, sandbox):
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_index_data", date=FUTURE_DATE)
        assert "超出可查询范围" in result

    def test_get_capital_flow_blocks_future(self, sandbox):
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_capital_flow", date=FUTURE_DATE)
        assert "超出可查询范围" in result

    def test_get_stock_detail_blocks_future(self, sandbox):
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_stock_detail", name="测试", date=FUTURE_DATE)
        assert "超出可查询范围" in result

    def test_get_past_report_blocks_future(self, sandbox):
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_past_report", date=FUTURE_DATE)
        # get_past_report 有两层检查：>= factory.date 和 backtest_max_date
        assert "分析日之前" in result or "超出可查询范围" in result

    def test_get_market_data_blocks_future(self, sandbox):
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_market_data", date=FUTURE_DATE)
        assert "超出可查询范围" in result


# ── 合法日期必须正常返回 ──────────────────────────────────────

class TestPastDataAllowed:
    """验证 <= backtest_max_date 的请求正常返回数据。"""

    def test_get_review_docs_allows_past(self, sandbox):
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_review_docs", date=PAST_DATE)
        assert "超出可查询范围" not in result
        assert "复盘" in result

    def test_get_review_docs_allows_current(self, sandbox):
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_review_docs", date=BACKTEST_DATE)
        assert "超出可查询范围" not in result

    def test_get_memory_allows_past(self, sandbox):
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_memory", date=PAST_DATE)
        assert "超出可查询范围" not in result
        assert "记忆" in result

    def test_get_memory_days_back_works(self, sandbox):
        """days_back 模式不传日期，应该正常工作。"""
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_memory", days_back=5)
        assert "超出可查询范围" not in result

    def test_get_index_data_allows_current(self, sandbox):
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_index_data", date=BACKTEST_DATE)
        assert "超出可查询范围" not in result

    def test_get_capital_flow_allows_current(self, sandbox):
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_capital_flow", date=BACKTEST_DATE)
        assert "超出可查询范围" not in result


# ── 边界条件 ──────────────────────────────────────────────────

class TestBoundaryConditions:
    """边界日期和非回测模式测试。"""

    def test_no_backtest_mode_uses_date_as_boundary(self, sandbox):
        """非回测模式（backtest_max_date=None）以 factory.date 为上界。"""
        factory = RetrievalToolFactory(
            data_dir=sandbox["data_dir"],
            date=BACKTEST_DATE,
            memory_dir=sandbox["memory_dir"],
            backtest_max_date=None,  # 非回测模式
        )
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_review_docs", date=FUTURE_DATE)
        assert "超出可查询范围" in result

    def test_exact_boundary_date_allowed(self, sandbox):
        """恰好等于 backtest_max_date 的请求应允许。"""
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_review_docs", date=BACKTEST_DATE)
        assert "超出可查询范围" not in result

    def test_day_after_boundary_blocked(self, sandbox):
        """backtest_max_date 后一天必须被拦截。"""
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        result = _invoke_tool(tools, "get_review_docs", date="2026-03-21")
        assert "超出可查询范围" in result


# ── 完备性检查 ────────────────────────────────────────────────

class TestToolCompleteness:
    """确保所有工具都被测试覆盖，防止新增工具遗漏。"""

    # 不接受日期参数的工具（无需日期边界检查）
    DATE_FREE_TOOLS = {"get_history_data", "get_lessons", "get_prev_report",
                       "get_quant_rules", "scan_trend_stocks"}

    # 接受日期参数且需要边界检查的工具
    DATE_BOUND_TOOLS = {"get_review_docs", "get_memory", "get_index_data",
                        "get_capital_flow", "get_stock_detail", "get_past_report",
                        "get_market_data"}

    def test_all_tools_accounted_for(self, sandbox):
        """所有工具要么在 DATE_FREE 要么在 DATE_BOUND 中，不能遗漏。"""
        factory = _make_factory(sandbox)
        tools = factory.create_tools()
        all_names = {t.name for t in tools}
        covered = self.DATE_FREE_TOOLS | self.DATE_BOUND_TOOLS
        uncovered = all_names - covered
        assert not uncovered, (
            f"发现未覆盖的工具: {uncovered}。"
            f"请在 DATE_FREE_TOOLS 或 DATE_BOUND_TOOLS 中注册，"
            f"并为 DATE_BOUND 工具添加泄露测试。"
        )
