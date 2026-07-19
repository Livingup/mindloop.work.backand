# -*- coding: utf-8 -*-
"""通用通信领域服务。"""
from __future__ import annotations

import asyncio
import logging
import time
import threading
from contextlib import suppress
from threading import RLock
from typing import Any, AsyncIterator
from uuid import uuid4

from agent_runtime.communication.communication_errors import CommunicationGapUnrecoverableError
from agent_runtime.communication.communication_message import CommunicationMessage
from agent_runtime.communication.communication_repository import CommunicationRepository
from agent_runtime.communication.communication_stream import CommunicationStream
from agent_runtime.communication.event_hub import InMemoryCommunicationEventHub
from agent_runtime.communication.message_owner_policy import require_message_agent_instance_id
from agent_runtime.infrastructure.asyncio_diagnostics import BlockingPool, monitored_to_thread

logger = logging.getLogger(__name__)
_COMMUNICATION_PUBLISH_SLOW_LOG_SECONDS = 1.0
_COMMUNICATION_TO_THREAD_QUEUE_SLOW_LOG_SECONDS = 1.0


def _new_id(prefix: str) -> str:
    """生成带业务前缀的通信对象 ID，方便前端、日志和排障识别对象类型。"""
    return f"{prefix}-{uuid4().hex}"


