# -*- coding: utf-8 -*-
"""通用通信模块北向网关实现。"""
from __future__ import annotations

from typing import AsyncIterator

from agent_runtime.communication.communication_message import CommunicationMessage
from agent_runtime.communication.communication_service import CommunicationService
from agent_runtime.communication.communication_stream import CommunicationStream
from agent_runtime.communication.north_gateway.communication_contracts import (
    CommunicationGateway,
    CreateCommunicationStreamRequest,
    PublishCommunicationMessageRequest,
)


class CommunicationGatewayImpl(CommunicationGateway):
    """通用通信模块北向网关实现。

    外部调用方通过该网关访问通信流、消息发布、历史读取和实时订阅能力，避免直接依赖仓储、
    消息总线或 SSE 等基础设施细节。
    """

    def __init__(self, service: CommunicationService) -> None:
        # 通用通信领域服务。
        self._service = service

    def create_stream(self, request: CreateCommunicationStreamRequest) -> CommunicationStream:
        """创建可订阅的逻辑通信流。"""
        return self._service.create_stream(
            stream_id=request.stream_id,
            topic=request.topic,
            owner_id=request.owner_id,
            metadata=request.metadata,
        )

    def get_stream(self, stream_id: str) -> CommunicationStream:
        """按 stream_id 获取通信流。"""
        return self._service.get_stream(stream_id)

    def close_stream(self, stream_id: str) -> CommunicationStream:
        """关闭通信流。"""
        return self._service.close_stream(stream_id)

    async def publish_message(
        self,
        request: PublishCommunicationMessageRequest,
    ) -> CommunicationMessage:
        """发布通信消息。"""
        return await self._service.publish_message(
            stream_id=request.stream_id,
            message_type=request.message_type,
            payload=request.payload,
            topic=request.topic,
            source=request.source,
            actor_id=request.actor_id,
            correlation_id=request.correlation_id,
            parent_message_id=request.parent_message_id,
            metadata=request.metadata,
            message_id=request.message_id,
        )

    def list_messages(
        self,
        stream_id: str,
        *,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[CommunicationMessage]:
        """读取某条通信流的历史消息。"""
        return self._service.list_messages(stream_id, after_seq=after_seq, limit=limit)

    async def subscribe_messages(self, stream_id: str) -> AsyncIterator[CommunicationMessage]:
        """订阅某条通信流的新消息。"""
        async for message in self._service.subscribe_messages(stream_id):
            yield message
