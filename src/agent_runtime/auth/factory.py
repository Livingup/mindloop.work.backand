# -*- coding: utf-8 -*-
"""运行时认证模块组合根。"""
from __future__ import annotations

from threading import RLock

from agent_runtime.agent.agent_runtime_config import AgentRuntimeStateConfig
from agent_runtime.auth.auth_service import RuntimeAuthService
from agent_runtime.auth.infrastructure.sqlite_auth_repository import SqliteRuntimeAuthRepository


class RuntimeAuthFactory:
    """创建运行时本地账号密码认证服务。"""

    _lock = RLock()
    _service: RuntimeAuthService | None = None
    _config_signature: tuple[str] | None = None

    @classmethod
    def create_service(cls) -> RuntimeAuthService:
        with cls._lock:
            config = cls._read_config()
            signature = (config.data_dir,)
            if cls._service is None or cls._config_signature != signature:
                repository = SqliteRuntimeAuthRepository(
                    data_dir_accessor=cls._build_data_dir_accessor(config),
                )
                cls._service = RuntimeAuthService(repository)
                cls._config_signature = signature
            return cls._service

    @staticmethod
    def _read_config() -> AgentRuntimeStateConfig:
        try:
            from agent_runtime.config import ConfigRegistry

            return ConfigRegistry.instance().agent_runtime
        except RuntimeError:
            return AgentRuntimeStateConfig()

    @staticmethod
    def _build_data_dir_accessor(config: AgentRuntimeStateConfig):
        try:
            from agent_runtime.config import ConfigRegistry

            ConfigRegistry.instance()
        except RuntimeError:
            return lambda: config.data_dir
        return lambda: ConfigRegistry.instance().agent_runtime.data_dir
