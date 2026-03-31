#!/usr/bin/env python3
"""
MootDX 工具 - 通达信数据接口
用法:
  python3 trading/mootdx_tool.py quotes          # 股票池实时行情
  python3 trading/mootdx_tool.py quotes 600396 000966  # 指定股票实时行情
  python3 trading/mootdx_tool.py kline 600396 [days]   # 日K线（默认20天）
  python3 trading/mootdx_tool.py pool-kline [days]     # 股票池全量日K线
  python3 trading/mootdx_tool.py minute 600396         # 当日分时
  python3 trading/mootdx_tool.py bid 600396 000966     # 五档盘口
"""

import sys
import os
import re
import pandas as pd
from mootdx.quotes import Quotes

STOCKS_MD = os.path.join(os.path.dirname(__file__), "stocks.md")


def get_client():
    return Quotes.factory(market="std")


def parse_stocks_md():
    """从 stocks.md 解析股票池，返回 [(name, star, sector), ...]"""
    stocks = []
    current_sector = None
    with open(STOCKS_MD, "r") as f:
        for line in f:
            m = re.match(r"^## (.+?)（", line)
            if m:
                current_sector = m.group(1)
                continue
            m = re.match(r"^\| (.+?) \| (.*?) \|", line)
            if m and current_sector:
                name = m.group(1).strip()
                if name in ("股票", "---", "------"):
                    continue
                star = "⭐" in m.group(2)
                stocks.append((name, star, current_sector))
    return stocks


def _normalize_name(name):
    """统一全角→半角、去空格/空字节、去ST前缀"""
    name = name.strip().replace("\u3000", "").replace(" ", "").replace("\x00", "")
    # 全角字母→半角
    result = []
    for ch in name:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        else:
            result.append(ch)
    return "".join(result)


def build_name_to_code():
    """构建 名称→(code, market) 映射，支持模糊匹配"""
    client = get_client()
    mapping = {}        # 精确名称 → (code, prefix)
    norm_mapping = {}   # 归一化名称 → (code, prefix)
    for market in [0, 1]:  # 0=深市, 1=沪市
        stocks = client.stocks(market=market)
        prefix = "sz" if market == 0 else "sh"
        for _, row in stocks.iterrows():
            name = row["name"].strip()
            code = row["code"]
            mapping[name] = (code, prefix)
            norm = _normalize_name(name)
            norm_mapping[norm] = (code, prefix)
            # 去ST/*ST前缀也存一份
            for pref in ["*ST", "ST"]:
                if norm.startswith(pref):
                    norm_mapping[norm[len(pref):]] = (code, prefix)

    # 返回一个支持模糊查找的wrapper
    class NameMap(dict):
        def __missing__(self, key):
            norm = _normalize_name(key)
            # 精确归一化匹配
            if norm in norm_mapping:
                self[key] = norm_mapping[norm]
                return self[key]
            # 去ST前缀匹配
            for pref in ["*ST", "ST"]:
                if norm.startswith(pref) and norm[len(pref):] in norm_mapping:
                    self[key] = norm_mapping[norm[len(pref):]]
                    return self[key]
            # 包含匹配（A股名称带A/B后缀的情况）
            for n, v in norm_mapping.items():
                if n.startswith(norm) and len(n) - len(norm) <= 1:
                    self[key] = v
                    return self[key]
            raise KeyError(key)

    result = NameMap(mapping)
    return result


def resolve_symbols(names_or_codes):
    """将股票名称或代码统一解析为 code 列表"""
    codes = []
    need_lookup = []
    for item in names_or_codes:
        if re.match(r"^\d{6}$", item):
            codes.append(item)
        elif re.match(r"^(sh|sz)\.\d{6}$", item, re.I):
            codes.append(item.split(".")[1])
        else:
            need_lookup.append(item)

    if need_lookup:
        name_map = build_name_to_code()
        for name in need_lookup:
            try:
                codes.append(name_map[name][0])
            except KeyError:
                print(f"[警告] 未找到股票: {name}", file=sys.stderr)
    return codes


def cmd_quotes(symbols=None):
    """实时行情"""
    client = get_client()

    if not symbols:
        # 从 stocks.md 读取全量
        pool = parse_stocks_md()
        name_map = build_name_to_code()
        symbols = []
        missed = []
        for name, star, sector in pool:
            try:
                symbols.append(name_map[name][0])
            except KeyError:
                missed.append(name)
        if missed:
            print(f"[警告] 未匹配: {', '.join(missed)}", file=sys.stderr)

    if not symbols:
        print("没有可查询的股票")
        return

    # mootdx quotes 一次最多约 80 只
    all_dfs = []
    for i in range(0, len(symbols), 80):
        batch = symbols[i : i + 80]
        df = client.quotes(symbol=batch)
        if df is not None and not df.empty:
            all_dfs.append(df)

    if not all_dfs:
        print("未获取到行情数据")
        return

    df = pd.concat(all_dfs, ignore_index=True)
    # 计算涨跌幅
    df["pctChg"] = ((df["price"] - df["last_close"]) / df["last_close"] * 100).round(2)
    # 成交额转亿
    df["amount_yi"] = (df["amount"] / 1e8).round(2)

    # 获取名称映射
    name_map = build_name_to_code()
    code_to_name = {v[0]: k for k, v in name_map.items()}
    df["name"] = df["code"].map(code_to_name).fillna("")

    result = df[["code", "name", "price", "pctChg", "open", "high", "low", "last_close", "vol", "amount_yi"]].copy()
    result.columns = ["代码", "名称", "现价", "涨跌%", "开盘", "最高", "最低", "昨收", "成交量", "成交额(亿)"]
    result = result.sort_values("涨跌%", ascending=False)

    print(result.to_string(index=False))
    print(f"\n共 {len(result)} 只，涨: {(result['涨跌%'] > 0).sum()}  跌: {(result['涨跌%'] < 0).sum()}  平: {(result['涨跌%'] == 0).sum()}")


