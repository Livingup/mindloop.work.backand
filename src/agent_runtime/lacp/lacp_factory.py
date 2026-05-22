from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING

from agent_runtime.config import ConfigRegistry
from agent_runtime.lacp.client import LacpClient
from agent_runtime.lacp.config import LacpConfig
from agent_runtime.lacp.registration_registry import LacpRegistrationRegistry

if TYPE_CHECKING:
    from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine
    from agent_runtime.agent.north_gateway.agent_runtime_contracts import AgentRuntimeGateway
    from agent_runtime.agent_definition.north_gateway.agent_definition_contracts import AgentDefinitionNorthGateway
    from agent_runtime.lacp.bridge import AgentRuntimeLacpBridge


class LacpFactory:
    _lock = RLock()
    _registry: LacpRegistrationRegistry | None = None
    _client: LacpClient | None = None
    _bridge: 'AgentRuntimeLacpBridge' | None = None

    @classmethod
    def create_registry(cls) -> LacpRegistrationRegistry:
        with cls._lock:
            if cls._registry is None:
                cls._registry = LacpRegistrationRegistry(lambda: ConfigRegistry.instance().lacp.data_dir)
            return cls._registry

    @classmethod
    def create_client(cls) -> LacpClient:
        with cls._lock:
            if cls._client is None:
                cls._client = LacpClient(
                    config_accessor=lambda: ConfigRegistry.instance().lacp,
                    registration_registry=cls.create_registry(),
                )
            return cls._client

    @classmethod
    def create_bridge(
        cls,
        *,
        runtime_gateway: 'AgentRuntimeGateway',
        agent_definition_gateway: 'AgentDefinitionNorthGateway',
        execution_engine: 'AgentExecutionEngine' | None = None,
    ) -> 'AgentRuntimeLacpBridge':
        from agent_runtime.lacp.bridge import AgentRuntimeLacpBridge

        with cls._lock:
            if cls._bridge is None:
                cls._bridge = AgentRuntimeLacpBridge(
                    lacp_client=cls.create_client(),
                    registration_registry=cls.create_registry(),
                    runtime_gateway=runtime_gateway,
                    agent_definition_gateway=agent_definition_gateway,
                    execution_engine=execution_engine,
                )
            elif execution_engine is not None:
                cls._bridge.set_execution_engine(execution_engine)
            return cls._bridge

    @staticmethod
    def default_config() -> LacpConfig:
        return LacpConfig()
