from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS image_generation_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    llm_policy_group_id TEXT,
    default_request_params_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class ImageGenerationRuntimeSettings:
    llm_policy_group_id: str | None
    updated_at: str | None = None


class ImageGenerationSettingsRepository:
    """图像生成运行时设置的本地 SQLite 仓储。"""

    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor

    def get_settings(self) -> ImageGenerationRuntimeSettings:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT llm_policy_group_id, updated_at "
                "FROM image_generation_settings WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return ImageGenerationRuntimeSettings(
                llm_policy_group_id=None,
                updated_at=None,
            )
        return ImageGenerationRuntimeSettings(
            llm_policy_group_id=_nullable_text(row[0]),
            updated_at=str(row[1]) if row[1] is not None else None,
        )

    def save_settings(
        self,
        *,
        llm_policy_group_id: str | None,
    ) -> ImageGenerationRuntimeSettings:
        normalized_policy_id = _nullable_text(llm_policy_group_id)
        now = _utc_now_text()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO image_generation_settings ("
                "id, llm_policy_group_id, default_request_params_json, updated_at"
                ") VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "llm_policy_group_id = excluded.llm_policy_group_id, "
                "default_request_params_json = excluded.default_request_params_json, "
                "updated_at = excluded.updated_at",
                (
                    normalized_policy_id,
                    "{}",
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_settings()

    def db_path(self) -> Path:
        return Path(self._data_dir_accessor()).expanduser() / "image_generation_settings.db"

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute(_SCHEMA)
        conn.commit()
        return conn


class PostgresImageGenerationSettingsRepository:
    """图像生成运行时设置的云端 PostgreSQL 仓储。

    启用云端用户配置时，llm_policy_group_id 只保存到云端 JSONB item；
    本地 SQLite 不再作为读写 fallback。
    """

    _NAMESPACE = "image_generation.runtime_settings"
    _ITEM_ID = "default"

    def __init__(self, store_accessor: Callable[[], Any]) -> None:
        self._store_accessor = store_accessor

    def get_settings(self) -> ImageGenerationRuntimeSettings:
        payload = self._store_accessor().get_payload(self._NAMESPACE, self._ITEM_ID)
        if payload is None:
            return ImageGenerationRuntimeSettings(
                llm_policy_group_id=None,
                updated_at=None,
            )
        return _settings_from_payload(payload)

    def save_settings(
        self,
        *,
        llm_policy_group_id: str | None,
    ) -> ImageGenerationRuntimeSettings:
        settings = ImageGenerationRuntimeSettings(
            llm_policy_group_id=_nullable_text(llm_policy_group_id),
            updated_at=_utc_now_text(),
        )
        self._store_accessor().save_payload(
            self._NAMESPACE,
            self._ITEM_ID,
            _settings_payload(settings),
        )
        return settings

    def contains_settings(self) -> bool:
        return self._store_accessor().contains_payload(self._NAMESPACE, self._ITEM_ID)

    def db_path(self) -> str:
        return "postgresql:cloud_user_config_items/image_generation.runtime_settings/default"


def create_image_generation_settings_repository(data_dir_accessor: Callable[[], str]):
    """按当前配置模式创建图像生成运行时设置仓储。

    PostgreSQL 云端用户配置启用时只装配云端仓储；未启用时只装配本地 SQLite 仓储。
    禁止云端失败后静默回退本地，避免形成双权威。
    """
    from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore
    from agent_runtime.config import ConfigRegistry

    if ConfigRegistry.instance().cloud_user_config_database.is_postgresql_enabled:
        store = PostgresUserConfigStore(
            lambda: ConfigRegistry.instance().cloud_user_config_database,
        )
        return PostgresImageGenerationSettingsRepository(lambda: store)
    return ImageGenerationSettingsRepository(data_dir_accessor)


def _settings_from_payload(payload: dict[str, Any]) -> ImageGenerationRuntimeSettings:
    return ImageGenerationRuntimeSettings(
        llm_policy_group_id=_nullable_text(payload.get("llm_policy_group_id")),
        updated_at=str(payload["updated_at"]) if payload.get("updated_at") is not None else None,
    )


def _settings_payload(settings: ImageGenerationRuntimeSettings) -> dict[str, Any]:
    return {
        "llm_policy_group_id": settings.llm_policy_group_id,
        "updated_at": settings.updated_at,
    }


def _nullable_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()
