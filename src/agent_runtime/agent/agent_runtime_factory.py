# -*- coding: utf-8 -*-
"""Agent 运行时任务与实例管理模块工厂。"""
from __future__ import annotations

from threading import RLock

from agent_runtime.agent.agent_runtime_config import (
    AGENT_RUNTIME_REPOSITORY_MEMORY,
    AGENT_RUNTIME_REPOSITORY_SQLITE,
    AgentRuntimeStateConfig,
)
from agent_runtime.agent.agent_runtime_repository import AgentRuntimeRepository
from agent_runtime.agent.agent_runtime_service import AgentRuntimeService
from agent_runtime.agent.infrastructure.in_memory_agent_runtime_repository import (
    InMemoryAgentRuntimeRepository,
)
from agent_runtime.agent.infrastructure.sqlite_agent_runtime_repository import (
    SqliteAgentRuntimeRepository,
)
from agent_runtime.agent.north_gateway.agent_runtime_gateway_impl import (
    AgentRuntimeGatewayImpl,
)


class AgentRuntimeFactory:
    """Agent 运行时模块工厂。

    工厂是 Agent 运行态管理模块的组合根：根据 ConfigRegistry 中的 agent_runtime 配置，
    装配进程内内存仓储或 SQLite 共享仓储，并返回进程内共享的北向网关实例。
    当 repository_type=sqlite 时，多个 FastAPI worker / Python 进程可以通过同一个 SQLite
    数据库共享 task、Agent 实例和父子关系树运行态。
    """

    _lock = RLock()
    _gateway: AgentRuntimeGatewayImpl | None = None
    _repository_signature: tuple[str, str] | None = None

    @classmethod
    def create(cls) -> AgentRuntimeGatewayImpl:
        """创建或返回进程内共享的 Agent 运行时北向网关。"""
        with cls._lock:
            config = cls._read_config()
            signature = (config.repository_type, config.data_dir)
            if cls._gateway is None or cls._repository_signature != signature:
                repository = cls._create_repository(config)
                service = AgentRuntimeService(repository)
                cls._gateway = AgentRuntimeGatewayImpl(service)
                cls._repository_signature = signature
            return cls._gateway

    @staticmethod
    def _read_config() -> AgentRuntimeStateConfig:
        """读取 Agent 运行态配置；ConfigRegistry 未初始化时使用 SQLite 默认配置。"""
        try:
            from agent_runtime.config import ConfigRegistry

            return ConfigRegistry.instance().agent_runtime
        except RuntimeError:
            return AgentRuntimeStateConfig()

    @staticmethod
    def _create_repository(
        config: AgentRuntimeStateConfig,
    ) -> AgentRuntimeRepository:
        """根据运行态配置创建具体仓储实现。"""
        if config.repository_type == AGENT_RUNTIME_REPOSITORY_MEMORY:
            return InMemoryAgentRuntimeRepository()
        if config.repository_type == AGENT_RUNTIME_REPOSITORY_SQLITE:
            return SqliteAgentRuntimeRepository(config.data_dir)
        raise ValueError(f"不支持的 Agent 运行态仓储类型: {config.repository_type}")