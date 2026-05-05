# -*- coding: utf-8 -*-
"""通用通信消息领域实体。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent_runtime.communication.communication_stream import utc_now


@dataclass(frozen=True, slots=True)
class CommunicationMessage:
    """发布到逻辑通信流上的通用消息。

    该实体不理解 Agent、LLM、工具、终端等业务语义，只维护消息路由、排序、追踪和载荷。
    前端、CLI UI 或其他客户端可根据 type 与 payload 自行渲染。
    """

    # 消息唯一标识，用于 SSE Last-Event-ID、审计和排障。
    message_id: str

    # 所属逻辑通信流标识。
    stream_id: str

    # 流内递增序号，用于断线恢复、排序和增量读取。
    seq: int

    # 消息类型，例如 llm.request.started、tool.call.completed；通用层不解释类型含义。
    type: str

    # 消息所属主题，例如 agent.task、terminal.session；通常与通信流 topic 一致。
    topic: str

    # 消息来源组件，例如 agent_execution_engine、llm_client、tool_dispatcher。
    source: str | None = None

    # 当前动作发起者标识，例如 agent_instance_id、user_id 或 system。
    actor_id: str | None = None

    # 关联上游命令、工具调用或请求的标识。
    correlation_id: str | None = None

    # 父消息标识，用于表示事件之间的层级关系。
    parent_message_id: str | None = None

    # 实际业务载荷。通用通信层只保证 JSON 可序列化存储。
    payload: dict[str, Any] = field(default_factory=dict)

    # 额外元数据。业务层可放 task_id、trace_id、terminal_session_id 等索引信息。
    metadata: dict[str, Any] = field(default_factory=dict)

    # 创建时间。
    created_at: datetime = field(default_factory=utc_now)

    # 消息协议版本，便于后续前后端兼容演进。
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        """转换为 REST/SSE 可以直接输出的字典。"""
        return {
            "message_id": self.message_id,
            "stream_id": self.stream_id,
            "seq": self.seq,
            "type": self.type,
            "topic": self.topic,
            "source": self.source,
            "actor_id": self.actor_id,
            "correlation_id": self.correlation_id,
            "parent_message_id": self.parent_message_id,
            "payload": self.payload,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "schema_version": self.schema_version,
        }
