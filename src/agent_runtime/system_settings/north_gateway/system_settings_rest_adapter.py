# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from agent_runtime.system_settings.north_gateway.system_settings_contracts import (
    SystemSettingsGateway,
)


class PersistenceInventoryItemResponse(BaseModel):
    item_id: str
    title: str
    scope: str
    storage_medium: str
    storage_location: str
    persisted_data: list[str] = Field(default_factory=list)
    config_sections: list[str] = Field(default_factory=list)
    server_sync_candidate: bool = False
    sync_recommendation: str
    rationale: str
    secret_handling: str | None = None
    migration_notes: str | None = None


class SystemSettingsResponse(BaseModel):
    config_path: str
    env_file_path: str | None = None
    raw_yaml: str
    raw_config: dict[str, Any] = Field(default_factory=dict)
    section_names: list[str] = Field(default_factory=list)
    loaded_section_names: list[str] = Field(default_factory=list)
    watch_enabled: bool = False
    persistence_inventory: list[PersistenceInventoryItemResponse] = Field(default_factory=list)


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
            "persistence_inventory": [
                {
                    "item_id": item.item_id,
                    "title": item.title,
                    "scope": item.scope,
                    "storage_medium": item.storage_medium,
                    "storage_location": item.storage_location,
                    "persisted_data": item.persisted_data,
                    "config_sections": item.config_sections,
                    "server_sync_candidate": item.server_sync_candidate,
                    "sync_recommendation": item.sync_recommendation,
                    "rationale": item.rationale,
                    "secret_handling": item.secret_handling,
                    "migration_notes": item.migration_notes,
                }
                for item in settings.persistence_inventory
            ],
        }

    return router
