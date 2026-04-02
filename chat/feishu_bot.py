"""飞书 Bot 连接模块 -- WebSocket 长连接接收消息 + REST API 回复"""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)

logger = logging.getLogger(__name__)


class FeishuBot:
    """飞书 Bot：通过 WebSocket 长连接接收消息，通过 REST API 回复。

    Args:
        app_id: 飞书应用 App ID
        app_secret: 飞书应用 App Secret
        on_message: 消息回调 (chat_id, user_id, text) -> None
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_message: Optional[Callable[[str, str, str], None]] = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.on_message = on_message

        self._client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.DEBUG)
            .build()
        )
        self._ws_client: Optional[lark.ws.Client] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动 WebSocket 长连接（阻塞调用）。"""
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_message)
            .build()
        )

        self._ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )

        logger.info("飞书 WebSocket 长连接启动中...")
        self._ws_client.start()

    def send_text(self, chat_id: str, text: str) -> bool:
        """发送文本消息到飞书群聊。

        Args:
            chat_id: 群聊 ID
            text: 消息文本

        Returns:
            是否发送成功
        """
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )

        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.error(
                "飞书消息发送失败: code=%s msg=%s", response.code, response.msg
            )
            return False
        return True

    def reply_text(self, message_id: str, text: str) -> bool:
        """回复指定消息（引用原消息）。

        Args:
            message_id: 被回复的消息 ID
            text: 回复文本

        Returns:
            是否发送成功
        """
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )

        response = self._client.im.v1.message.reply(request)
        if not response.success():
            logger.error(
                "飞书消息回复失败: code=%s msg=%s", response.code, response.msg
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_message(self, data: lark_oapi.api.im.v1.P2ImMessageReceiveV1) -> None:
        """处理收到的消息事件"""
        try:
            msg = data.event.message
            if msg is None:
                return

            # 只处理文本消息
            if msg.message_type != "text":
                return

            content = json.loads(msg.content) if msg.content else {}
            text = content.get("text", "").strip()
            if not text:
                return

            chat_id = msg.chat_id or ""
            user_id = ""
            if data.event.sender and data.event.sender.sender_id:
                user_id = data.event.sender.sender_id.open_id or ""

            logger.info("收到消息: chat_id=%s user=%s text=%s", chat_id, user_id, text[:50])

            if self.on_message:
                self.on_message(chat_id, user_id, text)

        except Exception as e:
            logger.error("处理消息异常: %s", e, exc_info=True)
