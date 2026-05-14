# -*- coding: utf-8 -*-
"""Chat Bubble 前端消息气泡领域服务。"""
from __future__ import annotations

from threading import RLock
from typing import Any
from uuid import uuid4

from agent_runtime.chat_bubble.chat_bubble_message import (
    CHAT_BUBBLE_STATUS_COMPLETED,
    ChatBubbleInteractionOption,
    ChatBubbleMessage,
    ChatBubbleRole,
    ChatBubbleStatus,
    ChatBubbleTextFormat,
)
from agent_runtime.chat_bubble.chat_bubble_repository import ChatBubbleRepository


def _new_bubble_id() -> str:
    """生成带业务前缀的前端消息气泡 ID，方便排障时识别对象类型。"""
    return f"chat-bubble-{uuid4().hex}"


def _require_non_blank(value: str, field_name: str) -> None:
    """校验 Chat Bubble 业务字段不能为空。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空。")
    if value != value.strip():
        raise ValueError(f"{field_name} 不能包含首尾空白字符。")


class ChatBubbleService:
    """前端消息气泡应用/领域服务。

    该服务只维护 Chat UI 的展示气泡状态，不从 Communication Event Log 回放 UI，
    也不把 LLM Conversation 当作前端历史消息。前端在产生、确认、更新气泡时通过
    REST API 调用本服务，服务负责 ID、排序号、Scene 边界和仓储持久化编排。
    """

    def __init__(self, repository: ChatBubbleRepository) -> None:
        # Chat Bubble 仓储。具体 SQLite 文件和索引由基础设施实现负责。
        self._repository = repository

        # 服务级锁用于保证同一进程内 scene sort_order 分配和保存的顺序性。
        self._lock = RLock()

    def create_message(
        self,
        *,
        scene_id: str,
        role: ChatBubbleRole,
        text: str,
        agent_instance_id: str | None = None,
        task_id: str | None = None,
        conversation_id: str | None = None,
        status: ChatBubbleStatus = CHAT_BUBBLE_STATUS_COMPLETED,
        text_format: ChatBubbleTextFormat = "markdown",
        channel: str | None = "web",
        bubble_id: str | None = None,
        client_message_id: str | None = None,
        backend_message_id: str | None = None,
        pending_message_id: str | None = None,
        interaction_options: tuple[ChatBubbleInteractionOption, ...] = (),
        selected_option_id: str | None = None,
        sort_order: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatBubbleMessage:
        """创建并持久化一条前端消息气泡。

        业务规则：
        - scene_id 是历史气泡列表的主归属，必须与 REST 路径保持一致；
        - bubble_id 可由前端传入以支持幂等写入，未传则由后端生成；
        - sort_order 未传或小于等于 0 时由仓储按 Scene 分配下一递增序号；
        - interaction_options 只表达前端恢复按钮状态所需的展示数据。
        """
        _require_non_blank(scene_id, "scene_id")
        actual_bubble_id = bubble_id or _new_bubble_id()
        with self._lock:
            current = self._repository.get_message(actual_bubble_id)
            if current is not None and current.scene_id != scene_id:
                raise ValueError("Chat 气泡已存在且不属于指定 Scene。")
            if sort_order is not None and sort_order > 0:
                actual_sort_order = sort_order
            elif current is not None:
                actual_sort_order = current.sort_order
            else:
                actual_sort_order = self._repository.next_sort_order(scene_id)
            message = ChatBubbleMessage(
                bubble_id=actual_bubble_id,
                scene_id=scene_id,
                agent_instance_id=agent_instance_id,
                task_id=task_id,
                conversation_id=conversation_id,
                role=role,
                status=status,
                text=text,
                text_format=text_format,
                channel=channel,
                client_message_id=client_message_id,
                backend_message_id=backend_message_id,
                pending_message_id=pending_message_id,
                interaction_options=interaction_options,
                selected_option_id=(
                    selected_option_id
                    if selected_option_id is not None
                    else current.selected_option_id if current is not None else None
                ),
                sort_order=actual_sort_order,
                created_at=current.created_at if current is not None else None,
                metadata=metadata or {},
            )
            return self._repository.save_message(message)

    def list_scene_messages(
        self,
        scene_id: str,
        *,
        limit: int = 100,
        before_sort_order: int | None = None,
        agent_instance_id: str | None = None,
    ) -> list[ChatBubbleMessage]:
        """读取 Scene 下前端历史消息气泡，供前端打开 Scene 时恢复 Chat UI。"""
        _require_non_blank(scene_id, "scene_id")
        actual_limit = max(1, min(int(limit), 500))
        if before_sort_order is not None and before_sort_order < 0:
            raise ValueError("before_sort_order 不能小于 0。")
        return self._repository.list_scene_messages(
            scene_id,
            limit=actual_limit,
            before_sort_order=before_sort_order,
            agent_instance_id=agent_instance_id,
        )

    def patch_message(
        self,
        *,
        scene_id: str,
        bubble_id: str,
        status: ChatBubbleStatus | None = None,
        text: str | None = None,
        text_format: ChatBubbleTextFormat | None = None,
        channel: str | None = None,
        backend_message_id: str | None = None,
        pending_message_id: str | None = None,
        interaction_options: tuple[ChatBubbleInteractionOption, ...] | None = None,
        selected_option_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatBubbleMessage:
        """更新一条消息气泡的前端展示状态。"""
        _require_non_blank(scene_id, "scene_id")
        _require_non_blank(bubble_id, "bubble_id")
        with self._lock:
            current = self._repository.get_message(bubble_id)
            if current is None:
                raise KeyError(f"Chat 气泡不存在: {bubble_id}")
            if current.scene_id != scene_id:
                raise ValueError("Chat 气泡不属于指定 Scene。")
            updated = current.with_patch(
                status=status,
                text=text,
                text_format=text_format,
                channel=channel,
                backend_message_id=backend_message_id,
                pending_message_id=pending_message_id,
                interaction_options=interaction_options,
                selected_option_id=selected_option_id,
                metadata=metadata,
            )
            return self._repository.save_message(updated)

    def delete_message(self, *, scene_id: str, bubble_id: str) -> bool:
        """删除一条前端消息气泡。"""
        _require_non_blank(scene_id, "scene_id")
        _require_non_blank(bubble_id, "bubble_id")
        with self._lock:
            current = self._repository.get_message(bubble_id)
            if current is None:
                return False
            if current.scene_id != scene_id:
                raise ValueError("Chat 气泡不属于指定 Scene。")
            return self._repository.delete_message(bubble_id)
