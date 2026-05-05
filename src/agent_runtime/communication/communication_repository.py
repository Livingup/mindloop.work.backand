# -*- coding: utf-8 -*-
"""通用通信模块仓储接口。"""
from __future__ import annotations

from typing import Protocol

from agent_runtime.communication.communication_message import CommunicationMessage
from agent_runtime.communication.communication_stream import CommunicationStream


class CommunicationRepository(Protocol):
    """通信流与通信消息仓储接口。"""

    def create_stream(self, stream: CommunicationStream) -> CommunicationStream:
        """创建通信流。stream_id 已存在时抛出 ValueError。"""
        ...

    def save_stream(self, stream: CommunicationStream) -> CommunicationStream:
        """保存通信流当前状态。"""
        ...

    def get_stream(self, stream_id: str) -> CommunicationStream | None:
        """按 stream_id 获取通信流，不存在时返回 None。"""
        ...

    def append_message(self, message: CommunicationMessage) -> CommunicationMessage:
        """追加通信消息，并分配流内递增 seq。"""
        ...

    def list_messages(
        self,
        stream_id: str,
        *,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[CommunicationMessage]:
        """按通信流和 seq 增量读取消息。"""
        ...
