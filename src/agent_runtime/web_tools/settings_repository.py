from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_runtime.web_tools.models import WebToolPolicyCandidate, WebToolsRuntimeSettings


_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_tools_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    candidates_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class WebToolsSettingsRepository:
    """Web Tools 运行时设置的本地 SQLite 仓储。"""

    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor

    def get_settings(self) -> WebToolsRuntimeSettings:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT candidates_json, updated_at FROM web_tools_settings WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return WebToolsRuntimeSettings()
        return WebToolsRuntimeSettings(
            candidates=_load_candidates(str(row[0] or "[]")),
            updated_at=str(row[1]) if row[1] is not None else None,
        )

    def save_settings(
        self,
        *,
        candidates: tuple[WebToolPolicyCandidate, ...],
    ) -> WebToolsRuntimeSettings:
        settings = WebToolsRuntimeSettings(candidates=candidates)
        now = _utc_now_text()
        payload = json.dumps([item.to_dict() for item in settings.candidates], ensure_ascii=False)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO web_tools_settings (id, candidates_json, updated_at) "
                "VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "candidates_json = excluded.candidates_json, "
                "updated_at = excluded.updated_at",
                (payload, now),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_settings()

    def db_path(self) -> Path:
        return Path(self._data_dir_accessor()).expanduser() / "web_tools_settings.db"

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute(_SCHEMA)
        conn.commit()
        return conn


class PostgresWebToolsSettingsRepository:
    """Web Tools 用户共享设置的云端 PostgreSQL 仓储。

    启用云端用户配置时，候选策略组顺序只保存到云端 JSONB item；
    本地 SQLite 不再作为读写 fallback。
    """

    _NAMESPACE = "web_tools.runtime_settings"
    _ITEM_ID = "default"

    def __init__(self, store_accessor: Callable[[], Any]) -> None:
        self._store_accessor = store_accessor

    def get_settings(self) -> WebToolsRuntimeSettings:
        payload = self._store_accessor().get_payload(self._NAMESPACE, self._ITEM_ID)
        if payload is None:
            return WebToolsRuntimeSettings()
        raw_candidates = payload.get("candidates", [])
        if not isinstance(raw_candidates, list):
            raise ValueError("web tools candidates must be a JSON array.")
        return WebToolsRuntimeSettings(
            candidates=tuple(
                WebToolPolicyCandidate.from_dict(item)
                for item in raw_candidates
                if isinstance(item, dict)
            ),
            updated_at=str(payload["updated_at"]) if payload.get("updated_at") is not None else None,
        )

    def save_settings(
        self,
        *,
        candidates: tuple[WebToolPolicyCandidate, ...],
    ) -> WebToolsRuntimeSettings:
        settings = WebToolsRuntimeSettings(
            candidates=candidates,
            updated_at=_utc_now_text(),
        )
        self._store_accessor().save_payload(
            self._NAMESPACE,
            self._ITEM_ID,
            {
                "candidates": [item.to_dict() for item in settings.candidates],
                "updated_at": settings.updated_at,
            },
        )
        return settings

    def contains_settings(self) -> bool:
        return self._store_accessor().contains_payload(self._NAMESPACE, self._ITEM_ID)

    def db_path(self) -> str:
        return "postgresql:cloud_user_config_items/web_tools.runtime_settings/default"


def create_web_tools_settings_repository(data_dir_accessor: Callable[[], str]):
    """按当前配置模式创建 Web Tools 设置仓储。

    PostgreSQL 云端用户配置启用时只装配云端仓储；未启用时只装配本地 SQLite 仓储。
    禁止云端失败后静默回退本地，避免形成双权威。
    """
    from agent_runtime.cloud_user_config.postgres_store import PostgresUserConfigStore
    from agent_runtime.config import ConfigRegistry

    if ConfigRegistry.instance().cloud_user_config_database.is_postgresql_enabled:
        store = PostgresUserConfigStore(
            lambda: ConfigRegistry.instance().cloud_user_config_database,
        )
        return PostgresWebToolsSettingsRepository(lambda: store)
    return WebToolsSettingsRepository(data_dir_accessor)


def _load_candidates(raw: str) -> tuple[WebToolPolicyCandidate, ...]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"web tools candidates_json is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("web tools candidates_json must be a JSON array.")
    candidates: list[WebToolPolicyCandidate] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("web tools candidate item must be a JSON object.")
        candidates.append(WebToolPolicyCandidate.from_dict(item))
    return tuple(candidates)


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()
