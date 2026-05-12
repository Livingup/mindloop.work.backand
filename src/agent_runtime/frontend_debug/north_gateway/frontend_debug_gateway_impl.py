from __future__ import annotations

from agent_runtime.frontend_debug.frontend_debug_service import FrontendDebugService
from agent_runtime.frontend_debug.frontend_debug_session import FrontendDebugSession
from agent_runtime.frontend_debug.north_gateway.frontend_debug_contracts import (
    CreateFrontendDebugSessionRequest,
    FrontendDebugSessionInfo,
)


class FrontendDebugGatewayImpl:
    """前端调试北向网关实现。

    该网关面向 Agent 本地工具暴露前端调试会话创建能力，负责把工具层 DTO 转换为领域服务调用，
    并把领域会话聚合根转换为稳定的对外 DTO。
    """

    def __init__(self, service: FrontendDebugService) -> None:
        # 前端调试领域服务，承载调试会话生命周期和元素选择事件回传业务逻辑。
        self._service = service

    def create_session(self, request: CreateFrontendDebugSessionRequest) -> FrontendDebugSessionInfo:
        """创建本机前端调试代理会话。"""
        session = self._service.create_session(request)
        return _session_to_info(session)


def _session_to_info(session: FrontendDebugSession) -> FrontendDebugSessionInfo:
    """把前端调试会话聚合根转换为北向 DTO。"""
    return FrontendDebugSessionInfo(
        session_id=session.session_id,
        target_url=session.target_url,
        proxy_base_url=session.proxy_base_url,
        debug_url=session.debug_url,
        status=session.status,
        session_name=session.session_name,
        task_id=session.task_id,
        agent_instance_id=session.agent_instance_id,
        communication_stream_id=session.communication_stream_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
        metadata=dict(session.metadata or {}),
    )
