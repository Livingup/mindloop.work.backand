# -*- coding: utf-8 -*-
"""通用通信模块工厂。"""
from __future__ import annotations

from threading import RLock

from agent_runtime.communication.communication_config import CommunicationConfig
from agent_runtime.communication.communication_service import CommunicationService
from agent_runtime.communication.event_hub import InMemoryCommunicationEventHub
from agent_runtime.communication.infrastructure.sqlite_communication_repository import (
    SqliteCommunicationRepository,
)
from agent_runtime.communication.north_gateway.communication_gateway_impl import (
    CommunicationGatewayImpl,
)


class CommunicationFactory:
    """通用通信模块工厂。

    工厂是通信模块组合根，负责装配 SQLite 消息仓储、进程内实时消息总线、领域服务和北向网关。
    """

    _lock = RLock()
    _gateway: CommunicationGatewayImpl | None = None
    _config_signature: tuple[str, int, int] | None = None

    @classmethod
    def create(cls) -> CommunicationGatewayImpl:
        """创建或返回进程内共享的通用通信北向网关。"""
        with cls._lock:
            config = cls._read_config()
            signature = (config.data_dir, config.replay_buffer_size, config.subscriber_queue_size)
            if cls._gateway is None or cls._config_signature != signature:
                data_dir_accessor = cls._build_data_dir_accessor(config)
                repository = SqliteCommunicationRepository(data_dir_accessor=data_dir_accessor)
                event_hub = InMemoryCommunicationEventHub(
                    replay_buffer_size=config.replay_buffer_size,
                    subscriber_queue_size=config.subscriber_queue_size,
                )
                service = CommunicationService(repository, event_hub)
                cls._gateway = CommunicationGatewayImpl(service)
                cls._config_signature = signature
            return cls._gateway

    @staticmethod
    def _read_config() -> CommunicationConfig:
        """读取通信模块配置；ConfigRegistry 未初始化时使用默认配置。"""
        try:
            from agent_runtime.config import ConfigRegistry

            return ConfigRegistry.instance().communication
        except RuntimeError:
            return CommunicationConfig()

    @staticmethod
    def _build_data_dir_accessor(config: CommunicationConfig):
        """构建通信数据目录访问器；ConfigRegistry 未初始化时回退到当前默认配置。"""
        try:
            from agent_runtime.config import ConfigRegistry

            ConfigRegistry.instance()
        except RuntimeError:
            return lambda: config.data_dir
        return lambda: ConfigRegistry.instance().communication.data_dir
