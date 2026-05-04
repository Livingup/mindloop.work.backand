# -*- coding: utf-8 -*-
from __future__ import annotations

from agent_runtime.system_settings.north_gateway.system_settings_contracts import (
    SystemSettingsInfo,
)
from agent_runtime.system_settings.system_settings_service import SystemSettingsService


class SystemSettingsGatewayImpl:
    def __init__(self, service: SystemSettingsService) -> None:
        self._service = service

    def get_settings(self) -> SystemSettingsInfo:
        snapshot = self._service.get_snapshot()
        return SystemSettingsInfo(
            config_path=snapshot.config_path,
            env_file_path=snapshot.env_file_path,
            raw_yaml=snapshot.raw_yaml,
            raw_config=snapshot.raw_config,
            section_names=snapshot.section_names,
            loaded_section_names=snapshot.loaded_section_names,
            watch_enabled=snapshot.watch_enabled,
        )
