# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import AsyncIterator
from uuid import uuid4

from agent_runtime.communication.communication_errors import CommunicationGapUnrecoverableError
from agent_runtime.communication.communication_message import CommunicationMessage
from agent_runtime.runtime_network_monitor import runtime_network_monitor

logger = logging.getLogger(__name__)

TERMINAL_OUTPUT_DELTA_TYPE = "terminal.output.delta"


def _is_term_event(message: CommunicationMessage) -> bool:
    return message.type == TERMINAL_OUTPUT_DELTA_TYPE


def _approx_message_bytes(message: CommunicationMessage) -> int:
    # 近似 UTF-8 体积，避免为淘汰路径做完整 JSON 序列化。
    payload_size = len(repr(message.payload))
    metadata_size = len(repr(message.metadata))
    fixed = len(message.message_id) + len(message.stream_id) + len(message.type) + len(message.topic) + 64
    return fixed + payload_size + metadata_size


@dataclass(slots=True)
class _StreamEventState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    next_seq: int = 1
    buffer: deque[CommunicationMessage] = field(default_factory=deque)
    buffer_bytes: int = 0
    main_count: int = 0
    term_count: int = 0
    subscribers: set[asyncio.Queue[CommunicationMessage | None]] = field(default_factory=set)
    last_publish_at: float = field(default_factory=time.monotonic)
    last_subscribe_at: float = field(default_factory=time.monotonic)


