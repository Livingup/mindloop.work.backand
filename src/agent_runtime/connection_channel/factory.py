from __future__ import annotations

from threading import RLock

from agent_runtime.config import ConfigRegistry
from agent_runtime.connection_channel.service import ConnectionChannelSettingsService
from agent_runtime.connection_channel.settings_repository import ConnectionChannelSettingsRepository


class ConnectionChannelFactory:
    _lock = RLock()
    _repository: ConnectionChannelSettingsRepository | None = None
    _service: ConnectionChannelSettingsService | None = None

    @classmethod
    def create_repository(cls) -> ConnectionChannelSettingsRepository:
        with cls._lock:
            if cls._repository is None:
                cls._repository = ConnectionChannelSettingsRepository(
                    lambda: ConfigRegistry.instance().connection_channel.data_dir,
                )
            return cls._repository

    @classmethod
    def create_service(cls) -> ConnectionChannelSettingsService:
        with cls._lock:
            if cls._service is None:
                cls._service = ConnectionChannelSettingsService(cls.create_repository())
            return cls._service

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._repository = None
            cls._service = None
