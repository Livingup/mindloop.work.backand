from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS lacp_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL,
    registry_url TEXT NOT NULL,
    api_key TEXT,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class LacpRuntimeSettings:
    enabled: bool
    registry_url: str
    api_key: str | None
    updated_at: str | None


class LacpSettingsRepository:
    """LACP Registry 服务设置的本地 SQLite 仓储。"""

    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor

    def get_settings(self) -> LacpRuntimeSettings:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT enabled, registry_url, api_key, updated_at FROM lacp_settings WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return LacpRuntimeSettings(
                enabled=False,
                registry_url="",
                api_key=None,
                updated_at=None,
            )
        api_key = str(row[2]) if row[2] is not None else None
        return LacpRuntimeSettings(
            enabled=bool(row[0]),
            registry_url=str(row[1] or ""),
            api_key=api_key if api_key else None,
            updated_at=str(row[3]),
        )

    def save_settings(
        self,
        *,
        enabled: bool,
        registry_url: str,
        api_key: str | None,
    ) -> LacpRuntimeSettings:
        normalized_url = registry_url.strip()
        normalized_api_key = api_key.strip() if isinstance(api_key, str) else None
        if normalized_api_key == "":
            normalized_api_key = None
        now = _utc_now_text()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO lacp_settings (id, enabled, registry_url, api_key, updated_at) "
                "VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                " enabled = excluded.enabled, "
                " registry_url = excluded.registry_url, "
                " api_key = excluded.api_key, "
                " updated_at = excluded.updated_at",
                (1 if enabled else 0, normalized_url, normalized_api_key, now),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_settings()

    def db_path(self) -> Path:
        return Path(self._data_dir_accessor()).expanduser() / "lacp_settings.db"

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute(_SCHEMA)
        conn.commit()
        return conn


class PostgresLacpSettingsRepository:
    """LACP Registry 服务设置的云端 PostgreSQL 仓储。

    只保存用户共享的 registry 服务配置；LACP 实例 registrations.json 仍由
    LacpRegistrationRegistry 本机保存，不进入云端用户共享配置。
    """

    _NAMESPACE = "lacp.registry_service_settings"
    _ITEM_ID = "default"

    def __init__(self, store_accessor: Callable[[], Any]) -> None:
        self._store_accessor = store_accessor

    def get_settings(self) -> LacpRuntimeSettings:
        payload = self._store_accessor().get_payload(self._NAMESPACE, self._ITEM_ID)
        if payload is None:
            return LacpRuntimeSettings(
                enabled=False,
                registry_url="",
                api_key=None,
                updated_at=None,
            )
        return LacpRuntimeSettings(
            enabled=bool(payload.get("enabled", False)),
            registry_url=str(payload.get("registry_url") or ""),
            api_key=_nullable_text(payload.get("api_key")),
            updated_at=str(payload["updated_at"]) if payload.get("updated_at") is not None else None,
        )

    def save_settings(
        self,
        *,
        enabled: bool,
        registry_url: str,
        api_key: str | None,
    ) -> LacpRuntimeSettings:
        settings = LacpRuntimeSettings(
            enabled=enabled,
            registry_url=registry_url.strip(),
            api_key=_nullable_text(api_key),
            updated_at=_utc_now_text(),
        )
        self._store_accessor().save_payload(
            self._NAMESPACE,
            self._ITEM_ID,
            {
                "enabled": settings.enabled,
                "registry_url": settings.registry_url,
                "api_key": settings.api_key,
                "updated_at": settings.updated_at,
            },
        )
        return settings

    def contains_settings(self) -> bool:
        return self._store_accessor().contains_payload(self._NAMESPACE, self._ITEM_ID)

    def db_path(self) -> str:
        return "postgresql:cloud_user_config_items/lacp.registry_service_settings/default"


def create_lacp_settings_repository(data_dir_accessor: Callable[[], str]):
    """按当前配置模式创建 LACP Registry 服务设置仓储。

    PostgreSQL 云端用户配置启用时只装配云端仓储；未启用时只装配本地 SQLite 仓储。
    禁止云端失败后静默回退本地，避免形成双权威。
    """
    from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore
    from agent_runtime.config import ConfigRegistry

    if ConfigRegistry.instance().cloud_user_config_database.is_postgresql_enabled:
        store = PostgresUserConfigStore(
            lambda: ConfigRegistry.instance().cloud_user_config_database,
        )
        return PostgresLacpSettingsRepository(lambda: store)
    return LacpSettingsRepository(data_dir_accessor)


def _nullable_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()
