#!/usr/bin/env python3
"""
构建全市场个股→概念板块映射表（完整版）
方案：遍历每只股票的同花顺个股页面，获取"涉及概念"字段
数据源：stockpage.10jqka.com.cn/{code}/
"""
import os
import re
import json
import time
import sqlite3
import sys
import requests
from bs4 import BeautifulSoup
from datetime import datetime

DATA_DIR = os.environ.get('TRADING_DATA_DIR', os.path.join(os.path.dirname(__file__), '..', 'trading'))
DB_PATH = os.path.join(DATA_DIR, 'stock_concept.db')
INTRADAY_DB = os.path.join(DATA_DIR, 'intraday', 'intraday.db')


def get_all_stock_codes(intraday_db):
    """从 intraday.db 获取全市场股票代码和名称"""
    conn = sqlite3.connect(intraday_db)
    c = conn.cursor()
    try:
        c.execute("SELECT code, name FROM snapshots WHERE code NOT LIKE '688%' GROUP BY code")
    except:
        # fallback: 只获取主板的
        c.execute("SELECT DISTINCT code, name FROM snapshots WHERE length(code)=6")
    rows = c.fetchall()
    conn.close()

    # 过滤：只要6位纯数字且不是北交所(8/4开头)、不是指数
    codes = {}
    for code, name in rows:
        if not code or not name:
            continue
        code = code.strip()
        if len(code) != 6 or not code.isdigit():
            continue
        # 排除指数、基金等
        if code.startswith(('0000', '399')):  # 指数
            continue
        codes[code] = name

    return codes


def get_stock_concepts(session, code):
    """从同花顺个股页面获取概念板块列表"""
    url = f"https://stockpage.10jqka.com.cn/{code}/"
    try:
        r = session.get(url, timeout=10)
        if r.status_code != 200:
            return None, None

        soup = BeautifulSoup(r.text, 'lxml')
        concepts = []
        industry = ""

        for dt in soup.find_all('dt'):
            dt_text = dt.get_text(strip=True)
            if '涉及概念' in dt_text:
                dd = dt.find_next_sibling('dd')
                if dd:
                    title = dd.get('title', '')
                    text = dd.get_text(strip=True)
                    raw = title if title else text
                    if raw and raw != '--':
                        concepts = [c.strip() for c in raw.split('，') if c.strip() and c.strip() != '--']
            elif '所属行业' in dt_text:
                dd = dt.find_next_sibling('dd')
                if dd:
                    industry = dd.get_text(strip=True).replace('---', '').strip()

        return concepts, industry
    except Exception as e:
        return None, None


def save_to_db(stock_data, db_path):
    """保存到 SQLite"""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # 股票→概念映射表
    c.execute("DROP TABLE IF EXISTS stock_concepts")
    c.execute('''CREATE TABLE stock_concepts (
        code TEXT PRIMARY KEY,
        name TEXT,
        industry TEXT,
        concepts TEXT,
        concept_count INTEGER,
        updated_at TEXT
    )''')

    today = datetime.now().strftime('%Y-%m-%d')
    for code in sorted(stock_data.keys()):
        data = stock_data[code]
        concepts = data.get('concepts', [])
        industry = data.get('industry', '')
        name = data.get('name', '')
        c.execute(
            "INSERT INTO stock_concepts (code, name, industry, concepts, concept_count, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (code, name, industry, json.dumps(concepts, ensure_ascii=False), len(concepts), today)
        )

    # 概念→股票反向映射表
    concept_map = {}
    for code, data in stock_data.items():
        for concept in data.get('concepts', []):
            if concept not in concept_map:
                concept_map[concept] = []
            concept_map[concept].append(code)

    c.execute('''CREATE TABLE IF NOT EXISTS concept_stocks (
        concept_name TEXT PRIMARY KEY,
        stock_codes TEXT,
        stock_count INTEGER,
        updated_at TEXT
    )''')
    c.execute("DELETE FROM concept_stocks")

    for concept_name in sorted(concept_map.keys()):
        stocks = sorted(set(concept_map[concept_name]))
        c.execute(
            "INSERT INTO concept_stocks (concept_name, stock_codes, stock_count, updated_at) VALUES (?, ?, ?, ?)",
            (concept_name, json.dumps(stocks, ensure_ascii=False), len(stocks), today)
        )

    c.execute("CREATE INDEX IF NOT EXISTS idx_sc_code ON stock_concepts(code)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cs_name ON concept_stocks(concept_name)")
    conn.commit()

    # 统计
    c.execute("SELECT COUNT(*) FROM stock_concepts WHERE concept_count > 0")
    with_data = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT json_each.value) FROM stock_concepts, json_each(concepts)")
    total_unique = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM concept_stocks")
    total_boards = c.fetchone()[0]
    conn.close()

    return with_data, total_unique, total_boards


def main():
    start = time.time()
    print("=" * 60)
    print("全市场个股→概念板块映射表（完整版）")
    print("=" * 60)

    # 1. 获取全市场股票列表
    print(f"\n[1/3] 从 intraday.db 获取股票列表...")
    stock_codes = get_all_stock_codes(INTRADAY_DB)
    print(f"  全市场股票数: {len(stock_codes)}")

    if not stock_codes:
        print("ERROR: 未获取到股票列表")
        sys.exit(1)

    # 2. 遍历每只股票获取概念
    print(f"\n[2/3] 从同花顺获取概念板块...")
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://q.10jqka.com.cn/",
    })

    stock_data = {}
    failed = []
    no_concept = 0

    for i, (code, name) in enumerate(sorted(stock_codes.items())):
        concepts, industry = get_stock_concepts(session, code)

        if concepts is None:
            failed.append(code)
            if (i + 1) % 100 == 0 or i < 10:
                print(f"  [{i+1}/{len(stock_codes)}] {code} {name}: 请求失败")
        elif len(concepts) == 0:
            no_concept += 1
            stock_data[code] = {'name': name, 'concepts': [], 'industry': industry or ''}
        else:
            stock_data[code] = {'name': name, 'concepts': concepts, 'industry': industry or ''}

        # 进度报告
        if (i + 1) % 100 == 0:
            elapsed = time.time() - start
            eta = elapsed / (i + 1) * (len(stock_codes) - i - 1)
            with_concept = sum(1 for d in stock_data.values() if d['concepts'])
            print(f"  [{i+1}/{len(stock_codes)}] 已完成, 有概念={with_concept}, 失败={len(failed)}, 耗时={elapsed:.0f}s, 剩余={eta:.0f}s")

        time.sleep(0.15)

    # 3. 保存
    print(f"\n[3/3] 保存到数据库: {DB_PATH}")
    with_data, unique_concepts, total_boards = save_to_db(stock_data, DB_PATH)

    elapsed = time.time() - start
    print(f"\n{'=' * 60}")
    print(f"完成! 耗时 {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"  股票总数: {len(stock_data)}")
    print(f"  有概念数据: {with_data}")
    print(f"  无概念: {no_concept}")
    print(f"  请求失败: {len(failed)}")
    print(f"  唯一概念: {unique_concepts} 个")
    print(f"  概念板块: {total_boards} 个")

    # 验证关键股票
    print(f"\n验证:")
    for check_code in ['300059', '000001', '002594', '601318']:
        if check_code in stock_data:
            d = stock_data[check_code]
            print(f"  {check_code} {d['name']}: {d['concepts'][:5]}{'...' if len(d['concepts'])>5 else ''} ({len(d['concepts'])}个)")


if __name__ == '__main__':
    main()
