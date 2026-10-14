# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.system_settings.persistence_inventory import PersistenceInventoryItem


@dataclass(frozen=True, slots=True)
class SystemSettingsSnapshot:
    config_path: str
    env_file_path: str | None
    raw_yaml: str
    raw_config: dict[str, Any]
    section_names: list[str] = field(default_factory=list)
    loaded_section_names: list[str] = field(default_factory=list)
    watch_enabled: bool = False
    persistence_inventory: list[PersistenceInventoryItem] = field(default_factory=list)
