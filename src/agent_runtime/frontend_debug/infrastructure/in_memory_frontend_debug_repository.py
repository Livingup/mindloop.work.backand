from __future__ import annotations

from threading import RLock

from agent_runtime.frontend_debug.frontend_debug_repository import FrontendDebugSessionRepository
from agent_runtime.frontend_debug.frontend_debug_session import (
    FRONTEND_DEBUG_SESSION_STATUS_ACTIVE,
    FrontendDebugSession,
)


class InMemoryFrontendDebugSessionRepository(FrontendDebugSessionRepository):
    """前端调试会话内存仓储。

    调试代理会话是开发期临时状态，第一版只需要在当前 AgentRuntime 进程内维护即可。
    仓储使用线程锁保护，因为调试代理 HTTP 服务在线程中接收页面元素回传，工具调用也可能并发创建会话。
    """

    def __init__(self) -> None:
        # session_id → 调试会话聚合根。
        self._sessions: dict[str, FrontendDebugSession] = {}
        self._lock = RLock()

    def save(self, session: FrontendDebugSession) -> FrontendDebugSession:
        """保存前端调试会话。"""
        with self._lock:
            self._sessions[session.session_id] = session
            return session

    def get(self, session_id: str) -> FrontendDebugSession | None:
        """按会话 ID 获取前端调试会话。"""
        with self._lock:
            return self._sessions.get(session_id)

    def list_active(self) -> list[FrontendDebugSession]:
        """列出仍处于 active 状态的调试会话。"""
        with self._lock:
            return [
                session
                for session in self._sessions.values()
                if session.status == FRONTEND_DEBUG_SESSION_STATUS_ACTIVE
            ]
