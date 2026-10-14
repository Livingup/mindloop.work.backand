# -*- coding: utf-8 -*-
"""通用通信流领域实体。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

CommunicationStreamStatus = Literal["active", "closed"]


def utc_now() -> datetime:
    """生成带 UTC 时区的当前时间，用于通信流与消息时间戳。"""
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class CommunicationStream:
    """可被一个或多个客户端订阅的逻辑通信流。

    通信流只表达消息分发的逻辑范围，不绑定 SSE、WebSocket 等具体物理连接。
    Agent 任务流、终端会话流、CLI UI 流都可以在业务层映射为该通用流。
    """

    # 通信流唯一标识。业务对象只需要持有该 ID 即可发布消息。
    stream_id: str

    # 通信流所属业务主题，例如 agent.task、terminal.session；通用层不解释主题含义。
    topic: str

    # 流所有者或创建者标识，可映射为用户 ID、服务 ID 或系统进程 ID。
    owner_id: str | None = None

    # 通信流状态。closed 后不再接受新消息发布。
    status: CommunicationStreamStatus = "active"

    # 通信流元数据。业务层可放 task_id、workspace_id 等关联信息。
    metadata: dict[str, Any] = field(default_factory=dict)

    # 创建时间。
    created_at: datetime = field(default_factory=utc_now)

    # 更新时间。
    updated_at: datetime = field(default_factory=utc_now)

    def ensure_active(self) -> None:
        """校验通信流仍可发布消息。"""
        if self.status != "active":
            raise ValueError(f"通信流已关闭，不能继续发布消息: {self.stream_id}")

    def close(self) -> "CommunicationStream":
        """关闭通信流，阻止后续继续写入实时消息。"""
        return CommunicationStream(
            stream_id=self.stream_id,
            topic=self.topic,
            owner_id=self.owner_id,
            status="closed",
            metadata=dict(self.metadata),
            created_at=self.created_at,
            updated_at=utc_now(),
        )
