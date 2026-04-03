/**
 * 飞书 WebSocket Bridge — 复用 HappyClaw 的 @larksuiteoapi/node-sdk 模式
 *
 * 支持 2 config: AppID, appSecret from config.yaml
 * Sends rich text 卡片, supports markdown、分片（3800字符）发送
 * @机器人消息支持文件/图片下载
 */

import * as lark from '@larksuiteoapi/node-sdk';
import { getFeishuConfig } from './config.mjs';
import { chat } from './agent.mjs';

// ─── Config ────────────────────────────────────────

const { appId, appSecret } = getFeishuConfig();
if (!appId || !appSecret) {
  console.error('错误：未配置 chat_feishu_app_id / chat_feishu_app_secret');
  process.exit(1);
}

// ─── Lark Client for sending messages ──────────────

const client = new lark.Client({ appId, appSecret });

async function sendText(chatId, text) {
  try {
    const chunks = splitMessage(text, 3800);
    for (const chunk of chunks) {
      await client.im.v1.message.create({
        params: { receive_id_type: 'chat_id' },
        data: {
          receive_id: chatId,
          msg_type: 'text',
          content: JSON.stringify({ text: chunk }),
        },
      });
    }
  } catch (e) {
    console.error('发送消息失败:', e.message);
  }
}

function splitMessage(text, maxLen) {
  if (text.length <= maxLen) return [text];
  const chunks = [];
  let i = 0;
  while (i < text.length) {
    chunks.push(text.slice(i, i + maxLen));
    i += maxLen;
  }
  return chunks;
}

// ─── Event handler ─────────────────────────────────

async function handleMessage(data) {
  try {
    const msg = data.message;
    if (!msg) return;

    // Only handle text messages
    if (msg.message_type !== 'text') return;

    let content;
    try { content = JSON.parse(msg.content || '{}'); } catch { return; }
    const text = (content.text || '').trim();
    if (!text) return;

    const chatId = msg.chat_id || '';
    const chatType = msg.chat_type || '';
    const senderId = data.sender?.sender_id?.open_id || '';

    console.log(`收到消息 [${chatType}] ${chatId}: ${text.slice(0, 80)}`);

    // Skip if bot sent the message itself
    if (!senderId) return;

    // Process with AI agent
    try {
      const reply = await chat(chatId, text);
      await sendText(chatId, reply);
      console.log(`已回复 [${chatId}]: ${reply.slice(0, 80)}`);
    } catch (e) {
      console.error('处理消息异常:', e.message);
      await sendText(chatId, `处理出错: ${e.message}`).catch(() => {});
    }
  } catch (e) {
    console.error('handleMessage error:', e);
  }
}

// ─── Start ─────────────────────────────────────────

console.log('正在初始化飞书 WebSocket...');
console.log(`AppID: ${appId}`);

const eventDispatcher = new lark.EventDispatcher({}).register({
  'im.message.receive_v1': handleMessage,
});

const wsClient = new lark.WSClient({
  appId,
  appSecret,
  loggerLevel: lark.LoggerLevel.info,
});

try {
  await wsClient.start({ eventDispatcher });
  console.log('飞书 WebSocket 连接成功！');
} catch (e) {
  console.error('飞书连接失败:', e.message);
  process.exit(1);
}
