/**
 * 检索工具 — 移植自 Python retrieval.py
 * 提供 10 个工具定义 + 实现，供 OpenAI function calling 使用
 */
import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';
import Database from 'better-sqlite3';
import { getDataDir } from './config.mjs';

const DATA_DIR = getDataDir();
const MEMORY_DIR = path.resolve(DATA_DIR, '../../memory/main');
const KNOWLEDGE_DIR = path.join(DATA_DIR, 'knowledge');

// ─── Helper ────────────────────────────────────────

function readCsv(filePath) {
  if (!fs.existsSync(filePath)) return null;
  const raw = fs.readFileSync(filePath, 'utf8');
  const lines = raw.trim().split('\n');
  if (lines.length < 2) return [];
  const headers = lines[0].split(',').map(h => h.trim().replace(/^\uFEFF/, ''));
  return lines.slice(1).map(line => {
    const vals = line.split(',');
    const row = {};
    headers.forEach((h, i) => row[h] = (vals[i] || '').trim());
    return row;
  });
}

function readMd(filePath) {
  if (!fs.existsSync(filePath)) return null;
  return fs.readFileSync(filePath, 'utf8');
}

function today() {
  // Use Asia/Shanghai timezone (UTC+8) instead of UTC
  return new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' });
}

function compactDate(d) {
  return d.replace(/-/g, '');
}

function fmtPct(v) {
  if (v == null) return '-';
  const n = parseFloat(v);
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}

function fmtAmt(v) {
  if (v == null) return '-';
  return parseFloat(v).toFixed(2);
}

function fmtPrice(v) {
  if (v == null) return '-';
  return parseFloat(v).toFixed(2);
}

// ─── Tool definitions (OpenAI function format) ─────

