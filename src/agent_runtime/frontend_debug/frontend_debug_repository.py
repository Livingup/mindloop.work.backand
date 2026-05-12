from __future__ import annotations

from typing import Protocol

from agent_runtime.frontend_debug.frontend_debug_session import FrontendDebugSession


class FrontendDebugSessionRepository(Protocol):
    """前端调试会话仓储接口。

    仓储只负责保存“调试代理会话 ↔ Agent 通信上下文”的映射关系，不负责 HTTP 代理、
    DOM 注入或消息推送等业务行为。领域服务通过该接口读取会话，避免依赖具体存储实现。
    """

    def save(self, session: FrontendDebugSession) -> FrontendDebugSession:
        """保存前端调试会话并返回当前版本。"""
        ...

    def get(self, session_id: str) -> FrontendDebugSession | None:
        """按会话 ID 获取前端调试会话；不存在时返回 None。"""
        ...

    def list_active(self) -> list[FrontendDebugSession]:
        """列出仍处于 active 状态的前端调试会话。"""
        ...
