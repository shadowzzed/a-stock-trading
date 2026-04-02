/**
 * AI Agent — 工具调用循环
 * 移植自 Python agent.py，使用 OpenAI SDK
 */
import OpenAI from 'openai';
import { getAIProviders } from './config.mjs';
import { toolDefinitions, getToolHandler } from './tools.mjs';

const today = new Date().toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' });

const SYSTEM_PROMPT = `今天是 ${today}。

你是「短线助手」，一位专业的 A 股短线交易分析 AI。

你可以帮助用户进行：
- 实时行情分析（个股、板块、指数）
- 历史复盘查询（过去 7 天的涨跌停、连板梯队、龙头追踪）
- 交易策略讨论
- 情绪周期判断

你有以下工具可以调用：
- get_history_data: 查询近几日历史情绪数据
- get_review_docs: 获取博主复盘文档
- get_memory: 获取近期每日行情认知
- get_lessons: 获取历史经验教训
- get_prev_report: 获取昨日 Agent 报告
- get_index_data: 获取指数行情
- get_capital_flow: 获取资金流向
- get_quant_rules: 获取量化规律
- get_stock_detail: 查询个股详细行情（intraday.db）
- get_past_report: 获取任意历史日期的 Agent 报告

回答要求：
- 简洁直接，不要冗长的开场白
- 用数据说话，引用具体的涨跌停数、炸板率、连板高度等
- 给出明确可操作的建议，不要模棱两可
- 如果数据不够，主动调用工具获取`;

// ─── LLM client with fallback ──────────────────────

function createClients() {
  const providers = getAIProviders();
  if (providers.length === 0) throw new Error('未配置 AI 提供商');

  return providers.map(p => ({
    name: p.name,
    client: new OpenAI({ baseURL: p.base, apiKey: p.key }),
    model: p.model,
  }));
}

const clients = createClients();

async function callLLM(messages, retries = 0) {
  const provider = clients[Math.min(retries, clients.length - 1)];
  try {
    return await provider.client.chat.completions.create({
      model: provider.model,
      messages,
      tools: toolDefinitions,
      tool_choice: 'auto',
      temperature: 0.3,
    });
  } catch (err) {
    console.error(`[${provider.name}] API error:`, err.message);
    if (retries + 1 < clients.length) {
      console.log(`Falling back to ${clients[retries + 1].name}...`);
      return callLLM(messages, retries + 1);
    }
    throw err;
  }
}

// ─── Chat history (per chat, in-memory) ────────────

const MAX_HISTORY = 20;
const histories = new Map(); // chatId → [{ role, content }]

function getHistory(chatId) {
  return [...(histories.get(chatId) || [])];
}

function appendHistory(chatId, role, content) {
  if (!histories.has(chatId)) histories.set(chatId, []);
  const h = histories.get(chatId);
  h.push({ role, content });
  if (h.length > MAX_HISTORY) histories.set(chatId, h.slice(-MAX_HISTORY));
}

// ─── Agent chat loop ───────────────────────────────

export async function chat(chatId, userMessage) {
  appendHistory(chatId, 'user', userMessage);

  const messages = [
    { role: 'system', content: SYSTEM_PROMPT },
    ...getHistory(chatId),
  ];

  // Tool-calling loop (max 5 rounds)
  for (let round = 0; round < 5; round++) {
    const response = await callLLM(messages);
    const choice = response.choices[0];
    const assistantMsg = choice.message;

    // No tool calls → done
    if (!assistantMsg.tool_calls || assistantMsg.tool_calls.length === 0) {
      appendHistory(chatId, 'assistant', assistantMsg.content);
      return assistantMsg.content || '（Agent 未生成有效回复）';
    }

    // Process tool calls
    messages.push(assistantMsg);
    for (const tc of assistantMsg.tool_calls) {
      const handler = getToolHandler(tc.function.name);
      if (!handler) {
        messages.push({ role: 'tool', tool_call_id: tc.id, content: `未知工具: ${tc.function.name}` });
        continue;
      }
      try {
        const args = JSON.parse(tc.function.arguments);
        console.log(`  [tool] ${tc.function.name}(${JSON.stringify(args).slice(0, 100)})`);
        const result = handler(args);
        messages.push({ role: 'tool', tool_call_id: tc.id, content: result });
      } catch (e) {
        console.error(`  [tool] ${tc.function.name} error:`, e.message);
        messages.push({ role: 'tool', tool_call_id: tc.id, content: `工具执行出错: ${e.message}` });
      }
    }
  }

  return '（Agent 工具调用超过最大轮次）';
}
