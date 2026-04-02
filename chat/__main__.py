"""盘中对话 Agent -- 飞书 Bot 模式入口

启动方式:
    python -m chat
    # 或
    chat-agent (安装后)
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections import defaultdict
from typing import Dict, List

# 确保项目根目录在 sys.path 中
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import get_config

from .agent import TradingChatAgent
from .feishu_bot import FeishuBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# 每个群聊维护独立的对话历史（简单内存实现）
MAX_HISTORY_PER_CHAT = 20
_chat_histories: Dict[str, List] = defaultdict(list)
_history_lock = threading.Lock()


def _get_history(chat_id: str) -> List:
    """获取群聊对话历史"""
    with _history_lock:
        return list(_chat_histories[chat_id])


def _append_history(chat_id: str, role: str, text: str) -> None:
    """追加对话历史"""
    from langchain_core.messages import AIMessage, HumanMessage

    msg = HumanMessage(content=text) if role == "user" else AIMessage(content=text)
    with _history_lock:
        history = _chat_histories[chat_id]
        history.append(msg)
        # 保留最近的消息
        if len(history) > MAX_HISTORY_PER_CHAT:
            _chat_histories[chat_id] = history[-MAX_HISTORY_PER_CHAT:]


def main() -> None:
    cfg = get_config()
    app_id = cfg.get("chat_feishu_app_id", "")
    app_secret = cfg.get("chat_feishu_app_secret", "")

    if not app_id or not app_secret:
        print("错误：未配置 chat_feishu_app_id / chat_feishu_app_secret")
        print("请在 config.yaml 中添加，或设置环境变量 CHAT_FEISHU_APP_ID / CHAT_FEISHU_APP_SECRET")
        sys.exit(1)

    # Create agent
    logger.info("正在初始化 Agent...")
    agent = TradingChatAgent()
    logger.info("Agent 已初始化")

    # Create bot
    bot = FeishuBot(app_id, app_secret)

    # Message handler
    def on_message(chat_id: str, user_id: str, text: str) -> None:
        # Skip empty messages
        if not text.strip():
            return

        logger.info("收到消息 [%s]: %s", chat_id, text[:80])

        # Append user message to history
        _append_history(chat_id, "user", text)

        try:
            history = _get_history(chat_id)
            reply = agent.chat(text, history=history[:-1])  # exclude current message from history
            _append_history(chat_id, "assistant", reply)
            bot.send_text(chat_id, reply)
            logger.info("已回复 [%s]: %s", chat_id, reply[:80])
        except Exception as e:
            logger.error("处理消息异常: %s", e, exc_info=True)
            bot.send_text(chat_id, f"处理出错: {e}")

    bot.on_message = on_message

    logger.info("正在连接飞书 WebSocket...")
    bot.start()


if __name__ == "__main__":
    main()
