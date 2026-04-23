"""会话仓储内存实现。

所有数据存储在 Python 字典中，进程结束即丢失。
仅用于开发调试和单元测试场景。
"""
from __future__ import annotations

from typing import Any

from agent_runtime.conversation_message.message.models import (
    Conversation,
    ConversationState,
    UniMessage,
)


class InMemoryConversationRepository:
    """会话仓储内存实现。

    所有数据存储在 Python 字典中，进程结束即丢失。
    仅用于开发调试和单元测试场景。
    """

    def __init__(self) -> None:
        self._store: dict[str, Conversation] = {}
        self._hierarchy: dict[str, dict[str, str]] = {}

    async def create(
        self, conv: Conversation, *, task_id: str, thread_id: str,
        agent_id: str, agent_instance_id: str,
    ) -> None:
        self._store[conv.id] = conv
        self._hierarchy[conv.id] = {
            "task_id": task_id, "thread_id": thread_id,
            "agent_id": agent_id, "agent_instance_id": agent_instance_id,
        }

    async def get(self, conversation_id: str) -> Conversation:
        conv = self._store.get(conversation_id)
        if conv is None:
            raise KeyError(f"Conversation not found: {conversation_id}")
        return conv

    async def save(self, conv: Conversation) -> None:
        self._store[conv.id] = conv

    async def delete(self, conversation_id: str) -> None:
        self._store.pop(conversation_id, None)
        self._hierarchy.pop(conversation_id, None)

    async def append_message(self, conversation_id: str, msg: UniMessage) -> None:
        conv = await self.get(conversation_id)
        conv.messages.append(msg)

    async def save_state(self, conversation_id: str, state: ConversationState) -> None:
        conv = await self.get(conversation_id)
        conv.state = state

    async def save_provider_state(
        self, conversation_id: str, provider: str, state: Any,
    ) -> None:
        conv = await self.get(conversation_id)
        conv.provider_state[provider] = state

    async def list_by_task(self, task_id: str) -> list[str]:
        return [cid for cid, h in self._hierarchy.items() if h["task_id"] == task_id]

    async def list_by_thread(self, thread_id: str) -> list[str]:
        return [cid for cid, h in self._hierarchy.items() if h["thread_id"] == thread_id]

    async def list_by_agent(self, agent_id: str) -> list[str]:
        return [cid for cid, h in self._hierarchy.items() if h["agent_id"] == agent_id]

    async def list_by_agent_instance(self, agent_instance_id: str) -> list[str]:
        return [cid for cid, h in self._hierarchy.items() if h["agent_instance_id"] == agent_instance_id]
