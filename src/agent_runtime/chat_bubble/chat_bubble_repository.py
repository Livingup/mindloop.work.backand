# -*- coding: utf-8 -*-
"""Chat Bubble 前端消息气泡仓储边界。"""
from __future__ import annotations

from typing import Protocol

from agent_runtime.chat_bubble.chat_bubble_message import ChatBubbleMessage


class ChatBubbleRepository(Protocol):
    """前端消息气泡仓储接口。

    Chat Bubble 是面向前端 UI 的展示状态，而不是 Communication Event Log 或
    LLM Conversation 上下文。仓储边界只表达前端打开 Scene、维护消息气泡列表所需
    的持久化能力，具体 SQLite 文件、索引、分页实现由基础设施层负责。
    """

    def save_message(self, message: ChatBubbleMessage) -> ChatBubbleMessage:
        """保存一条消息气泡，已存在时覆盖当前展示状态。"""

    def get_message(self, bubble_id: str) -> ChatBubbleMessage | None:
        """按气泡 ID 查询消息气泡。"""

    def list_scene_messages(
        self,
        scene_id: str,
        *,
        limit: int = 100,
        before_sort_order: int | None = None,
        agent_instance_id: str | None = None,
    ) -> list[ChatBubbleMessage]:
        """按 Scene 查询前端历史消息气泡，结果按 sort_order 升序返回。"""

    def delete_message(self, bubble_id: str) -> bool:
        """删除一条消息气泡，返回是否实际删除。"""

    def next_sort_order(self, scene_id: str) -> int:
        """为指定 Scene 分配下一条消息气泡排序号。"""
