from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any


FRONTEND_DEBUG_SESSION_STATUS_ACTIVE = "active"
FRONTEND_DEBUG_SESSION_STATUS_CLOSED = "closed"


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间，用于统一记录调试会话生命周期时间。"""
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class FrontendDebugSession:
    """前端调试会话聚合根。

    一个调试会话表示 Agent 创建的一次“目标前端页面 ↔ 本地调试代理 ↔ Agent 聊天流”的绑定关系。
    会话保存 target_url、debug_url、task_id、agent_instance_id 和 communication_stream_id，
    使注入脚本回传 DOM 元素信息时，后端可以把元素上下文投递到正确的聊天输入框。
    """

    session_id: str
    target_url: str
    proxy_base_url: str
    debug_url: str
    agent_instance_id: str
    status: str = FRONTEND_DEBUG_SESSION_STATUS_ACTIVE
    session_name: str | None = None
    task_id: str | None = None
    communication_stream_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def close(self) -> "FrontendDebugSession":
        """关闭调试会话，表示该代理页面不再接收新的元素回传。"""
        return replace(
            self,
            status=FRONTEND_DEBUG_SESSION_STATUS_CLOSED,
            updated_at=utc_now(),
        )

    def ensure_active(self) -> None:
        """校验调试会话仍然可用。"""
        if self.status != FRONTEND_DEBUG_SESSION_STATUS_ACTIVE:
            raise ValueError(f"前端调试会话已关闭: {self.session_id}")
