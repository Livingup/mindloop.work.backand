from __future__ import annotations

from threading import RLock

from agent_runtime.frontend_debug.frontend_debug_service import FrontendDebugService
from agent_runtime.frontend_debug.infrastructure.in_memory_frontend_debug_repository import (
    InMemoryFrontendDebugSessionRepository,
)
from agent_runtime.frontend_debug.infrastructure.threaded_frontend_debug_proxy import (
    HttpFrontendDebugElementSelectionPublisher,
    ThreadedFrontendDebugProxyManager,
)
from agent_runtime.frontend_debug.north_gateway.frontend_debug_gateway_impl import (
    FrontendDebugGatewayImpl,
)


class FrontendDebugFactory:
    """前端调试模块工厂。

    工厂作为模块组合根，负责装配调试会话仓储、本机 HTTP 代理管理器、元素选择事件发布器、
    领域服务和北向网关。业务对象不直接读取全局配置，避免调试能力散落到工具入口中。
    """

    _lock = RLock()
    _gateway: FrontendDebugGatewayImpl | None = None

    @classmethod
    def create(cls) -> FrontendDebugGatewayImpl:
        """创建或返回进程内共享的前端调试北向网关。"""
        with cls._lock:
            if cls._gateway is None:
                repository = InMemoryFrontendDebugSessionRepository()
                proxy_manager = ThreadedFrontendDebugProxyManager()
                element_selection_publisher = HttpFrontendDebugElementSelectionPublisher()
                service = FrontendDebugService(
                    repository=repository,
                    proxy_manager=proxy_manager,
                    element_selection_publisher=element_selection_publisher,
                )
                cls._gateway = FrontendDebugGatewayImpl(service)
            return cls._gateway
