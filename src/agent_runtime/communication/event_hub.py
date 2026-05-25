# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from threading import RLock
from typing import AsyncIterator

from agent_runtime.communication.communication_message import CommunicationMessage
from agent_runtime.runtime_network_monitor import runtime_network_monitor

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _StreamEventState:
    lock: RLock = field(default_factory=RLock)
    next_seq: int = 1
    buffer: deque[CommunicationMessage] = field(default_factory=deque)
    subscribers: set[asyncio.Queue[CommunicationMessage | None]] = field(default_factory=set)


class InMemoryCommunicationEventHub:
    def __init__(
        self,
        *,
        replay_buffer_size: int = 5000,
        subscriber_queue_size: int = 1000,
    ) -> None:
        if replay_buffer_size <= 0:
            raise ValueError("replay_buffer_size 必须大于 0。")
        if subscriber_queue_size <= 0:
            raise ValueError("subscriber_queue_size 必须大于 0。")
        self._replay_buffer_size = replay_buffer_size
        self._subscriber_queue_size = subscriber_queue_size
        self._states: dict[str, _StreamEventState] = {}
        self._states_lock = RLock()

    async def publish(self, message: CommunicationMessage) -> CommunicationMessage:
        state = self._state_for(message.stream_id)
        overflow_queues: list[asyncio.Queue[CommunicationMessage | None]] = []
        with state.lock:
            seq = message.seq if message.seq > 0 else state.next_seq
            saved = _message_with_seq(message, seq)
            state.next_seq = max(state.next_seq, seq + 1)
            state.buffer.append(saved)
            while len(state.buffer) > self._replay_buffer_size:
                state.buffer.popleft()
            for queue in tuple(state.subscribers):
                try:
                    queue.put_nowait(saved)
                except asyncio.QueueFull:
                    state.subscribers.discard(queue)
                    overflow_queues.append(queue)
        for queue in overflow_queues:
            _signal_subscriber_overflow(queue)
        if overflow_queues:
            queue_full_total = runtime_network_monitor.event_hub_queue_full(len(overflow_queues))
            runtime_network_monitor.record({
                "layer": "backend",
                "type": "event_hub.queue_full",
                "stream_id": saved.stream_id,
                "seq": saved.seq,
                "overflow_subscribers": len(overflow_queues),
                "event_hub_queue_full_count": queue_full_total,
                **runtime_network_monitor.snapshot(),
            })
            logger.warning(
                "通信事件订阅队列已满，已断开慢订阅者: stream_id=%s, overflow_subscribers=%s, seq=%s",
                saved.stream_id,
                len(overflow_queues),
                saved.seq,
            )
        return saved

    def list_messages(
        self,
        stream_id: str,
        *,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[CommunicationMessage]:
        state = self._state_for(stream_id)
        actual_after_seq = max(after_seq, 0)
        actual_limit = max(min(limit, 5000), 1)
        with state.lock:
            if state.next_seq <= actual_after_seq:
                state.next_seq = actual_after_seq + 1
            return [
                message
                for message in state.buffer
                if message.seq > actual_after_seq
            ][:actual_limit]

    def list_latest_messages(self, stream_id: str, *, limit: int = 500) -> list[CommunicationMessage]:
        state = self._state_for(stream_id)
        actual_limit = max(min(limit, 5000), 1)
        with state.lock:
            messages = list(state.buffer)[-actual_limit:]
            return messages

    def get_stream_diagnostics(self, stream_id: str) -> dict[str, int | None]:
        state = self._state_for(stream_id)
        with state.lock:
            first_seq = state.buffer[0].seq if state.buffer else None
            last_seq = state.buffer[-1].seq if state.buffer else None
            return {
                "subscriber_count": len(state.subscribers),
                "buffered_message_count": len(state.buffer),
                "buffer_first_seq": first_seq,
                "buffer_last_seq": last_seq,
                "next_seq": state.next_seq,
                "subscriber_queue_size": self._subscriber_queue_size,
                "replay_buffer_size": self._replay_buffer_size,
            }

    async def subscribe(self, stream_id: str, *, after_seq: int = 0) -> AsyncIterator[CommunicationMessage]:
        state = self._state_for(stream_id)
        actual_after_seq = max(after_seq, 0)
        queue: asyncio.Queue[CommunicationMessage | None] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        with state.lock:
            if state.next_seq <= actual_after_seq:
                state.next_seq = actual_after_seq + 1
            replay_messages = [
                message
                for message in state.buffer
                if message.seq > actual_after_seq
            ]
            state.subscribers.add(queue)
        active_subscribers = runtime_network_monitor.event_hub_subscribed()
        runtime_network_monitor.record({
            "layer": "backend",
            "type": "event_hub.subscribe",
            "stream_id": stream_id,
            "after_seq": actual_after_seq,
            "replay_count": len(replay_messages),
            "event_hub_subscribers": active_subscribers,
            **runtime_network_monitor.snapshot(),
        })
        try:
            for message in replay_messages:
                yield message
            while True:
                message = await queue.get()
                if message is None:
                    raise RuntimeError("通信事件订阅队列已满，请重新连接以恢复实时事件。")
                yield message
        finally:
            with state.lock:
                state.subscribers.discard(queue)
            active_subscribers = runtime_network_monitor.event_hub_unsubscribed()
            runtime_network_monitor.record({
                "layer": "backend",
                "type": "event_hub.unsubscribe",
                "stream_id": stream_id,
                "event_hub_subscribers": active_subscribers,
                **runtime_network_monitor.snapshot(),
            })

    def _state_for(self, stream_id: str) -> _StreamEventState:
        with self._states_lock:
            state = self._states.get(stream_id)
            if state is None:
                state = _StreamEventState()
                self._states[stream_id] = state
            return state


def _message_with_seq(message: CommunicationMessage, seq: int) -> CommunicationMessage:
    return CommunicationMessage(
        message_id=message.message_id,
        stream_id=message.stream_id,
        seq=seq,
        type=message.type,
        topic=message.topic,
        source=message.source,
        actor_id=message.actor_id,
        correlation_id=message.correlation_id,
        parent_message_id=message.parent_message_id,
        payload=dict(message.payload),
        metadata=dict(message.metadata),
        created_at=message.created_at,
        schema_version=message.schema_version,
    )


def _signal_subscriber_overflow(queue: asyncio.Queue[CommunicationMessage | None]) -> None:
    with suppress(asyncio.QueueEmpty):
        while True:
            queue.get_nowait()
    with suppress(asyncio.QueueFull):
        queue.put_nowait(None)
