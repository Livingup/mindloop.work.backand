# -*- coding: utf-8 -*-
"""通用通信领域服务。"""
from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator
from uuid import uuid4

from agent_runtime.communication.communication_message import CommunicationMessage
from agent_runtime.communication.communication_repository import CommunicationRepository
from agent_runtime.communication.communication_stream import CommunicationStream
from agent_runtime.communication.event_hub import InMemoryCommunicationEventHub

logger = logging.getLogger(__name__)
_COMMUNICATION_PUBLISH_SLOW_LOG_SECONDS = 1.0


def _new_id(prefix: str) -> str:
    """生成带业务前缀的通信对象 ID，方便前端、日志和排障识别对象类型。"""
    return f"{prefix}-{uuid4().hex}"


def _require_non_blank(value: str, field_name: str) -> None:
    """校验通信流和消息的关键字符串字段不能为空。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空。")
    if value != value.strip():
        raise ValueError(f"{field_name} 不能包含首尾空白字符。")


class CommunicationService:
    """通用通信领域服务。

    该服务提供业务无关的通信流创建、消息发布、历史消息读取和实时订阅能力。
    Agent、终端、文件操作等业务模块只需要把自身事件包装为通信消息发布到 stream_id。
    """

    def __init__(
        self,
        repository: CommunicationRepository,
        event_hub: InMemoryCommunicationEventHub,
    ) -> None:
        # 通信流元数据仓储。runtime event 不再通过 SQLite 消息日志承载。
        self._repository = repository

        # 进程内短期事件流。负责 stream 内 seq、短时间断线续传和 SSE 订阅分发。
        self._event_hub = event_hub

        self._stream_cache: dict[str, CommunicationStream] = {}

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
        created = self._repository.create_stream(stream)
        self._stream_cache[created.stream_id] = created
        return created

    def get_stream(self, stream_id: str) -> CommunicationStream:
        """按 stream_id 获取通信流。"""
        _require_non_blank(stream_id, "stream_id")
        cached = self._stream_cache.get(stream_id)
        if cached is not None:
            return cached
        stream = self._repository.get_stream(stream_id)
        if stream is None:
            raise KeyError(f"通信流不存在: {stream_id}")
        self._stream_cache[stream_id] = stream
        return stream

    def close_stream(self, stream_id: str) -> CommunicationStream:
        """关闭通信流。"""
        stream = self.get_stream(stream_id)
        closed = stream.close()
        saved = self._repository.save_stream(closed)
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
        logger.debug(
            "通信消息发布开始: stream_id=%s, message_type=%s, source=%s, correlation_id=%s",
            stream_id,
            message_type,
            source,
            correlation_id,
        )
        _require_non_blank(stream_id, "stream_id")
        _require_non_blank(message_type, "message_type")
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
        saved = await self._event_hub.publish(message)
        duration_seconds = time.perf_counter() - started_at
        if duration_seconds >= _COMMUNICATION_PUBLISH_SLOW_LOG_SECONDS:
            logger.warning(
                "通信消息发布较慢: stream_id=%s, message_type=%s, seq=%s, duration_ms=%s",
                stream_id,
                message_type,
                saved.seq,
                int(duration_seconds * 1000),
            )
        return saved

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
        return self._event_hub.list_messages(
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
        return self._event_hub.list_latest_messages(
            stream_id,
            limit=max(min(limit, 5000), 1),
        )

    async def subscribe_messages(
        self,
        stream_id: str,
        *,
        after_seq: int = 0,
    ) -> AsyncIterator[CommunicationMessage]:
        """订阅某条通信流的新消息。"""
        _require_non_blank(stream_id, "stream_id")
        self.get_stream(stream_id)
        async for message in self._event_hub.subscribe(stream_id, after_seq=max(after_seq, 0)):
            yield message
