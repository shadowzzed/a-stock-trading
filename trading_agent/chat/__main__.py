"""盘中对话 Agent -- 飞书 Bot 模式入口

启动方式:
    python -m trading_agent.chat              # 飞书 Bot 模式
    python -m trading_agent.chat --cli        # 本地 CLI 测试模式
    # 或（从 trading_agent/ 目录）
    python -m chat              # 飞书 Bot 模式
    python -m chat --cli        # 本地 CLI 测试模式
"""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import sys
import threading
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

# 确保项目根目录在 sys.path 中
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import get_config

from .graph import create_graph, create_graph_with_sqlite
from .feishu_bot import FeishuBot

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# 端口锁，防止重复启动
LOCK_PORT = 19876
_lock_socket = None


def _acquire_lock() -> bool:
    """尝试绑定端口作为进程锁，返回是否成功。"""
    global _lock_socket
    try:
        _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _lock_socket.bind(("127.0.0.1", LOCK_PORT))
        _lock_socket.listen(1)
        logger.info("端口锁已获取 (:%d)", LOCK_PORT)
        return True
    except OSError:
        logger.error("端口锁获取失败 (:%d)，可能已有实例在运行。请先 kill 旧进程。", LOCK_PORT)
        return False


# ── LangGraph 图实例（全局单例）──
_graph = None
_graph_lock = threading.Lock()

# Checkpoint DB 路径
_CHECKPOINT_DIR = Path(_project_root) / "trading" / "checkpoints"
_CHECKPOINT_DB = _CHECKPOINT_DIR / "chat.db"


def _get_graph():
    """获取或创建 LangGraph 图实例（延迟初始化，线程安全）。"""
    global _graph
    if _graph is not None:
        return _graph

    with _graph_lock:
        if _graph is not None:
            return _graph

        # 确保 checkpoint 目录存在
        _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        db_path = str(_CHECKPOINT_DB)

        logger.info("正在初始化 LangGraph（checkpoint: %s）...", db_path)
        _graph = create_graph_with_sqlite(db_path)
        logger.info("LangGraph 初始化完成")
        return _graph


def _chat_id_to_thread(chat_id: str) -> dict:
    """将飞书 chat_id 映射为 LangGraph thread config。"""
    return {"configurable": {"thread_id": chat_id}}


def run_cli() -> None:
    """本地 CLI 测试模式，直接与 LangGraph 对话。"""
    print("=== Trade Agent CLI 模式 (LangGraph) ===")
    print("输入消息直接对话，输入 /quit 退出\n")

    graph = _get_graph()
    thread_id = "cli-test"
    config = _chat_id_to_thread(thread_id)

    while True:
        try:
            user_input = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            break

        if not user_input:
            continue
        if user_input == "/clear":
            # 重置会话：用新 thread_id
            thread_id = f"cli-test-{os.getpid()}-{id(config)}"
            config = _chat_id_to_thread(thread_id)
            print("\n上下文已重置\n")
            continue
        if user_input == "/quit":
            break

        try:
            result = graph.invoke(
                {"messages": [HumanMessage(content=user_input)]},
                config=config,
            )
            # 提取最后一条 AI 消息
            messages = result.get("messages", [])
            reply = ""
            for msg in reversed(messages):
                if isinstance(msg, AIMessage) and msg.content:
                    reply = msg.content
                    break
            if reply:
                print(f"\nAgent> {reply}\n")
            else:
                print("\nAgent> （无有效回复）\n")
        except Exception as e:
            print(f"\n[错误] {e}\n")
            logger.error("CLI 对话异常: %s", e, exc_info=True)


def run_bot() -> None:
    """飞书 Bot 模式。"""
    if not _acquire_lock():
        sys.exit(1)

    cfg = get_config()
    app_id = cfg.get("chat_feishu_app_id", "")
    app_secret = cfg.get("chat_feishu_app_secret", "")

    if not app_id or not app_secret:
        print("错误：未配置 chat_feishu_app_id / chat_feishu_app_secret")
        print("请在 config.yaml 中添加，或设置环境变量 CHAT_FEISHU_APP_ID / CHAT_FEISHU_APP_SECRET")
        sys.exit(1)

    # Initialize graph
    logger.info("正在初始化 LangGraph...")
    graph = _get_graph()
    logger.info("LangGraph 已初始化")

    # Create bot
    bot = FeishuBot(app_id, app_secret)

    # 每个 chat_id 对应的 thread_id（/clear 时更换）
    _thread_ids: Dict[str, str] = {}

    def _get_thread_id(chat_id: str) -> str:
        """获取 chat_id 当前的 thread_id。"""
        if chat_id not in _thread_ids:
            _thread_ids[chat_id] = chat_id
        return _thread_ids[chat_id]

    # Message handler
    def on_message(chat_id: str, user_id: str, text: str) -> None:
        # /clear: 重置对话上下文（更换 thread_id，旧 checkpoint 保留但不加载）
        if text.strip() == "/clear":
            import time
            _thread_ids[chat_id] = f"{chat_id}-reset-{int(time.time())}"
            bot.send_text(chat_id, "上下文已重置")
            return

        # Skip empty messages
        if not text.strip():
            return

        logger.info("收到消息 [%s]: %s", chat_id, text[:80])

        try:
            thread_id = _get_thread_id(chat_id)
            config = _chat_id_to_thread(thread_id)
            result = graph.invoke(
                {"messages": [HumanMessage(content=text)]},
                config=config,
            )

            # 提取最后一条 AI 消息
            messages = result.get("messages", [])
            reply = ""
            for msg in reversed(messages):
                if isinstance(msg, AIMessage) and msg.content:
                    reply = msg.content
                    break

            if not reply:
                reply = "（无有效回复）"

            bot.send_text(chat_id, reply)
            logger.info("已回复 [%s]: %s", chat_id, reply[:80])
        except Exception as e:
            logger.error("处理消息异常: %s", e, exc_info=True)
            bot.send_text(chat_id, f"处理出错: {e}")

    bot.on_message = on_message

    logger.info("正在连接飞书 WebSocket...")
    bot.start()


def main() -> None:
    if "--cli" in sys.argv:
        run_cli()
    else:
        run_bot()


if __name__ == "__main__":
    main()
