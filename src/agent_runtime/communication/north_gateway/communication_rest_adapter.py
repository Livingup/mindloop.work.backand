# -*- coding: utf-8 -*-
"""通用通信模块 REST/SSE 接入适配。"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent_runtime.communication.communication_message import CommunicationMessage
from agent_runtime.communication.north_gateway.communication_contracts import (
    CommunicationGateway,
    CreateCommunicationStreamRequest,
    PublishCommunicationMessageRequest,
)
from agent_runtime.runtime_network_monitor import (
    new_network_trace_id,
    runtime_network_monitor,
)


COMMUNICATION_SSE_EVENT_TYPE = "communication.message"
COMMUNICATION_SSE_PING_EVENT_TYPE = "communication.ping"
COMMUNICATION_SSE_KEEPALIVE_SECONDS = 15


class CommunicationStreamCreateBody(BaseModel):
    """创建通信流请求体。"""

    topic: str
    owner_id: str | None = None
    stream_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CommunicationStreamResponse(BaseModel):
    """通信流响应体。"""

    stream_id: str
    topic: str
    owner_id: str | None = None
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class CommunicationMessagePublishBody(BaseModel):
    """发布通信消息请求体。"""

    message_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    topic: str | None = None
    source: str | None = None
    actor_id: str | None = None
    correlation_id: str | None = None
    parent_message_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    message_id: str | None = None


class CommunicationMessageResponse(BaseModel):
    """通信消息响应体。"""

    message_id: str
    stream_id: str
    seq: int
    type: str
    topic: str
    source: str | None = None
    actor_id: str | None = None
    correlation_id: str | None = None
    parent_message_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    schema_version: str


def create_communication_router(gateway: CommunicationGateway) -> APIRouter:
    """根据通信网关创建 REST/SSE 路由。"""
    router = APIRouter()

    @router.post("/streams", response_model=CommunicationStreamResponse, status_code=201)
    def create_stream(body: CommunicationStreamCreateBody) -> CommunicationStreamResponse:
        """创建逻辑通信流。"""
        try:
            stream = gateway.create_stream(
                CreateCommunicationStreamRequest(
                    stream_id=body.stream_id,
                    topic=body.topic,
                    owner_id=body.owner_id,
                    metadata=body.metadata,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return _stream_response(stream)

    @router.get("/streams/{stream_id}", response_model=CommunicationStreamResponse)
    def get_stream(stream_id: str) -> CommunicationStreamResponse:
        """读取逻辑通信流。"""
        try:
            stream = gateway.get_stream(stream_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"通信流不存在: {stream_id}")
        return _stream_response(stream)

    @router.post("/streams/{stream_id}/close", response_model=CommunicationStreamResponse)
    def close_stream(stream_id: str) -> CommunicationStreamResponse:
        """关闭逻辑通信流。"""
        try:
            stream = gateway.close_stream(stream_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"通信流不存在: {stream_id}")
        return _stream_response(stream)

    @router.get("/streams/{stream_id}/messages", response_model=list[CommunicationMessageResponse])
    def list_messages(
        stream_id: str,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=5000),
        latest: bool = Query(default=False),
    ) -> list[CommunicationMessageResponse]:
        """读取通信流历史消息。"""
        try:
            # latest 用于聊天场景恢复：只取尾部消息窗口，但返回顺序仍保持 seq 升序，方便前端按事件顺序重放。
            messages = (
                gateway.list_latest_messages(stream_id, limit=limit)
                if latest
                else gateway.list_messages(stream_id, after_seq=after_seq, limit=limit)
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"通信流不存在: {stream_id}")
        return [_message_response(message) for message in messages]

    @router.post("/streams/{stream_id}/messages", response_model=CommunicationMessageResponse, status_code=201)
    async def publish_message(
        stream_id: str,
        body: CommunicationMessagePublishBody,
    ) -> CommunicationMessageResponse:
        """发布通信消息。"""
        try:
            message = await gateway.publish_message(
                PublishCommunicationMessageRequest(
                    stream_id=stream_id,
                    message_type=body.message_type,
                    payload=body.payload,
                    topic=body.topic,
                    source=body.source,
                    actor_id=body.actor_id,
                    correlation_id=body.correlation_id,
                    parent_message_id=body.parent_message_id,
                    metadata=body.metadata,
                    message_id=body.message_id,
                )
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"通信流不存在: {stream_id}")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return _message_response(message)

    @router.get("/streams/{stream_id}/events")
    async def stream_events(
        stream_id: str,
        request: Request,
        after_seq: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        """通过 SSE 订阅通信流消息。"""
        trace_id = request.headers.get("x-network-trace-id") or request.query_params.get("network_trace_id") or new_network_trace_id("sse")
        started_at = time.perf_counter()
        try:
            gateway.get_stream(stream_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"通信流不存在: {stream_id}")
        effective_after_seq = _effective_after_seq(after_seq, request.headers.get("last-event-id"))

        async def event_generator() -> AsyncIterator[str]:
            active_sse = runtime_network_monitor.begin_sse_connection()
            runtime_network_monitor.record({
                "layer": "backend",
                "type": "sse.subscribe.start",
                "trace_id": trace_id,
                "stream_id": stream_id,
                "after_seq": effective_after_seq,
                "active_sse_connections": active_sse,
                **runtime_network_monitor.snapshot(),
            })
            subscription = gateway.subscribe_messages(stream_id, after_seq=effective_after_seq)
            # Request-local helper task: the SSE generator owns cancellation and cleanup so keepalive
            # timeouts, disconnects, and subscription.aclose() stay in the same request boundary.
            next_message = asyncio.create_task(anext(subscription))
            sent_count = 0
            keepalive_count = 0
            first_event_recorded = False
            close_reason = "completed"
            try:
                while True:
                    done, _pending = await asyncio.wait(
                        {next_message},
                        timeout=COMMUNICATION_SSE_KEEPALIVE_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if await request.is_disconnected():
                        close_reason = "client_disconnected"
                        return
                    if done:
                        message = next_message.result()
                        sent_count += 1
                        if not first_event_recorded:
                            first_event_recorded = True
                            runtime_network_monitor.record({
                                "layer": "backend",
                                "type": "sse.first_event",
                                "trace_id": trace_id,
                                "stream_id": stream_id,
                                "after_seq": effective_after_seq,
                                "first_seq": message.seq,
                                "duration_ms": round((time.perf_counter() - started_at) * 1000),
                                **runtime_network_monitor.snapshot(),
                            })
                        yield _format_sse_message(message)
                        next_message = asyncio.create_task(anext(subscription))
                    else:
                        keepalive_count += 1
                        # comment keep-alive 服务代理/网关保活；named ping 供浏览器 JS 观测 transport 活性。
                        yield ": keep-alive\n\n"
                        yield _format_sse_ping(
                            stream_id=stream_id,
                            sent_count=sent_count,
                            keepalive_count=keepalive_count,
                        )
            except Exception as exc:
                close_reason = exc.__class__.__name__
                runtime_network_monitor.record({
                    "layer": "backend",
                    "type": "sse.stream.error",
                    "trace_id": trace_id,
                    "stream_id": stream_id,
                    "after_seq": effective_after_seq,
                    "duration_ms": round((time.perf_counter() - started_at) * 1000),
                    "sent_count": sent_count,
                    "keepalive_count": keepalive_count,
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                    **runtime_network_monitor.snapshot(),
                })
                raise
            finally:
                next_message.cancel()
                with suppress(asyncio.CancelledError):
                    await next_message
                await subscription.aclose()
                active_after = runtime_network_monitor.end_sse_connection()
                runtime_network_monitor.record({
                    "layer": "backend",
                    "type": "sse.close",
                    "trace_id": trace_id,
                    "stream_id": stream_id,
                    "after_seq": effective_after_seq,
                    "duration_ms": round((time.perf_counter() - started_at) * 1000),
                    "sent_count": sent_count,
                    "keepalive_count": keepalive_count,
                    "close_reason": close_reason,
                    "active_sse_connections": active_after,
                    **runtime_network_monitor.snapshot(),
                })

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


def _stream_response(stream: Any) -> CommunicationStreamResponse:
    """把通信流实体转换为 REST 响应体。"""
    return CommunicationStreamResponse(
        stream_id=stream.stream_id,
        topic=stream.topic,
        owner_id=stream.owner_id,
        status=stream.status,
        metadata=stream.metadata,
        created_at=stream.created_at.isoformat(),
        updated_at=stream.updated_at.isoformat(),
    )


def _message_response(message: CommunicationMessage) -> CommunicationMessageResponse:
    """把通信消息实体转换为 REST 响应体。"""
    return CommunicationMessageResponse(**message.to_dict())


def _format_sse_message(message: CommunicationMessage) -> str:
    """把通信消息格式化为 SSE 帧。"""
    payload = json.dumps(message.to_dict(), ensure_ascii=False, separators=(",", ":"))
    return f"id: {message.seq}\nevent: {COMMUNICATION_SSE_EVENT_TYPE}\ndata: {payload}\n\n"


def _format_sse_ping(*, stream_id: str, sent_count: int, keepalive_count: int) -> str:
    """格式化浏览器可见的 SSE ping 帧，不占用业务 seq，也不写入通信消息日志。"""
    payload = json.dumps(
        {
            "stream_id": stream_id,
            "server_time": datetime.now(timezone.utc).isoformat(),
            "sent_count": sent_count,
            "keepalive_count": keepalive_count,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {COMMUNICATION_SSE_PING_EVENT_TYPE}\ndata: {payload}\n\n"


def _effective_after_seq(after_seq: int, last_event_id: str | None) -> int:
    """计算 SSE 断线恢复游标：query after_seq 与 Last-Event-ID 取较大有效 seq。"""
    return max(after_seq, _last_event_id_to_seq(last_event_id), 0)


def _last_event_id_to_seq(value: str | None) -> int:
    if value is None:
        return 0
    try:
        return max(int(value), 0)
    except ValueError:
        return 0
