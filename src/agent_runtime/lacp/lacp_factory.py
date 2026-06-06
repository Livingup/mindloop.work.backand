from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING

from agent_runtime.config import ConfigRegistry
from agent_runtime.lacp.client import LacpClient
from agent_runtime.lacp.config import LacpConfig
from agent_runtime.lacp.registration_registry import LacpRegistrationRegistry
from agent_runtime.lacp.settings_repository import LacpSettingsRepository, create_lacp_settings_repository

if TYPE_CHECKING:
    from agent_runtime.agent.agent_execution_engine import AgentExecutionEngine
    from agent_runtime.agent.north_gateway.agent_runtime_contracts import AgentRuntimeGateway
    from agent_runtime.agent_definition.north_gateway.agent_definition_contracts import AgentDefinitionNorthGateway
    from agent_runtime.lacp.bridge import AgentRuntimeLacpBridge


class LacpFactory:
    _lock = RLock()
    _registry: LacpRegistrationRegistry | None = None
    _settings_repository: LacpSettingsRepository | None = None
    _client: LacpClient | None = None
    _bridge: 'AgentRuntimeLacpBridge' | None = None

    @classmethod
    def reset_for_runtime_mode_change(cls) -> None:
        """清理依赖当前共享配置模式的缓存实例。"""
        with cls._lock:
            cls._settings_repository = None
            cls._client = None
            cls._bridge = None

    @classmethod
    def create_settings_repository(cls) -> LacpSettingsRepository:
        with cls._lock:
            if cls._settings_repository is None:
                cls._settings_repository = create_lacp_settings_repository(
                    lambda: ConfigRegistry.instance().lacp.data_dir,
                )
            return cls._settings_repository

    @classmethod
    def create_registry(cls) -> LacpRegistrationRegistry:
        with cls._lock:
            if cls._registry is None:
                cls._registry = LacpRegistrationRegistry(lambda: ConfigRegistry.instance().lacp.data_dir)
            return cls._registry

    @classmethod
    def current_config(cls) -> LacpConfig:
        base = ConfigRegistry.instance().lacp
        settings = cls.create_settings_repository().get_settings()
        return LacpConfig(
            enabled=settings.enabled,
            registry_url=settings.registry_url,
            api_key=settings.api_key,
            data_dir=base.data_dir,
            connect_timeout_seconds=base.connect_timeout_seconds,
            request_timeout_seconds=base.request_timeout_seconds,
            reconnect_initial_delay_seconds=base.reconnect_initial_delay_seconds,
            reconnect_max_delay_seconds=base.reconnect_max_delay_seconds,
        )

    @classmethod
    def create_client(cls) -> LacpClient:
        with cls._lock:
            if cls._client is None:
                cls._client = LacpClient(
                    config_accessor=cls.current_config,
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