class InMemoryCommunicationEventHub:
    """进程内 communication ring + fan-out。

    memory 模式下该 hub 同时承担权威 seq 分配、有界 ring、订阅 fan-out。
    sqlite 模式下仍可用于 fan-out；消息 seq 已由仓储分配。
    """

    def __init__(
        self,
        *,
        replay_buffer_size: int = 4000,
        subscriber_queue_size: int = 1000,
        memory_ring_max_events: int | None = None,
        memory_ring_max_bytes: int = 24 * 1024 * 1024,
        memory_ring_main_min_events: int = 1000,
        memory_ring_terminal_max_events: int = 3000,
        global_memory_ring_max_bytes: int = 2560 * 1024 * 1024,
        stream_idle_reclaim_minutes: int = 30,
        stream_idle_shrink_events: int = 64,
        idle_scan_interval_seconds: int = 60,
        runtime_boot_id: str | None = None,
        enable_idle_reclaimer: bool = True,
    ) -> None:
        max_events = memory_ring_max_events if memory_ring_max_events is not None else replay_buffer_size
        if max_events <= 0:
            raise ValueError("memory_ring_max_events/replay_buffer_size 必须大于 0。")
        if subscriber_queue_size <= 0:
            raise ValueError("subscriber_queue_size 必须大于 0。")
        if memory_ring_max_bytes <= 0:
            raise ValueError("memory_ring_max_bytes 必须大于 0。")
        if memory_ring_main_min_events < 0:
            raise ValueError("memory_ring_main_min_events 不能为负数。")
        if memory_ring_terminal_max_events < 0:
            raise ValueError("memory_ring_terminal_max_events 不能为负数。")
        if global_memory_ring_max_bytes <= 0:
            raise ValueError("global_memory_ring_max_bytes 必须大于 0。")
        self._replay_buffer_size = max_events
        self._memory_ring_max_events = max_events
        self._memory_ring_max_bytes = memory_ring_max_bytes
        self._memory_ring_main_min_events = memory_ring_main_min_events
        self._memory_ring_terminal_max_events = min(memory_ring_terminal_max_events, max_events)
        self._global_memory_ring_max_bytes = global_memory_ring_max_bytes
        self._subscriber_queue_size = subscriber_queue_size
        self._stream_idle_reclaim_seconds = max(stream_idle_reclaim_minutes, 0) * 60
        self._stream_idle_shrink_events = max(stream_idle_shrink_events, 0)
        self._idle_scan_interval_seconds = max(idle_scan_interval_seconds, 1)
        self._runtime_boot_id = runtime_boot_id or str(uuid4())
        self._states: dict[str, _StreamEventState] = {}
        self._states_lock = threading.RLock()
        self._global_bytes = 0
        self._reclaimer_stop = threading.Event()
        self._reclaimer_thread: threading.Thread | None = None
        if enable_idle_reclaimer and self._stream_idle_reclaim_seconds > 0:
            self._reclaimer_thread = threading.Thread(
                target=self._idle_reclaim_loop,
                name="communication-ring-reclaimer",
                daemon=True,
            )
            self._reclaimer_thread.start()

    @property
    def runtime_boot_id(self) -> str:
        return self._runtime_boot_id

    def close(self) -> None:
        self._reclaimer_stop.set()
        thread = self._reclaimer_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    async def publish(self, message: CommunicationMessage) -> CommunicationMessage:
        state = self._state_for(message.stream_id)
        overflow_queues: list[asyncio.Queue[CommunicationMessage | None]] = []
        with state.lock:
            seq = message.seq if message.seq > 0 else state.next_seq
            saved = _message_with_seq(message, seq)
            state.next_seq = max(state.next_seq, seq + 1)
            self._append_locked(state, saved)
            self._evict_stream_locked(state)
            state.last_publish_at = time.monotonic()
            for queue in tuple(state.subscribers):
                try:
                    queue.put_nowait(saved)
                except asyncio.QueueFull:
                    state.subscribers.discard(queue)
                    overflow_queues.append(queue)
        self._evict_global()
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
        enforce_contiguous_gap: bool = False,
    ) -> list[CommunicationMessage]:
        state = self._state_for(stream_id)
        actual_after_seq = max(after_seq, 0)
        actual_limit = max(min(limit, 5000), 1)
        with state.lock:
            first_seq = state.buffer[0].seq if state.buffer else 0
            last_seq = state.buffer[-1].seq if state.buffer else 0
            if enforce_contiguous_gap and actual_after_seq > 0:
                if not state.buffer:
                    # 空 ring 且客户端仍持有旧 cursor：无法提供 after_seq+1。
                    raise CommunicationGapUnrecoverableError(
                        stream_id=stream_id,
                        after_seq=actual_after_seq,
                        ring_first_seq=0,
                        ring_last_seq=0,
                    )
                expected = actual_after_seq + 1
                if expected < first_seq:
                    raise CommunicationGapUnrecoverableError(
                        stream_id=stream_id,
                        after_seq=actual_after_seq,
                        ring_first_seq=first_seq,
                        ring_last_seq=last_seq,
                    )
            return [message for message in state.buffer if message.seq > actual_after_seq][:actual_limit]

    def list_latest_messages(self, stream_id: str, *, limit: int = 500) -> list[CommunicationMessage]:
        state = self._state_for(stream_id)
        actual_limit = max(min(limit, 5000), 1)
        with state.lock:
            return list(state.buffer)[-actual_limit:]

    def get_stream_diagnostics(self, stream_id: str) -> dict[str, int | str | None]:
        state = self._state_for(stream_id)
        with state.lock:
            first_seq = state.buffer[0].seq if state.buffer else None
            last_seq = state.buffer[-1].seq if state.buffer else None
            return {
                "subscriber_count": len(state.subscribers),
                "buffered_message_count": len(state.buffer),
                "buffer_first_seq": first_seq,
                "buffer_last_seq": last_seq,
                "ring_first_seq": first_seq if first_seq is not None else 0,
                "ring_last_seq": last_seq if last_seq is not None else 0,
                "ring_event_count": len(state.buffer),
                "ring_bytes": state.buffer_bytes,
                "ring_main_count": state.main_count,
                "ring_term_count": state.term_count,
                "next_seq": state.next_seq,
                "subscriber_queue_size": self._subscriber_queue_size,
                "replay_buffer_size": self._replay_buffer_size,
                "memory_ring_max_events": self._memory_ring_max_events,
                "memory_ring_max_bytes": self._memory_ring_max_bytes,
                "global_ring_bytes": self._global_bytes,
                "global_memory_ring_max_bytes": self._global_memory_ring_max_bytes,
                "runtime_boot_id": self._runtime_boot_id,
            }

    async def subscribe(self, stream_id: str, *, after_seq: int = 0) -> AsyncIterator[CommunicationMessage]:
        state = self._state_for(stream_id)
        actual_after_seq = max(after_seq, 0)
        queue: asyncio.Queue[CommunicationMessage | None] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        with state.lock:
            if state.next_seq <= actual_after_seq:
                # 客户端 cursor 超前：不回退 next_seq，只返回空 replay，由调用方/前端 reset。
                pass
            replay_messages = [message for message in state.buffer if message.seq > actual_after_seq]
            state.subscribers.add(queue)
            state.last_subscribe_at = time.monotonic()
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

    def drop_stream_state(self, stream_id: str) -> None:
        with self._states_lock:
            state = self._states.pop(stream_id, None)
        if state is None:
            return
        with state.lock:
            self._global_bytes = max(0, self._global_bytes - state.buffer_bytes)
            state.buffer.clear()
            state.buffer_bytes = 0
            state.main_count = 0
            state.term_count = 0

    def _state_for(self, stream_id: str) -> _StreamEventState:
        with self._states_lock:
            state = self._states.get(stream_id)
            if state is None:
                state = _StreamEventState()
                self._states[stream_id] = state
            return state

    def _append_locked(self, state: _StreamEventState, message: CommunicationMessage) -> None:
        size = _approx_message_bytes(message)
        state.buffer.append(message)
        state.buffer_bytes += size
        self._global_bytes += size
        if _is_term_event(message):
            state.term_count += 1
        else:
            state.main_count += 1

    def _remove_message_locked(self, state: _StreamEventState, message: CommunicationMessage) -> None:
        size = _approx_message_bytes(message)
        state.buffer_bytes = max(0, state.buffer_bytes - size)
        self._global_bytes = max(0, self._global_bytes - size)
        if _is_term_event(message):
            state.term_count = max(0, state.term_count - 1)
        else:
            state.main_count = max(0, state.main_count - 1)

    def _evict_stream_locked(self, state: _StreamEventState) -> None:
        while (
            len(state.buffer) > self._memory_ring_max_events
            or state.buffer_bytes > self._memory_ring_max_bytes
            or state.term_count > self._memory_ring_terminal_max_events
        ):
            victim = self._select_victim_locked(state)
            if victim is None:
                break
            # deque 从最旧扫描删除：victim 一定是 buffer 中某个元素；优先 popleft 快路径。
            if state.buffer and state.buffer[0] is victim:
                state.buffer.popleft()
            else:
                try:
                    state.buffer.remove(victim)
                except ValueError:
                    break
            self._remove_message_locked(state, victim)

    def _select_victim_locked(self, state: _StreamEventState) -> CommunicationMessage | None:
        if not state.buffer:
            return None
        # 1) TERM 超预算：必须删最旧 TERM
        if state.term_count > self._memory_ring_terminal_max_events:
            for message in state.buffer:
                if _is_term_event(message):
                    return message
        over_hard = (
            len(state.buffer) > self._memory_ring_max_events
            or state.buffer_bytes > self._memory_ring_max_bytes
        )
        if not over_hard:
            return None
        # 2) 优先删最旧 TERM
        for message in state.buffer:
            if _is_term_event(message):
                return message
        # 3) 无 TERM：MAIN 超过软保底时删最旧 MAIN；否则最后手段仍删最旧 MAIN
        if state.main_count > self._memory_ring_main_min_events:
            return state.buffer[0]
        return state.buffer[0]

    def _evict_global(self) -> None:
        guard = 0
        while self._global_bytes > self._global_memory_ring_max_bytes and guard < 100000:
            guard += 1
            stream_id = self._pick_stream_for_global_evict()
            if stream_id is None:
                break
            state = self._state_for(stream_id)
            with state.lock:
                if not state.buffer:
                    self.drop_stream_state(stream_id)
                    continue
                victim = self._select_victim_locked(state) or state.buffer[0]
                if state.buffer and state.buffer[0] is victim:
                    state.buffer.popleft()
                else:
                    try:
                        state.buffer.remove(victim)
                    except ValueError:
                        continue
                self._remove_message_locked(state, victim)

    def _pick_stream_for_global_evict(self) -> str | None:
        with self._states_lock:
            items = list(self._states.items())
        if not items:
            return None

        def sort_key(item: tuple[str, _StreamEventState]) -> tuple[int, float, float, str]:
            stream_id, state = item
            with state.lock:
                has_subs = 1 if state.subscribers else 0
                return (has_subs, state.last_publish_at, state.last_subscribe_at, stream_id)

        items.sort(key=sort_key)
        return items[0][0]

    def _idle_reclaim_loop(self) -> None:
        while not self._reclaimer_stop.wait(self._idle_scan_interval_seconds):
            try:
                self._reclaim_idle_streams()
            except Exception:  # noqa: BLE001 - 后台回收不得打断进程
                logger.exception("communication ring idle reclaim failed")

    def _reclaim_idle_streams(self) -> None:
        now = time.monotonic()
        with self._states_lock:
            stream_ids = list(self._states.keys())
        for stream_id in stream_ids:
            state = self._states.get(stream_id)
            if state is None:
                continue
            with state.lock:
                if state.subscribers:
                    continue
                idle_publish = now - state.last_publish_at
                idle_subscribe = now - state.last_subscribe_at
                if idle_publish < self._stream_idle_reclaim_seconds or idle_subscribe < self._stream_idle_reclaim_seconds:
                    continue
                if self._stream_idle_shrink_events > 0 and len(state.buffer) > self._stream_idle_shrink_events:
                    while len(state.buffer) > self._stream_idle_shrink_events:
                        victim = state.buffer.popleft()
                        self._remove_message_locked(state, victim)
                    continue
            # shrink=0 或已缩到阈值：删除整个 stream 状态
            if self._stream_idle_shrink_events == 0:
                self.drop_stream_state(stream_id)
            else:
                with state.lock:
                    if not state.subscribers and len(state.buffer) <= self._stream_idle_shrink_events:
                        # 保留短尾，不删 stream，避免频繁重建；仅缩 ring。
                        pass


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