def _require_non_blank(value: str, field_name: str) -> None:
    """校验通信流和消息的关键字符串字段不能为空。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空。")
    if value != value.strip():
        raise ValueError(f"{field_name} 不能包含首尾空白字符。")


def _duration_ms_since(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _current_task_name() -> str | None:
    task = asyncio.current_task()
    return task.get_name() if task is not None else None


def _trace_id_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata:
        return None
    value = metadata.get("trace_id")
    return str(value) if value is not None else None


def _log_publish_diagnostic(
    level: int,
    message: str,
    *,
    trace_id: str | None,
    stream_id: str,
    event_type: str,
    operation: str,
    phase: str,
    task_name: str | None,
    duration_ms: int | None = None,
    timeout_seconds: float | None = None,
    correlation_id: str | None = None,
    message_id: str | None = None,
    seq: int | None = None,
    executor_queue_delay_ms: int | None = None,
    worker_thread_name: str | None = None,
    error_type: str | None = None,
    error: str | None = None,
) -> None:
    if not logger.isEnabledFor(level):
        return
    logger.log(
        level,
        "%s: trace_id=%s stream_id=%s event_type=%s service_id=%s operation=%s phase=%s task_name=%s "
        "duration_ms=%s timeout_seconds=%s correlation_id=%s message_id=%s seq=%s "
        "executor_queue_delay_ms=%s worker_thread_name=%s error_type=%s error=%s",
        message,
        trace_id,
        stream_id,
        event_type,
        None,
        operation,
        phase,
        task_name,
        duration_ms,
        timeout_seconds,
        correlation_id,
        message_id,
        seq,
        executor_queue_delay_ms,
        worker_thread_name,
        error_type,
        error,
    )


class CommunicationService:
    """通用通信领域服务。

    该服务提供业务无关的通信流创建、消息发布、历史消息读取和实时订阅能力。
    Agent、终端、文件操作等业务模块只需要把自身事件包装为通信消息发布到 stream_id。
    """

    def __init__(
        self,
        repository: CommunicationRepository,
        event_hub: InMemoryCommunicationEventHub,
        *,
        storage_mode: str = "memory",
    ) -> None:
        # 通信流与消息日志仓储。sqlite 模式下是权威日志；memory 模式可不写入。
        self._repository = repository

        # 进程内 ring + fan-out。memory 模式下同时承担权威 seq 与短窗重放。
        self._event_hub = event_hub
        self._storage_mode = storage_mode if storage_mode in ("memory", "sqlite") else "memory"

        self._stream_cache: dict[str, CommunicationStream] = {}
        self._stream_cache_lock = RLock()

    @property
    def storage_mode(self) -> str:
        return self._storage_mode

    @property
    def runtime_boot_id(self) -> str:
        return self._event_hub.runtime_boot_id

    def _is_memory_mode(self) -> bool:
        return self._storage_mode == "memory"

    def create_stream(
        self,
        *,
        topic: str,
        owner_id: str | None = None,
        stream_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CommunicationStream:
        """创建一条可订阅的逻辑通信流。"""
        _require_non_blank(topic, "topic")
        actual_stream_id = stream_id or _new_id("stream")
        _require_non_blank(actual_stream_id, "stream_id")
        stream = CommunicationStream(
            stream_id=actual_stream_id,
            topic=topic,
            owner_id=owner_id,
            metadata=dict(metadata or {}),
        )
        if self._is_memory_mode():
            with self._stream_cache_lock:
                existing = self._stream_cache.get(actual_stream_id)
                if existing is not None:
                    raise ValueError(f"通信流已存在: {actual_stream_id}")
                self._stream_cache[actual_stream_id] = stream
            return stream
        created = self._repository.create_stream(stream)
        with self._stream_cache_lock:
            self._stream_cache[created.stream_id] = created
        return created

    def get_stream(self, stream_id: str) -> CommunicationStream:
        """按 stream_id 获取通信流。"""
        _require_non_blank(stream_id, "stream_id")
        with self._stream_cache_lock:
            cached = self._stream_cache.get(stream_id)
        if cached is not None:
            return cached
        if self._is_memory_mode():
            raise KeyError(f"通信流不存在: {stream_id}")
        stream = self._repository.get_stream(stream_id)
        if stream is None:
            raise KeyError(f"通信流不存在: {stream_id}")
        with self._stream_cache_lock:
            self._stream_cache[stream_id] = stream
        return stream

    def close_stream(self, stream_id: str) -> CommunicationStream:
        """关闭通信流。"""
        stream = self.get_stream(stream_id)
        closed = stream.close()
        if self._is_memory_mode():
            with self._stream_cache_lock:
                self._stream_cache[closed.stream_id] = closed
            return closed
        saved = self._repository.save_stream(closed)
        with self._stream_cache_lock:
            self._stream_cache[saved.stream_id] = saved
        return saved

    async def publish_message(
        self,
        *,
        stream_id: str,
        message_type: str,
        payload: dict[str, Any] | None = None,
        topic: str | None = None,
        source: str | None = None,
        actor_id: str | None = None,
        correlation_id: str | None = None,
        parent_message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> CommunicationMessage:
        """发布一条通信消息，并实时推送给当前进程内订阅者。"""
        started_at = time.perf_counter()
        task_name = _current_task_name()
        trace_id = _trace_id_from_metadata(metadata) or correlation_id
        operation = "communication.publish_message"
        _log_publish_diagnostic(
            logging.DEBUG,
            "Communication message publish started",
            trace_id=trace_id,
            stream_id=stream_id,
            event_type=message_type,
            operation=operation,
            phase="publish_message.started",
            task_name=task_name,
            duration_ms=0,
            correlation_id=correlation_id,
            message_id=message_id,
        )
        _require_non_blank(stream_id, "stream_id")
        _require_non_blank(message_type, "message_type")
        require_message_agent_instance_id(message_type, metadata)
        try:
            if self._is_memory_mode():
                return await self._publish_message_memory(
                    stream_id=stream_id,
                    message_type=message_type,
                    payload=payload,
                    topic=topic,
                    source=source,
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                    parent_message_id=parent_message_id,
                    metadata=metadata,
                    message_id=message_id,
                    started_at=started_at,
                    task_name=task_name,
                    trace_id=trace_id,
                    operation=operation,
                )
            to_thread_submitted_at = time.perf_counter()
            saved = await monitored_to_thread(
                self._append_message_sync_with_diagnostics,
                operation="communication.publish_message.append",
                caller="CommunicationService.publish_message",
                pool=BlockingPool.COMMUNICATION,
                log=logger,
                diagnostic_context={
                    "trace_id": trace_id,
                    "stream_id": stream_id,
                    "event_type": message_type,
                    "correlation_id": correlation_id,
                    "message_id": message_id,
                },
                stream_id=stream_id,
                message_type=message_type,
                payload=payload,
                topic=topic,
                source=source,
                actor_id=actor_id,
                correlation_id=correlation_id,
                parent_message_id=parent_message_id,
                metadata=metadata,
                message_id=message_id,
                diagnostics_trace_id=trace_id,
                diagnostics_task_name=task_name,
                diagnostics_submitted_at=to_thread_submitted_at,
            )
            to_thread_duration_ms = _duration_ms_since(to_thread_submitted_at)
            _log_publish_diagnostic(
                logging.DEBUG,
                "Communication message append thread await completed",
                trace_id=trace_id,
                stream_id=stream_id,
                event_type=message_type,
                operation=operation,
                phase="to_thread.await_completed",
                task_name=task_name,
                duration_ms=to_thread_duration_ms,
                correlation_id=correlation_id,
                message_id=saved.message_id,
                seq=saved.seq,
            )

            hub_started_at = time.perf_counter()
            _log_publish_diagnostic(
                logging.DEBUG,
                "Communication event hub publish started",
                trace_id=trace_id,
                stream_id=stream_id,
                event_type=message_type,
                operation=operation,
                phase="event_hub.publish.started",
                task_name=task_name,
                duration_ms=0,
                correlation_id=correlation_id,
                message_id=saved.message_id,
                seq=saved.seq,
            )
            try:
                await self._event_hub.publish(saved)
            except asyncio.CancelledError:
                _log_publish_diagnostic(
                    logging.WARNING,
                    "Communication event hub publish cancelled",
                    trace_id=trace_id,
                    stream_id=stream_id,
                    event_type=message_type,
                    operation=operation,
                    phase="event_hub.publish.cancelled",
                    task_name=task_name,
                    duration_ms=_duration_ms_since(hub_started_at),
                    correlation_id=correlation_id,
                    message_id=saved.message_id,
                    seq=saved.seq,
                    error_type="CancelledError",
                )
                raise
            except Exception as exc:
                _log_publish_diagnostic(
                    logging.ERROR,
                    "Communication event hub publish failed",
                    trace_id=trace_id,
                    stream_id=stream_id,
                    event_type=message_type,
                    operation=operation,
                    phase="event_hub.publish.exception",
                    task_name=task_name,
                    duration_ms=_duration_ms_since(hub_started_at),
                    correlation_id=correlation_id,
                    message_id=saved.message_id,
                    seq=saved.seq,
                    error_type=exc.__class__.__name__,
                    error=str(exc),
                )
                raise
            hub_duration_ms = _duration_ms_since(hub_started_at)
            hub_level = (
                logging.WARNING
                if hub_duration_ms >= int(_COMMUNICATION_PUBLISH_SLOW_LOG_SECONDS * 1000)
                else logging.DEBUG
            )
            _log_publish_diagnostic(
                hub_level,
                "Communication event hub publish completed",
                trace_id=trace_id,
                stream_id=stream_id,
                event_type=message_type,
                operation=operation,
                phase="event_hub.publish.completed",
                task_name=task_name,
                duration_ms=hub_duration_ms,
                correlation_id=correlation_id,
                message_id=saved.message_id,
                seq=saved.seq,
            )

            duration_ms = _duration_ms_since(started_at)
            level = (
                logging.WARNING
                if duration_ms >= int(_COMMUNICATION_PUBLISH_SLOW_LOG_SECONDS * 1000)
                else logging.DEBUG
            )
            _log_publish_diagnostic(
                level,
                "Communication message publish completed",
                trace_id=trace_id,
                stream_id=stream_id,
                event_type=message_type,
                operation=operation,
                phase="publish_message.completed",
                task_name=task_name,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
                message_id=saved.message_id,
                seq=saved.seq,
            )
            return saved
        except asyncio.CancelledError:
            _log_publish_diagnostic(
                logging.WARNING,
                "Communication message publish cancelled",
                trace_id=trace_id,
                stream_id=stream_id,
                event_type=message_type,
                operation=operation,
                phase="publish_message.cancelled",
                task_name=task_name,
                duration_ms=_duration_ms_since(started_at),
                correlation_id=correlation_id,
                message_id=message_id,
                error_type="CancelledError",
            )
            raise
        except Exception as exc:
            logger.exception(
                "Communication message publish failed: trace_id=%s stream_id=%s event_type=%s service_id=%s operation=%s "
                "phase=%s task_name=%s duration_ms=%s timeout_seconds=%s correlation_id=%s message_id=%s error_type=%s error=%s",
                trace_id,
                stream_id,
                message_type,
                None,
                operation,
                "publish_message.exception",
                task_name,
                _duration_ms_since(started_at),
                None,
                correlation_id,
                message_id,
                exc.__class__.__name__,
                exc,
            )
            raise

    def list_messages(
        self,
        stream_id: str,
        *,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[CommunicationMessage]:
        """读取某条通信流中的历史消息。"""
        _require_non_blank(stream_id, "stream_id")
        self.get_stream(stream_id)
        if self._is_memory_mode():
            return self._event_hub.list_messages(
                stream_id,
                after_seq=max(after_seq, 0),
                limit=max(min(limit, 5000), 1),
                enforce_contiguous_gap=True,
            )
        return self._repository.list_messages(
            stream_id,
            after_seq=max(after_seq, 0),
            limit=max(min(limit, 5000), 1),
        )

    def list_latest_messages(
        self,
        stream_id: str,
        *,
        limit: int = 500,
    ) -> list[CommunicationMessage]:
        """读取某条通信流最新一段历史消息。

        最新窗口用于聊天场景恢复：只取尾部 N 条消息，但仍按 seq 升序返回，
        让前端 hydrate/replay 时保持和实时事件一致的时间顺序。
        """
        _require_non_blank(stream_id, "stream_id")
        self.get_stream(stream_id)
        if self._is_memory_mode():
            return self._event_hub.list_latest_messages(
                stream_id,
                limit=max(min(limit, 5000), 1),
            )
        return self._repository.list_latest_messages(
            stream_id,
            limit=max(min(limit, 5000), 1),
        )

    def get_stream_diagnostics(self, stream_id: str) -> dict[str, Any]:
        _require_non_blank(stream_id, "stream_id")
        stream = self.get_stream(stream_id)
        if self._is_memory_mode():
            latest_messages = self._event_hub.list_latest_messages(stream_id, limit=1)
        else:
            latest_messages = self._repository.list_latest_messages(stream_id, limit=1)
        latest_message = latest_messages[-1] if latest_messages else None
        hub_diagnostics = self._event_hub.get_stream_diagnostics(stream_id)
        return {
            "stream_id": stream.stream_id,
            "topic": stream.topic,
            "owner_id": stream.owner_id,
            "status": stream.status,
            "metadata": dict(stream.metadata),
            "created_at": stream.created_at.isoformat(),
            "updated_at": stream.updated_at.isoformat(),
            "latest_seq": latest_message.seq if latest_message is not None else 0,
            "latest_message_type": latest_message.type if latest_message is not None else None,
            "latest_message_created_at": latest_message.created_at.isoformat() if latest_message is not None else None,
            "storage_mode": self._storage_mode,
            "runtime_boot_id": self.runtime_boot_id,
            **hub_diagnostics,
        }

    async def _iter_live_gap_messages(
        self,
        stream_id: str,
        *,
        after_seq: int,
        live_seq: int,
    ) -> AsyncIterator[CommunicationMessage]:
        """从 durable log 分页补齐到指定 live seq，禁止跨越缺口推进 cursor。"""
        cursor = max(after_seq, 0)
        while cursor < live_seq:
            gap_messages = await monitored_to_thread(
                self._repository.list_messages,
                stream_id,
                operation="communication.subscribe.gap_messages",
                caller="CommunicationService.subscribe_messages",
                pool=BlockingPool.COMMUNICATION,
                log=logger,
                diagnostic_context={
                    "stream_id": stream_id,
                    "cursor": cursor,
                    "live_seq": live_seq,
                    "limit": 5000,
                },
                after_seq=cursor,
                limit=5000,
            )
            if not gap_messages:
                raise RuntimeError(
                    "通信日志无法补齐实时事件缺口: "
                    f"stream_id={stream_id}, expected_seq={cursor + 1}, live_seq={live_seq}"
                )
            page_progressed = False
            for gap_message in gap_messages:
                if gap_message.seq <= cursor:
                    continue
                expected_seq = cursor + 1
                if gap_message.seq != expected_seq:
                    raise RuntimeError(
                        "通信日志存在不可恢复的实时事件缺口: "
                        f"stream_id={stream_id}, expected_seq={expected_seq}, "
                        f"actual_seq={gap_message.seq}, live_seq={live_seq}"
                    )
                cursor = gap_message.seq
                page_progressed = True
                yield gap_message
                if cursor >= live_seq:
                    return
            if not page_progressed:
                raise RuntimeError(
                    "通信日志补齐未产生进度: "
                    f"stream_id={stream_id}, cursor={cursor}, live_seq={live_seq}"
                )

    async def subscribe_messages(
        self,
        stream_id: str,
        *,
        after_seq: int = 0,
    ) -> AsyncIterator[CommunicationMessage]:
        """订阅某条通信流的新消息。"""
        _require_non_blank(stream_id, "stream_id")
        if self._is_memory_mode():
            self.get_stream(stream_id)
            cursor = max(after_seq, 0)
            subscription = self._event_hub.subscribe(stream_id, after_seq=cursor)
            try:
                async for message in subscription:
                    if message.seq <= cursor:
                        continue
                    cursor = message.seq
                    yield message
            finally:
                await subscription.aclose()
            return

        await monitored_to_thread(
            self.get_stream,
            stream_id,
            operation="communication.subscribe.get_stream",
            caller="CommunicationService.subscribe_messages",
            pool=BlockingPool.COMMUNICATION,
            log=logger,
            diagnostic_context={"stream_id": stream_id, "after_seq": after_seq},
        )
        cursor = max(after_seq, 0)
        subscription = self._event_hub.subscribe(stream_id, after_seq=cursor)
        # Request-local helper task: subscribe_messages owns cancellation/aclose so replay, gap-fill,
        # and disconnect cleanup remain ordered inside the consuming SSE/request coroutine.
        next_live_message = asyncio.create_task(anext(subscription))
        await asyncio.sleep(0)
        try:
            while True:
                replay_messages = await monitored_to_thread(
                    self._repository.list_messages,
                    stream_id,
                    operation="communication.subscribe.replay_messages",
                    caller="CommunicationService.subscribe_messages",
                    pool=BlockingPool.COMMUNICATION,
                    log=logger,
                    diagnostic_context={"stream_id": stream_id, "cursor": cursor, "limit": 5000},
                    after_seq=cursor,
                    limit=5000,
                )
                if not replay_messages:
                    break
                for message in replay_messages:
                    if message.seq <= cursor:
                        continue
                    cursor = message.seq
                    yield message
                if len(replay_messages) < 5000:
                    break
            while True:
                message = await next_live_message
                next_live_message = asyncio.create_task(anext(subscription))
                if message.seq <= cursor:
                    continue
                if message.seq > cursor + 1:
                    async for gap_message in self._iter_live_gap_messages(
                        stream_id,
                        after_seq=cursor,
                        live_seq=message.seq,
                    ):
                        cursor = gap_message.seq
                        yield gap_message
                    if message.seq <= cursor:
                        continue
                cursor = message.seq
                yield message
        finally:
            next_live_message.cancel()
            with suppress(asyncio.CancelledError):
                await next_live_message
            await subscription.aclose()

    async def _publish_message_memory(
        self,
        *,
        stream_id: str,
        message_type: str,
        payload: dict[str, Any] | None,
        topic: str | None,
        source: str | None,
        actor_id: str | None,
        correlation_id: str | None,
        parent_message_id: str | None,
        metadata: dict[str, Any] | None,
        message_id: str | None,
        started_at: float,
        task_name: str | None,
        trace_id: str | None,
        operation: str,
    ) -> CommunicationMessage:
        stream = self.get_stream(stream_id)
        stream.ensure_active()
        message = CommunicationMessage(
            message_id=message_id or _new_id("message"),
            stream_id=stream.stream_id,
            seq=0,
            type=message_type,
            topic=topic or stream.topic,
            source=source,
            actor_id=actor_id,
            correlation_id=correlation_id,
            parent_message_id=parent_message_id,
            payload=dict(payload or {}),
            metadata=dict(metadata or {}),
        )
        hub_started_at = time.perf_counter()
        saved = await self._event_hub.publish(message)
        hub_duration_ms = _duration_ms_since(hub_started_at)
        duration_ms = _duration_ms_since(started_at)
        level = (
            logging.WARNING
            if duration_ms >= int(_COMMUNICATION_PUBLISH_SLOW_LOG_SECONDS * 1000)
            else logging.DEBUG
        )
        _log_publish_diagnostic(
            level,
            "Communication memory message publish completed",
            trace_id=trace_id,
            stream_id=stream_id,
            event_type=message_type,
            operation=operation,
            phase="publish_message.memory.completed",
            task_name=task_name,
            duration_ms=duration_ms,
            correlation_id=correlation_id,
            message_id=saved.message_id,
            seq=saved.seq,
        )
        if hub_duration_ms >= int(_COMMUNICATION_PUBLISH_SLOW_LOG_SECONDS * 1000):
            _log_publish_diagnostic(
                logging.WARNING,
                "Communication event hub publish slow",
                trace_id=trace_id,
                stream_id=stream_id,
                event_type=message_type,
                operation=operation,
                phase="event_hub.publish.completed",
                task_name=task_name,
                duration_ms=hub_duration_ms,
                correlation_id=correlation_id,
                message_id=saved.message_id,
                seq=saved.seq,
            )
        return saved

    def _append_message_sync_with_diagnostics(
        self,
        *,
        stream_id: str,
        message_type: str,
        payload: dict[str, Any] | None,
        topic: str | None,
        source: str | None,
        actor_id: str | None,
        correlation_id: str | None,
        parent_message_id: str | None,
        metadata: dict[str, Any] | None,
        message_id: str | None,
        diagnostics_trace_id: str | None,
        diagnostics_task_name: str | None,
        diagnostics_submitted_at: float,
    ) -> CommunicationMessage:
        operation = "communication.publish_message"
        worker_started_at = time.perf_counter()
        queue_delay_ms = int((worker_started_at - diagnostics_submitted_at) * 1000)
        worker_thread_name = threading.current_thread().name
        queue_level = (
            logging.WARNING
            if queue_delay_ms >= int(_COMMUNICATION_TO_THREAD_QUEUE_SLOW_LOG_SECONDS * 1000)
            else logging.DEBUG
        )
        _log_publish_diagnostic(
            queue_level,
            "Communication append worker started",
            trace_id=diagnostics_trace_id,
            stream_id=stream_id,
            event_type=message_type,
            operation=operation,
            phase="to_thread.worker_started",
            task_name=diagnostics_task_name,
            duration_ms=queue_delay_ms,
            correlation_id=correlation_id,
            message_id=message_id,
            executor_queue_delay_ms=queue_delay_ms,
            worker_thread_name=worker_thread_name,
        )
        append_started_at = time.perf_counter()
        try:
            saved = self._append_message_sync(
                stream_id=stream_id,
                message_type=message_type,
                payload=payload,
                topic=topic,
                source=source,
                actor_id=actor_id,
                correlation_id=correlation_id,
                parent_message_id=parent_message_id,
                metadata=metadata,
                message_id=message_id,
            )
        except Exception as exc:
            _log_publish_diagnostic(
                logging.ERROR,
                "Communication append worker failed",
                trace_id=diagnostics_trace_id,
                stream_id=stream_id,
                event_type=message_type,
                operation=operation,
                phase="append_message_sync.exception",
                task_name=diagnostics_task_name,
                duration_ms=_duration_ms_since(append_started_at),
                correlation_id=correlation_id,
                message_id=message_id,
                worker_thread_name=worker_thread_name,
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            raise
        append_duration_ms = _duration_ms_since(append_started_at)
        append_level = (
            logging.WARNING
            if append_duration_ms >= int(_COMMUNICATION_PUBLISH_SLOW_LOG_SECONDS * 1000)
            else logging.DEBUG
        )
        _log_publish_diagnostic(
            append_level,
            "Communication append worker completed",
            trace_id=diagnostics_trace_id,
            stream_id=stream_id,
            event_type=message_type,
            operation=operation,
            phase="append_message_sync.completed",
            task_name=diagnostics_task_name,
            duration_ms=append_duration_ms,
            correlation_id=correlation_id,
            message_id=saved.message_id,
            seq=saved.seq,
            worker_thread_name=worker_thread_name,
        )
        return saved

    def _append_message_sync(
        self,
        *,
        stream_id: str,
        message_type: str,
        payload: dict[str, Any] | None,
        topic: str | None,
        source: str | None,
        actor_id: str | None,
        correlation_id: str | None,
        parent_message_id: str | None,
        metadata: dict[str, Any] | None,
        message_id: str | None,
    ) -> CommunicationMessage:
        stream = self.get_stream(stream_id)
        stream.ensure_active()
        message = CommunicationMessage(
            message_id=message_id or _new_id("message"),
            stream_id=stream.stream_id,
            seq=0,
            type=message_type,
            topic=topic or stream.topic,
            source=source,
            actor_id=actor_id,
            correlation_id=correlation_id,
            parent_message_id=parent_message_id,
            payload=dict(payload or {}),
            metadata=dict(metadata or {}),
        )
        return self._repository.append_message(message)
