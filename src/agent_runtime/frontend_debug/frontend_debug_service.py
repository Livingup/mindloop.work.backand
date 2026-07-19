from __future__ import annotations

from typing import Any, Callable, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from agent_runtime.frontend_debug.frontend_debug_repository import FrontendDebugSessionRepository
from agent_runtime.frontend_debug.frontend_debug_session import FrontendDebugSession, utc_now
from agent_runtime.frontend_debug.north_gateway.frontend_debug_contracts import (
    CreateFrontendDebugSessionRequest,
)


class FrontendDebugProxyManager(Protocol):
    """前端调试代理管理器抽象。

    领域服务只声明自己需要“为某个调试会话启动本机代理”的能力，不依赖具体 HTTP Server、
    线程模型或端口分配策略，保持业务会话管理和基础设施实现解耦。
    """

    def start_session_proxy(
        self,
        *,
        session_id: str,
        target_url: str,
        on_element_selected: Callable[[str, dict[str, Any]], None],
    ) -> str:
        """启动会话专属本机代理并返回代理基础地址。"""
        ...


class FrontendDebugElementSelectionPublisher(Protocol):
    """前端元素选择事件发布器抽象。"""

    def publish_element_selected(
        self,
        *,
        session: FrontendDebugSession,
        payload: dict[str, Any],
    ) -> None:
        """把元素选择事件发布到调试会话绑定的 Agent 通信流。"""
        ...


class FrontendDebugService:
    """前端调试领域服务。

    负责创建“目标前端页面 ↔ 本机代理地址 ↔ Agent 通信流”的调试会话，
    并在注入脚本回传 DOM 元素信息后，把元素上下文发布到创建该调试页的 Agent 聊天流。
    """

    def __init__(
        self,
        repository: FrontendDebugSessionRepository,
        proxy_manager: FrontendDebugProxyManager,
        element_selection_publisher: FrontendDebugElementSelectionPublisher,
    ) -> None:
        # 调试会话仓储，保存 session_id 到 Agent 通信上下文的映射。
        self._repository = repository
        # 本机代理管理器，负责为每个调试会话启动独立的 127.0.0.1 随机端口代理。
        self._proxy_manager = proxy_manager
        # 元素选择事件发布器，负责把 DOM 上下文投递回前端聊天输入框。
        self._element_selection_publisher = element_selection_publisher

    def create_session(self, request: CreateFrontendDebugSessionRequest) -> FrontendDebugSession:
        """创建前端调试会话。

        target_url 只要求是合法 http/https URL，不限制域名；用户通常会传入自己本机启动的
        前端开发地址。debug_url 会保持 target_url 的 path/query/fragment，方便直接打开同一路由。
        """
        target_url = _normalize_target_url(request.target_url)
        agent_instance_id = _require_agent_instance_id(request.agent_instance_id)
        session_id = f"frontend-debug-{uuid4().hex}"
        proxy_base_url = self._proxy_manager.start_session_proxy(
            session_id=session_id,
            target_url=target_url,
            on_element_selected=self.handle_element_selected,
        )
        debug_url = _build_debug_url(proxy_base_url, target_url)
        now = utc_now()
        session = FrontendDebugSession(
            session_id=session_id,
            target_url=target_url,
            proxy_base_url=proxy_base_url,
            debug_url=debug_url,
            session_name=request.session_name,
            task_id=request.task_id,
            agent_instance_id=agent_instance_id,
            communication_stream_id=request.communication_stream_id,
            request_id=request.request_id,
            trace_id=request.trace_id,
            created_at=now,
            updated_at=now,
            metadata=dict(request.metadata or {}),
        )
        return self._repository.save(session)

    def get_session(self, session_id: str) -> FrontendDebugSession:
        """读取前端调试会话；不存在时抛出 KeyError。"""
        session = self._repository.get(session_id)
        if session is None:
            raise KeyError(f"前端调试会话不存在: {session_id}")
        return session

    def build_element_selected_payload(
        self,
        session_id: str,
        element_payload: dict[str, Any],
    ) -> tuple[FrontendDebugSession, dict[str, Any]]:
        """构建前端元素选择事件载荷。

        该方法只负责业务载荷归一化，不直接发布消息，便于 HTTP 代理线程通过自身基础设施
        完成事件投递，同时保证 payload 中携带完整的调试会话和 DOM 元素上下文。
        """
        session = self.get_session(session_id)
        session.ensure_active()
        payload = {
            "session_id": session.session_id,
            "session_name": session.session_name,
            "target_url": session.target_url,
            "debug_url": session.debug_url,
            "selected_at": utc_now().isoformat(),
            "element": dict(element_payload or {}),
        }
        return session, payload

    def handle_element_selected(self, session_id: str, element_payload: dict[str, Any]) -> None:
        """处理注入脚本回传的 DOM 元素信息。

        代理线程收到页面 POST 后调用该方法；领域服务根据 session_id 找到创建该页面的 Agent
        通信流，再通过发布器发送 frontend.debug.element.selected 事件，让 web-frontend 回填输入框。
        """
        session, payload = self.build_element_selected_payload(session_id, element_payload)
        self._element_selection_publisher.publish_element_selected(
            session=session,
            payload=payload,
        )


def _require_agent_instance_id(agent_instance_id: str) -> str:
    value = agent_instance_id.strip() if isinstance(agent_instance_id, str) else ""
    if not value:
        raise ValueError("前端调试会话必须显式提供非空 agent_instance_id。")
    return value


def _normalize_target_url(target_url: str) -> str:
    """校验并标准化前端调试目标 URL。"""
    value = (target_url or "").strip()
    if not value:
        raise ValueError("target_url 不能为空。")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("target_url 仅支持 http 或 https URL。")
    if not parsed.netloc:
        raise ValueError("target_url 必须包含主机和端口信息。")
    return value


def _build_debug_url(proxy_base_url: str, target_url: str) -> str:
    """根据代理基础地址和目标 URL 构造用户可打开的调试 URL。"""
    parsed = urlsplit(target_url)
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{proxy_base_url}{path}{query}{fragment}"



