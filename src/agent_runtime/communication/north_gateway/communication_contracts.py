# -*- coding: utf-8 -*-
"""通用通信模块北向网关契约。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from agent_runtime.communication.communication_message import CommunicationMessage
from agent_runtime.communication.communication_stream import CommunicationStream


@dataclass(frozen=True, slots=True)
class CreateCommunicationStreamRequest:
    """创建通用通信流请求。"""

    # 通信流业务主题，例如 agent.task、terminal.session；通用层不解释主题含义。
    topic: str

    # 通信流所有者标识，可映射为用户、服务或系统进程。
    owner_id: str | None = None

    # 可选通信流 ID。不传时由通信服务生成。
    stream_id: str | None = None

    # 通信流元数据。业务模块可放 task_id、workspace_id 等关联信息。
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PublishCommunicationMessageRequest:
    """发布通用通信消息请求。"""

    # 目标逻辑通信流 ID。
    stream_id: str

    # 消息类型，例如 llm.request.started、tool.call.completed。
    message_type: str

    # 消息载荷。
    payload: dict[str, Any] = field(default_factory=dict)

    # 可选主题。不传时使用目标通信流 topic。
    topic: str | None = None

    # 消息来源组件。
    source: str | None = None

    # 当前动作发起者。
    actor_id: str | None = None

    # 关联上游命令、工具调用或 LLM 请求的 ID。
    correlation_id: str | None = None

    # 父消息 ID。
    parent_message_id: str | None = None

    # 附加元数据。
    metadata: dict[str, Any] = field(default_factory=dict)

    # 可选消息 ID。不传时由通信服务生成。
    message_id: str | None = None


class CommunicationGateway(Protocol):
    """通用通信模块北向网关。"""

    def create_stream(self, request: CreateCommunicationStreamRequest) -> CommunicationStream:
        """创建可订阅的逻辑通信流。"""
        ...

    def get_stream(self, stream_id: str) -> CommunicationStream:
        """按 stream_id 获取通信流。"""
        ...

    def close_stream(self, stream_id: str) -> CommunicationStream:
        """关闭通信流。"""
        ...

    async def publish_message(
        self,
        request: PublishCommunicationMessageRequest,
    ) -> CommunicationMessage:
        """发布通信消息。"""
        ...

    def list_messages(
        self,
        stream_id: str,
        *,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[CommunicationMessage]:
        """读取某条通信流的历史消息。"""
        ...

    async def subscribe_messages(self, stream_id: str) -> AsyncIterator[CommunicationMessage]:
        """订阅某条通信流的新消息。"""
        ...
