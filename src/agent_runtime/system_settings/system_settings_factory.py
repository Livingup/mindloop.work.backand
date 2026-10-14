# -*- coding: utf-8 -*-
from __future__ import annotations

from agent_runtime.system_settings.north_gateway.system_settings_gateway_impl import (
    SystemSettingsGatewayImpl,
)
from agent_runtime.system_settings.system_settings_service import SystemSettingsService


class SystemSettingsFactory:
    @staticmethod
    def create() -> SystemSettingsGatewayImpl:
        from agent_runtime.config import ConfigRegistry

        service = SystemSettingsService(
            registry_accessor=lambda: ConfigRegistry.instance(),
        )
        return SystemSettingsGatewayImpl(service)
