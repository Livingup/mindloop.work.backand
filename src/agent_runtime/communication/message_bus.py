# -*- coding: utf-8 -*-
"""进程内通信消息总线。"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import suppress
from typing import AsyncIterator

from agent_runtime.communication.communication_message import CommunicationMessage


class InMemoryCommunicationMessageBus:
    """进程内实时消息总线。

    该总线只负责当前 Python 进程内的实时订阅分发。权威事件历史由 CommunicationRepository
    持久化保存；未来多 worker 场景可用 Redis Stream / NATS 替换本类而不影响业务层。
    """

    def __init__(self) -> None:
        # stream_id 到订阅队列集合的映射。
        self._subscribers: dict[str, set[asyncio.Queue[CommunicationMessage]]] = defaultdict(set)

        # 保护订阅集合增删的异步锁。
        self._lock = asyncio.Lock()

    async def publish(self, message: CommunicationMessage) -> None:
        """把消息发布给当前进程内订阅同一 stream_id 的全部连接。"""
        async with self._lock:
            queues = tuple(self._subscribers.get(message.stream_id, set()))
        for queue in queues:
            with suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    async def subscribe(self, stream_id: str) -> AsyncIterator[CommunicationMessage]:
        """订阅某个通信流的实时消息。"""
        queue: asyncio.Queue[CommunicationMessage] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers[stream_id].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                queues = self._subscribers.get(stream_id)
                if queues is not None:
                    queues.discard(queue)
                    if not queues:
                        self._subscribers.pop(stream_id, None)
