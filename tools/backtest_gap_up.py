#!/usr/bin/env python3
"""全市场「高开过顶」策略回测"""

import os
import pandas as pd
import numpy as np
from mootdx.quotes import Quotes
from tqdm import tqdm
import warnings
warnings.filterwarnings('ignore')

def get_all_a_stocks(client):
    """获取全部A股代码列表"""
    stocks_all = []
    for market in [0, 1]:
        s = client.stocks(market=market)
        if s is not None and len(s) > 0:
            s['market'] = market
            stocks_all.append(s)
    df = pd.concat(stocks_all, ignore_index=True)
    # 过滤A股: 00/30/60/68 开头
    a = df[df['code'].str.match(r'^(00|30|60|68)\d{4}$')].copy()
    # 排除ST
    a = a[~a['name'].str.contains('ST|退', na=False)]
    return a

def fetch_klines(client, stocks_df, days=15):
    """批量拉取日K线"""
    all_data = []
    for _, row in tqdm(stocks_df.iterrows(), total=len(stocks_df), desc="拉取K线"):
        try:
            market = row['market']
            code = row['code']
            bars = client.bars(symbol=code, frequency=9, offset=days)
            if bars is not None and len(bars) > 0:
                bars = bars.copy()
                bars['code'] = code
                bars['name'] = row['name'].strip()
                bars['market'] = market
                all_data.append(bars)
        except Exception:
            continue
    if not all_data:
        return pd.DataFrame()
    df = pd.concat(all_data, ignore_index=True)
    df['date'] = pd.to_datetime(df['datetime'].str[:10])
    return df

def run_backtest(df, n_trading_days=7):
    """运行高开过顶回测"""
    df = df.sort_values(['code', 'date']).reset_index(drop=True)

    # 计算前一日最高价和次日开盘价
    df['prev_high'] = df.groupby('code')['high'].shift(1)
    df['prev_close'] = df.groupby('code')['close'].shift(1)
    df['next_open'] = df.groupby('code')['open'].shift(-1)
    df['next_date'] = df.groupby('code')['date'].shift(-1)

    # 最近N个交易日
    trading_days = sorted(df['date'].unique())
    last_n = trading_days[-n_trading_days:]

    # 高开过顶条件: open > prev_high，且有次日数据
    signals = df[
        (df['date'].isin(last_n)) &
        (df['open'] > df['prev_high']) &
        df['next_open'].notna() &
        df['prev_high'].notna()
    ].copy()

    # 排除一字涨停买不到的情况: open == high == close (涨停价)
    # 判断涨停: 涨幅接近10%或20%
    signals['open_pct'] = (signals['open'] / signals['prev_close'] - 1) * 100
    # 一字板: open == high (无法买入)
    signals = signals[signals['open'] != signals['high']].copy()

    # 计算盈亏
    signals['profit_pct'] = ((signals['next_open'] - signals['open']) / signals['open'] * 100).round(2)
    signals['gap_pct'] = ((signals['open'] / signals['prev_high'] - 1) * 100).round(2)

    return signals, last_n

