# -*- coding: utf-8 -*-
"""通用通信模块 REST/SSE 接入适配。"""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent_runtime.communication.communication_message import CommunicationMessage
from agent_runtime.communication.north_gateway.communication_contracts import (
    CommunicationGateway,
    CreateCommunicationStreamRequest,
    PublishCommunicationMessageRequest,
)


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
        try:
            gateway.get_stream(stream_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"通信流不存在: {stream_id}")

        async def event_generator() -> AsyncIterator[str]:
            for message in gateway.list_messages(stream_id, after_seq=after_seq, limit=5000):
                if await request.is_disconnected():
                    return
                yield _format_sse_message(message)

            subscription = gateway.subscribe_messages(stream_id)
            next_message = asyncio.create_task(anext(subscription))
            try:
                while True:
                    done, _pending = await asyncio.wait(
                        {next_message},
                        timeout=15,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if await request.is_disconnected():
                        return
                    if done:
                        message = next_message.result()
                        yield _format_sse_message(message)
                        next_message = asyncio.create_task(anext(subscription))
                    else:
                        yield ": keep-alive\n\n"
            finally:
                next_message.cancel()
                with suppress(asyncio.CancelledError):
                    await next_message
                await subscription.aclose()

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
    return f"id: {message.seq}\nevent: {message.type}\ndata: {payload}\n\n"
