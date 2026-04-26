# -*- coding: utf-8 -*-
"""Agent 运行时任务与实例管理模块工厂。"""
from __future__ import annotations

from threading import RLock

from agent_runtime.agent.agent_runtime_service import AgentRuntimeService
from agent_runtime.agent.infrastructure.in_memory_agent_runtime_repository import (
    InMemoryAgentRuntimeRepository,
)
from agent_runtime.agent.north_gateway.agent_runtime_gateway_impl import (
    AgentRuntimeGatewayImpl,
)


class AgentRuntimeFactory:
    """Agent 运行时模块工厂。

    本模块当前定位为程序内部动态运行态管理，先使用线程安全内存仓储。
    工厂在单 Python 进程内返回同一个网关实例，确保不同内部调用方看到同一份 task/instance 运行态。
    后续如需多进程共享任务状态，可在本工厂中切换到 SQLite/PostgreSQL 仓储实现。
    """

    _lock = RLock()
    _gateway: AgentRuntimeGatewayImpl | None = None

    @classmethod
    def create(cls) -> AgentRuntimeGatewayImpl:
        """创建或返回进程内共享的 Agent 运行时北向网关。"""
        with cls._lock:
            if cls._gateway is None:
                repository = InMemoryAgentRuntimeRepository()
                service = AgentRuntimeService(repository)
                cls._gateway = AgentRuntimeGatewayImpl(service)
            return cls._gateway