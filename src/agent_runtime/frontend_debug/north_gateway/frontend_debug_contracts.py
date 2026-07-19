from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CreateFrontendDebugSessionRequest:
    """创建前端调试会话请求。

    target_url 是用户或 Agent 已启动的前端页面地址；工具会为该地址创建本机代理 URL，
    并在代理返回的 HTML 中注入元素选择脚本。
    """

    target_url: str
    agent_instance_id: str
    session_name: str | None = None
    task_id: str | None = None
    communication_stream_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FrontendDebugSessionInfo:
    """前端调试会话对外 DTO。"""

    session_id: str
    target_url: str
    proxy_base_url: str
    debug_url: str
    status: str
    session_name: str | None
    task_id: str | None
    agent_instance_id: str
    communication_stream_id: str | None
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


class FrontendDebugGateway(Protocol):
    """前端调试模块北向网关契约。"""

    def create_session(self, request: CreateFrontendDebugSessionRequest) -> FrontendDebugSessionInfo:
        """创建本机前端调试代理会话。"""
        ...
