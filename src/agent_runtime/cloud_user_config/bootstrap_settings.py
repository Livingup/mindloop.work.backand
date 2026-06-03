# -*- coding: utf-8 -*-
"""云端用户共享配置库的本机 bootstrap 启动凭据。

本文件只保存如何连接用户云端 PostgreSQL 权威库的机器本地凭据。
它不保存云端共享业务数据，也不作为 prompt、Agent 定义、LLM Registry、OSS 设置等
用户共享配置的本地缓存。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from agent_runtime.cloud_user_config.config import (
    CloudUserConfigDatabaseConfig,
    CloudUserConfigDatabasePoolConfig,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_user_config_bootstrap_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL,
    backend TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    database_name TEXT NOT NULL,
    username TEXT NOT NULL,
    password TEXT,
    sslmode TEXT NOT NULL,
    pool_min_size INTEGER NOT NULL,
    pool_max_size INTEGER NOT NULL,
    connect_timeout_seconds INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class CloudUserConfigBootstrapSettings:
    """本机保存的云端用户共享配置库连接凭据。"""

    enabled: bool
    backend: str
    host: str
    port: int
    database: str
    username: str
    password: str | None
    sslmode: str
    pool: CloudUserConfigDatabasePoolConfig
    updated_at: str | None

    @property
    def configured(self) -> bool:
        return all((
            self.enabled,
            self.backend == "postgresql",
            self.host.strip(),
            self.port > 0,
            self.database.strip(),
            self.username.strip(),
            (self.password or "").strip(),
        ))

    def to_database_config(
        self,
        base_config: CloudUserConfigDatabaseConfig,
    ) -> CloudUserConfigDatabaseConfig:
        """把本机 bootstrap 凭据覆盖到 YAML 默认元数据上。"""
        return replace(
            base_config,
            enabled=self.enabled,
            backend=self.backend,
            host=self.host,
            port=self.port,
            database=self.database,
            username=self.username,
            password=self.password or "",
            sslmode=self.sslmode,
            pool=self.pool,
        )


class CloudUserConfigBootstrapSettingsRepository:
    """机器本地 bootstrap SQLite 仓储。"""

    def __init__(self, data_dir_accessor: Callable[[], str]) -> None:
        self._data_dir_accessor = data_dir_accessor

    def get_settings(self) -> CloudUserConfigBootstrapSettings | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT enabled, backend, host, port, database_name, username, password, "
                "sslmode, pool_min_size, pool_max_size, connect_timeout_seconds, updated_at "
                "FROM cloud_user_config_bootstrap_settings WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return CloudUserConfigBootstrapSettings(
            enabled=bool(row[0]),
            backend=str(row[1] or "local"),
            host=str(row[2] or ""),
            port=int(row[3] or 0),
            database=str(row[4] or ""),
            username=str(row[5] or ""),
            password=_nullable_text(row[6]),
            sslmode=str(row[7] or "require"),
            pool=CloudUserConfigDatabasePoolConfig(
                min_size=max(int(row[8] or 1), 1),
                max_size=max(int(row[9] or 5), 1),
                connect_timeout_seconds=max(int(row[10] or 10), 1),
            ),
            updated_at=str(row[11]) if row[11] is not None else None,
        )

    def get_effective_database_config(
        self,
        base_config: CloudUserConfigDatabaseConfig,
    ) -> CloudUserConfigDatabaseConfig:
        settings = self.get_settings()
        if settings is None or not settings.configured:
            return base_config
        return settings.to_database_config(base_config)

    def save_config(
        self,
        config: CloudUserConfigDatabaseConfig,
    ) -> CloudUserConfigBootstrapSettings:
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO cloud_user_config_bootstrap_settings ("
                "id, enabled, backend, host, port, database_name, username, password, sslmode, "
                "pool_min_size, pool_max_size, connect_timeout_seconds, updated_at"
                ") VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "enabled = excluded.enabled, backend = excluded.backend, host = excluded.host, "
                "port = excluded.port, database_name = excluded.database_name, username = excluded.username, "
                "password = excluded.password, sslmode = excluded.sslmode, "
                "pool_min_size = excluded.pool_min_size, pool_max_size = excluded.pool_max_size, "
                "connect_timeout_seconds = excluded.connect_timeout_seconds, updated_at = excluded.updated_at",
                (
                    1 if config.enabled else 0,
                    config.backend.strip() or "postgresql",
                    config.host.strip(),
                    int(config.port),
                    config.database.strip(),
                    config.username.strip(),
                    _normalize_nullable_text(config.password),
                    config.sslmode.strip() or "require",
                    max(int(config.pool.min_size), 1),
                    max(int(config.pool.max_size), 1),
                    max(int(config.pool.connect_timeout_seconds), 1),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        saved = self.get_settings()
        if saved is None:
            raise RuntimeError("云端用户配置 bootstrap 凭据保存后无法读取。")
        return saved

    def db_path(self) -> Path:
        return Path(self._data_dir_accessor()).expanduser() / "cloud_user_config_bootstrap.db"

    def _connect(self) -> sqlite3.Connection:
        path = self.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute(_SCHEMA)
        conn.commit()
        return conn


def _nullable_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_nullable_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None
