# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Any, Callable

import yaml

from agent_runtime.config import ConfigRegistry
from agent_runtime.system_settings.system_settings_snapshot import (
    SystemSettingsSnapshot,
)


_YAML_KEY_VALUE_PATTERN = re.compile(r"^(\s*)([^:#\n]+?)(\s*:\s*)(.*?)(\s*(?:#.*)?)$")

_SECRET_KEYWORDS = (
    "secret",
    "password",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
)


class SystemSettingsService:
    def __init__(self, registry_accessor: Callable[[], ConfigRegistry]) -> None:
        self._registry_accessor = registry_accessor

    def get_snapshot(self) -> SystemSettingsSnapshot:
        registry_snapshot = self._registry_accessor().snapshot()
        masked_config = _mask_sensitive_data(registry_snapshot.raw_config_data)
        masked_yaml = _mask_sensitive_yaml(registry_snapshot.raw_yaml)
        if not masked_yaml:
            masked_yaml = yaml.safe_dump(
                masked_config,
                allow_unicode=True,
                sort_keys=False,
            )
        return SystemSettingsSnapshot(
            config_path=registry_snapshot.yaml_path,
            env_file_path=registry_snapshot.env_file_path,
            raw_yaml=masked_yaml,
            raw_config=masked_config,
            section_names=registry_snapshot.section_names,
            loaded_section_names=registry_snapshot.loaded_section_names,
            watch_enabled=registry_snapshot.watch_enabled,
        )


def _mask_sensitive_data(value: Any, key_name: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            key: _mask_sensitive_data(child, str(key))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_mask_sensitive_data(item, key_name) for item in value]
    if key_name is not None and _is_sensitive_key(key_name):
        return "********"
    return value


def _is_sensitive_key(key_name: str) -> bool:
    normalized = key_name.lower()
    return any(keyword in normalized for keyword in _SECRET_KEYWORDS)


def _mask_sensitive_yaml(raw_yaml: str) -> str:
    if not raw_yaml:
        return ""
    masked_lines: list[str] = []
    for line in raw_yaml.splitlines():
        match = _YAML_KEY_VALUE_PATTERN.match(line)
        if match is None:
            masked_lines.append(line)
            continue
        indent, key_name, separator, value, trailing_comment = match.groups()
        if _is_sensitive_key(key_name.strip()) and value.strip():
            masked_lines.append(
                f"{indent}{key_name}{separator}\"********\"{trailing_comment}"
            )
            continue
        masked_lines.append(line)
    if raw_yaml.endswith("\n"):
        return "\n".join(masked_lines) + "\n"
    return "\n".join(masked_lines)