def main():
    client = Quotes.factory(market='std')

    print("=" * 70)
    print("全市场「高开过顶」策略回测")
    print("规则: 开盘价 > 前一日最高价 → 开盘买入，次日开盘卖出")
    print("排除: 一字涨停（无法买入）、ST股")
    print("=" * 70)

    # 获取股票列表
    stocks = get_all_a_stocks(client)
    print(f"\nA股标的数: {len(stocks)} (已排除ST)")

    # 拉取K线 (15天覆盖7+交易日)
    df = fetch_klines(client, stocks, days=15)
    print(f"K线数据: {len(df)} 条, 覆盖 {df['code'].nunique()} 只股票")

    trading_days = sorted(df['date'].unique())
    print(f"交易日: {[str(d)[:10] for d in trading_days]}")

    # 运行回测
    signals, last_n = run_backtest(df, n_trading_days=7)
    print(f"\n回测区间: {str(last_n[0])[:10]} ~ {str(last_n[-1])[:10]}")
    print(f"共触发 {len(signals)} 笔信号\n")

    # 每日明细
    for date_val in last_n:
        day = signals[signals['date'] == date_val].sort_values('profit_pct', ascending=False)
        if len(day) == 0:
            print(f"=== {str(date_val)[:10]} === 无信号")
            continue

        wins = (day['profit_pct'] > 0).sum()
        avg = day['profit_pct'].mean()
        print(f"=== {str(date_val)[:10]} === {len(day)}笔, 胜率 {wins}/{len(day)} ({wins/len(day)*100:.0f}%), 平均 {avg:+.2f}%")

        # 显示盈利前5和亏损前5
        top5 = day.head(5)
        bottom5 = day.tail(5)

        print(f"  【盈利TOP5】")
        for _, r in top5.iterrows():
            print(f"    ✅ {r['name']:8s} ({r['code']}) | 高开{r['gap_pct']:+.1f}% | 买{r['open']:.2f}→卖{r['next_open']:.2f} | {r['profit_pct']:+.2f}%")

        if len(day) > 10:
            print(f"  ... 省略 {len(day)-10} 笔 ...")

        print(f"  【亏损TOP5】")
        for _, r in bottom5.iterrows():
            emoji = '✅' if r['profit_pct'] > 0 else '❌'
            print(f"    {emoji} {r['name']:8s} ({r['code']}) | 高开{r['gap_pct']:+.1f}% | 买{r['open']:.2f}→卖{r['next_open']:.2f} | {r['profit_pct']:+.2f}%")
        print()

    # 汇总统计
    total = len(signals)
    wins = (signals['profit_pct'] > 0).sum()
    losses = (signals['profit_pct'] < 0).sum()
    avg_profit = signals['profit_pct'].mean()
    median_profit = signals['profit_pct'].median()
    max_win = signals['profit_pct'].max()
    max_loss = signals['profit_pct'].min()
    avg_win = signals[signals['profit_pct'] > 0]['profit_pct'].mean() if wins > 0 else 0
    avg_loss = signals[signals['profit_pct'] < 0]['profit_pct'].mean() if losses > 0 else 0

    print("=" * 70)
    print("汇总统计")
    print("=" * 70)
    print(f"  总交易笔数: {total}")
    print(f"  胜率: {wins}/{total} = {wins/total*100:.1f}%")
    print(f"  平均盈亏: {avg_profit:+.2f}%")
    print(f"  中位数盈亏: {median_profit:+.2f}%")
    print(f"  最大盈利: {max_win:+.2f}%")
    print(f"  最大亏损: {max_loss:+.2f}%")
    print(f"  平均盈利(赢): {avg_win:+.2f}%")
    print(f"  平均亏损(亏): {avg_loss:+.2f}%")
    if avg_loss != 0:
        print(f"  盈亏比: {abs(avg_win/avg_loss):.2f}")

    # 按高开幅度分组分析
    print(f"\n{'='*70}")
    print("按高开幅度分组")
    print("="*70)
    bins = [0, 1, 2, 3, 5, 10, 100]
    labels = ['0-1%', '1-2%', '2-3%', '3-5%', '5-10%', '>10%']
    signals['gap_group'] = pd.cut(signals['gap_pct'], bins=bins, labels=labels, right=False)
    for label in labels:
        g = signals[signals['gap_group'] == label]
        if len(g) == 0:
            continue
        w = (g['profit_pct'] > 0).sum()
        print(f"  {label:6s}: {len(g):4d}笔, 胜率 {w}/{len(g)} ({w/len(g)*100:.0f}%), 平均盈亏 {g['profit_pct'].mean():+.2f}%")

    # 保存完整信号到CSV
    output_cols = ['date', 'code', 'name', 'open', 'high', 'close', 'prev_high', 'next_open', 'gap_pct', 'profit_pct']
    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "daily")
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, "backtest_gap_up_full.csv")
    signals[output_cols].sort_values(['date', 'profit_pct'], ascending=[True, False]).to_csv(
        output_file, index=False, encoding='utf-8-sig'
    )
    print(f"\n完整信号已保存到 {output_file}")

if __name__ == '__main__':
    main()