export const toolDefinitions = [
  {
    type: 'function',
    function: {
      name: 'get_history_data',
      description: '获取近几日的情绪数据对比（涨停数、跌停数、连板梯队、炸板率等）',
      parameters: {
        type: 'object',
        properties: {
          days_back: { type: 'number', description: '回溯天数，默认7，最大14', default: 7 },
        },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_review_docs',
      description: '获取复盘文档（博主复盘、分析笔记等 markdown 文件）',
      parameters: {
        type: 'object',
        properties: {
          date: { type: 'string', description: '日期 YYYY-MM-DD，默认今天' },
          reviewer: { type: 'string', description: '按文件名筛选（子串匹配）' },
        },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_memory',
      description: '获取近期每日行情认知（memory 文件）',
      parameters: {
        type: 'object',
        properties: {
          days_back: { type: 'number', description: '回溯天数，默认3', default: 3 },
        },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_lessons',
      description: '获取历史经验教训',
      parameters: { type: 'object', properties: {} },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_prev_report',
      description: '获取昨日 Agent 裁决报告',
      parameters: {
        type: 'object',
        properties: {
          date: { type: 'string', description: '日期，默认昨天' },
        },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_index_data',
      description: '获取指数行情数据（上证、深证、创业板等的收盘价、涨跌幅、成交额）',
      parameters: {
        type: 'object',
        properties: {
          date: { type: 'string', description: '日期 YYYY-MM-DD，默认今天' },
        },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_capital_flow',
      description: '获取资金流数据（板块资金流向）',
      parameters: {
        type: 'object',
        properties: {
          date: { type: 'string', description: '日期 YYYY-MM-DD，默认今天' },
        },
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_quant_rules',
      description: '获取量化选股规则和规律',
      parameters: { type: 'object', properties: {} },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_stock_detail',
      description: '从 intraday 数据库查询个股详细行情（分时快照）',
      parameters: {
        type: 'object',
        properties: {
          name: { type: 'string', description: '股票名称（模糊匹配）' },
          code: { type: 'string', description: '股票代码（精确匹配）' },
          date: { type: 'string', description: '日期，默认今天' },
        },
        required: [],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_past_report',
      description: '获取任意历史日期的 Agent 报告',
      parameters: {
        type: 'object',
        properties: {
          date: { type: 'string', description: '日期 YYYY-MM-DD' },
        },
        required: ['date'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'get_market_data',
      description: '获取行情快照数据（支持按日期+时间查询）。可查市场概览、股票池行情、个股详情。数据源优先从本地 SQLite，不存在时自动从通达信接口实时拉取。',
      parameters: {
        type: 'object',
        properties: {
          date: { type: 'string', description: '日期 YYYY-MM-DD，默认今天' },
          time: { type: 'string', description: '时间点，如 "09:25"、"10:00"、"11:30"、"close"。默认返回该日最新可用快照。历史数据支持的时间取决于已拉取的快照。' },
          name: { type: 'string', description: '股票名称（模糊匹配，可选）' },
          code: { type: 'string', description: '股票代码（精确匹配，可选）' },
          mode: { type: 'string', enum: ['overview', 'stock', 'pool'], description: 'overview=市场概览（涨幅TOP+跌幅TOP+涨停统计），stock=个股详情，pool=股票池行情。默认overview' },
          sort_by: { type: 'string', enum: ['pctChg', 'amount', 'volume'], description: '排序字段，默认 pctChg' },
          top_n: { type: 'number', description: '返回数量（个股模式默认5，概览模式默认10）' },
        },
      },
    },
  },
];

// ─── Tool implementations ──────────────────────────

const _cache = new Map();

function cached(key, fn) {
  if (!_cache.has(key)) _cache.set(key, fn());
  return _cache.get(key);
}

const toolHandlers = {
  get_history_data({ days_back = 7 }) {
    const d = today();
    days_back = Math.min(days_back, 14);
    return cached(('hist:' + d + ':' + days_back), () => {
      const lines = ['## 近期情绪数据对比', '| 日期 | 涨停数 | 跌停数 | 最高连板 | 炸板率 |', '|------|--------|--------|---------|--------|'];
      for (let i = 0; i < days_back; i++) {
        const dt = new Date(Date.now() - i * 86400000);
        const ds = dt.toISOString().slice(0, 10);
        const dc = compactDate(ds);
        const dailyDir = path.join(DATA_DIR, 'daily', ds);
        if (!fs.existsSync(dailyDir)) continue;

        // Read limit-up CSV
        const limitUpPath = path.join(dailyDir, `涨停板_${dc}.csv`);
        const limitDownPath = path.join(dailyDir, `跌停板_${dc}.csv`);
        const limitUp = readCsv(limitUpPath);
        const limitDown = readCsv(limitDownPath);
        const upCount = limitUp ? limitUp.length : 0;
        const downCount = limitDown ? limitDown.length : 0;

        // Max board from limit-up data
        let maxBoard = 0;
        if (limitUp) {
          for (const row of limitUp) {
            const bd = parseInt(row.连板数 || row.连续涨停天数 || '0');
            if (bd > maxBoard) maxBoard = bd;
          }
        }

        lines.push(`| ${ds} | ${upCount} | ${downCount} | ${maxBoard}板 | - |`);

        // 连板梯队详情（≥2板的个股）
        if (limitUp && maxBoard >= 2) {
          const tiers = {};
          for (const row of limitUp) {
            const bd = parseInt(row.连板数 || row.连续涨停天数 || '1');
            if (bd >= 2) {
              if (!tiers[bd]) tiers[bd] = [];
              const stockName = (row['名称'] || row['股票名称'] || '').trim();
              const stockCode = (row['代码'] || row['股票代码'] || '').trim();
              tiers[bd].push(`${stockName}（${stockCode}）`);
            }
          }
          const sortedTiers = Object.keys(tiers).sort((a, b) => b - a);
          for (const bd of sortedTiers) {
            lines.push(`  ${bd}板：${tiers[bd].join('、')}`);
          }
        }
      }
      return lines.length > 3 ? lines.join('\n') : '无数据';
    });
  },

  get_review_docs({ date, reviewer } = {}) {
    const ds = date || today();
    const dailyDir = path.join(DATA_DIR, 'daily', ds);
    if (!fs.existsSync(dailyDir)) return `无数据（目录不存在: ${ds}）`;

    const reviewDir = path.join(dailyDir, 'review_docs');
    let searchPaths = [];
    if (fs.existsSync(reviewDir)) {
      searchPaths = fs.readdirSync(reviewDir).filter(f => f.endsWith('.md')).map(f => path.join(reviewDir, f));
    } else {
      // 兼容旧目录
      searchPaths = fs.readdirSync(dailyDir).filter(f => f.includes('复盘') && f.endsWith('.md')).map(f => path.join(dailyDir, f));
    }

    if (reviewer) searchPaths = searchPaths.filter(f => path.basename(f).includes(reviewer));

    const parts = [];
    for (const p of searchPaths) {
      const content = readMd(p);
      if (content) parts.push(`### ${path.basename(p)}\n${content}`);
    }
    return parts.length > 0 ? parts.join('\n\n') : '无复盘文档';
  },

  get_memory({ days_back = 3 } = {}) {
    const parts = [];
    for (let i = 0; i < days_back; i++) {
      const dt = new Date(Date.now() - i * 86400000);
      const ds = dt.toISOString().slice(0, 10);
      const mp = path.join(MEMORY_DIR, `${ds}.md`);
      const content = readMd(mp);
      if (content) parts.push(`## ${ds}\n${content}`);
    }
    return parts.length > 0 ? parts.join('\n\n') : '无记忆数据';
  },

  get_lessons() {
    return cached('lessons', () => {
      const fp = path.join(KNOWLEDGE_DIR, '项目数据导出_0309-0324.md');
      const content = readMd(fp);
      return content || '无经验教训数据';
    });
  },

  get_prev_report({ date } = {}) {
    const ds = date || (() => {
      const dt = new Date(Date.now() - 86400000);
      return dt.toISOString().slice(0, 10);
    })();
    const fp = path.join(DATA_DIR, 'daily', ds, 'agent_05_裁决报告.md');
    const content = readMd(fp);
    return content || `无报告（${ds}）`;
  },

  get_index_data({ date } = {}) {
    const ds = date || today();
    const dc = compactDate(ds);
    const fp = path.join(DATA_DIR, 'daily', ds, `指数_${dc}.csv`);
    const rows = readCsv(fp);
    if (!rows || rows.length === 0) return '无指数数据';

    const lines = ['## 指数行情', '| 指数 | 收盘 | 涨跌幅 | 成交额(亿) |', '|------|------|--------|-----------|'];
    for (const r of rows) {
      const name = r['名称'] || r['代码'] || '?';
      const close = r['收盘价'] || '-';
      const pct = parseFloat(r['涨跌幅'] || '0');
      const amt = parseFloat(r['成交额'] || '0') / 1e8;
      lines.push(`| ${name} | ${close} | ${pct >= 0 ? '+' : ''}${pct.toFixed(2)}% | ${amt.toFixed(0)} |`);
    }
    return lines.join('\n');
  },

  get_capital_flow({ date } = {}) {
    const ds = date || today();
    const dc = compactDate(ds);
    const fp = path.join(DATA_DIR, 'daily', ds, `板块资金流_${dc}.csv`);
    const rows = readCsv(fp);
    if (!rows || rows.length === 0) return '无资金流数据';

    const flowCol = rows[0]['净额'] !== undefined ? '净额' : (rows[0]['主力净流入'] !== undefined ? '主力净流入' : null);
    const nameCol = rows[0]['名称'] !== undefined ? '名称' : Object.keys(rows[0])[0];

    if (!flowCol) return 'CSV 格式不匹配';

    const sorted = [...rows].sort((a, b) => parseFloat(b[flowCol]) - parseFloat(a[flowCol]));
    const top5 = sorted.slice(0, 5);
    const bot5 = sorted.slice(-5).reverse();

    const lines = ['## 板块资金流向（今日）', '', '### 净流入 TOP5', '| 板块 | 净额(亿) |', '|------|---------|'];
    for (const r of top5) {
      lines.push(`| ${r[nameCol]} | ${(parseFloat(r[flowCol]) / 1e8).toFixed(2)} |`);
    }
    lines.push('', '### 净流出 TOP5');
    for (const r of bot5) {
      lines.push(`| ${r[nameCol]} | ${(parseFloat(r[flowCol]) / 1e8).toFixed(2)} |`);
    }
    return lines.join('\n');
  },

  get_quant_rules() {
    return cached('quant_rules', () => {
      const fp = path.join(KNOWLEDGE_DIR, '框架.md');
      const content = readMd(fp);
      if (!content) return '无量化规则';
      // Extract quant rules section
      const match = content.match(/##\s*3[\.、].*量化选股[\s\S]*?(?=\n## |\n$)/);
      return match ? match[0] : content.slice(0, 2000);
    });
  },

  get_stock_detail({ name, code, date: d } = {}) {
    if (!name && !code) return '请提供 name 或 code 参数';
    let ds = d || today();
    const dbPath = path.join(DATA_DIR, 'intraday', 'intraday.db');
    if (!fs.existsSync(dbPath)) return '无数据（intraday.db 不存在）';

    try {
      const db = new Database(dbPath, { readonly: true });

      // 非交易日 fallback：如果未显式指定日期且当日无数据，自动找最近交易日
      let fallbackDate = null;
      if (!d) {
        const dateCheck = db.prepare("SELECT 1 FROM snapshots WHERE date = ? LIMIT 1").get(ds);
        if (!dateCheck) {
          const lastTrading = db.prepare(
            "SELECT date FROM snapshots WHERE date < ? ORDER BY date DESC LIMIT 1"
          ).get(ds);
          if (lastTrading) {
            fallbackDate = lastTrading.date;
            ds = fallbackDate;
          }
        }
      }

      const conditions = ['date = ?'];
      const params = [ds];
      if (code) { conditions.push('code LIKE ?'); params.push(`%${code}%`); }
      if (name) { conditions.push('name LIKE ?'); params.push(`%${name}%`); }
      const where = conditions.join(' AND ');

      const rows = db.prepare(`SELECT date, ts, code, name, price, pctChg, open, high, low, last_close, volume, amount, amount_yi, is_limit_up, is_limit_down, sector FROM snapshots WHERE ${where} ORDER BY ts LIMIT 20`).all(...params);
      db.close();

      if (rows.length === 0) return '无匹配数据';

      const first = rows[0];
      const last = rows[rows.length - 1];
      const lines = [`## ${first.name}（${first.code}）`, `日期: ${ds}，共 ${rows.length} 条快照${fallbackDate ? '（非交易日，已回退至最近交易日）' : ''}`, '', '| 时间 | 价格 | 涨跌幅 | 成交额(亿) | 涨停 |', '|------|------|--------|-----------|------|'];
      for (const r of rows) {
        const lu = r.is_limit_up ? '涨停' : (r.is_limit_down ? '跌停' : '');
        lines.push(`| ${r.ts} | ${fmtPrice(r.price)} | ${parseFloat(r.pctChg || 0) >= 0 ? '+' : ''}${parseFloat(r.pctChg || 0).toFixed(2)}% | ${parseFloat(r.amount_yi || 0).toFixed(2)} | ${lu} |`);
      }
      return lines.join('\n');
    } catch (e) {
      return `查询出错: ${e.message}`;
    }
  },

  get_past_report({ date }) {
    if (!date) return '请提供 date 参数';
    const fp = path.join(DATA_DIR, 'daily', date, 'agent_05_裁决报告.md');
    const content = readMd(fp);
    return content || `无报告（${date}）`;
  },

  get_market_data({ date, time, name, code, mode = 'overview', sort_by = 'pctChg', top_n } = {}) {
    let ds = date || today();
    const dbPath = path.join(DATA_DIR, 'intraday', 'intraday.db');

    // Resolve target timestamp
    function resolveTs(db, queryDate) {
      if (time && time !== 'close' && time !== 'latest') {
        const row = db.prepare(
          "SELECT ts FROM snapshots WHERE date = ? AND ts <= ? ORDER BY ts DESC LIMIT 1"
        ).get(queryDate, time + ':59');
        return row ? row.ts : null;
      }
      const row = db.prepare(
        "SELECT ts FROM snapshots WHERE date = ? ORDER BY ts DESC LIMIT 1"
      ).get(queryDate);
      return row ? row.ts : null;
    }

    // Try intraday.db
    let db = null;
    let rows = [];
    let actualTs = null;
    let fallbackDate = null;

    if (fs.existsSync(dbPath)) {
      try {
        db = new Database(dbPath, { readonly: true });

        // Check if data exists for this date
        let dateCheck = db.prepare("SELECT 1 FROM snapshots WHERE date = ? LIMIT 1").get(ds);

        // 非交易日 fallback：如果未显式指定日期且当日无数据，自动找最近交易日
        if (!dateCheck && !date) {
          const lastTrading = db.prepare(
            "SELECT date FROM snapshots WHERE date < ? ORDER BY date DESC LIMIT 1"
          ).get(ds);
          if (lastTrading) {
            fallbackDate = lastTrading.date;
            ds = fallbackDate;
            dateCheck = true;
          }
        }

        if (dateCheck) {
          actualTs = resolveTs(db, ds);
          if (actualTs) {
            let query = "SELECT * FROM snapshots WHERE date = ? AND ts = ?";
            const params = [ds, actualTs];
            if (code) { query += " AND code LIKE ?"; params.push(`%${code}%`); }
            if (name) { query += " AND name LIKE ?"; params.push(`%${name}%`); }
            if (mode === 'pool') { query += " AND in_pool = 1"; }

            // Sorting
            const sortCol = sort_by === 'amount' ? 'amount_yi' : (sort_by === 'volume' ? 'volume' : 'pctChg');
            query += ` ORDER BY ${sortCol} DESC`;
            rows = db.prepare(query).all(...params);
          }
        }
      } catch (e) {
        console.error('[get_market_data] DB error:', e.message);
      }
    }

    // Fallback: pull real-time via mootdx_tool.py (only for today)
    if (rows.length === 0 && ds === today()) {
      try {
        const mootdxPath = path.join(DATA_DIR, 'mootdx_tool.py');
        const cmd = code
          ? `python3 ${mootdxPath} quotes ${code}`
          : name
            ? `python3 ${mootdxPath} quotes`
            : `python3 ${mootdxPath} quotes`;
        const output = execSync(cmd, { timeout: 15000, encoding: 'utf8' });

        // Parse tabular output from mootdx_tool.py
        const lines = output.trim().split('\n');
        const dataStart = lines.findIndex(l => l.match(/^\d{6}/));
        if (dataStart >= 0) {
          const headerLine = lines[dataStart - 1] || '';
          for (let i = dataStart; i < lines.length; i++) {
            const parts = lines[i].trim().split(/\s+/);
            if (parts.length >= 6 && parts[0].match(/^\d{6}$/)) {
              const row = {
                code: parts[0], name: parts[1], price: parseFloat(parts[2]),
                pctChg: parseFloat(parts[3]), open: parseFloat(parts[4]),
                high: parseFloat(parts[5]), low: parseFloat(parts[6]),
                last_close: parseFloat(parts[7]),
                amount_yi: parseFloat(parts[parts.length - 1]) || 0,
              };
              // Apply filters
              if (name && !row.name.includes(name)) continue;
              if (code && !row.code.includes(code)) continue;
              rows.push(row);
            }
          }
          actualTs = '实时';
        }
      } catch (e) {
        console.error('[get_market_data] mootdx fallback error:', e.message);
      }
    }

    if (db) db.close();
    if (rows.length === 0) return `无行情数据（${ds} ${time || ''}），本地数据库和通达信接口均无数据`;

    // ─── Format output based on mode ───

    if (mode === 'stock') {
      // Individual stock detail
      const n = top_n || 5;
      const filtered = rows.slice(0, n);
      const lines = [`## ${filtered[0].name || ''}（${filtered[0].code}）`, `日期: ${ds}  时间: ${actualTs}${fallbackDate ? '（非交易日，已回退至最近交易日）' : ''}`, '',
        '| 代码 | 名称 | 现价 | 涨跌幅 | 开盘 | 最高 | 最低 | 成交额(亿) | 涨停 |',
        '|------|------|------|--------|------|------|------|-----------|------|'];
      for (const r of filtered) {
        const lu = r.is_limit_up ? '涨停' : (r.is_limit_down ? '跌停' : '');
        lines.push(`| ${r.code} | ${r.name} | ${fmtPrice(r.price)} | ${fmtPct(r.pctChg)} | ${fmtPrice(r.open)} | ${fmtPrice(r.high)} | ${fmtPrice(r.low)} | ${fmtAmt(r.amount_yi)} | ${lu} |`);
      }
      return lines.join('\n');
    }

    // overview / pool mode
    const n = top_n || 10;

    // Statistics
    const limitUps = rows.filter(r => r.is_limit_up);
    const limitDowns = rows.filter(r => r.is_limit_down);
    const upCount = rows.filter(r => (r.pctChg || 0) > 0).length;
    const downCount = rows.filter(r => (r.pctChg || 0) < 0).length;
    const totalAmount = rows.reduce((s, r) => s + (r.amount_yi || 0), 0);

    const sorted = [...rows].sort((a, b) => (b.pctChg || 0) - (a.pctChg || 0));
    const topGainers = sorted.slice(0, n);
    const topLosers = sorted.slice(-n).reverse();

    const lines = [
      `## 行情概览（${mode === 'pool' ? '股票池' : '全市场'}）`,
      `日期: ${ds}  时间: ${actualTs}  总数: ${rows.length}${fallbackDate ? '（非交易日，已回退至最近交易日）' : ''}`,
      `涨: ${upCount}  跌: ${downCount}  涨停: ${limitUps.length}  跌停: ${limitDowns.length}  总成交: ${totalAmount.toFixed(1)}亿`,
      '',
      '### 涨幅 TOP' + n,
      '| 代码 | 名称 | 现价 | 涨跌幅 | 成交额(亿) |',
      '|------|------|------|--------|-----------|',
    ];
    for (const r of topGainers) {
      lines.push(`| ${r.code} | ${r.name} | ${fmtPrice(r.price)} | ${fmtPct(r.pctChg)} | ${fmtAmt(r.amount_yi)} |`);
    }

    lines.push('', '### 跌幅 TOP' + n);
    for (const r of topLosers) {
      lines.push(`| ${r.code} | ${r.name} | ${fmtPrice(r.price)} | ${fmtPct(r.pctChg)} | ${fmtAmt(r.amount_yi)} |`);
    }

    // Append limit-up details if any
    if (limitUps.length > 0 && mode !== 'pool') {
      lines.push('', `### 涨停（${limitUps.length}只）`);
      for (const r of limitUps) {
        lines.push(`- ${r.name}（${r.code}）${r.amount_yi ? r.amount_yi.toFixed(1) + '亿' : ''}`);
      }
    }

    return lines.join('\n');
  },
};

// ─── Export ────────────────────────────────────────

export function getToolHandler(name) {
  return toolHandlers[name] || null;
}
