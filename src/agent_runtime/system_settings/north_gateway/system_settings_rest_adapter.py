# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agent_runtime.system_settings.north_gateway.system_settings_contracts import (
    SystemSettingsGateway,
)


class SystemSettingsResponse(BaseModel):
    config_path: str
    env_file_path: str | None = None
    raw_yaml: str
    raw_config: dict[str, Any] = Field(default_factory=dict)
    section_names: list[str] = Field(default_factory=list)
    loaded_section_names: list[str] = Field(default_factory=list)
    watch_enabled: bool = False


def create_system_settings_router(gateway: SystemSettingsGateway) -> APIRouter:
    router = APIRouter()

    @router.get("", response_model=SystemSettingsResponse)
    def get_system_settings() -> dict[str, Any]:
        settings = gateway.get_settings()
        return {
            "config_path": settings.config_path,
            "env_file_path": settings.env_file_path,
            "raw_yaml": settings.raw_yaml,
            "raw_config": settings.raw_config,
            "section_names": settings.section_names,
            "loaded_section_names": settings.loaded_section_names,
            "watch_enabled": settings.watch_enabled,
        }

    return router