def cmd_kline(symbol, days=20):
    """日K线"""
    client = get_client()
    df = client.bars(symbol=symbol, frequency=9, offset=days)
    if df is None or df.empty:
        print(f"未获取到 {symbol} 的K线数据")
        return

    df["pctChg"] = ((df["close"] - df["close"].shift(1)) / df["close"].shift(1) * 100).round(2)
    df["amount_yi"] = (df["amount"] / 1e8).round(2)
    df["date"] = pd.to_datetime(df["datetime"]).dt.strftime("%m-%d")

    result = df[["date", "open", "close", "high", "low", "pctChg", "vol", "amount_yi"]].copy()
    result.columns = ["日期", "开盘", "收盘", "最高", "最低", "涨跌%", "成交量", "成交额(亿)"]
    print(f"=== {symbol} 日K线（最近{days}天）===")
    print(result.to_string(index=False))


def cmd_pool_kline(days=20):
    """股票池全量日K线，输出CSV"""
    client = get_client()
    pool = parse_stocks_md()
    name_map = build_name_to_code()

    all_data = []
    for name, star, sector in pool:
        try:
            code, prefix = name_map[name]
        except KeyError:
            continue
        df = client.bars(symbol=code, frequency=9, offset=days)
        if df is None or df.empty:
            continue
        df["code"] = f"{prefix}.{code}"
        df["名称"] = name
        df["板块"] = sector
        df["date"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d")
        df["pctChg"] = ((df["close"] - df["close"].shift(1)) / df["close"].shift(1) * 100).round(2)
        all_data.append(df)

    if not all_data:
        print("未获取到数据")
        return

    merged = pd.concat(all_data, ignore_index=True)
    result = merged[["date", "code", "名称", "open", "high", "low", "close", "vol", "amount", "pctChg", "板块"]].copy()
    result.columns = ["date", "code", "名称", "open", "high", "low", "close", "volume", "amount", "pctChg", "板块"]

    outfile = f"trading/daily/pool_kline_{days}d.csv"
    result.to_csv(outfile, index=False, encoding="utf-8-sig")
    print(f"已输出 {len(result)} 条记录到 {outfile}")
    print(f"覆盖 {result['名称'].nunique()} 只股票，{result['date'].nunique()} 个交易日")


def cmd_minute(symbol):
    """分时数据"""
    client = get_client()
    df = client.minute(symbol=symbol)
    if df is None or df.empty:
        print(f"未获取到 {symbol} 的分时数据")
        return
    print(f"=== {symbol} 当日分时 ===")
    print(df.to_string())


def cmd_bid(symbols):
    """五档盘口"""
    client = get_client()
    df = client.quotes(symbol=symbols)
    if df is None or df.empty:
        print("未获取到盘口数据")
        return

    name_map = build_name_to_code()
    code_to_name = {v[0]: k for k, v in name_map.items()}

    for _, row in df.iterrows():
        name = code_to_name.get(row["code"], row["code"])
        pct = (row["price"] - row["last_close"]) / row["last_close"] * 100
        print(f"\n{'='*40}")
        print(f"{name} ({row['code']})  现价: {row['price']}  涨跌: {pct:.2f}%")
        print(f"{'─'*40}")
        for i in range(5, 0, -1):
            print(f"  卖{i}  {row[f'ask{i}']:.2f}  {int(row[f'ask_vol{i}'])}")
        print(f"  {'─'*30}")
        for i in range(1, 6):
            print(f"  买{i}  {row[f'bid{i}']:.2f}  {int(row[f'bid_vol{i}'])}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "quotes":
        symbols = sys.argv[2:] if len(sys.argv) > 2 else None
        if symbols:
            symbols = resolve_symbols(symbols)
        cmd_quotes(symbols)
    elif cmd == "kline":
        if len(sys.argv) < 3:
            print("用法: mootdx_tool.py kline <code> [days]")
            sys.exit(1)
        days = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        cmd_kline(sys.argv[2], days)
    elif cmd == "pool-kline":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        cmd_pool_kline(days)
    elif cmd == "minute":
        if len(sys.argv) < 3:
            print("用法: mootdx_tool.py minute <code>")
            sys.exit(1)
        cmd_minute(sys.argv[2])
    elif cmd == "bid":
        if len(sys.argv) < 3:
            print("用法: mootdx_tool.py bid <code1> [code2] ...")
            sys.exit(1)
        cmd_bid(sys.argv[2:])
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
